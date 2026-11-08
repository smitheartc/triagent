"""Triagent orchestrator: webhook in, investigation job, SSE trace out.

Run
    cd server
    cp .env.example .env                       # fill the NVIDIA_API_KEY_* lines and ES_URL
    uvicorn main:app --reload --port 8080

Manual investigation (same body as the webhook)
    curl -s -XPOST localhost:8080/investigate -H 'content-type: application/json' -d @../t/ticket.json
    # -> {"job_id": "job-...", "ticket_id": "INC...", "status": "queued"}

Watch it live (replays what is already recorded, then tails; ends with event "done" or "failed")
    curl -N localhost:8080/jobs/<job_id>/events

Final state, report and trace
    curl -s localhost:8080/jobs/<job_id> | python3 -m json.tool

Endpoints
    POST /webhook             Jira / jira_mock payload; dedupes on ticket_id
    POST /investigate         same body and behavior, for curl; optional ?seed=N pins the model seed (scorer)
    GET  /jobs                newest first, JobSummary rows (Inbox)
    GET  /jobs/{job_id}       status (queued|running|critic|remediating|done|failed), report, trace
    GET  /jobs/{job_id}/events  Server-Sent Events stream of trace entries
    POST /jobs/{job_id}/actions  approve | escalate | ask (recorded on the job)
    GET  /scores/latest       newest scorer run from t/scores/ (Scoreboard)
    POST /devices             register an Expo push token
    GET  /health              ES reachability, API key presence per role, configured model ids

All shapes are defined in contracts.py. fake_server.py serves this same app with scripted jobs.

Jobs live in memory and are written to t/jobs/<job_id>.json when they finish. Restarting the server forgets them.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import investigator
import llm
from contracts import Action, ActionRequest, DeviceRegistration, WebhookPayload, summarize
from tools import ESTools
from tracelog import Trace

load_dotenv(Path(__file__).with_name(".env"))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("triagent.main")

REPO_ROOT = Path(__file__).resolve().parent.parent
TERMINAL = ("done", "failed")
SSE_KEEPALIVE_S = 15.0


# ---------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    es_url: str
    es_api_key: str | None
    nvidia_base_url: str
    nvidia_keys_set: dict[str, bool]
    model_investigator: str
    model_critic: str
    model_small: str
    max_tool_calls: int
    jobs_dir: Path
    critic_enabled: bool = True
    scores_dir: Path = REPO_ROOT / "t" / "scores"
    fake: bool = False                    # set by fake_server.py

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            es_url=os.environ.get("ES_URL", "http://localhost:9200"),
            es_api_key=os.environ.get("ES_API_KEY") or None,
            nvidia_base_url=os.environ.get("NVIDIA_BASE_URL") or llm.DEFAULT_BASE_URL,
            nvidia_keys_set={role: bool(llm.key_for(role)) for role in llm.ROLES},
            model_investigator=llm.model_for("investigator"),
            model_critic=llm.model_for("critic"),
            model_small=llm.model_for("small"),
            max_tool_calls=int(os.environ.get("TRIAGENT_MAX_TOOL_CALLS", "20")),
            jobs_dir=Path(os.environ.get("TRIAGENT_JOBS_DIR") or REPO_ROOT / "t" / "jobs"),
            critic_enabled=os.environ.get("TRIAGENT_CRITIC", "1") != "0",
        )


# ---------------------------------------------------------------------------------------------
# job registry (the only mutable state in the server)
# ---------------------------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    ticket: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    status: str = "queued"
    report: dict[str, Any] | None = None
    critic: dict[str, Any] | None = None
    remediation: dict[str, Any] | None = None
    error: str | None = None
    seed: int | None = None
    report_initial: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    totals_initial: dict[str, int] | None = None
    initial_seconds: float | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    trace: Trace = field(init=False)

    def __post_init__(self) -> None:
        # Trace.add may run in the worker thread; fan-out to SSE queues always happens on the event loop.
        self.trace = Trace(on_add=self._on_add)

    def _on_add(self, entry: dict[str, Any]) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._fanout(entry)
        else:
            self.loop.call_soon_threadsafe(self._fanout, entry)

    def _fanout(self, entry: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            q.put_nowait(entry)

    def set_status(self, status: str, **extra: Any) -> None:
        self.status = status
        self.trace.add("status", status=status, **extra)

    def subscribe(self) -> tuple[list[dict[str, Any]], asyncio.Queue]:
        """Snapshot + live queue, taken atomically on the event loop. Duplicates are filtered by seq."""
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return self.trace.entries(), q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def view(self, with_trace: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": self.job_id, "ticket_id": self.ticket.get("ticket_id"),
                               "status": self.status, "ticket": self.ticket, "report": self.report,
                               "critic": self.critic, "remediation": self.remediation, "error": self.error,
                               "created": self.created, "finished": self.finished, "seed": self.seed,
                               "report_initial": self.report_initial, "totals_initial": self.totals_initial,
                               "initial_seconds": self.initial_seconds, "actions": self.actions,
                               "totals": self.trace.totals()}
        if with_trace:
            out["trace"] = self.trace.entries()
        return out


class Registry:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.by_ticket: dict[str, str] = {}
        self.tasks: set[asyncio.Task] = set()
        self.devices: dict[str, dict[str, Any]] = {}   # expo push token -> registration (notify.py reads it)


# ---------------------------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------------------------
def _optional_module(name: str) -> Any | None:
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise
    return mod if callable(getattr(mod, "run", None)) else None


def choose_report(first: dict[str, Any], final: dict[str, Any]) -> str:
    """Keep the retry only if its review passes or fails fewer checks than the first; else keep the first report."""
    if final.get("verdict") == "pass" or len(final.get("failed") or []) < len(first.get("failed") or []):
        return "retry"
    return "initial"


def with_review_notes(report: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """The report with the reviewer's failed checks appended to open_questions, for the on-call engineer."""
    notes = [f"Reviewer: {name} failed: {review['checks'][name]['detail']}"[:400]
             for name in review.get("failed") or [] if name in (review.get("checks") or {})]
    return {**report, "open_questions": list(report.get("open_questions") or []) + notes}


