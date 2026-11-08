"""Critic and the retry path, no network."""
from __future__ import annotations

import asyncio
import json

import critic
import investigator
import llm
import main
from tracelog import Trace

DOCS = {
    "chg": {"@timestamp": "2026-11-07T10:00:00Z", "event.code": "CICD-CONFIG-PUSH", "observer.name": "lb-1",
            "change.id": "CHG1"},
    "q": {"@timestamp": "2026-11-07T10:30:00Z", "event.code": "LB-QUEUE-HIGH", "observer.name": "lb-1",
          "service.name": "auth-svc"},
    "err": {"@timestamp": "2026-11-07T11:00:00Z", "event.code": "APP-ERROR", "service.name": "auth-svc"},
    "far": {"@timestamp": "2026-11-07T09:00:00Z", "event.code": "SYS-CRON", "host.name": "cmp-x"},
}
TICKET = {"ticket_id": "INC1", "created": "2026-11-07T11:05:00Z", "title": "auth-svc 5xx", "namespace": "dal1"}


def report(chain, rc_ids=("chg",), rejected=True, category="config", confidence=0.9):
    return {"problem": "p", "root_cause": {"summary": "s", "category": category, "entity": "lb-1",
                                           "entity_type": "load_balancer", "identifiers": {}, "event_ids": list(rc_ids)},
            "causal_chain": [{"timestamp": "", "event_id": e, "what": "w", "why_it_matters": "y"} for e in chain],
            "rejected_candidates": [{"what": "x", "event_ids": [], "why_rejected": "r"}] if rejected else [],
            "affected": {}, "confidence": confidence, "open_questions": []}


class FakeTools:
    def __init__(self):
        self.asked = []

    def dispatch(self, name, args):
        assert name == "get_doc"
        self.asked.append(args["event_id"])
        d = DOCS.get(args["event_id"])
        return {"found": True, "doc": d} if d else {"found": False, "event_id": args["event_id"]}


def test_code_checks_pass_on_a_linked_ordered_chain():
    rep = report(["chg", "q", "err"])
    auto = critic.code_checks(rep, {i: DOCS.get(i) for i in critic.cited_ids(rep)}, TICKET["created"])
    assert all(c["ok"] for c in auto.values()), auto


def test_code_checks_catch_missing_order_and_links():
    rep = report(["q", "chg", "far", "ghost"])
    auto = critic.code_checks(rep, {i: DOCS.get(i) for i in critic.cited_ids(rep)}, TICKET["created"])
    assert not auto["ids_exist"]["ok"] and "ghost" in auto["ids_exist"]["detail"]
    assert not auto["chain_time_order"]["ok"]
    assert not auto["adjacent_steps_linked"]["ok"]
    late = report(["chg", "q", "err"])
    assert not critic.code_checks(late, {i: DOCS[i] for i in ("chg", "q", "err")},
                                  "2026-11-07T10:45:00Z")["chain_time_order"]["ok"]


def _model_says(verdict_json: str):
    def chat(messages, tools=None, model=None, thinking=True, max_tokens=4096, seed=None, trace=None,
             role="investigator"):
        assert tools is None and role == "critic"          # the critic never gets tools
        assert "Cited events" in messages[1]["content"]
        return llm.ChatResult(text=verdict_json, tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 1},
                              latency_ms=1)
    return chat


ALL_OK = json.dumps({"verdict": "pass", "corrections": [], "checks": {
    n: {"ok": True, "detail": "fine"} for n in critic.CHECKS}})


def test_code_result_overrides_a_lenient_model(monkeypatch):
    monkeypatch.setattr(llm, "chat", _model_says(ALL_OK))
    out = critic.run(report(["q", "chg"]), TICKET, FakeTools(), Trace())
    assert out["verdict"] == "fail" and out["failed"] == ["chain_time_order"]
    assert any("chain_time_order" in c for c in out["corrections"])


