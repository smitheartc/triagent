"""store.py (Supabase over PostgREST) and notify.py (Expo push), with fake transports: no network."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import httpx
import pytest

import contracts as C
import critic
import investigator
import jira_mock
import llm
import main
import notify
import remediate
import store
from test_ticket_patch import DOC, REMEDIATION, REPORT, _review


class FakeSupabase:
    """Records PostgREST requests; GET /jobs and /devices answer from what was upserted."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rows: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        table = req.url.path.rsplit("/", 1)[-1]
        if req.method == "POST":
            row = json.loads(req.content)
            (self.rows if table == "jobs" else self.devices)[row["job_id" if table == "jobs" else
                                                                 "expo_push_token"]] = row
            return httpx.Response(201)
        if table == "jobs":
            rows = sorted(self.rows.values(), key=lambda r: r["created"], reverse=True)
            return httpx.Response(200, json=[{"job": r["job"]} for r in rows])
        return httpx.Response(200, json=list(self.devices.values()))


@pytest.fixture
def supa(monkeypatch):
    fake = FakeSupabase()
    st = store.Store("https://proj.supabase.co", "sb_secret_test")
    st._client = httpx.Client(base_url="https://proj.supabase.co/rest/v1", transport=httpx.MockTransport(fake))
    monkeypatch.setattr(store, "_default", st)
    fake.store = st
    return fake


def _pipeline_job(monkeypatch, origin: str, saver=None, devices=None) -> main.Job:
    monkeypatch.setattr(investigator, "run", lambda *a, **k: REPORT)
    monkeypatch.setattr(critic, "run", lambda *a: _review("pass"))
    monkeypatch.setattr(remediate, "run", lambda *a: REMEDIATION)
    settings = dataclasses.replace(main.Settings.from_env(), ticket_backend="none")

    async def go():
        job = main.Job(job_id=f"job-{origin}", ticket=jira_mock.payload_from_doc(DOC),
                       loop=asyncio.get_running_loop(), origin=origin, saver=saver, devices=devices)
        await main.run_pipeline(job, settings)
        return job
    return asyncio.run(go())


# ---------------------------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------------------------
def test_save_upserts_the_contract_row(supa):
    job = {"job_id": "job-1", "ticket_id": "INC1", "status": "done", "created": 1.0, "ticket": {"title": "t"},
           "report": REPORT, "totals": {}}
    supa.store.save(job)
    req = supa.requests[-1]
    assert req.method == "POST" and req.url.params["on_conflict"] == "job_id"
    assert req.headers["apikey"] == "sb_secret_test" and "authorization" not in req.headers   # new-style key
    assert "merge-duplicates" in req.headers["prefer"]
    row = json.loads(req.content)
    assert row == C.supabase_row(job) and row["entity"] == "pdu-b03-a" and row["job"]["report"] == REPORT


def test_legacy_jwt_key_also_goes_in_authorization():
    assert store.Store("u", "eyJhbGciOi.x.y")._headers()["Authorization"] == "Bearer eyJhbGciOi.x.y"


def test_store_off_without_keys_makes_no_calls():
    st = store.Store(None, None)
    st.save({"job_id": "x"})
    assert not st.enabled and st.load_jobs() == [] and st.devices() == []


