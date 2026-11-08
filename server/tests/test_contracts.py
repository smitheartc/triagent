"""The shapes in contracts.py are what the server actually produces. If one of these fails, a section boundary moved."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

import contracts as C
import critic
import fakes
import investigator
import main
from tracelog import Trace

TICKET = {"ticket_id": "INC1", "title": "auth-svc 5xx", "created": "2026-11-07T11:05:00Z", "service": "auth-svc",
          "labels": {}, "namespace": "dal1"}


def _report(eids=("e1",)):
    return {"problem": "p", "root_cause": {"summary": "s", "category": "config", "entity": "lb-1",
                                           "entity_type": "load_balancer", "identifiers": {}, "event_ids": list(eids)},
            "causal_chain": [{"timestamp": "2026-11-07T10:00:00Z", "event_id": "e1", "what": "w",
                              "why_it_matters": "y"}],
            "rejected_candidates": [{"what": "x", "event_ids": [], "why_rejected": "r"}], "affected": {},
            "confidence": 0.9, "open_questions": []}


def _review(verdict):
    return {"verdict": verdict, "failed": [] if verdict == "pass" else ["confidence_consistent"],
            "checks": {n: {"ok": verdict == "pass" or n != "confidence_consistent", "detail": "", "source": "model"}
                       for n in C.CRITIC_CHECKS}, "corrections": [], "model": "m", "docs_checked": 1, "docs_cited": 1}


def test_real_pipeline_job_matches_contract(monkeypatch, tmp_path):
    def fake_investigate(ticket, es, trace, max_calls, seed=None, feedback=None):
        trace.add("tool", call_no=1, name="search_events", arguments={"lookback": "1h"}, result="{}")
        trace.add("llm", model="m", prompt_tokens=10, completion_tokens=2, latency_ms=5, tool_name="search_events")
        return _report()
    reviews = iter([_review("fail"), _review("pass")])
    monkeypatch.setattr(investigator, "run", fake_investigate)
    monkeypatch.setattr(critic, "run", lambda *a: next(reviews))
    settings = dataclasses.replace(main.Settings.from_env(), jobs_dir=tmp_path)

    async def go():
        job = main.Job(job_id="job-x", ticket=dict(C.Ticket(**TICKET).model_dump()), loop=asyncio.get_running_loop())
        job.set_status("queued")
        await main.run_pipeline(job, settings)
        return job
    job = asyncio.run(go())
    view = job.view()
    assert set(view) == set(C.Job.model_fields), set(view) ^ set(C.Job.model_fields)
    parsed = C.Job.model_validate(view)
    assert parsed.status == "done" and parsed.critic.retried and parsed.report_initial is not None
    on_disk = json.loads((tmp_path / "job-x.json").read_text())
    C.Job.model_validate(on_disk)
    C.JobSummary.model_validate(C.summarize(view))


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGENT_FAKE_SPEED", "1000")
    import fake_server
    settings = dataclasses.replace(main.Settings.from_env(), jobs_dir=tmp_path / "jobs", scores_dir=tmp_path / "scores")
    with TestClient(fake_server.build_app(settings)) as c:
        yield c


def _sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            out.append((ev, data))
    return out


def test_fake_server_serves_contract_shapes(fake_client):
    c = fake_client
    health = C.Health.model_validate(c.get("/health").json())
    assert health.ok and health.fake

    rows = [C.JobSummary.model_validate(r) for r in c.get("/jobs").json()]
    assert len(rows) == 4 and rows[0].created >= rows[-1].created
    statuses = {r.job_id: r.status for r in rows}
    assert "failed" in statuses.values() and list(statuses.values()).count("done") >= 2

    for r in rows:
        job = C.Job.model_validate(c.get(f"/jobs/{r.job_id}").json())
        if job.status == "done":
            assert job.report and job.critic and job.remediation and job.report_initial
    retried = [C.Job.model_validate(c.get(f"/jobs/{r.job_id}").json()) for r in rows]
    assert any(j.critic and j.critic.retried and j.critic.kept == "retry" for j in retried)

    with c.stream("GET", "/jobs/job-fakelive0001/events") as resp:
        events = _sse(resp.read().decode())
    assert events[-1][0] in C.TERMINAL_STATUSES
    C.SSETerminal.model_validate(events[-1][1])
    entries = [C.TraceEntry.model_validate(d) for e, d in events if e == "trace"]
    assert "template=deploy" in next(d["message"] for e, d in events if d.get("kind") == "info")
    assert [e.seq for e in entries] == list(range(1, len(entries) + 1))
    assert {"status", "llm", "tool", "finding", "critic", "remediation"} <= {e.kind for e in entries}


def test_fake_server_investigate_actions_scores_devices(fake_client):
    c = fake_client
    enq = C.EnqueueResponse.model_validate(c.post("/investigate", json={
        "ticket_id": "INC77", "title": "web-frontend: HTTP 5xx error rate above 25%",
        "created": "2026-11-08T10:00:00Z"}).json())
    assert c.post("/investigate", json={"ticket_id": "INC77", "title": "x", "created": "2026-11-08T10:00:00Z"}
                  ).json()["duplicate"] is True
    with c.stream("GET", f"/jobs/{enq.job_id}/events") as resp:
        assert _sse(resp.read().decode())[-1][0] == "done"
    job = C.Job.model_validate(c.get(f"/jobs/{enq.job_id}").json())
    assert job.report.root_cause.category == "config"

    act = C.Action.model_validate(c.post(f"/jobs/{enq.job_id}/actions", json={"type": "approve", "note": "ok"}).json())
    assert act.state == "recorded"
    ask = C.Action.model_validate(c.post(f"/jobs/{enq.job_id}/actions",
                                         json={"type": "ask", "question": "why lb-1?"}).json())
    assert ask.state == "queued" and ask.id
    ask2 = C.Action.model_validate(c.post(f"/jobs/{enq.job_id}/ask", json={"question": "Why was it rejected?"}).json())
    assert c.post(f"/jobs/{enq.job_id}/actions", json={"type": "ask"}).status_code == 422
    assert c.post(f"/jobs/{enq.job_id}/ask", json={"question": ""}).status_code == 422
    deadline = time.time() + 5
    while time.time() < deadline:
        actions = {a.id: a for a in C.Job.model_validate(c.get(f"/jobs/{enq.job_id}").json()).actions if a.id}
        if all(actions[i].state == "answered" for i in (ask.id, ask2.id)):
            break
        time.sleep(0.02)
    assert actions[ask2.id].answer and actions[ask2.id].event_ids      # a rejected candidate, with its ids
    assert len(C.Job.model_validate(c.get(f"/jobs/{enq.job_id}").json()).actions) == 3

    C.ScoreRun.model_validate(c.get("/scores/latest").json())
    assert c.post("/devices", json={"expo_push_token": "ExponentPushToken[x]", "platform": "ios"}).json() == {"ok": True}
    assert c.get("/jobs/nope").status_code == 404


def test_backend_fakes_drive_the_real_investigator(monkeypatch):
    tools = fakes.FakeTools.from_dcsim()
    ticket = next(d for d in tools.docs if d["event"]["code"] == "INC-CREATED")
    tools.anchor = fakes.real_tools.parse_ts(ticket["@timestamp"])
    agg = tools.dispatch("aggregate", {"field": "event.code", "lookback": "6h"})
    changes = tools.dispatch("find_changes", {"lookback": "6h"})
    assert agg["buckets"] and changes["rows"] and "counts" in changes
    deploy = next(r for r in changes["rows"] if r["event.code"] == "CICD-DEPLOY-SUCCESS")
    report = {**_report([deploy["event.id"]]),
              "causal_chain": [{"timestamp": deploy["ts"], "event_id": deploy["event.id"], "what": "deploy",
                                "why_it_matters": "cause"}]}
    record = []
    monkeypatch.setattr(fakes.llm, "chat", fakes.fake_chat([
        [{"name": "aggregate", "arguments": {"field": "event.code"}},
         {"name": "find_changes", "arguments": {"lookback": "6h"}}],
        {"name": "submit_report", "arguments": report}], record))
    out = C.Report.model_validate(investigator.run({"ticket_id": "T", "title": "t", "created": ticket["@timestamp"]},
                                                   tools, Trace(), max_calls=5))
    assert out.root_cause.event_ids == [deploy["event.id"]] and record[0]["tools"][0] == "search_events"
    for name in ("search_events", "metric_series", "expand_scope", "get_doc"):
        res = tools.dispatch(name, {"lookback": "6h", "dataset": "system.host", "field": "datacenter.host.mem_used_pct",
                                    "entity": deploy.get("service.name", ""), "event_id": deploy["event.id"]})
        assert "error" not in res, (name, res)


def test_json_schema_export():
    schema = C.json_schema()
    names = set(schema.get("$defs", {}))
    assert {"Job", "JobSummary", "TraceEntry", "Report", "Remediation", "CriticResult"} <= names
