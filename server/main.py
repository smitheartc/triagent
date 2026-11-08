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
    POST /webhook             Jira / jira_mock payload; dedupes on ticket_id + created
    POST /investigate         same body and behavior, for curl; optional ?seed=N pins the model seed (scorer)
    GET  /jobs                newest first, JobSummary rows (Inbox)
    GET  /jobs/{job_id}       status (queued|running|critic|remediating|done|failed), report, trace
    GET  /jobs/{job_id}/events  Server-Sent Events stream of trace entries
    POST /jobs/{job_id}/actions  approve | escalate | ask (recorded on the job; ask runs a follow-up)
    POST /jobs/{job_id}/ask   follow-up question: the investigator resumes with the report and trace as context
                              (TRIAGENT_ASK_MAX_CALLS, default 8); the answer lands on the returned Action
    GET  /scores/latest       newest scorer run from t/scores/ (Scoreboard)
    POST /devices             register an Expo push token
    GET  /health              ES reachability, API key presence per role, configured model ids

All shapes are defined in contracts.py. fake_server.py serves this same app with scripted jobs.

Ticket patch: when a job that arrived on /webhook finishes (done or failed), patch_ticket() renders
templates/ticket_comment.md (or ticket_comment_failed.md) and posts it to the ticket system chosen by TICKET_BACKEND:
mock (default, jira_mock.py at JIRA_MOCK_URL), none, jira or snow (not implemented yet). Jobs started with
/investigate (curl, scorer) never comment. A failed patch is recorded in the trace and never fails the job.

Supabase (store.py, off until SUPABASE_URL and SUPABASE_SERVICE_KEY are set): every status change upserts the job's
row on one writer thread, so writes stay in order and never block the pipeline; at startup the newest jobs are loaded
back so GET /jobs survives a restart; POST /devices also saves the token. Push (notify.py): when a webhook job ends,
every registered device gets an Expo push (TRIAGENT_PUSH=webhook|all|off, default webhook). fake_server.py does
neither.

At most TRIAGENT_MAX_PARALLEL_JOBS investigations run at once (default 3; the NVIDIA account limit is per minute);
the rest wait in status "queued".

Jobs live in memory and are written to t/jobs/<job_id>.json when they finish. Restarting the server forgets them.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import string
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import investigator
import llm
import notify
import store
import tuning_knobs
import jira_mock
from contracts import (DEFAULT_ASK_MAX_CALLS, Action, ActionRequest, AskRequest, DeviceRegistration, WebhookPayload,
                       summarize)
from tools import ESTools
from tracelog import Trace

load_dotenv(Path(__file__).with_name(".env"))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("triagent.main")

REPO_ROOT = Path(__file__).resolve().parent.parent
TERMINAL = ("done", "failed")
TEMPLATES_DIR = Path(__file__).with_name("templates")
TICKET_BACKENDS = ("mock", "none", "jira", "snow")
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
    critic_retry: int = 1                 # tuning_knobs: 1 = one investigator retry on a failed review; 0 = none
    scores_dir: Path = REPO_ROOT / "t" / "scores"
    fake: bool = False                    # set by fake_server.py
    ticket_backend: str = "mock"          # TICKET_BACKEND: mock | none | jira | snow
    jira_mock_url: str = "http://localhost:8082"
    public_url: str = ""                  # TRIAGENT_PUBLIC_URL: base URL put in ticket comments, if set
    max_parallel_jobs: int = 3
    push_mode: str = "webhook"            # TRIAGENT_PUSH: webhook | all | off
    ask_max_calls: int = DEFAULT_ASK_MAX_CALLS

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
            max_tool_calls=tuning_knobs.max_tool_calls(),
            jobs_dir=Path(os.environ.get("TRIAGENT_JOBS_DIR") or REPO_ROOT / "t" / "jobs"),
            critic_enabled=tuning_knobs.critic_enabled(),
            critic_retry=tuning_knobs.critic_retry(),
            ticket_backend=(os.environ.get("TICKET_BACKEND") or "mock").strip().lower(),
            jira_mock_url=os.environ.get("JIRA_MOCK_URL") or "http://localhost:8082",
            public_url=(os.environ.get("TRIAGENT_PUBLIC_URL") or "").rstrip("/"),
            max_parallel_jobs=max(1, int(os.environ.get("TRIAGENT_MAX_PARALLEL_JOBS", "3"))),
            push_mode=(os.environ.get("TRIAGENT_PUSH") or "webhook").strip().lower(),
            ask_max_calls=max(2, int(os.environ.get("TRIAGENT_ASK_MAX_CALLS", str(DEFAULT_ASK_MAX_CALLS)))),
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
    origin: str = "investigate"           # "webhook" jobs get a ticket comment and a push; not in the Job JSON
    saver: Any = None                     # StoreWriter, set at enqueue when Supabase is on; not in the Job JSON
    devices: dict[str, dict[str, Any]] | None = None   # Registry.devices (push tokens); not in the Job JSON
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
        store_report(self)

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
        self.slots: asyncio.Semaphore | None = None     # created on the server's loop at the first enqueue