def store_report(job: Job) -> None:
    """Hook: persist the report to Supabase. Later build step; no-op for now."""


def patch_ticket(job: Job) -> None:
    """Hook: post the report as a comment on the ticket (jira_mock / Jira / SNOW). Later build step; no-op for now."""


def persist(job: Job, jobs_dir: Path) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job.job_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job.view(), indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


async def run_pipeline(job: Job, settings: Settings) -> None:
    """investigator -> critic (one retry on fail) -> remediate (if present) -> persist -> hooks."""
    ticket = job.ticket
    es = ESTools(settings.es_url, namespace=ticket.get("namespace") or "dal1", anchor=ticket["created"],
                 api_key=settings.es_api_key)
    try:
        job.set_status("running")
        job.trace.phase = "investigate"
        job.report = await asyncio.to_thread(investigator.run, ticket, es, job.trace, settings.max_tool_calls,
                                             seed=job.seed)
        # The first report and its cost are kept so the scorer can compare before / after the critic.
        job.report_initial = job.report
        job.initial_seconds = time.time() - job.created
        job.totals_initial = job.trace.totals({"investigate"})

        critic = _optional_module("critic") if settings.critic_enabled else None
        if critic is None:
            job.trace.phase = None
            job.trace.add("info", message="critic skipped: " + ("TRIAGENT_CRITIC=0" if not settings.critic_enabled
                                                                else "server/critic.py not present"))
        else:
            # critic.run(report, ticket, es_tools, trace) -> {"verdict": "pass"|"fail", "checks", "corrections"}
            job.trace.phase = "critic"
            job.set_status("critic")
            first = await asyncio.to_thread(critic.run, job.report, ticket, es, job.trace)
            job.critic = {"first": first, "final": first, "retried": False}
            if first["verdict"] == "fail":
                # One investigator retry with the corrections appended, then one more review. No second retry.
                job.trace.phase = "retry"
                job.set_status("running", retry=True)
                job.report = await asyncio.to_thread(
                    investigator.run, ticket, es, job.trace, settings.max_tool_calls, seed=job.seed,
                    feedback=critic.feedback(first, job.report_initial))
                job.trace.phase = "critic_retry"
                job.set_status("critic", retry=True)
                final = await asyncio.to_thread(critic.run, job.report, ticket, es, job.trace)
                kept = choose_report(first, final)
                if kept == "initial":
                    job.report = with_review_notes(job.report_initial, first)
                elif final["verdict"] == "fail":
                    job.report = with_review_notes(job.report, final)
                job.critic = {"first": first, "final": final, "retried": True, "kept": kept}
                job.trace.phase = None
                job.trace.add("info", message=f"critic: kept the {kept} report (first review failed "
                                              f"{len(first['failed'])} checks, retry review failed {len(final['failed'])})")
            job.trace.phase = None

        remediate = _optional_module("remediate")
        if remediate is None:
            job.trace.add("info", message="remediation skipped: server/remediate.py not present yet")
        else:
            # remediate.run(report, trace, ticket=None) -> contracts.Remediation dict
            job.set_status("remediating")
            job.remediation = await asyncio.to_thread(remediate.run, job.report, job.trace, ticket)

        store_report(job)
        patch_ticket(job)
        job.finished = time.time()
        job.set_status("done", totals=job.trace.totals())
    except Exception as exc:  # noqa: BLE001 - a job must always end in a terminal state
        log.exception("job %s failed", job.job_id)
        job.error = f"{type(exc).__name__}: {exc}"
        job.trace.add("error", message=job.error)
        job.finished = time.time()
        job.set_status("failed")
    finally:
        es.close()
        try:
            path = await asyncio.to_thread(persist, job, settings.jobs_dir)
            log.info("job %s %s -> %s", job.job_id, job.status, path)
        except OSError:
            log.exception("could not persist job %s", job.job_id)