def test_failed_write_raises_and_is_reported(monkeypatch):
    st = store.Store("https://p.supabase.co", "k")
    st._client = httpx.Client(base_url="https://p.supabase.co/rest/v1",
                              transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(RuntimeError):
        st.save({"job_id": "job-1", "status": "done", "created": 1.0, "ticket": {}})
    assert "401" in st.status()["last_error"]


def test_every_status_change_writes_a_valid_row(monkeypatch, supa):
    writer = main.StoreWriter(supa.store)
    job = _pipeline_job(monkeypatch, "investigate", saver=writer)
    writer.pool.shutdown(wait=True)
    posts = [json.loads(r.content) for r in supa.requests if r.method == "POST"]
    statuses = [p["status"] for p in posts]
    assert statuses[0] == "running" and statuses[-2:] == ["done", "done"] and "remediating" in statuses
    final = supa.rows[job.job_id]
    C.JobSummary.model_validate({k: v for k, v in final.items() if k != "job"})
    C.Job.model_validate(final["job"])
    assert final["job"]["trace"] == job.view()["trace"]     # the last write carries the whole trace


def test_restart_loads_jobs_back_and_fails_unfinished_ones(supa):
    loop = asyncio.new_event_loop()
    done = main.Job(job_id="job-a", ticket=jira_mock.payload_from_doc(DOC), loop=loop)
    done.set_status("running")
    done.report = REPORT
    done.set_status("done")
    running = main.Job(job_id="job-b", ticket=jira_mock.payload_from_doc(DOC), loop=loop)
    running.created = done.created + 1
    running.set_status("running")
    for j in (done, running):
        supa.store.save(j.view())

    views = supa.store.load_jobs()
    assert [v["job_id"] for v in views] == ["job-b", "job-a"]
    a, b = (main.job_from_view(v, loop) for v in reversed(views))
    assert a.status == "done" and a.report == REPORT and len(a.trace) == len(done.trace)
    assert b.status == "failed" and "restarted" in b.error
    C.Job.model_validate(b.view())
    assert [e["seq"] for e in b.trace.entries()] == list(range(1, len(b.trace) + 1))


# ---------------------------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------------------------
def _expo(monkeypatch, tickets=None):
    sent: list[list[dict]] = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, json={"data": tickets or [{"status": "ok", "id": "t"} for _ in json]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(notify.httpx, "post", post)
    return sent


def _summary(monkeypatch, text="PDU pdu-b03-a breaker tripped; rack B03 hosts down. Reset the breaker."):
    calls = []

    def chat(messages, role="investigator", thinking=True, trace=None, **kw):
        calls.append({"role": role, "thinking": thinking, "prompt": messages[0]["content"]})
        if trace:
            trace({"model": "nvidia/nemotron-small", "prompt_tokens": 50, "completion_tokens": 20, "latency_ms": 3,
                   "tool_name": None})
        return llm.ChatResult(text=text, tool_calls=[], usage={}, latency_ms=3)
    monkeypatch.setattr(notify.llm, "chat", chat)
    return calls


def test_webhook_job_pushes_to_every_device(monkeypatch, supa):
    sent, calls = _expo(monkeypatch), _summary(monkeypatch)
    supa.devices["ExponentPushToken[db]"] = {"expo_push_token": "ExponentPushToken[db]"}
    job = _pipeline_job(monkeypatch, "webhook", devices={"ExponentPushToken[mem]": {}})
    msgs = sent[0]
    assert {m["to"] for m in msgs} == {"ExponentPushToken[mem]", "ExponentPushToken[db]"}
    for m in msgs:
        C.PushMessage.model_validate(m)
        assert m["title"] == "P1 INC7000001: cause found (power)"
        assert m["data"] == {"job_id": "job-webhook", "status": "done"} and m["body"].startswith("PDU pdu-b03-a")
    assert calls[0]["role"] == "small" and calls[0]["thinking"] is False
    assert "push sent to 2 of 2 devices" in job.trace.entries()[-1]["message"]


def test_investigate_job_does_not_push(monkeypatch):
    sent = _expo(monkeypatch)
    _pipeline_job(monkeypatch, "investigate", devices={"ExponentPushToken[mem]": {}})
    assert sent == []


def test_dead_token_is_traced_and_summary_falls_back(monkeypatch):
    _expo(monkeypatch, tickets=[{"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}])

    def broken(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(notify.llm, "chat", broken)
    loop = asyncio.new_event_loop()
    job = main.Job(job_id="job-n", ticket=jira_mock.payload_from_doc(DOC), loop=loop, origin="webhook")
    job.status, job.report = "done", REPORT
    msgs = notify.push(job, ["ExponentPushToken[old]"])
    assert msgs[0]["body"] == "pdu-b03-a: PDU pdu-b03-a breaker tripped"           # the fixed sentence
    errors = [e["message"] for e in job.trace.entries() if e["kind"] == "error"]
    assert any("DeviceNotRegistered" in m for m in errors)


def test_failed_job_push_says_so():
    loop = asyncio.new_event_loop()
    job = main.Job(job_id="job-f", ticket=jira_mock.payload_from_doc(DOC), loop=loop)
    job.status, job.error = "failed", "RuntimeError: model unavailable"
    assert notify.title_for(job) == "P1 INC7000001: investigation failed"
    assert "model unavailable" in notify.summarize(job) and len(notify.summarize(job)) <= notify.BODY_MAX


def test_devices_route_saves_token_to_supabase(monkeypatch, supa, tmp_path):
    from fastapi.testclient import TestClient
    main.app.state.registry = main.Registry()
    main.app.state.settings = dataclasses.replace(main.Settings.from_env(), jobs_dir=tmp_path)
    before = getattr(main.app.state, "store_writer", None)
    main.app.state.store_writer = main.StoreWriter(supa.store)
    try:
        with TestClient(main.app) as c:
            r = c.post("/devices", json={"expo_push_token": "ExponentPushToken[x]", "platform": "ios"})
    finally:
        main.app.state.store_writer = before
    assert r.json() == {"ok": True} and supa.devices["ExponentPushToken[x]"]["platform"] == "ios"