class StoreWriter:
    """Upserts Job snapshots to Supabase on one thread, in the order they were taken. Failures are logged only."""

    def __init__(self, st: store.Store) -> None:
        self.store = st
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store")

    def __call__(self, snapshot: dict[str, Any]) -> None:
        self.pool.submit(self._save, snapshot)

    def _save(self, snapshot: dict[str, Any]) -> None:
        try:
            self.store.save(snapshot)
        except Exception as exc:  # noqa: BLE001 - Supabase being down must not touch the pipeline
            log.warning("supabase: job %s (%s) not saved: %s", snapshot.get("job_id"), snapshot.get("status"), exc)


def job_from_view(view: dict[str, Any], loop: asyncio.AbstractEventLoop) -> Job:
    """A registry Job rebuilt from its Job JSON (a Supabase row). A job that never finished is marked failed."""
    job = Job(job_id=view["job_id"], ticket=view["ticket"], loop=loop)
    for name in ("status", "report", "critic", "remediation", "error", "seed", "report_initial", "totals_initial",
                 "initial_seconds", "created", "finished"):
        if view.get(name) is not None:
            setattr(job, name, view[name])
    job.actions = list(view.get("actions") or [])
    job.trace.restore(view.get("trace") or [])
    if job.status not in TERMINAL:
        job.error = job.error or "server restarted during the investigation"
        job.finished = job.finished or time.time()
        job.trace.add("error", message=job.error)
        job.status = "failed"
        job.trace.add("status", status="failed")
    return job


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
    """Queue a Supabase upsert of the job as it is now (called on every status change). No-op without Supabase."""
    if job.saver is not None:
        job.saver(job.view())


def push_finished(job: Job, settings: Settings) -> None:
    """Expo push for a finished job to every known device (POST /devices since start, plus Supabase's devices)."""
    if settings.fake or settings.push_mode == "off" or (settings.push_mode != "all" and job.origin != "webhook"):
        return
    tokens = list(job.devices or {})
    try:
        tokens += [d["expo_push_token"] for d in store.default().devices()]
    except Exception as exc:  # noqa: BLE001
        log.warning("supabase: devices not read, pushing to the %d in memory: %s", len(tokens), exc)
    notify.push(job, tokens)


def _bullets(items: list[str], empty: str = "- none") -> str:
    return "\n".join(f"- {x}" for x in items) if items else empty


def _numbered(items: list[str], empty: str) -> str:
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)) if items else empty


