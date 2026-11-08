"""jira_mock and main.patch_ticket: issue lifecycle, comment rendering, the webhook-only rule, dedupe, slots."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient

import contracts as C
import critic
import investigator
import jira_mock
import main
import remediate

DOC = {"@timestamp": "2026-11-07T14:01:00.000Z", "data_stream": {"namespace": "dal1"},
       "event": {"id": "ticket-doc-1", "code": "INC-CREATED"},
       "labels": {"dc": "dal1", "row": "B", "rack": "B03", "cluster": "k8s-prod-1"},
       "ticket": {"id": "INC7000001", "title": "Rack B03: all hosts unreachable", "priority": "P1",
                  "description": "ping + BMC down"}}
REPORT = {
    "problem": "All hosts in rack B03 unreachable",
    "root_cause": {"summary": "PDU pdu-b03-a breaker tripped", "category": "power", "entity": "pdu-b03-a",
                   "entity_type": "pdu", "identifiers": {"outlet": "all"}, "event_ids": ["ev-root"]},
    "causal_chain": [{"timestamp": "2026-11-07T13:40:00Z", "event_id": "ev-root", "what": "breaker trip",
                      "why_it_matters": "rack loses feed A"},
                     {"timestamp": "2026-11-07T13:58:00Z", "event_id": "ev-down", "what": "hosts down"}],
    "rejected_candidates": [{"what": "deploy of web", "event_ids": ["ev-red"], "why_rejected": "other row"}],
    "affected": {"rack": "B03"}, "confidence": 0.82, "open_questions": ["Was feed B already degraded?"],
}
REMEDIATION = {"covered": True, "immediate": ["Reset breaker on PDU pdu-b03-a from the PDU web console"],
               "verify": ["PDU outlet power back above 0 W"], "prevent": ["Balance load across feeds"],
               "rollback_risk": "low", "category": "power", "runbook": "runbooks/power.md"}


def _review(verdict: str) -> dict[str, Any]:
    return {"verdict": verdict, "failed": [], "corrections": [], "model": "m", "docs_checked": 1, "docs_cited": 1,
            "checks": {n: {"ok": True, "detail": "", "source": "code"} for n in C.CRITIC_CHECKS}}


# ---------------------------------------------------------------------------------------------
# a live mock on an ephemeral port, fed from a stubbed ES
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def mock_server():
    mock = jira_mock.Mock("http://127.0.0.1:9", "http://127.0.0.1:9/webhook", settle=0)
    docs: list[dict[str, Any]] = [{"_index": "idx", "_id": "a1", "_source": DOC}]
    mock.ticket_docs = lambda: docs
    srv = ThreadingHTTPServer(("127.0.0.1", 0), jira_mock.make_handler(mock))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mock.docs = docs
    mock.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield mock
    srv.shutdown()
    srv.server_close()


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_payload_from_doc_is_a_valid_ticket():
    p = jira_mock.payload_from_doc(DOC)
    t = C.Ticket.model_validate(p)
    assert (t.ticket_id, t.namespace, t.source_event_id, t.created) == ("INC7000001", "dal1", "ticket-doc-1",
                                                                       DOC["@timestamp"])
    assert t.labels == {"row": "B", "rack": "B03", "cluster": "k8s-prod-1"}   # dc dropped


def test_mock_opens_once_and_reopens_on_a_new_doc(mock_server):
    m = mock_server
    assert m.scan() == ["INC7000001"] and m.scan() == []
    status, _ = m.add_comment("INC7000001", {"body": "first", "job_id": "job-1", "status": "done"})
    assert status == 201 and len(m.issues["INC7000001"]["comments"]) == 1
    # same ticket id, new ES doc (re-ingest with the same seed): a fresh issue, no comments
    m.docs.append({"_index": "idx", "_id": "a2", "_source": {**DOC, "@timestamp": "2026-11-08T09:00:00.000Z"}})
    assert m.scan() == ["INC7000001"]
    issue = m.issues["INC7000001"]
    assert issue["reopened"] and issue["comments"] == [] and issue["webhook"]["state"] == "pending"


def test_mock_retries_webhook_until_server_answers(mock_server):
    m = mock_server
    m.scan()
    m.fire_pending()    # nothing listens on 127.0.0.1:9
    wh = m.issues["INC7000001"]["webhook"]
    assert wh["state"] == "pending" and wh["attempts"] == 1 and "error" in wh


def test_mock_comment_http_contract(mock_server):
    m = mock_server
    m.scan()
    body = jira_mock.comment_payload("**Triagent**\nhello", "job-1", "done")
    req = urllib.request.Request(f"{m.url}/issue/INC7000001/comment", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 201
        assert set(json.loads(r.read())) == set(jira_mock.COMMENT_RESPONSE_FIELDS)
    issue = _get(f"{m.url}/issue/INC7000001")
    assert issue["comments"][0]["body"].startswith("**Triagent**") and issue["comments"][0]["job_id"] == "job-1"
    assert _get(f"{m.url}/issues")[0]["comments"] == 1
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(f"{m.url}/issue/INC404/comment", data=b'{"body":"x"}',
                                                      method="POST"), timeout=5)
    assert e.value.code == 404


def test_mock_serves_the_board_and_its_row_fields(mock_server):
    m = mock_server
    m.scan()
    m.add_comment("INC7000001", jira_mock.comment_payload("report", "job-1", "done"))
    with urllib.request.urlopen(f"{m.url}/", timeout=5) as r:
        page = r.read().decode()
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/html")
    assert "DC-OPS triage board" in page and 'fetch("/issues")' in page and "http" not in page.split("<script>")[0]
    row = _get(f"{m.url}/issues")[0]
    assert row["comment_status"] == "done" and row["service"] == DOC.get("service", {}).get("name")
    assert row["labels"] == {k: v for k, v in DOC["labels"].items() if k in ("row", "rack", "cluster")}


# ---------------------------------------------------------------------------------------------
# main.py: render + patch
# ---------------------------------------------------------------------------------------------
def _settings(tmp_path, **kw) -> main.Settings:
    return dataclasses.replace(main.Settings.from_env(), jobs_dir=tmp_path, **kw)


def _job(origin: str = "webhook") -> main.Job:
    loop = asyncio.new_event_loop()
    job = main.Job(job_id="job-t1", ticket=jira_mock.payload_from_doc(DOC), loop=loop, origin=origin)
    job.report, job.remediation = REPORT, REMEDIATION
    job.critic = {"first": _review("pass"), "final": _review("pass"), "retried": False}
    job.finished = job.created + 42
    return job


def test_comment_carries_cause_evidence_and_remediation(tmp_path):
    c = main.render_comment(_job(), _settings(tmp_path, public_url="http://demo:8080"), "done")
    assert set(c) == {"body", "job_id", "status", "author"} and c["status"] == "done"
    body = c["body"]
    for needle in ("power, pdu `pdu-b03-a`", "`ev-root`", "`ev-down`", "`ev-red`", "Reset breaker on PDU pdu-b03-a",
                   "runbooks/power.md", "confidence 0.82", "reviewer pass", "Was feed B already degraded?",
                   "http://demo:8080/jobs/job-t1", "42s"):
        assert needle in body, needle
    assert "$" not in body


def test_failed_comment(tmp_path):
    job = _job()
    job.report, job.error = None, "RuntimeError: model unavailable"
    c = main.render_comment(job, _settings(tmp_path), "failed")
    assert c["status"] == "failed" and "model unavailable" in c["body"] and "$" not in c["body"]


def test_patch_failure_is_traced_not_raised(tmp_path):
    job = _job()
    main.patch_ticket(job, _settings(tmp_path, jira_mock_url="http://127.0.0.1:9"), "done")
    assert any(e["kind"] == "error" and "comment not posted" in e["message"] for e in job.trace.entries())
    job2 = _job()
    main.patch_ticket(job2, _settings(tmp_path, ticket_backend="jira"), "done")
    assert "not implemented" in job2.trace.entries()[-1]["message"]


def _run(monkeypatch, settings, origin):
    monkeypatch.setattr(investigator, "run", lambda *a, **k: REPORT)
    monkeypatch.setattr(critic, "run", lambda *a: _review("pass"))
    monkeypatch.setattr(remediate, "run", lambda *a: REMEDIATION)

    async def go():
        job = main.Job(job_id=f"job-{origin}", ticket=jira_mock.payload_from_doc(DOC),
                       loop=asyncio.get_running_loop(), origin=origin)
        await main.run_pipeline(job, settings)
        return job
    return asyncio.run(go())


def test_webhook_job_ends_with_a_comment_on_the_mock_issue(monkeypatch, tmp_path, mock_server):
    mock_server.scan()
    job = _run(monkeypatch, _settings(tmp_path, jira_mock_url=mock_server.url), "webhook")
    assert job.status == "done"
    comments = mock_server.issues["INC7000001"]["comments"]
    assert len(comments) == 1 and comments[0]["job_id"] == "job-webhook" and "pdu-b03-a" in comments[0]["body"]
    entries = job.trace.entries()
    posted = next(i for i, e in enumerate(entries) if "posted to jira_mock" in (e.get("message") or ""))
    done = next(i for i, e in enumerate(entries) if e.get("status") == "done")
    assert posted < done                       # patch traced before the terminal entry, so SSE viewers see it
    assert "push skipped" in entries[-1]["message"]   # push runs after done (no devices registered here)
    C.Job.model_validate(job.view())


def test_investigate_job_never_comments(monkeypatch, tmp_path, mock_server):
    mock_server.scan()
    _run(monkeypatch, _settings(tmp_path, jira_mock_url=mock_server.url), "investigate")
    assert mock_server.issues["INC7000001"]["comments"] == []


def test_every_job_ends_with_a_cost_and_latency_line(monkeypatch, tmp_path, client):
    job = _run(monkeypatch, _settings(tmp_path, ticket_backend="none"), "investigate")
    main.app.state.registry.jobs[job.job_id] = job
    view = client.get(f"/jobs/{job.job_id}").json()
    line = next(e["message"] for e in view["trace"] if (e.get("message") or "").startswith("cost and latency: "))
    assert "tokens" in line and "tool calls" in line and "end to end" in line and "first report at" in line


# ---------------------------------------------------------------------------------------------
# main.py: dedupe on ticket_id + created, parallel slots
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    main.app.state.registry = main.Registry()
    main.app.state.settings = _settings(tmp_path, max_parallel_jobs=1, ticket_backend="none")
    with TestClient(main.app) as c:
        yield c


def test_reraised_ticket_gets_a_new_job(client):
    gate = threading.Event()

    async def wait(job, settings):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        job.set_status("done")
    main.app.state.pipeline = wait
    t = jira_mock.payload_from_doc(DOC)
    a = client.post("/webhook", json=t).json()
    b = client.post("/webhook", json=t).json()
    c = client.post("/webhook", json={**t, "created": "2026-11-08T09:00:00Z"}).json()
    assert b["duplicate"] and b["job_id"] == a["job_id"]
    assert c["job_id"] != a["job_id"] and not c.get("duplicate")
    # max_parallel_jobs=1: the second job waits in queued while the first holds the slot
    time.sleep(0.1)
    jobs = main.app.state.registry.jobs
    assert jobs[c["job_id"]].status == "queued"
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and jobs[c["job_id"]].status != "done":
        time.sleep(0.02)
    assert jobs[a["job_id"]].status == jobs[c["job_id"]].status == "done"
    assert jobs[a["job_id"]].origin == "webhook"
