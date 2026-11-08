"""Kickoff-path tests: no network, fake llm.chat and fake tools."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import investigator
import llm
import main
from tracelog import Trace

TICKET = {"ticket_id": "INC0000001", "title": "checkout-api: 5xx above SLO", "description": "error-rate alert",
          "priority": "P1", "created": "2026-11-07T14:03:00Z", "service": "checkout-api", "host": None,
          "labels": {"cluster": "k8s-prod-2"}, "namespace": "dal1", "source_event_id": "ticket-doc"}


# ---------------------------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------------------------
class FakeTools:
    def __init__(self, known_ids: set[str] | None = None) -> None:
        self.known = known_ids or set()
        self.calls: list[tuple[str, dict]] = []

    def catalog(self) -> list[dict[str, Any]]:
        return [{"code": "APP-ERROR", "index": "logs-app.logs-dal1", "action": "app-request-errors", "count": 3},
                {"code": "LB-RELOAD", "index": "logs-network.lb-dal1", "action": "lb-config-reload", "count": 0}]

    def dispatch(self, name: str, args: dict) -> Any:
        self.calls.append((name, args))
        if name == "get_doc":
            return {"found": args["event_id"] in self.known, "event_id": args["event_id"]}
        if name == "note_finding":
            return {"ok": True}
        return {"total": 0, "rows": []}


def _result(calls: list[dict] | None = None, text: str = "") -> llm.ChatResult:
    calls = calls or []
    return llm.ChatResult(text=text, tool_calls=[{"id": f"c{i}", **c} for i, c in enumerate(calls)],
                          usage={"prompt_tokens": 10, "completion_tokens": 5}, latency_ms=1, structured=True,
                          model="nvidia/nemotron-fake")


def _fake_chat(script):
    """script(messages, tools) -> ChatResult. Wraps it with the llm.chat signature and records tool lists."""
    seen: list[list[str]] = []

    def chat(messages, tools=None, model=None, thinking=True, max_tokens=4096, seed=None, trace=None,
             role="investigator"):
        assert role == "investigator"
        seen.append([t["function"]["name"] for t in tools or []])
        res = script(messages, tools)
        if trace:
            trace({"model": model, **res.usage, "latency_ms": 1,
                   "tool_name": res.tool_calls[0]["name"] if res.tool_calls else None})
        return res
    chat.seen = seen
    return chat


GOOD_REPORT = {
    "problem": "checkout-api returns 5xx",
    "root_cause": {"summary": "config push lowered maxconn", "category": "config", "entity": "lb-1",
                   "entity_type": "load_balancer", "identifiers": {"change_id": "CHG1"},
                   "event_ids": ["real-1", "fake-9"]},
    "causal_chain": [{"timestamp": "2026-11-07T13:00:00Z", "event_id": "real-1", "what": "push", "why_it_matters": "x"},
                     {"timestamp": "2026-11-07T13:30:00Z", "event_id": "fake-9", "what": "?", "why_it_matters": "y"}],
    "rejected_candidates": [{"what": "unrelated deploy", "event_ids": ["real-2"], "why_rejected": "other service"}],
    "affected": {"service": "checkout-api"}, "confidence": 0.8, "open_questions": [],
}


# ---------------------------------------------------------------------------------------------
# main.py: webhook dedupe and SSE replay
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    main.app.state.registry = main.Registry()
    main.app.state.settings = dataclasses.replace(main.Settings.from_env(), jobs_dir=tmp_path)
    with TestClient(main.app) as c:
        yield c


def _wait_status(client: TestClient, job_id: str, status: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {status}")


def test_webhook_dedupes_on_ticket_id(client):
    async def idle(job, settings):  # never runs the real pipeline
        return None
    main.app.state.pipeline = idle

    r1 = client.post("/webhook", json=TICKET)
    r2 = client.post("/webhook", json=TICKET)
    r3 = client.post("/investigate", json={**TICKET, "ticket_id": "INC0000002"})
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.json()["status"] == "queued"
    assert r2.json()["job_id"] == r1.json()["job_id"] and r2.json()["duplicate"] is True
    assert r3.json()["job_id"] != r1.json()["job_id"]
    assert len(main.app.state.registry.jobs) == 2

    bad = client.post("/webhook", json={"title": "no ticket id"})
    assert bad.status_code == 422


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            out.append((event, data))
    return out


def test_sse_replays_recorded_entries(client):
    async def finished(job, settings):
        job.set_status("running")
        job.trace.add("tool", call_no=1, name="aggregate", arguments={"field": "event.code"}, result="{}")
        job.trace.add("finding", finding="lb queue grows")
        job.report = {"ok": True}
        job.set_status("done")
    main.app.state.pipeline = finished

    job_id = client.post("/investigate", json=TICKET).json()["job_id"]
    _wait_status(client, job_id, "done")

    with client.stream("GET", f"/jobs/{job_id}/events") as r:
        events = _parse_sse(r.read().decode())
    kinds = [(e, d.get("kind")) for e, d in events]
    assert kinds[:5] == [("trace", "status"), ("trace", "status"), ("trace", "tool"), ("trace", "finding"),
                         ("trace", "status")]
    assert [d["seq"] for e, d in events if e == "trace"] == [1, 2, 3, 4, 5]
    assert events[-1][0] == "done"


def test_sse_tails_live_entries_and_ends_failed(client):
    async def slow(job, settings):
        job.set_status("running")
        await asyncio.sleep(0.3)
        await asyncio.to_thread(job.trace.add, "tool", call_no=1, name="search_events", arguments={}, result="{}")
        await asyncio.sleep(0.1)
        job.error = "boom"
        job.set_status("failed")
    main.app.state.pipeline = slow

    job_id = client.post("/investigate", json=TICKET).json()["job_id"]
    with client.stream("GET", f"/jobs/{job_id}/events") as r:
        events = _parse_sse(r.read().decode())
    seqs = [d["seq"] for e, d in events if e == "trace"]
    assert seqs == sorted(set(seqs)) and len(seqs) == 4   # queued, running, tool, failed; no duplicates
    assert events[-1] == ("failed", events[-1][1]) and events[-1][1]["error"] == "boom"


# ---------------------------------------------------------------------------------------------
# llm.py: text fallback parser
# ---------------------------------------------------------------------------------------------
NAMES = ["search_events", "aggregate", "submit_report"]


def test_text_fallback_clean_json():
    call = llm.parse_text_tool_call('{"name": "aggregate", "arguments": {"field": "host.name", "top": 5}}', NAMES)
    assert call["name"] == "aggregate" and call["arguments"] == {"field": "host.name", "top": 5}


def test_text_fallback_json_inside_prose():
    text = ('I will first look at the codes. {"note": "not a call"} Calling now: '
            '{"name": "search_events", "arguments": "{\\"filters\\": {\\"service.name\\": \\"auth-svc\\"}}"} '
            'and then I will read the docs.')
    call = llm.parse_text_tool_call(text, NAMES)
    assert call["name"] == "search_events"
    assert call["arguments"] == {"filters": {"service.name": "auth-svc"}}


def test_text_fallback_after_think_block():
    text = ('<think>Maybe {"name": "aggregate", "arguments": {"field": "wrong"}} first? No.</think>\n'
            '{"name": "search_events", "parameters": {"query": "maxconn"}}')
    call = llm.parse_text_tool_call(text, NAMES)
    assert call["name"] == "search_events" and call["arguments"] == {"query": "maxconn"}
    assert llm.parse_text_tool_call('{"name": "rm_rf", "arguments": {}}', NAMES) is None
    assert llm.parse_text_tool_call("no json here", NAMES) is None


def test_model_guard(monkeypatch):
    monkeypatch.delenv("TRIAGENT_ALLOW_ANY_MODEL", raising=False)
    with pytest.raises(llm.ModelNotAllowed):
        llm.check_model("gpt-4o")
    llm.check_model("nvidia/nemotron-3-super-120b-a12b")


def test_key_per_role_falls_back_to_shared_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "shared")
    monkeypatch.setenv("NVIDIA_API_KEY_INVESTIGATOR", "inv")
    monkeypatch.delenv("NVIDIA_API_KEY_CRITIC", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY_SMALL", "")
    assert [llm.key_for(r) for r in llm.ROLES] == ["inv", "shared", "shared"]
    monkeypatch.delenv("NVIDIA_API_KEY")
    assert llm.key_for("critic") is None
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY_CRITIC"):
        llm.chat([{"role": "user", "content": "x"}], role="critic")


def test_tool_args_sent_as_json_strings_are_decoded():
    from tools import ESTools, coerce_args
    assert coerce_args({"filters": '{"service.name": "web-frontend"}', "entities": "lb-1"}) == \
        {"filters": {"service.name": "web-frontend"}, "entities": ["lb-1"]}
    assert coerce_args({"entities": '["a", "b"]', "exclude": ""}) == {"entities": ["a", "b"]}
    t = ESTools("http://127.0.0.1:9", "dal1", anchor="2026-11-08T00:31:00Z")
    try:
        bad = t.dispatch("search_events", {"filters": "service.name=web-frontend"})
        assert "filters must be an object" in bad["error"]
        assert "error" in t.dispatch("search_events", {"filters": ["not", "an", "object"]})
    finally:
        t.close()


# ---------------------------------------------------------------------------------------------
# investigator.py
# ---------------------------------------------------------------------------------------------
def test_investigator_stops_at_max_calls_and_returns_unknown(monkeypatch):
    chat = _fake_chat(lambda messages, tools:
                      _result([{"name": "search_events", "arguments": {"query": "error"}}])
                      if len(tools) > 1 else _result(text="I am not sure yet."))
    monkeypatch.setattr(llm, "chat", chat)
    tools, trace = FakeTools(), Trace()

    report = investigator.run(TICKET, tools, trace, max_calls=4)

    searches = [c for c in tools.calls if c[0] == "search_events"]
    assert len(searches) == 4
    assert chat.seen[-1] == ["submit_report"]             # final call restricted to submit_report
    assert report["root_cause"]["category"] == "unknown"
    assert report["confidence"] == 0.1
    assert report["open_questions"]
    assert trace.totals()["tool_calls"] == 4


def test_note_finding_with_kind_is_traced(monkeypatch):
    replies = iter([_result([{"name": "note_finding",
                              "arguments": {"finding": "GC warnings since 22:44", "kind": "precursor",
                                            "event_ids": ["real-1"]}}]),
                    _result([{"name": "submit_report", "arguments": GOOD_REPORT}])])
    monkeypatch.setattr(llm, "chat", _fake_chat(lambda messages, tools: next(replies)))
    trace = Trace()
    investigator.run(TICKET, FakeTools(known_ids={"real-1", "real-2", "fake-9"}), trace, max_calls=5)
    finding = next(e for e in trace.entries() if e["kind"] == "finding")
    assert finding["finding_kind"] == "precursor" and finding["event_ids"] == ["real-1"]
    assert trace.add("info", kind="x")["data_kind"] == "x"


def test_investigator_drops_unknown_event_ids(monkeypatch):
    chat = _fake_chat(lambda messages, tools: _result([{"name": "submit_report", "arguments": GOOD_REPORT}]))
    monkeypatch.setattr(llm, "chat", chat)
    report = investigator.run(TICKET, FakeTools(known_ids={"real-1", "real-2"}), Trace(), max_calls=5)

    assert report["root_cause"]["event_ids"] == ["real-1"]
    assert [s["event_id"] for s in report["causal_chain"]] == ["real-1"]
    assert any("fake-9" in q for q in report["open_questions"])
    assert report["confidence"] == 0.8


def test_investigator_sends_validation_error_back_once(monkeypatch):
    bad = {**GOOD_REPORT, "root_cause": {**GOOD_REPORT["root_cause"], "category": "aliens"}}
    replies = iter([_result([{"name": "submit_report", "arguments": bad}]),
                    _result([{"name": "submit_report", "arguments": GOOD_REPORT}])])
    seen_error: list[bool] = []

    def script(messages, tools):
        seen_error.append(any("failed schema validation" in str(m.get("content")) for m in messages))
        return next(replies)
    monkeypatch.setattr(llm, "chat", _fake_chat(script))
    report = investigator.run(TICKET, FakeTools(known_ids={"real-1", "real-2", "fake-9"}), Trace(), max_calls=5)
    assert seen_error == [False, True]
    assert report["root_cause"]["category"] == "config"


# ---------------------------------------------------------------------------------------------
# find_changes ranking helpers and result truncation
# ---------------------------------------------------------------------------------------------
def test_group_changes_one_row_per_ticket_and_deploy():
    from tools import group_changes

    def doc(code, ts, eid, **extra):
        return {"@timestamp": ts, "event": {"id": eid, "code": code}, "log": {"level": "info"}, **extra}
    chg = {"change": {"id": "CHG1"}}
    dep = {"service": {"name": "svc"}, "cicd": {"commit": "abc"}}
    rows = group_changes([doc("CHG-CLOSED", "T4", "c4", **chg), doc("CHG-IMPLEMENTED", "T3", "c3", **chg),
                          doc("CHG-CREATED", "T1", "c1", **chg), doc("CICD-DEPLOY-SUCCESS", "T6", "d2", **dep),
                          doc("CICD-DEPLOY-START", "T5", "d1", **dep), doc("FW-RULE-CHANGE", "T7", "f1")])
    by = {r["event.id"]: r for r in rows}
    assert set(by) == {"c3", "d2", "f1"}
    assert by["c3"]["change.lifecycle"] == {"created": "T1", "implemented": "T3", "closed": "T4"}
    assert sorted(by["c3"]["other_event_ids"]) == ["c1", "c4"]
    assert by["d2"]["deploy.started"] == "T5" and by["d2"]["other_event_ids"] == ["d1"]


def test_text_query_turns_or_words_into_operators():
    from tools import text_query
    q = text_query("example.com OR dns AND zone", "and", ["message"])["simple_query_string"]
    assert q["query"] == "example.com | dns + zone" and q["default_operator"] == "and"


def test_dump_drops_whole_rows_not_json():
    big = {"total": 50, "rows": [{"event.id": str(i), "message": "x" * 300} for i in range(50)]}
    out = json.loads(investigator._dump(big, 4000))
    assert out["rows_omitted"] > 0 and len(out["rows"]) + out["rows_omitted"] == 50
    assert out["rows"][0]["event.id"] == "0"
