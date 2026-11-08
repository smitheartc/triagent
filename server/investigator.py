"""Tool-use loop: ticket in, report dict out.

run(ticket, es_tools, trace, max_calls) drives Nemotron (TRIAGENT_MODEL_INVESTIGATOR) over the eight tools in
tools.TOOL_SCHEMAS until it calls submit_report or the tool budget runs out, then validates the report and drops
any cited event id that does not exist in the cluster.

ask(job, question, es_tools, trace, max_calls) is a follow-up on a finished job: the same loop and model, seeded
with the ticket, the final report and a digest of the trace (prompts/ask.md), ending with answer_question instead
of submit_report. Cited ids are checked in the cluster the same way.

es_tools needs dispatch(name, args) and, optionally, catalog() and metric_fields(). tools.ESTools provides all.
trace needs add(kind, **data) (tracelog.Trace).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

import llm
import tuning_knobs
from contracts import Report
from tools import ANSWER_SCHEMA, ASK_TOOL_SCHEMAS, SUBMIT_SCHEMA, TOOL_SCHEMAS

log = logging.getLogger("triagent.investigator")

PROMPT_PATH = Path(__file__).parent / "prompts" / "investigator.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
ASK_PROMPT = (Path(__file__).parent / "prompts" / "ask.md").read_text(encoding="utf-8")
DIGEST_RESULT_CHARS = 300       # per tool result in the follow-up digest
DIGEST_MAX_CHARS = 24000        # whole digest; the middle is dropped first
TRACE_RESULT_CHARS = 500
MODEL_RESULT_CHARS = 12000      # safety net on top of the per-tool caps
MAX_IDLE_TURNS = 3              # consecutive model turns without a tool call before we stop asking
MAX_TOKENS = 8192


# ---------------------------------------------------------------------------------------------
# message building
# ---------------------------------------------------------------------------------------------
def build_catalog(es_tools: Any) -> list[dict[str, Any]]:
    """Compact event catalog [{code, index, action}] for codes with count > 0."""
    cat = getattr(es_tools, "catalog", None)
    if callable(cat):
        rows = cat()
    else:  # fallback: terms aggregation on event.code through the aggregate tool
        res = es_tools.dispatch("aggregate", {"field": "event.code", "include_metrics": True, "lookback": "24h",
                                              "top": 30})
        rows = [{"code": b["value"], "index": "", "action": "", "count": b["count"]}
                for b in (res or {}).get("buckets", [])]
    return [{"code": r["code"], "index": r.get("index", ""), "action": r.get("action", "")}
            for r in rows if r.get("count", 1) > 0]


def build_user_message(ticket: dict[str, Any], catalog: list[dict[str, Any]],
                       metric_fields: dict[str, list[str]] | None = None, feedback: str | None = None) -> str:
    t = {k: v for k, v in ticket.items() if v not in (None, "", {}, [])}
    lines = ["Incident ticket:", json.dumps(t, indent=2), "",
             f"Ticket time: {ticket.get('created')}. Tool time windows end here unless you set `end`.", "",
             "Event catalog (event.code  index  event.action), codes present in the cluster:"]
    lines += [f"{c['code']}  {c['index']}  {c['action']}" for c in catalog]
    if metric_fields:
        lines += ["", "Numeric fields per metrics dataset (for metric_series):"]
        lines += [f"{ds}: {', '.join(fs)}" for ds, fs in sorted(metric_fields.items())]
    if feedback:
        lines += ["", feedback]
    return "\n".join(lines)


def _dump(obj: Any, limit: int) -> str:
    """Compact JSON within limit. Row lists lose whole rows from the end (and say so) instead of being cut mid-JSON."""
    s = json.dumps(obj, separators=(",", ":"), default=str)
    if len(s) <= limit:
        return s
    if isinstance(obj, dict) and isinstance(obj.get("rows"), list) and obj["rows"]:
        rows = list(obj["rows"])
        while rows:
            rows.pop()
            trimmed = {**obj, "rows": rows, "rows_omitted": len(obj["rows"]) - len(rows),
                       "rows_omitted_note": "result too long; narrow the window, filters or size to see the rest"}
            s = json.dumps(trimmed, separators=(",", ":"), default=str)
            if len(s) <= limit:
                return s
    return s[: limit - 40] + f'..."[truncated {len(s) - limit + 40} chars]"'


# ---------------------------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------------------------
class _Loop:
    def __init__(self, ticket: dict[str, Any], es_tools: Any, trace: Any, max_calls: int,
                 model: str | None, seed: int | None, feedback: str | None = None) -> None:
        self.ticket, self.tools, self.trace, self.max_calls = ticket, es_tools, trace, max_calls
        self.model = model or llm.model_for("investigator")
        self.seed = seed
        self.feedback = feedback
        self.calls = 0
        self.findings: list[str] = []
        self.messages: list[dict[str, Any]] = []

    def chat(self, tools: list[dict[str, Any]]) -> llm.ChatResult:
        res = llm.chat(self.messages, tools=tools, model=self.model, role="investigator",
                       thinking=tuning_knobs.thinking_investigator(),
                       max_tokens=MAX_TOKENS, seed=self.seed, trace=lambda d: self.trace.add("llm", **d))
        self.messages.append(res.assistant_message())
        if res.text:
            self.trace.add("assistant", text=res.text[:TRACE_RESULT_CHARS])
        return res

    def reply(self, res: llm.ChatResult, call: dict[str, Any], content: Any, text_replies: list[str]) -> None:
        body = content if isinstance(content, str) else _dump(content, MODEL_RESULT_CHARS)
        if res.structured:
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": body})
        else:
            text_replies.append(f"Result of {call['name']}:\n{body}")

    def flush_text(self, text_replies: list[str]) -> None:
        if text_replies:
            self.messages.append({"role": "user", "content": "\n\n".join(text_replies)})

    def run_tool(self, call: dict[str, Any]) -> Any:
        self.calls += 1
        name, args = call["name"], call.get("arguments") or {}
        result = self.tools.dispatch(name, args)
        self.trace.add("tool", call_no=self.calls, name=name, arguments=args,
                       result=_dump(result, TRACE_RESULT_CHARS))
        if name == "note_finding":
            self.trace.add("finding", finding=args.get("finding"), event_ids=args.get("event_ids") or [],
                           finding_kind=args.get("kind"))
            self.findings.append(str(args.get("finding", "")))
        return result

    def validate(self, args: dict[str, Any]) -> tuple[Report | None, str | None]:
        args, decoded = decode_json_fields(args)
        if decoded:
            self.trace.add("info", message=f"submit_report sent {', '.join(decoded)} as JSON text; decoded it")
        try:
            return Report.model_validate(args), None
        except ValidationError as exc:
            return None, str(exc)

    def correct(self, error: str) -> Report | None:
        """Send the validation error back once and ask for a corrected submit_report."""
        self.trace.add("info", message="report failed validation; asking for a corrected submit_report",
                       error=error[:TRACE_RESULT_CHARS])
        self.messages.append({"role": "user", "content": "submit_report failed schema validation:\n" + error +
                              "\nCall submit_report again with a corrected report. Keep the same evidence."})
        res = self.chat([SUBMIT_SCHEMA])
        call = next((c for c in res.tool_calls if c["name"] == "submit_report"), None)
        if call is None:
            return None
        self.trace.add("tool", call_no=self.calls, name="submit_report", arguments=call["arguments"],
                       result="(correction)")
        report, err = self.validate(call["arguments"])
        if err:
            self.trace.add("error", message="corrected report still invalid", error=err[:TRACE_RESULT_CHARS])
        return report

    def submit(self, res: llm.ChatResult, call: dict[str, Any], text_replies: list[str]) -> tuple[Report | None, str | None]:
        report, err = self.validate(call.get("arguments") or {})
        self.reply(res, call, {"ok": True} if report else {"error": "validation failed", "detail": err},
                   text_replies)
        return report, err

    def turn(self, tools: list[dict[str, Any]]) -> tuple[Report | None, str | None, bool]:
        """One model turn. Returns (report, validation_error, made_tool_calls)."""
        res = self.chat(tools)
        if not res.tool_calls:
            return None, None, False
        report: Report | None = None
        error: str | None = None
        text_replies: list[str] = []
        for call in res.tool_calls:
            if report is not None or error is not None:
                self.reply(res, call, {"error": "not executed: submit_report was called"}, text_replies)
            elif call["name"] == "submit_report":
                self.calls += 1
                self.trace.add("tool", call_no=self.calls, name="submit_report", arguments=call.get("arguments"),
                               result="(submitted)")
                report, error = self.submit(res, call, text_replies)
            elif self.calls >= self.max_calls:
                self.reply(res, call, {"error": "not executed: tool budget exhausted"}, text_replies)
            else:
                self.reply(res, call, self.run_tool(call), text_replies)
        self.flush_text(text_replies)
        return report, error, True

    def finish(self, report: Report | None, error: str | None) -> Report | None:
        if report is None and error is not None:
            return self.correct(error)
        return report

    def run(self) -> Report:
        catalog = build_catalog(self.tools)
        mf = getattr(self.tools, "metric_fields", None)
        metric_fields = mf() if callable(mf) else None
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT.replace("{max_calls}", str(self.max_calls))},
                         {"role": "user", "content": build_user_message(self.ticket, catalog, metric_fields,
                                                                        self.feedback)}]
        self.trace.add("info", message=f"investigation started: model={self.model} budget={self.max_calls} "
                                       f"catalog_codes={len(catalog)}")
        idle = 0
        while self.calls < self.max_calls:
            report, error, acted = self.turn(TOOL_SCHEMAS)
            if report is not None or error is not None:
                return self.finish(report, error) or self.fallback("submitted report failed validation twice")
            if acted:
                idle = 0
                continue
            idle += 1
            if idle >= MAX_IDLE_TURNS:
                break
            self.messages.append({"role": "user", "content": "Continue the investigation: call a tool, or call "
                                  "submit_report if you have enough evidence."})

        self.trace.add("info", message=f"tool budget reached ({self.calls}/{self.max_calls}) without submit_report; "
                                       f"asking for a final report")
        self.messages.append({"role": "user", "content": "Tool budget exhausted. Call submit_report now with what "
                              "you have found. Cite only event ids you retrieved, set confidence honestly, and list "
                              "what you could not confirm in open_questions. If you found no concrete cause, use "
                              "category unknown."})
        report, error, _ = self.turn([SUBMIT_SCHEMA])
        if report is not None or error is not None:
            return self.finish(report, error) or self.fallback("final report failed validation twice")
        return self.fallback("no submit_report after the tool budget was exhausted")

    def fallback(self, why: str) -> Report:
        self.trace.add("info", message=f"returning fallback report: {why}")
        oq = [f"No valid root cause was submitted ({why}); {self.calls} tool calls used."]
        oq += [f"Finding: {f}" for f in self.findings if f] or ["No findings were recorded."]
        return Report.model_validate({
            "problem": f"{self.ticket.get('title', 'incident')} (root cause not determined)",
            "root_cause": {"summary": "Root cause not determined by the investigator.", "category": "unknown",
                           "entity": "unknown", "entity_type": "unknown", "identifiers": {}, "event_ids": []},
            "causal_chain": [], "rejected_candidates": [],
            "affected": {k: self.ticket[k] for k in ("service", "host") if self.ticket.get(k)},
            "confidence": 0.1, "open_questions": oq})


# ---------------------------------------------------------------------------------------------
# event id verification
# ---------------------------------------------------------------------------------------------
def verify_event_ids(report: Report, es_tools: Any, trace: Any) -> Report:
    """Drop every cited event id that does not exist in the cluster. Never invents ids."""
    ids = set(report.root_cause.event_ids) | {s.event_id for s in report.causal_chain}
    for rc in report.rejected_candidates:
        ids |= set(rc.event_ids)
    missing: set[str] = set()
    unverified: set[str] = set()
    for eid in sorted(ids):
        res = es_tools.dispatch("get_doc", {"event_id": eid})
        if isinstance(res, dict) and res.get("error"):
            unverified.add(eid)
        elif not (isinstance(res, dict) and res.get("found")):
            missing.add(eid)
    trace.add("verify", checked=len(ids), dropped=sorted(missing), unverified=sorted(unverified))
    if not missing and not unverified:
        return report

    data = report.model_dump()
    rc = data["root_cause"]
    before = len(rc["event_ids"])
    rc["event_ids"] = [e for e in rc["event_ids"] if e not in missing]
    data["causal_chain"] = [s for s in data["causal_chain"] if s["event_id"] not in missing]
    for cand in data["rejected_candidates"]:
        cand["event_ids"] = [e for e in cand["event_ids"] if e not in missing]
    if missing:
        data["open_questions"].append(
            f"Removed {len(missing)} cited event id(s) that do not exist in the cluster: {', '.join(sorted(missing))}")
        if before and not rc["event_ids"] and rc["category"] != "unknown":
            data["confidence"] = min(data["confidence"], 0.3)
            data["open_questions"].append("No cited root-cause event survived verification; confidence capped at 0.3.")
    if unverified:
        data["open_questions"].append(
            f"Could not verify {len(unverified)} event id(s) (Elasticsearch error): {', '.join(sorted(unverified))}")
    return Report.model_validate(data)


def run(ticket: dict[str, Any], es_tools: Any, trace: Any, max_calls: int = 20, *, model: str | None = None,
        seed: int | None = None, feedback: str | None = None) -> dict[str, Any]:
    """Investigate one ticket. Returns a report dict matching the Report contract in CLAUDE.md.

    feedback (the critic's corrections plus the previous report) is appended to the first user message on a retry.
    """
    loop = _Loop(ticket, es_tools, trace, max_calls, model, seed, feedback)
    report = loop.run()
    report = verify_event_ids(report, es_tools, trace)
    trace.add("info", message=f"investigation finished: {loop.calls} tool calls, category="
                              f"{report.root_cause.category}, confidence={report.confidence}")
    return report.model_dump()


# ---------------------------------------------------------------------------------------------
# follow-up questions
# ---------------------------------------------------------------------------------------------
def build_digest(job: dict[str, Any]) -> str:
    """The investigation as the model saw it: tool calls with arguments and shortened results, findings, and
    earlier follow-ups with their answers. Long digests lose the middle, never the first or last steps."""
    lines: list[str] = []
    for e in job.get("trace") or []:
        if e.get("kind") == "tool" and e.get("name") not in ("submit_report", "answer_question"):
            res = (e.get("result") or "")[:DIGEST_RESULT_CHARS]
            lines.append(f"[{e.get('phase') or '-'} #{e.get('call_no')}] {e.get('name')} "
                         f"{json.dumps(e.get('arguments') or {}, separators=(',', ':'))} -> {res}")
        elif e.get("kind") == "finding":
            lines.append(f"[finding {e.get('finding_kind') or 'other'}] {e.get('finding')} "
                         f"ids={e.get('event_ids') or []}")
    for a in job.get("actions") or []:
        if a.get("type") == "ask" and a.get("state") == "answered":
            lines.append(f"[earlier follow-up] Q: {a.get('question')} A: {a.get('answer')} ids={a.get('event_ids')}")
    text = "\n".join(lines)
    if len(text) > DIGEST_MAX_CHARS:
        half = DIGEST_MAX_CHARS // 2
        text = text[:half] + f"\n... [{len(text) - DIGEST_MAX_CHARS} chars of the middle omitted] ...\n" + text[-half:]
    return text


def build_ask_message(job: dict[str, Any], question: str, catalog: list[dict[str, Any]]) -> str:
    t = {k: v for k, v in (job.get("ticket") or {}).items() if v not in (None, "", {}, [])}
    return "\n".join([
        "Incident ticket:", json.dumps(t, indent=2), "",
        f"Ticket time: {t.get('created')}. Tool time windows end here unless you set `end`.", "",
        "Your final report:", json.dumps(job.get("report"), indent=2), "",
        "Investigation digest (tool calls and findings, in order):", build_digest(job) or "(empty)", "",
        "Event catalog (event.code  index  event.action), codes present in the cluster:",
        *[f"{c['code']}  {c['index']}  {c['action']}" for c in catalog], "",
        "Follow-up question from the on-call engineer:", question.strip()])


def decode_json_fields(args: Any) -> tuple[Any, list[str]]:
    """The model sometimes sends an object or list field of submit_report as JSON text (root_cause as
    "{\"summary\": ...}"), and repeats it when asked to correct it. Decode such fields, top level and
    root_cause.identifiers, before validation. Returns (args, names of the decoded fields)."""
    if not isinstance(args, dict):
        return args, []
    out, decoded = dict(args), []
    for key, value in args.items():
        parsed = _json_container(value)
        if parsed is not None:
            out[key] = parsed
            decoded.append(key)
    rc = out.get("root_cause")
    if isinstance(rc, dict) and (ids := _json_container(rc.get("identifiers"))) is not None:
        out["root_cause"] = {**rc, "identifiers": ids}
        decoded.append("root_cause.identifiers")
    return out, decoded


def _json_container(value: Any) -> Any:
    if not isinstance(value, str) or value.strip()[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None

class _AskLoop(_Loop):
    """The investigator loop with answer_question as the terminal tool."""

    def __init__(self, job: dict[str, Any], question: str, es_tools: Any, trace: Any, max_calls: int,
                 model: str | None, seed: int | None) -> None:
        super().__init__(job.get("ticket") or {}, es_tools, trace, max_calls, model, seed)
        self.job, self.question = job, question

    def answer_turn(self, tools: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
        res = self.chat(tools)
        if not res.tool_calls:
            return None, False
        answer: dict[str, Any] | None = None
        text_replies: list[str] = []
        for call in res.tool_calls:
            args = call.get("arguments") or {}
            if answer is not None:
                self.reply(res, call, {"error": "not executed: answer_question was called"}, text_replies)
            elif call["name"] == "answer_question":
                self.calls += 1
                self.trace.add("tool", call_no=self.calls, name="answer_question", arguments=args,
                               result="(answered)")
                if isinstance(args.get("answer"), str) and args["answer"].strip():
                    answer = args
                    self.reply(res, call, {"ok": True}, text_replies)
                else:
                    self.reply(res, call, {"error": "answer must be a non-empty string"}, text_replies)
            elif call["name"] == "submit_report":
                self.reply(res, call, {"error": "submit_report is not available in a follow-up; call "
                                                "answer_question"}, text_replies)
            elif self.calls >= self.max_calls - 1:   # keep the last call for answer_question
                self.reply(res, call, {"error": "not executed: tool budget exhausted; call answer_question"},
                           text_replies)
            else:
                self.reply(res, call, self.run_tool(call), text_replies)
        self.flush_text(text_replies)
        return answer, True

    def run(self) -> dict[str, Any] | None:  # type: ignore[override]
        catalog = build_catalog(self.tools)
        self.messages = [{"role": "system", "content": ASK_PROMPT.replace("{max_calls}", str(self.max_calls))},
                         {"role": "user", "content": build_ask_message(self.job, self.question, catalog)}]
        self.trace.add("info", message=f"follow-up started: model={self.model} budget={self.max_calls}")
        idle = 0
        while self.calls < self.max_calls - 1:
            answer, acted = self.answer_turn(ASK_TOOL_SCHEMAS)
            if answer is not None:
                return answer
            if acted:
                idle = 0
                continue
            idle += 1
            if idle >= MAX_IDLE_TURNS:
                break
            self.messages.append({"role": "user", "content": "Call a tool, or call answer_question if you can "
                                  "answer now."})
        self.messages.append({"role": "user", "content": "Tool budget exhausted. Call answer_question now with "
                              "what you have. Cite only event ids you retrieved or that appear in the report or "
                              "digest, and say what you could not confirm."})
        answer, _ = self.answer_turn([ANSWER_SCHEMA])
        return answer


def ask(job: dict[str, Any], question: str, es_tools: Any, trace: Any, max_calls: int = 8, *,
        model: str | None = None, seed: int | None = None) -> dict[str, Any]:
    """Answer a follow-up about a finished job (its Job JSON). Returns {"answer", "event_ids", "confidence"}.

    Raises RuntimeError if the model never calls answer_question. Cited ids that do not exist are dropped and
    the answer says so.
    """
    loop = _AskLoop(job, question, es_tools, trace, max(2, max_calls), model, seed)
    got = loop.run()
    if got is None:
        raise RuntimeError(f"no answer_question after {loop.calls} tool calls")
    ids = list(dict.fromkeys(str(e) for e in got.get("event_ids") or [] if e))
    missing, unverified = [], []
    for eid in ids:
        res = es_tools.dispatch("get_doc", {"event_id": eid})
        if isinstance(res, dict) and res.get("error"):
            unverified.append(eid)
        elif not (isinstance(res, dict) and res.get("found")):
            missing.append(eid)
    trace.add("verify", checked=len(ids), dropped=missing, unverified=unverified)
    answer = got["answer"].strip()
    if missing:
        answer += f" (Removed {len(missing)} cited id(s) that do not exist in the cluster.)"
    conf = got.get("confidence")
    return {"answer": answer, "event_ids": [e for e in ids if e not in missing],
            "confidence": float(conf) if isinstance(conf, (int, float)) else None}