def _dur(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def cost_line(job: Job) -> str:
    """One line of cost and latency for a finished job: the trace's last info entry, the server log and the ticket
    comment all carry it. Tokens are the cost unit (NVIDIA hosted credits are billed by token)."""
    t = job.trace.totals()
    tokens = t["prompt_tokens"] + t["completion_tokens"]
    line = (f"{tokens:,} tokens ({t['prompt_tokens']:,} in, {t['completion_tokens']:,} out) over {t['llm_calls']} "
            f"model calls and {t['tool_calls']} tool calls; {_dur((job.finished or time.time()) - job.created)} "
            f"end to end")
    if job.initial_seconds is not None:
        line += f", first report at {_dur(job.initial_seconds)}"
    return line + f", {_dur(t['llm_latency_ms'] / 1000)} waiting on the model"


def _log_cost(job: Job) -> None:
    line = cost_line(job)
    job.trace.add("info", message=f"cost and latency: {line}")
    log.info("job %s %s: %s", job.job_id, job.ticket.get("ticket_id"), line)


def _cost(job: Job) -> str:
    return cost_line(job)


def render_comment(job: Job, settings: Settings, status: str) -> dict[str, Any]:
    """The comment (jira_mock.comment_payload) for a job ending in status (done|failed), from templates/."""
    link = f"{settings.public_url}/jobs/{job.job_id}" if settings.public_url else ""
    common = {"job": f"{job.job_id} ({link})" if link else job.job_id, "cost": _cost(job)}
    if status != "done" or not job.report:
        body = string.Template((TEMPLATES_DIR / "ticket_comment_failed.md").read_text(encoding="utf-8")).substitute(
            common, error=job.error or "no report produced")
        return jira_mock.comment_payload(body.strip() + "\n", job.job_id, "failed")

    r = job.report
    rc = r.get("root_cause") or {}
    rem = job.remediation or {}
    final = (job.critic or {}).get("final") or {}
    critic = final.get("verdict") or "skipped"
    if final.get("verdict") == "fail":
        critic += f" ({len(final.get('failed') or [])} checks failed, see open questions)"
    chain = [f"`{c.get('event_id')}` {c.get('timestamp', '')}: {c.get('what', '')}"
             + (f" ({c['why_it_matters']})" if c.get("why_it_matters") else "")
             for c in r.get("causal_chain") or []]
    rejected = [f"{x.get('what', '')}: {x.get('why_rejected', '')}"
                + (" (" + ", ".join(f"`{e}`" for e in x.get("event_ids") or []) + ")" if x.get("event_ids") else "")
                for x in r.get("rejected_candidates") or []]
    runbook = rem.get("runbook") or "no runbook"
    if rem and not rem.get("covered", True):
        runbook += ", does not cover this cause: safe triage steps"
    fields = {
        "confidence": f"{float(r.get('confidence') or 0):.2f}", "critic": critic,
        "problem": r.get("problem", ""),
        "category": rc.get("category", "unknown"), "entity_type": rc.get("entity_type", ""),
        "entity": rc.get("entity", ""), "root_cause": rc.get("summary", ""),
        "identifiers": ", ".join(f"{k}={v}" for k, v in (rc.get("identifiers") or {}).items()) or "none",
        "cause_event_ids": ", ".join(f"`{e}`" for e in rc.get("event_ids") or []) or "none",
        "chain": _numbered(chain, "none recorded"), "rejected": _bullets(rejected),
        "runbook": runbook,
        "immediate": _numbered(rem.get("immediate") or [], "remediation not available"),
        "verify": _bullets(rem.get("verify") or []), "prevent": _bullets(rem.get("prevent") or []),
        "rollback_risk": rem.get("rollback_risk") or "unknown",
        "open_questions": _bullets(r.get("open_questions") or []),
    }
    body = string.Template((TEMPLATES_DIR / "ticket_comment.md").read_text(encoding="utf-8")).substitute(
        common, **fields)
    return jira_mock.comment_payload(body.strip() + "\n", job.job_id, "done")


def _post_mock_comment(settings: Settings, ticket_id: str, comment: dict[str, Any]) -> dict[str, Any]:
    url = f"{settings.jira_mock_url.rstrip('/')}/issue/{ticket_id}/comment"
    r = httpx.post(url, json=comment, timeout=10.0)
    if r.status_code >= 300:
        raise RuntimeError(f"POST {url} -> {r.status_code}: {r.text[:200]}")
    res = r.json()
    if not isinstance(res, dict) or any(k not in res for k in jira_mock.COMMENT_RESPONSE_FIELDS):
        raise RuntimeError(f"POST {url}: unexpected reply {str(res)[:200]}")
    return res


def patch_ticket(job: Job, settings: Settings, status: str) -> None:
    """Post the finished job as a comment on its ticket. Only for webhook jobs. Never raises: a ticket system being
    down must not turn a good investigation into a failed job; the outcome goes into the trace instead."""
    if job.origin != "webhook" or settings.ticket_backend == "none":
        return
    ticket_id = job.ticket.get("ticket_id")
    try:
        comment = render_comment(job, settings, status)
        if settings.ticket_backend == "mock":
            res = _post_mock_comment(settings, ticket_id, comment)
            job.trace.add("info", message=f"ticket {ticket_id}: comment {res['id']} posted to jira_mock")
        elif settings.ticket_backend in ("jira", "snow"):
            raise NotImplementedError(f"TICKET_BACKEND={settings.ticket_backend} is not implemented yet; use mock")
        else:
            raise ValueError(f"unknown TICKET_BACKEND {settings.ticket_backend!r}; one of {', '.join(TICKET_BACKENDS)}")
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("job %s: ticket patch for %s failed: %s", job.job_id, ticket_id, exc)
        job.trace.add("error", message=f"ticket {ticket_id}: comment not posted ({type(exc).__name__}: {exc})")


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
            if first["verdict"] == "fail" and settings.critic_retry < 1:
                # No retry: keep the report, with the reviewer's failed checks appended for the on-call engineer.
                job.report = with_review_notes(job.report, first)
            elif first["verdict"] == "fail":
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
        job.finished = time.time()
        _log_cost(job)
        # Before the terminal status: SSE ends once the status is terminal, and the patch outcome is a trace entry.
        await asyncio.to_thread(patch_ticket, job, settings, "done")
        job.set_status("done", totals=job.trace.totals())
        await asyncio.to_thread(push_finished, job, settings)
    except Exception as exc:  # noqa: BLE001 - a job must always end in a terminal state
        log.exception("job %s failed", job.job_id)
        job.error = f"{type(exc).__name__}: {exc}"
        job.trace.add("error", message=job.error)
        job.finished = time.time()
        _log_cost(job)
        await asyncio.to_thread(patch_ticket, job, settings, "failed")
        job.set_status("failed")
        await asyncio.to_thread(push_finished, job, settings)
    finally:
        es.close()
        try:
            path = await asyncio.to_thread(persist, job, settings.jobs_dir)
            log.info("job %s %s -> %s", job.job_id, job.status, path)
        except OSError:
            log.exception("could not persist job %s", job.job_id)
        store_report(job)   # the final row includes entries added after the terminal status (push outcome)


async def run_ask(job: Job, action: dict[str, Any], settings: Settings) -> None:
    """Follow-up: investigator.ask over the finished job, then the answer onto the action. Never raises."""
    ticket = job.ticket
    es = ESTools(settings.es_url, namespace=ticket.get("namespace") or "dal1", anchor=ticket["created"],
                 api_key=settings.es_api_key)
    job.trace.phase = "ask"
    try:
        action["state"] = "answering"
        job.trace.add("action", action=dict(action))
        got = await asyncio.to_thread(investigator.ask, job.view(), action["question"], es, job.trace,
                                      settings.ask_max_calls, seed=job.seed)
        action.update(state="answered", answer=got["answer"], event_ids=got["event_ids"])
    except Exception as exc:  # noqa: BLE001 - an ask must always end answered or failed
        log.exception("job %s: follow-up %s failed", job.job_id, action.get("id"))
        action.update(state="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        es.close()
        job.trace.add("action", action=dict(action))
        job.trace.phase = None
        store_report(job)
        try:
            await asyncio.to_thread(persist, job, settings.jobs_dir)
        except OSError:
            log.exception("could not persist job %s", job.job_id)


# ---------------------------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------------------------
router = APIRouter()


def _registry(request: Request) -> Registry:
    return request.app.state.registry


async def _run_in_slot(app: FastAPI, job: Job) -> None:
    reg: Registry = app.state.registry
    async with reg.slots:
        await app.state.pipeline(job, app.state.settings)


async def _enqueue(request: Request, payload: WebhookPayload, seed: int | None = None,
                   origin: str = "investigate") -> JSONResponse:
    reg = _registry(request)
    existing = reg.by_ticket.get(payload.ticket_id)
    # Same ticket_id and created = a repeated delivery. A new created is the ticket raised again (dcsim reuses ids
    # across runs with the same seed), which gets its own investigation.
    if existing is not None and reg.jobs[existing].ticket.get("created") == payload.created:
        job = reg.jobs[existing]
        return JSONResponse({"job_id": job.job_id, "ticket_id": payload.ticket_id, "status": job.status,
                             "duplicate": True})
    job = Job(job_id=f"job-{uuid.uuid4().hex[:12]}", ticket=payload.model_dump(), loop=asyncio.get_running_loop(),
              seed=seed, origin=origin, saver=getattr(request.app.state, "store_writer", None), devices=reg.devices)
    reg.jobs[job.job_id] = job
    reg.by_ticket[payload.ticket_id] = job.job_id
    job.set_status("queued")
    if reg.slots is None:
        reg.slots = asyncio.Semaphore(request.app.state.settings.max_parallel_jobs)
    task = asyncio.create_task(_run_in_slot(request.app, job))
    reg.tasks.add(task)
    task.add_done_callback(reg.tasks.discard)
    log.info("ticket %s -> %s queued", payload.ticket_id, job.job_id)
    return JSONResponse({"job_id": job.job_id, "ticket_id": payload.ticket_id, "status": "queued"})


@router.post("/webhook")
async def webhook(payload: WebhookPayload, request: Request) -> JSONResponse:
    return await _enqueue(request, payload, origin="webhook")


@router.post("/investigate")
async def investigate(payload: WebhookPayload, request: Request, seed: int | None = None) -> JSONResponse:
    """Same body and dedupe as /webhook. Optional ?seed=N is passed to the model (the scorer pins it per run)."""
    return await _enqueue(request, payload, seed)


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    """JobSummary rows, newest first (the app's Inbox)."""
    jobs = sorted(_registry(request).jobs.values(), key=lambda j: j.created, reverse=True)[:max(1, min(limit, 500))]
    return [summarize(j.view(with_trace=False)) for j in jobs]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _start_ask(request: Request, job: Job, question: str, by: str) -> dict[str, Any]:
    if job.status != "done" or not job.report:
        raise HTTPException(409, f"job {job.job_id} is {job.status}; follow-ups need a finished report")
    action = Action(type="ask", by=by, question=question.strip(), ts=_now(), state="queued",
                    id=f"ask-{uuid.uuid4().hex[:10]}").model_dump()
    job.actions.append(action)
    job.trace.add("action", action=dict(action))
    reg = _registry(request)
    if reg.slots is None:
        reg.slots = asyncio.Semaphore(request.app.state.settings.max_parallel_jobs)

    async def go() -> None:
        async with reg.slots:
            await request.app.state.asker(job, action, request.app.state.settings)
    task = asyncio.create_task(go())
    reg.tasks.add(task)
    task.add_done_callback(reg.tasks.discard)
    log.info("job %s: follow-up %s queued", job.job_id, action["id"])
    return dict(action)


@router.post("/jobs/{job_id}/ask")
async def job_ask(job_id: str, body: AskRequest, request: Request) -> dict[str, Any]:
    """Follow-up question. Returns the Action (queued); poll GET /jobs/{job_id} for its answer by id."""
    job = _registry(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")
    if not body.question.strip():
        raise HTTPException(422, "ask needs a question")
    return await _start_ask(request, job, body.question, body.by)


@router.post("/jobs/{job_id}/actions")
async def job_action(job_id: str, body: ActionRequest, request: Request) -> dict[str, Any]:
    """Record an approve / escalate action on a job; an ask starts a follow-up (same as POST /jobs/{id}/ask)."""
    job = _registry(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")
    if body.type == "ask":
        if not (body.question or "").strip():
            raise HTTPException(422, "ask needs a question")
        return await _start_ask(request, job, body.question, body.by)
    action = Action(**body.model_dump(), ts=_now(), state="recorded").model_dump()
    job.actions.append(action)
    job.trace.add("action", action=action)
    store_report(job)
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
    if getattr(request.app.state, "store_writer", None) is not None:
        try:
            await asyncio.to_thread(store.default().save_device, body.model_dump())
        except Exception as exc:  # noqa: BLE001 - the token is still registered in memory
            log.warning("supabase: device not saved: %s", exc)
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
    return {"ok": ok, "elasticsearch": es, "supabase": store.default().status(), "nvidia_keys_set": s.nvidia_keys_set,
            "nvidia_base_url": s.nvidia_base_url,
            "models": {"investigator": s.model_investigator, "critic": s.model_critic, "small": s.model_small},
            "max_tool_calls": s.max_tool_calls}


def create_app(settings: Settings | None = None, pipeline: Any = None, asker: Any = None) -> FastAPI:
    """A Triagent app. fake_server.py builds one with the scripted pipeline; tests swap state.pipeline."""
    application = FastAPI(title="Triagent", version="0.1.0")
    # The app's web build is served from another origin (Expo on :8081). No auth on this API (LAN demo), so any origin.
    application.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    application.include_router(router)
    application.state.registry = Registry()
    application.state.settings = settings or Settings.from_env()
    application.state.pipeline = pipeline or run_pipeline
    application.state.asker = asker or run_ask
    st = store.default()
    application.state.store_writer = StoreWriter(st) if st.enabled and not application.state.settings.fake else None

    async def _load_jobs() -> None:
        if getattr(application.state, "store_writer", None) is None:
            return
        try:
            views = await asyncio.to_thread(st.load_jobs)
        except Exception as exc:  # noqa: BLE001 - start empty rather than not at all
            log.warning("supabase: jobs not loaded at startup: %s", exc)
            return
        reg, loop = application.state.registry, asyncio.get_running_loop()
        for view in views:
            if view.get("job_id") and view["job_id"] not in reg.jobs:
                job = job_from_view(view, loop)
                job.saver = application.state.store_writer
                reg.jobs[job.job_id] = job
                if view.get("status") not in TERMINAL:
                    store_report(job)
        # by_ticket is not restored: a ticket raised again after a restart is investigated again, as before.
        log.info("supabase: loaded %d jobs", len(views))

    application.router.on_startup.append(_load_jobs)
    return application


app = create_app()