def test_model_judged_check_can_fail_and_bad_output_falls_back(monkeypatch):
    bad = json.loads(ALL_OK)
    bad["checks"]["root_cause_concrete"] = {"ok": False, "detail": "cause is a symptom"}
    bad["corrections"] = ["Find the change before the first error."]
    monkeypatch.setattr(llm, "chat", _model_says("<think>hmm</think>" + json.dumps(bad)))
    tools = FakeTools()
    out = critic.run(report(["chg", "q", "err"]), TICKET, tools, Trace())
    assert out["verdict"] == "fail" and out["failed"] == ["root_cause_concrete"]
    assert sorted(tools.asked) == ["chg", "err", "q"]
    monkeypatch.setattr(llm, "chat", _model_says("I cannot decide."))
    tr = Trace()
    out = critic.run(report(["chg", "q", "err"]), TICKET, FakeTools(), tr)
    assert out["verdict"] == "pass" and "code checks only" in out["note"]

    replies = iter(["<think>out of tokens", json.dumps(bad)])            # second, JSON-only call recovers
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: llm.ChatResult(
        text=next(replies), tool_calls=[], usage={}, latency_ms=1))
    out = critic.run(report(["chg", "q", "err"]), TICKET, FakeTools(), Trace())
    assert out["failed"] == ["root_cause_concrete"] and "note" not in out


def test_pipeline_retries_once_with_feedback(monkeypatch, tmp_path):
    calls = []

    def fake_investigate(ticket, es, trace, max_calls, seed=None, feedback=None):
        calls.append(feedback)
        trace.add("tool", call_no=1, name="search_events", arguments={}, result="{}")
        return report(["chg", "q", "err"], category="deploy" if feedback is None else "config")

    verdicts = iter([{"verdict": "fail", "failed": ["category_matches"],
                      "checks": {"category_matches": {"ok": False, "detail": "push is config"}},
                      "corrections": ["Use category config."]},
                     {"verdict": "pass", "failed": [], "checks": {}, "corrections": []}])
    monkeypatch.setattr(investigator, "run", fake_investigate)
    monkeypatch.setattr(critic, "run", lambda *a: next(verdicts))
    settings = main.Settings.from_env()
    settings = main.Settings(**{**settings.__dict__, "jobs_dir": tmp_path, "critic_enabled": True})

    async def go():
        job = main.Job(job_id="job-t", ticket={**TICKET, "labels": {}}, loop=asyncio.get_running_loop())
        await main.run_pipeline(job, settings)
        return job
    job = asyncio.run(go())

    assert job.status == "done" and len(calls) == 2
    assert calls[0] is None and "Use category config." in calls[1] and "category_matches" in calls[1]
    assert job.report_initial["root_cause"]["category"] == "deploy"
    assert job.report["root_cause"]["category"] == "config"
    assert job.critic["retried"] and job.critic["final"]["verdict"] == "pass" and job.critic["kept"] == "retry"
    assert job.totals_initial["tool_calls"] == 1 and job.trace.totals()["tool_calls"] == 2
    phases = {e.get("phase") for e in job.trace.entries() if e["kind"] == "tool"}
    assert phases == {"investigate", "retry"}


def test_retry_rule_keeps_the_better_report():
    one = {"verdict": "fail", "failed": ["confidence_consistent"],
           "checks": {"confidence_consistent": {"ok": False, "detail": "too high"}}}
    two = {"verdict": "fail", "failed": ["chain_time_order", "category_matches"], "checks": {
        "chain_time_order": {"ok": False, "detail": "out of order"}, "category_matches": {"ok": False, "detail": "x"}}}
    assert main.choose_report(one, two) == "initial"
    assert main.choose_report(two, one) == "retry"
    assert main.choose_report(one, {"verdict": "pass", "failed": []}) == "retry"
    assert main.choose_report(one, one) == "initial"
    noted = main.with_review_notes(report(["chg"]), one)
    assert noted["open_questions"][-1].startswith("Reviewer: confidence_consistent failed: too high")


def test_rate_limit_waits_retry_after(monkeypatch):
    import httpx
    import openai
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(429, headers={"retry-after": "7"}, request=req)
    calls = {"n": 0}

    class FakeCompletions:
        def create(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise openai.RateLimitError("slow down", response=resp, body=None)
            msg = type("M", (), {"content": "ok", "tool_calls": None})()
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": usage})()

    fake = type("Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()})()
    monkeypatch.setattr(llm, "_client", lambda *a: fake)
    assert llm.chat([{"role": "user", "content": "x"}], role="small").text == "ok"
    assert waits == [7.0, 7.0]