# ---------------------------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------------------------
router = APIRouter()


def _registry(request: Request) -> Registry:
    return request.app.state.registry


async def _enqueue(request: Request, payload: WebhookPayload, seed: int | None = None) -> JSONResponse:
    reg = _registry(request)
    existing = reg.by_ticket.get(payload.ticket_id)
    if existing is not None:
        job = reg.jobs[existing]
        return JSONResponse({"job_id": job.job_id, "ticket_id": payload.ticket_id, "status": job.status,
                             "duplicate": True})
    job = Job(job_id=f"job-{uuid.uuid4().hex[:12]}", ticket=payload.model_dump(), loop=asyncio.get_running_loop(),
              seed=seed)
    reg.jobs[job.job_id] = job
    reg.by_ticket[payload.ticket_id] = job.job_id
    job.set_status("queued")
    task = asyncio.create_task(request.app.state.pipeline(job, request.app.state.settings))
    reg.tasks.add(task)
    task.add_done_callback(reg.tasks.discard)
    log.info("ticket %s -> %s queued", payload.ticket_id, job.job_id)
    return JSONResponse({"job_id": job.job_id, "ticket_id": payload.ticket_id, "status": "queued"})


@router.post("/webhook")
async def webhook(payload: WebhookPayload, request: Request) -> JSONResponse:
    return await _enqueue(request, payload)


@router.post("/investigate")
async def investigate(payload: WebhookPayload, request: Request, seed: int | None = None) -> JSONResponse:
    """Same body and dedupe as /webhook. Optional ?seed=N is passed to the model (the scorer pins it per run)."""
    return await _enqueue(request, payload, seed)


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    """JobSummary rows, newest first (the app's Inbox)."""
    jobs = sorted(_registry(request).jobs.values(), key=lambda j: j.created, reverse=True)[:max(1, min(limit, 500))]
    return [summarize(j.view(with_trace=False)) for j in jobs]


