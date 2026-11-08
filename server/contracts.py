"""Triagent contracts: every shape that crosses a section boundary. Code against these; never redefine them.

Changing a shape here is its own commit titled "contracts: ...", and everyone working in parallel rebases on it.
tests/test_contracts.py checks that the real server's output validates against these models, so a drift fails CI.

Sections and their entry points (Python, all under server/)
  tools.py          class ESTools: dispatch(name, args) -> dict, catalog(), metric_fields(), get_doc(...)
  llm.py            chat(messages, tools=None, model=None, thinking=True, max_tokens=4096, seed=None, trace=None,
                         role="investigator"|"critic"|"small") -> llm.ChatResult       (only module importing openai)
  investigator.py   run(ticket: dict, es_tools, trace: Trace, max_calls=20, *, seed=None, feedback=None) -> Report dict
                    ask(job: dict, question: str, es_tools, trace: Trace, max_calls=8, *, seed=None)
                        -> {"answer": str, "event_ids": [str]}   (follow-up: stored report + trace as context)
  critic.py         run(report: dict, ticket: dict, es_tools, trace: Trace) -> CriticReview dict
  remediate.py      run(report: dict, trace: Trace, ticket: dict | None = None) -> Remediation dict
  main.py           FastAPI; owns the job registry; runs investigator -> critic -> remediate -> store -> ticket patch
  store.py          (to build) save(job: Job) -> None, upserting the SupabaseJobRow below
  jira_mock.py      (to build) polls INC-CREATED docs, POSTs Ticket to /webhook, serves POST /issue/{id}/comment
  notify.py         (to build) push(job) -> PushMessage via Expo, text from prompts/summarize.md on the small model
  scorer.py         standalone, stdlib only; reads t/scenario.json and Job JSON; writes ScoreRun JSON
  app/              Expo client; reads the HTTP API + SSE below (and Supabase once store.py lands)

HTTP API (main.py; fake_server.py serves the identical API with scripted jobs, no Elasticsearch or NVIDIA)
  POST /webhook                 body Ticket -> EnqueueResponse            dedupes on ticket_id
  POST /investigate[?seed=N]    body Ticket -> EnqueueResponse            same; seed pins the model seed
  GET  /jobs                    -> list[JobSummary], newest first         Inbox
  GET  /jobs/{job_id}           -> Job                                    Incident detail
  GET  /jobs/{job_id}/events    -> text/event-stream                      Live investigation
        replays the recorded trace, then tails it live:
          id: <seq>\\nevent: trace\\ndata: <TraceEntry JSON>\\n\\n
        then exactly one terminal event:
          event: done|failed\\ndata: <SSETerminal JSON>\\n\\n
        a line ": keepalive" every 15 s while idle
  POST /jobs/{job_id}/actions   body ActionRequest -> Action              Actions (approve | escalate | ask)
  POST /jobs/{job_id}/ask       body AskRequest -> Action (state queued)  follow-up; /actions with type ask is the same
        409 unless the job is done. The answer arrives on the same Action (matched by id) in GET /jobs/{job_id}:
        state queued -> answering -> answered (answer, event_ids) | failed (error). The job's status stays done; the
        follow-up's trace entries carry phase "ask" and end with an "action" entry holding the final Action.
  GET  /scores/latest           -> ScoreRun (404 until a scorer run exists)   Scoreboard
  POST /devices                 body DeviceRegistration -> {"ok": true}   Expo push token
  GET  /health                  -> Health
  No auth on the server for the demo (LAN only). The app uses one shared demo token for Supabase.

On disk: t/jobs/<job_id>.json is exactly the Job JSON from GET /jobs/{job_id}.

Export JSON Schema for the app (e.g. for json-schema-to-typescript):
  python3 server/contracts.py --schema app/src/contracts.schema.json
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------------------------
# constants shared by tools, prompts, fakes and the app
# ---------------------------------------------------------------------------------------------
CATEGORIES = ("cooling", "power", "network", "storage", "memory", "deploy", "config", "certificate", "security",
              "unknown")
Category = Literal["cooling", "power", "network", "storage", "memory", "deploy", "config", "certificate",
                   "security", "unknown"]
JobStatus = Literal["queued", "running", "critic", "remediating", "done", "failed"]
TERMINAL_STATUSES = ("done", "failed")
Phase = Literal["investigate", "critic", "retry", "critic_retry", "ask"]
TraceKind = Literal["status", "llm", "tool", "finding", "verify", "info", "error", "assistant", "critic",
                    "remediation", "action"]
TOOL_NAMES = ("search_events", "aggregate", "metric_series", "find_changes", "expand_scope", "get_doc",
              "note_finding", "submit_report", "answer_question")   # answer_question ends a follow-up only
PIVOT_FIELDS = ("host.name", "labels.row", "labels.rack", "labels.cluster", "service.name", "observer.name",
                "user.name", "source.ip", "change.id", "kubernetes.pod.name", "event.code", "event.id")
CRITIC_CHECKS = ("ids_exist", "chain_time_order", "root_cause_concrete", "adjacent_steps_linked",
                 "rejections_listed", "category_matches", "confidence_consistent")
MAX_SEARCH_SIZE = 50        # search_events and find_changes rows
MAX_AGG_TOP = 30            # aggregate buckets
MAX_SERIES_POINTS = 60      # metric_series points
MESSAGE_TRIM = 200          # chars of message in a result row
TRACE_RESULT_TRIM = 500     # chars of a tool result kept in a trace entry
DEFAULT_MAX_TOOL_CALLS = 20
DEFAULT_ASK_MAX_CALLS = 8   # follow-up budget, answer_question included


class _Open(BaseModel):
    """Tolerates fields added later, so older clients keep working."""
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------------------------
# ticket (webhook payload)
# ---------------------------------------------------------------------------------------------
class Ticket(BaseModel):
    """Body of POST /webhook and /investigate. jira_mock builds it from the INC-CREATED doc; real Jira/SNOW too."""
    model_config = ConfigDict(extra="ignore")

    ticket_id: str = Field(min_length=1)
    title: str
    description: str = ""
    priority: str = "P3"
    created: str                                   # ISO 8601 UTC, e.g. 2026-11-07T14:03:00Z
    service: str | None = None
    host: str | None = None
    labels: dict[str, str | None] = Field(default_factory=dict)   # row, rack, cluster when known
    namespace: str = "dal1"
    source_event_id: str | None = None             # event.id of the INC-CREATED doc


WebhookPayload = Ticket   # the name main.py has always used


# ---------------------------------------------------------------------------------------------
# report (output of submit_report, stored verbatim)
# ---------------------------------------------------------------------------------------------
class RootCause(BaseModel):
    summary: str
    category: Category
    entity: str
    entity_type: str
    identifiers: dict[str, Any] = Field(default_factory=dict)    # change_id, commit, version, user, interface, ...
    event_ids: list[str] = Field(default_factory=list)


class ChainStep(BaseModel):
    timestamp: str
    event_id: str
    what: str
    why_it_matters: str = ""


class RejectedCandidate(BaseModel):
    what: str
    event_ids: list[str] = Field(default_factory=list)
    why_rejected: str


class Report(BaseModel):
    problem: str
    root_cause: RootCause
    causal_chain: list[ChainStep] = Field(default_factory=list)
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)
    affected: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    open_questions: list[str] = Field(default_factory=list)   # the critic's failed checks are appended as "Reviewer: ..."


# ---------------------------------------------------------------------------------------------
# critic and remediation
# ---------------------------------------------------------------------------------------------
class CriticCheck(BaseModel):
    ok: bool
    detail: str = ""
    source: Literal["code", "model", "missing"]   # code = computed exactly; missing = the model skipped it


class CriticReview(_Open):
    """One critic pass. checks has exactly the seven CRITIC_CHECKS keys."""
    verdict: Literal["pass", "fail"]
    failed: list[str] = Field(default_factory=list)
    checks: dict[str, CriticCheck] = Field(default_factory=dict)
    corrections: list[str] = Field(default_factory=list)
    model: str | None = None
    docs_checked: int | None = None
    docs_cited: int | None = None
    note: str | None = None


class CriticResult(BaseModel):
    """Job.critic. On a failed first review the investigator retries once; kept says which report was returned."""
    first: CriticReview
    final: CriticReview
    retried: bool
    kept: Literal["initial", "retry"] | None = None


class Remediation(_Open):
    """Runbook-grounded steps. Every immediate step names the entity type and a command or console action."""
    covered: bool                         # False when the runbook did not cover the cause (safe triage steps)
    immediate: list[str]
    verify: list[str]
    prevent: list[str]
    rollback_risk: str
    category: str | None = None
    runbook: str | None = None            # e.g. runbooks/power.md
    entity_types: list[str] = Field(default_factory=list)
    source: Literal["model", "runbook"] = "model"   # runbook = model failed, steps copied from the runbook
    note: str | None = None


# ---------------------------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------------------------
class TraceEntry(_Open):
    """One trace line. seq is 1-based and gapless per job; SSE uses it as the event id.

    Fields by kind (all optional beyond seq/ts/kind):
      status       status, retry (bool), totals
      llm          model, prompt_tokens, completion_tokens, latency_ms, tool_name
      tool         call_no, name, arguments, result (JSON truncated to TRACE_RESULT_TRIM)
      finding      finding, event_ids, finding_kind (symptom|precursor|candidate_cause|rejected|other)
      verify       checked, dropped, unverified
      assistant    text (the model's visible text for a turn)
      critic       verdict, failed, corrections, note
      remediation  runbook, covered, source, steps, note
      action       action (Action)
      info, error  message
    """
    seq: int
    ts: str                               # ISO 8601 UTC with milliseconds
    kind: TraceKind
    phase: Phase | None = None
    status: JobStatus | None = None
    message: str | None = None
    name: str | None = None
    call_no: int | None = None
    arguments: dict[str, Any] | None = None
    result: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    tool_name: str | None = None
    finding: str | None = None
    event_ids: list[str] | None = None
    finding_kind: str | None = None
    text: str | None = None
    verdict: Literal["pass", "fail"] | None = None


class Totals(BaseModel):
    llm_calls: int = 0
    tool_calls: int = 0                   # budgeted tool calls, each counted once
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_latency_ms: int = 0


Trace = Any   # tracelog.Trace: .add(kind, **fields) -> entry dict, .entries(), .totals(phases=None), .phase


# ---------------------------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------------------------
class Action(BaseModel):
    type: Literal["approve", "escalate", "ask"]
    by: str = "demo"
    note: str | None = None               # approve / escalate comment
    question: str | None = None           # ask: the follow-up question
    ts: str
    state: Literal["recorded", "queued", "answering", "answered", "failed", "not_implemented"] = "recorded"
    id: str | None = None                 # ask: stable id to find this action again in GET /jobs/{job_id}
    answer: str | None = None             # ask, state answered: the investigator's answer
    event_ids: list[str] = Field(default_factory=list)   # ask: event ids the answer cites, verified in the cluster
    error: str | None = None              # ask, state failed: why


class AskRequest(BaseModel):
    """Body of POST /jobs/{job_id}/ask."""
    question: str = Field(min_length=1)
    by: str = "demo"


class ActionRequest(BaseModel):
    type: Literal["approve", "escalate", "ask"]
    by: str = "demo"
    note: str | None = None
    question: str | None = None


class Job(BaseModel):
    """GET /jobs/{job_id} and t/jobs/<job_id>.json. Times are Unix epoch seconds (floats)."""
    job_id: str
    ticket_id: str
    status: JobStatus
    ticket: Ticket
    report: Report | None = None              # the report the pipeline returns
    report_initial: Report | None = None      # the investigator's first report, before critic and retry
    critic: CriticResult | None = None
    remediation: Remediation | None = None
    actions: list[Action] = Field(default_factory=list)
    error: str | None = None
    created: float
    finished: float | None = None
    initial_seconds: float | None = None      # seconds until report_initial existed
    seed: int | None = None
    totals: Totals
    totals_initial: Totals | None = None      # cost of the first investigation only
    trace: list[TraceEntry] = Field(default_factory=list)


class JobSummary(BaseModel):
    """GET /jobs rows, newest first. Also the columns of the Supabase jobs table."""
    job_id: str
    ticket_id: str
    title: str
    priority: str
    service: str | None = None
    status: JobStatus
    category: Category | None = None
    entity: str | None = None
    confidence: float | None = None
    critic_verdict: Literal["pass", "fail"] | None = None
    created: float
    finished: float | None = None


def summarize(job: dict[str, Any]) -> dict[str, Any]:
    """JobSummary fields from a Job dict. main.py, fake_server.py and store.py all use this."""
    t = job.get("ticket") or {}
    rc = (job.get("report") or {}).get("root_cause") or {}
    crit = (job.get("critic") or {}).get("final") or {}
    return {"job_id": job["job_id"], "ticket_id": job.get("ticket_id") or t.get("ticket_id"),
            "title": t.get("title", ""), "priority": t.get("priority", ""), "service": t.get("service"),
            "status": job["status"], "category": rc.get("category"), "entity": rc.get("entity"),
            "confidence": (job.get("report") or {}).get("confidence"), "critic_verdict": crit.get("verdict"),
            "created": job["created"], "finished": job.get("finished")}


# Supabase (store.py writes it; the app reads it). One row per job: the JobSummary columns plus the full Job JSON.
SUPABASE_JOBS_SQL = """
create table if not exists jobs (
  job_id          text primary key,
  ticket_id       text not null,
  title           text not null,
  priority        text,
  service         text,
  status          text not null,
  category        text,
  entity          text,
  confidence      double precision,
  critic_verdict  text,
  created         double precision not null,
  finished        double precision,
  job             jsonb not null          -- the full Job JSON
);
create index if not exists jobs_created_idx on jobs (created desc);
alter publication supabase_realtime add table jobs;   -- the Inbox subscribes to changes
"""


def supabase_row(job: dict[str, Any]) -> dict[str, Any]:
    return {**summarize(job), "job": job}


# ---------------------------------------------------------------------------------------------
# HTTP bodies
# ---------------------------------------------------------------------------------------------
class EnqueueResponse(BaseModel):
    job_id: str
    ticket_id: str
    status: JobStatus
    duplicate: bool = False


class SSETerminal(BaseModel):
    job_id: str
    status: Literal["done", "failed"]
    error: str | None = None
    totals: Totals


class DeviceRegistration(BaseModel):
    expo_push_token: str
    user: str = "demo"
    platform: Literal["ios", "android", "web"] | None = None


class PushMessage(BaseModel):
    """What notify.py sends to Expo when a job finishes. data.job_id deep-links to Incident detail."""
    to: str
    title: str                            # e.g. "P1 INC5560989: cause found (deploy)"
    body: str                             # one or two lines from prompts/summarize.md
    data: dict[str, Any]                  # {"job_id": ..., "status": ...}


class Health(_Open):
    ok: bool
    fake: bool = False                    # true when served by fake_server.py
    elasticsearch: dict[str, Any] = Field(default_factory=dict)
    nvidia_keys_set: dict[str, bool] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS


class ScoreRow(_Open):
    """One scorer row (scenario x run). Scorer output may carry more fields."""
    scenario: str
    run: int
    status: str
    passed: bool | None = None
    entity_ok: bool | None = None
    category_ok: bool | None = None
    jaccard: float | None = None
    red_herring: bool | None = None
    confidence: float | None = None
    tool_calls: int | None = None
    seconds: float | None = None
    tokens: int | None = None
    critic_first: str | None = None
    retried: bool | None = None
    before: dict[str, Any] | None = None


class ScoreRun(BaseModel):
    """t/scores/<stamp>.json and GET /scores/latest."""
    meta: dict[str, Any]
    rows: list[ScoreRow]


# ---------------------------------------------------------------------------------------------
# protocols for backend sections (fakes.py implements both)
# ---------------------------------------------------------------------------------------------
class ToolsLike(Protocol):
    """What investigator.py and critic.py need from tools.ESTools (fakes.FakeTools mirrors it)."""

    def dispatch(self, name: str, arguments: dict[str, Any] | None) -> Any: ...

    def catalog(self) -> list[dict[str, Any]]: ...


ChatFn = Callable[..., Any]   # llm.chat signature (see the header); fakes.fake_chat builds scripted ones


# ---------------------------------------------------------------------------------------------
# JSON Schema export for the app
# ---------------------------------------------------------------------------------------------
APP_MODELS = (Ticket, Report, Job, JobSummary, TraceEntry, CriticResult, Remediation, Action, ActionRequest,
              EnqueueResponse, SSETerminal, DeviceRegistration, PushMessage, Health, ScoreRun, AskRequest)


def json_schema() -> dict[str, Any]:
    from pydantic.json_schema import models_json_schema
    _, schema = models_json_schema([(m, "serialization") for m in APP_MODELS],
                                   title="Triagent contracts")
    return schema


if __name__ == "__main__":
    import argparse
    import json
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Triagent contracts")
    ap.add_argument("--schema", help="write JSON Schema for the app models to this path ('-' for stdout)")
    ap.add_argument("--sql", action="store_true", help="print the Supabase jobs table SQL")
    a = ap.parse_args()
    if a.sql:
        sys.stdout.write(SUPABASE_JOBS_SQL)
    if a.schema:
        text = json.dumps(json_schema(), indent=2)
        if a.schema == "-":
            sys.stdout.write(text + "\n")
        else:
            Path(a.schema).parent.mkdir(parents=True, exist_ok=True)
            Path(a.schema).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {a.schema}", file=sys.stderr)
    if not (a.sql or a.schema):
        ap.print_help()
