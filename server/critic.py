"""Critic: a no-tools review of a report against the full text of the docs it cites.

run(report, ticket, es_tools, trace) -> {"verdict": "pass"|"fail", "checks": {...7...}, "corrections": [...], ...}

The model (TRIAGENT_MODEL_CRITIC, reasoning on) gets no tool schemas. This module fetches every cited event with
get_doc and sends the model the report, those docs, the ticket time, and three checks computed in code
(ids_exist, chain_time_order, adjacent_steps_linked). Those three are exact, so the code result wins over the
model's for them. The model judges the other four.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import llm

log = logging.getLogger("triagent.critic")

PROMPT = (Path(__file__).parent / "prompts" / "critic.md").read_text(encoding="utf-8")
CHECKS = ["ids_exist", "chain_time_order", "root_cause_concrete", "adjacent_steps_linked", "rejections_listed",
          "category_matches", "confidence_consistent"]
CODE_CHECKS = ("ids_exist", "chain_time_order", "adjacent_steps_linked")
MAX_DOCS = 40
MAX_TOKENS = 12288
JSON_ONLY = ("Return only the JSON object described in the instructions, with all seven checks. "
             "No reasoning, no prose.")

# Fields whose values link two events (the investigator's pivot fields plus their aliases on other datasets).
LINK_FIELDS = (
    "host.name", "service.name", "observer.name", "user.name", "source.ip", "destination.ip", "change.id",
    "change.configuration_item", "kubernetes.pod.name", "kubernetes.node.name", "labels.rack", "labels.row",
    "labels.cluster", "orchestrator.cluster.name", "cicd.commit", "cicd.pipeline", "datacenter.app.load_balancer",
    "datacenter.iface.peer", "datacenter.iface.device", "datacenter.crac.unit", "datacenter.pdu.unit",
    "datacenter.ups.unit", "datacenter.tls.endpoint", "tls.server.x509.subject.common_name", "rule.name",
)


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cited_ids(report: dict[str, Any]) -> list[str]:
    rc = report.get("root_cause") or {}
    ids = list(rc.get("event_ids") or []) + [s.get("event_id") for s in report.get("causal_chain") or []]
    for cand in report.get("rejected_candidates") or []:
        ids += list(cand.get("event_ids") or [])
    seen: dict[str, None] = {}
    for i in ids:
        if i and i not in seen:
            seen[i] = None
    return list(seen)


def fetch_docs(es_tools: Any, ids: list[str]) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {}
    for eid in ids[:MAX_DOCS]:
        res = es_tools.dispatch("get_doc", {"event_id": eid})
        out[eid] = res.get("doc") if isinstance(res, dict) and res.get("found") else None
    return out


def _link_values(doc: dict[str, Any]) -> set[str]:
    vals: set[str] = set()
    for f in LINK_FIELDS:
        v = doc.get(f)
        for x in v if isinstance(v, list) else [v]:
            if x not in (None, ""):
                vals.add(str(x).lower())
    return vals


def code_checks(report: dict[str, Any], docs: dict[str, dict[str, Any] | None], ticket_time: str | None
                ) -> dict[str, dict[str, Any]]:
    """The three checks that are exact: ids exist, chain order, adjacent steps share an identifier."""
    missing = [i for i, d in docs.items() if d is None]
    out: dict[str, dict[str, Any]] = {"ids_exist": {
        "ok": not missing, "detail": f"missing: {', '.join(missing)}" if missing else f"all {len(docs)} found"}}

    chain = [s for s in report.get("causal_chain") or [] if s.get("event_id")]
    times = [(s["event_id"], _ts((docs.get(s["event_id"]) or {}).get("@timestamp"))) for s in chain]
    problems: list[str] = []
    for (a, ta), (b, tb) in zip(times, times[1:]):
        if ta and tb and tb < ta:
            problems.append(f"{b} ({tb:%H:%M:%S}) comes after {a} ({ta:%H:%M:%S}) but is earlier")
    tt = _ts(ticket_time)
    last = next((t for _, t in reversed(times) if t), None)
    if tt and last and last > tt:
        problems.append(f"last chain step ({last:%H:%M:%S}) is after the ticket ({tt:%H:%M:%S})")
    if not chain:
        problems.append("causal chain is empty")
    out["chain_time_order"] = {"ok": not problems, "detail": "; ".join(problems) or "in order, ends before ticket"}

    unlinked: list[str] = []
    for a, b in zip(chain, chain[1:]):
        da, db = docs.get(a["event_id"]), docs.get(b["event_id"])
        if da is None or db is None:
            continue
        if not (_link_values(da) & _link_values(db)):
            unlinked.append(f"{a['event_id']} -> {b['event_id']}")
    out["adjacent_steps_linked"] = {"ok": not unlinked, "detail": ("no shared identifier: " + "; ".join(unlinked))
                                    if unlinked else "every adjacent pair shares an identifier"}
    return out


def build_message(report: dict[str, Any], docs: dict[str, dict[str, Any] | None], ticket_time: str | None,
                  auto: dict[str, dict[str, Any]]) -> str:
    return "\n\n".join([
        f"Ticket time: {ticket_time}",
        "Report:\n" + json.dumps(report, indent=1, default=str),
        "Cited events (event id -> full document; null means not found):\n" +
        json.dumps(docs, indent=1, default=str),
        "Automatic checks (exact; use them for checks 1, 2 and 4):\n" + json.dumps(auto, indent=1),
    ])


def parse_verdict(text: str) -> dict[str, Any] | None:
    body = llm.strip_think(text)
    for obj in llm._json_objects(body):
        if "verdict" in obj or "checks" in obj:
            return obj
    return None


def merge(model: dict[str, Any] | None, auto: dict[str, dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    mchecks = (model or {}).get("checks") or {}
    for name in CHECKS:
        if name in CODE_CHECKS:
            checks[name] = {**auto[name], "source": "code"}
            continue
        m = mchecks.get(name)
        if isinstance(m, dict) and "ok" in m:
            checks[name] = {"ok": bool(m.get("ok")), "detail": str(m.get("detail", ""))[:500], "source": "model"}
        else:
            checks[name] = {"ok": True, "detail": "not assessed (critic output missing this check)",
                            "source": "missing"}
    corrections = [str(c) for c in (model or {}).get("corrections") or [] if c]
    for name in CODE_CHECKS:
        if not checks[name]["ok"]:
            corrections.append(f"Fix {name}: {checks[name]['detail']}")
    failed = [n for n, c in checks.items() if not c["ok"]]
    return {"verdict": "fail" if failed else "pass", "failed": failed, "checks": checks,
            "corrections": corrections}


def run(report: dict[str, Any], ticket: dict[str, Any], es_tools: Any, trace: Any) -> dict[str, Any]:
    ticket_time = ticket.get("created")
    ids = cited_ids(report)
    docs = fetch_docs(es_tools, ids)
    auto = code_checks(report, docs, ticket_time)
    messages = [{"role": "system", "content": PROMPT},
                {"role": "user", "content": build_message(report, docs, ticket_time, auto)}]
    model_out: dict[str, Any] | None = None
    note = None
    try:
        res = llm.chat(messages, role="critic", thinking=True, max_tokens=MAX_TOKENS,
                       trace=lambda d: trace.add("llm", **d))
        model_out = parse_verdict(res.text)
        if model_out is None:
            # Usually the reasoning ran out of tokens before the JSON. Ask once more for the JSON alone.
            res = llm.chat(messages + [{"role": "user", "content": JSON_ONLY}], role="critic", thinking=False,
                           max_tokens=4096, trace=lambda d: trace.add("llm", **d))
            model_out = parse_verdict(res.text)
            note = None if model_out is not None else "critic output was not JSON twice; code checks only"
    except Exception as exc:  # noqa: BLE001 - a critic outage must not lose the report
        log.warning("critic model call failed: %s", exc)
        note = f"critic model call failed ({type(exc).__name__}); code checks only"
    out = merge(model_out, auto)
    out.update(model=llm.model_for("critic"), docs_checked=len(docs), docs_cited=len(ids))
    if note:
        out["note"] = note
    trace.add("critic", verdict=out["verdict"], failed=out["failed"], corrections=out["corrections"][:10],
              **({"note": note} if note else {}))
    return out


def feedback(review: dict[str, Any], report: dict[str, Any]) -> str:
    """Text appended to the investigator's first message on the retry."""
    failed = [f"- {n}: {review['checks'][n]['detail']}" for n in review.get("failed", [])]
    corr = [f"- {c}" for c in review.get("corrections", [])]
    return "\n".join([
        "A reviewer rejected a previous report for this ticket. Investigate again and fix what it found.",
        "Failed checks:", *(failed or ["- (none listed)"]),
        "Corrections:", *(corr or ["- (none listed)"]),
        "Previous report (you may reuse its event ids; check anything the reviewer doubted):",
        json.dumps(report, separators=(",", ":"), default=str),
    ])