@router.post("/jobs/{job_id}/actions")
async def job_action(job_id: str, body: ActionRequest, request: Request) -> dict[str, Any]:
    """Record an approve / escalate / ask action on a job. Re-entering the investigator for 'ask' is a later step."""
    job = _registry(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")
    if body.type == "ask" and not (body.question or "").strip():
        raise HTTPException(422, "ask needs a question")
    action = Action(**body.model_dump(), ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    state="not_implemented" if body.type == "ask" else "recorded").model_dump()
    job.actions.append(action)
    job.trace.add("action", action=action)
    return action


@router.get("/scores/latest")
async def latest_scores(request: Request) -> dict[str, Any]:
    """The newest scorer run (t/scores/<stamp>.json), for the Scoreboard."""
    s: Settings = request.app.state.settings
    runs = sorted(p for p in s.scores_dir.glob("*.json") if p.stem[:8].isdigit()) if s.scores_dir.exists() else []
    if not runs:
        raise HTTPException(404, "no scorer run yet; run python3 server/scorer.py")
    return json.loads(runs[-1].read_text(encoding="utf-8"))


@router.post("/devices")
async def register_device(body: DeviceRegistration, request: Request) -> dict[str, Any]:
    _registry(request).devices[body.expo_push_token] = body.model_dump()
    return {"ok": True}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _registry(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")
    return job.view()


def _sse(event: str, data: dict[str, Any], seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = _registry(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")

    async def stream() -> AsyncIterator[str]:
        snapshot, q = job.subscribe()
        last = 0
        try:
            for e in snapshot:
                last = e["seq"]
                yield _sse("trace", e, e["seq"])
            # Terminal status is set before its trace entry is queued, so draining q after it is enough.
            while not (job.status in TERMINAL and q.empty()):
                try:
                    e = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"
                    continue
                if e["seq"] <= last:
                    continue
                last = e["seq"]
                yield _sse("trace", e, e["seq"])
            yield _sse(job.status, {"job_id": job.job_id, "status": job.status, "error": job.error,
                                    "totals": job.trace.totals()})
        finally:
            job.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    s: Settings = request.app.state.settings
    if s.fake:
        return {"ok": True, "fake": True, "elasticsearch": {"url": "fake", "ok": True},
                "nvidia_keys_set": {"investigator": False, "critic": False, "small": False},
                "models": {"investigator": "fake", "critic": "fake", "small": "fake"},
                "max_tool_calls": s.max_tool_calls}
    headers = {"Authorization": f"ApiKey {s.es_api_key}"} if s.es_api_key else {}
    es: dict[str, Any] = {"url": s.es_url}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{s.es_url.rstrip('/')}/_cluster/health", headers=headers)
        es.update(ok=r.status_code == 200, status=r.json().get("status") if r.status_code == 200 else r.status_code)
    except httpx.HTTPError as exc:
        es.update(ok=False, error=str(exc))
    # Only the investigator is required today; critic and small are later build steps.
    ok = es["ok"] and s.nvidia_keys_set["investigator"]
    return {"ok": ok, "elasticsearch": es, "nvidia_keys_set": s.nvidia_keys_set,
            "nvidia_base_url": s.nvidia_base_url,
            "models": {"investigator": s.model_investigator, "critic": s.model_critic, "small": s.model_small},
            "max_tool_calls": s.max_tool_calls}


def create_app(settings: Settings | None = None, pipeline: Any = None) -> FastAPI:
    """A Triagent app. fake_server.py builds one with the scripted pipeline; tests swap state.pipeline."""
    application = FastAPI(title="Triagent", version="0.1.0")
    application.include_router(router)
    application.state.registry = Registry()
    application.state.settings = settings or Settings.from_env()
    application.state.pipeline = pipeline or run_pipeline
    return application


app = create_app()
