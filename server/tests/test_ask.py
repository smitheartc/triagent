"""Follow-up asks: investigator.ask (scripted model, fake tools) and POST /jobs/{id}/ask on the real app."""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

import contracts as C
import investigator
import llm
import main
from test_kickoff import GOOD_REPORT, TICKET, FakeTools, _fake_chat, _result
from tracelog import Trace

SCENARIOS = ("cooling_failure", "bad_config_change", "disk_cascade", "memory_leak", "pdu_overload", "bad_optic",
             "cert_expiry", "security_breach", "disk_fill")


def _job(**extra) -> dict:
    trace = Trace()
    trace.phase = "investigate"
    trace.add("tool", call_no=1, name="find_changes", arguments={"lookback": "6h"}, result='{"rows":[...]}')
    trace.add("finding", finding="deploy on another service", event_ids=["real-2"], finding_kind="rejected")
    trace.add("tool", call_no=2, name="submit_report", arguments={}, result="(submitted)")
    return {"job_id": "job-a", "status": "done", "ticket": TICKET, "report": GOOD_REPORT, "trace": trace.entries(),
            "actions": [], **extra}


def test_digest_has_tool_calls_findings_and_earlier_answers():
    d = investigator.build_digest(_job(actions=[{"type": "ask", "state": "answered", "question": "q1?",
                                                 "answer": "a1", "event_ids": ["real-1"]}]))
    assert 'find_changes {"lookback":"6h"}' in d and "[finding rejected] deploy on another service" in d
    assert "submit_report" not in d and "Q: q1? A: a1" in d


def test_ask_prompt_has_no_scenario_hints():
    text = investigator.ASK_PROMPT.lower()
    assert not [s for s in SCENARIOS if s in text or s.replace("_", " ") in text]


def test_ask_answers_with_verified_ids(monkeypatch):
    tools = FakeTools(known_ids={"real-1", "real-2"})
    script = iter([
        _result([{"name": "get_doc", "arguments": {"event_id": "real-2"}}]),
        _result([{"name": "answer_question", "arguments": {
            "answer": "The deploy was on another service and finished after the failure began.",
            "event_ids": ["real-2", "made-up-9"], "confidence": 0.8}}]),
    ])
    chat = _fake_chat(lambda m, t: next(script))
    monkeypatch.setattr(llm, "chat", chat)
    trace = Trace()
    got = investigator.ask(_job(), "Why did you reject the deploy?", tools, trace, max_calls=6)
    assert got["event_ids"] == ["real-2"] and "Removed 1 cited id" in got["answer"]
    assert "answer_question" in chat.seen[0] and "submit_report" not in chat.seen[0]
    assert [e["name"] for e in trace.entries() if e["kind"] == "tool"] == ["get_doc", "answer_question"]
    assert next(e for e in trace.entries() if e["kind"] == "verify")["dropped"] == ["made-up-9"]


def test_ask_keeps_last_call_for_the_answer(monkeypatch):
    tools = FakeTools(known_ids={"real-1"})

    def script(messages, tool_list):
        if [t["function"]["name"] for t in tool_list] == ["answer_question"]:
            return _result([{"name": "answer_question", "arguments": {"answer": "partial", "event_ids": ["real-1"]}}])
        return _result([{"name": "search_events", "arguments": {"lookback": "1h"}}])
    monkeypatch.setattr(llm, "chat", _fake_chat(script))
    trace = Trace()
    got = investigator.ask(_job(), "what else?", tools, trace, max_calls=4)
    searches = [e for e in trace.entries() if e["kind"] == "tool" and e["name"] == "search_events"]
    assert got["answer"] == "partial" and len(searches) == 3      # 4 calls: 3 tools + the answer


def test_ask_without_answer_raises(monkeypatch):
    monkeypatch.setattr(llm, "chat", _fake_chat(lambda m, t: _result(text="I think it was the deploy.")))
    with pytest.raises(RuntimeError):
        investigator.ask(_job(), "why?", FakeTools(), Trace(), max_calls=4)


# ---------------------------------------------------------------------------------------------
# the route on the real app
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    main.app.state.registry = main.Registry()
    main.app.state.settings = dataclasses.replace(main.Settings.from_env(), jobs_dir=tmp_path, ticket_backend="none")
    with TestClient(main.app) as c:
        yield c


def _finished_job(status="done") -> main.Job:
    job = main.Job(job_id="job-r", ticket=dict(TICKET), loop=asyncio.get_event_loop_policy().get_event_loop())
    job.report, job.status = GOOD_REPORT, status
    main.app.state.registry.jobs[job.job_id] = job
    return job


def _wait(client, action_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = C.Job.model_validate(client.get("/jobs/job-r").json())
        a = next(a for a in job.actions if a.id == action_id)
        if a.state in ("answered", "failed"):
            return job, a
        time.sleep(0.02)
    raise AssertionError("ask never finished")


def test_route_runs_the_real_runner(client, monkeypatch, tmp_path):
    seen = {}

    def fake_ask(job, question, es, trace, max_calls, seed=None):
        seen.update(question=question, max_calls=max_calls, has_trace="trace" in job, phase=trace.phase)
        trace.add("tool", call_no=1, name="answer_question", arguments={}, result="(answered)")
        return {"answer": "Other service.", "event_ids": ["real-2"], "confidence": 0.7}
    monkeypatch.setattr(investigator, "ask", fake_ask)
    _finished_job()
    act = C.Action.model_validate(client.post("/jobs/job-r/ask", json={"question": "Why not the deploy?"}).json())
    assert act.state == "queued" and act.type == "ask"
    job, a = _wait(client, act.id)
    assert (a.state, a.answer, a.event_ids) == ("answered", "Other service.", ["real-2"])
    assert seen == {"question": "Why not the deploy?", "max_calls": main.app.state.settings.ask_max_calls,
                    "has_trace": True, "phase": "ask"}
    phases = {e.phase for e in job.trace if e.kind in ("tool", "action")}
    assert "ask" in phases and job.status == "done"
    assert (tmp_path / "job-r.json").exists()


def test_route_marks_failed_and_refuses_unfinished_jobs(client, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no answer_question after 8 tool calls")
    monkeypatch.setattr(investigator, "ask", broken)
    job = _finished_job()
    act = client.post("/jobs/job-r/actions", json={"type": "ask", "question": "why?"}).json()
    _, a = _wait(client, act["id"])
    assert a.state == "failed" and "no answer_question" in a.error
    job.status = "running"
    assert client.post("/jobs/job-r/ask", json={"question": "why?"}).status_code == 409
    assert client.post("/jobs/nope/ask", json={"question": "why?"}).status_code == 404
