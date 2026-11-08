"""Score the triage pipeline against the dcsim answer key. Standard library only.

One command from a clean checkout (docker compose up, server deps installed, server/.env filled in):

    python3 server/scorer.py --runs 3

What it does
  1. Answer key. Reads t/scenario.json. If it is missing and the cluster has no dcsim data, it loads the data
     first (es_setup + dcsim --scenario all at a fixed --start, so ticket ids are stable). If dcsim data exists
     but the key is missing or does not match the cluster, it stops; pass --ingest to delete the dcsim data
     streams and reload them.
  2. Server. Uses --server if it answers /health, otherwise starts uvicorn from server/ for the run.
  3. For every scenario and run: builds the webhook payload from the ticket's INC-CREATED doc in ES (what
     jira_mock sends), POSTs it to /investigate?seed=<seed>, follows /jobs/<id>/events until done or failed,
     then fetches the report and trace totals from /jobs/<id>.
  4. Scores each run and prints a markdown table; writes it to t/scores/<timestamp>.md (+ .json).

Only the scorer reads the answer key, after the fact. The server never sees it.

Scoring (CLAUDE.md, Scoring)
  cause entity   report root_cause.entity == key entity (case-insensitive), or a key change id / commit
                 appears in report root_cause.identifiers (commit prefix of >= 7 chars counts)
  category       key category mapped: facility.cooling->cooling, facility.power->power, network.*->network,
                 hardware.storage->storage, software.release->memory|deploy,
                 change.configuration->certificate if entity_type is certificate else config, security.*->security
  chain overlap  Jaccard of report causal_chain event ids vs key causal_chain ids that have an event_id
  red herring    HARD FAIL if any red_herrings[].event_id is in root_cause.event_ids, or a red herring's entity
                 is the report's cause entity or one of its identifiers (location keys such as rack, row, cluster
                 or labels.rack don't count: a correct report names its own rack, and a red herring can share it)
  pass           cause entity and category correct and no red herring
  "none" scenarios (no root cause): pass if category is unknown and confidence <= 0.3
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jira_mock  # noqa: E402  (stdlib only, like this file)

SERVER_DIR = Path(__file__).resolve().parent
ROOT = SERVER_DIR.parent
DEFAULT_START = "2026-11-07T08:00:00Z"   # fixed, so ingests are reproducible and ticket ids stable
CATEGORY_MAP = [("facility.cooling", {"cooling"}), ("facility.power", {"power"}), ("network.", {"network"}),
                ("hardware.storage", {"storage"}), ("software.release", {"memory", "deploy"}),
                ("security.", {"security"})]


# ---------------------------------------------------------------------------------------------
# small HTTP helpers (stdlib)
# ---------------------------------------------------------------------------------------------
def env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    p = SERVER_DIR / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 30.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


class ES:
    def __init__(self, url: str, api_key: str | None) -> None:
        self.url = url.rstrip("/")
        self.h = {"Authorization": f"ApiKey {api_key}"} if api_key else {}

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return http(method, self.url + path, body, self.h)

    def streams(self, ns: str) -> list[str]:
        st, body = self.call("GET", f"/_data_stream/logs-*-{ns},metrics-*-{ns}")
        return [d["name"] for d in (body or {}).get("data_streams", [])] if st == 200 else []

    def ticket_doc(self, ns: str, event_id: str) -> dict[str, Any] | None:
        st, body = self.call("POST", f"/logs-incident.tickets-{ns}/_search",
                             {"size": 1, "query": {"term": {"event.id": event_id}}})
        hits = (body or {}).get("hits", {}).get("hits", []) if st == 200 else []
        return hits[0]["_source"] if hits else None


# ---------------------------------------------------------------------------------------------
# data: answer key and ingest
# ---------------------------------------------------------------------------------------------
def es_setup_path() -> Path:
    for p in (ROOT / "sim" / "es_setup.py", ROOT / "escluster" / "es_setup.py"):
        if p.exists():
            return p
    raise SystemExit("es_setup.py not found in sim/ or escluster/")


def ingest(es: ES, args: argparse.Namespace) -> None:
    ns = args.namespace
    old = es.streams(ns)
    if old:
        print(f"deleting {len(old)} dcsim data streams in namespace {ns}", file=sys.stderr)
        for name in old:   # one by one: a single DELETE with a missing name 404s and deletes nothing
            st, body = es.call("DELETE", f"/_data_stream/{name}")
            if st not in (200, 404):
                raise SystemExit(f"could not delete {name}: {st} {body}")
    py = sys.executable
    auth = ["--es-api-key", args.es_api_key] if args.es_api_key else []
    steps = [
        [py, str(es_setup_path()), "--es-url", es.url, "--catalog", str(ROOT / "sim" / "event_catalog.json"),
         "--namespace", ns, "--verify", *auth],
        [py, str(ROOT / "sim" / "dcsim.py"), "--scenario", "all", "--seed", str(args.sim_seed), "--start", args.start,
         "--duration", args.duration, "--namespace", ns, "--es-url", es.url, "--answer-key", str(args.answer_key),
         "--catalog", str(ROOT / "t" / "event_catalog.json"), "--out", "", *auth],
    ]
    args.answer_key.parent.mkdir(parents=True, exist_ok=True)
    for cmd in steps:
        print("running:", " ".join(c if c != args.es_api_key else "***" for c in cmd), file=sys.stderr)
        if subprocess.run(cmd, cwd=ROOT).returncode != 0:
            raise SystemExit(f"ingest step failed: {cmd[1]}")
    time.sleep(2)  # let the last bulk refresh


def load_key(es: ES, args: argparse.Namespace) -> dict[str, Any]:
    if args.ingest:
        ingest(es, args)
    elif not args.answer_key.exists():
        if es.streams(args.namespace):
            raise SystemExit(f"{args.answer_key} is missing but the cluster already holds dcsim data in "
                             f"'{args.namespace}'. Pass --ingest to delete it and reload all scenarios.")
        print(f"{args.answer_key} missing and the cluster is empty: loading all scenarios", file=sys.stderr)
        ingest(es, args)
    key = json.loads(args.answer_key.read_text(encoding="utf-8"))
    missing = [s["scenario"] for s in key.get("scenarios", [])
               if s.get("ticket") and not es.ticket_doc(key.get("namespace", args.namespace), s["ticket"]["event_id"])]
    if missing:
        raise SystemExit(f"answer key tickets not found in the cluster for {missing}: {args.answer_key} does not "
                         f"match the loaded data. Pass --ingest to reload.")
    return key


def payload_from_doc(src: dict[str, Any], ns: str) -> dict[str, Any]:
    """Webhook payload exactly as jira_mock builds it from an INC-CREATED doc."""
    return jira_mock.payload_from_doc(src, ns)


# ---------------------------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------------------------
def server_python() -> str:
    for p in (ROOT / ".venv" / "bin" / "python", SERVER_DIR / ".venv" / "bin" / "python",
              ROOT / ".venv" / "Scripts" / "python.exe"):
        if p.exists():
            return str(p)
    return sys.executable


def ensure_server(args: argparse.Namespace, log_path: Path) -> subprocess.Popen | None:
    st, body = http("GET", args.server + "/health", timeout=3) if _reachable(args.server) else (0, None)
    proc = None
    if st != 200:
        if args.no_spawn:
            raise SystemExit(f"server {args.server} is not reachable (and --no-spawn is set)")
        py = server_python()
        if subprocess.run([py, "-c", "import uvicorn, fastapi, openai, httpx"], capture_output=True).returncode:
            raise SystemExit(f"{py} cannot import the server deps. Run: pip install -r server/requirements.txt "
                             f"(or create .venv at the repo root)")
        port = urllib.parse.urlparse(args.server).port or 8080
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"starting server: {py} -m uvicorn main:app --port {port}  (log {log_path})", file=sys.stderr)
        proc = subprocess.Popen([py, "-m", "uvicorn", "main:app", "--port", str(port)], cwd=SERVER_DIR,
                                stdout=log_path.open("w"), stderr=subprocess.STDOUT)
        for _ in range(60):
            if proc.poll() is not None:
                raise SystemExit(f"server exited during startup; see {log_path}")
            if _reachable(args.server):
                st, body = http("GET", args.server + "/health", timeout=3)
                if st == 200:
                    break
            time.sleep(0.5)
        else:
            proc.terminate()
            raise SystemExit("server did not come up within 30s")
    if not (body or {}).get("ok"):
        if proc:
            proc.terminate()
        raise SystemExit(f"server /health is not ok: {json.dumps(body)}")
    return proc


def _reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url + "/health", timeout=3).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


def wait_sse(server: str, job_id: str, timeout: float) -> str:
    """Follow the SSE stream until the terminal event. Returns 'done', 'failed' or 'timeout'."""
    deadline = time.time() + timeout
    with urllib.request.urlopen(f"{server}/jobs/{job_id}/events", timeout=timeout) as r:
        event = None
        for raw in r:
            if time.time() > deadline:
                return "timeout"
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line == "" and event in ("done", "failed"):
                return event
    return "timeout"


def run_one(args: argparse.Namespace, base: dict[str, Any], run: int, tag: str) -> dict[str, Any]:
    payload = dict(base, ticket_id=f"{base['ticket_id']}.{tag}.{run}")   # unique per run: the server dedupes
    seed = args.seed + run - 1
    t0 = time.time()
    st, body = http("POST", f"{args.server}/investigate?seed={seed}", payload)
    if st != 200:
        return {"status": "failed", "error": f"POST /investigate {st}: {body}", "seconds": 0.0}
    job_id = body["job_id"]
    try:
        end = wait_sse(args.server, job_id, args.timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        end = f"timeout ({exc})"
    st, job = http("GET", f"{args.server}/jobs/{job_id}")
    job = job if st == 200 else {}
    secs = (job.get("finished") or time.time()) - (job.get("created") or t0)
    return {"status": job.get("status", end), "error": job.get("error"), "job_id": job_id, "seed": seed,
            "report": job.get("report"), "totals": job.get("totals") or {}, "seconds": round(secs, 1),
            "report_initial": job.get("report_initial"), "totals_initial": job.get("totals_initial"),
            "initial_seconds": job.get("initial_seconds"), "critic": job.get("critic")}


def score_before_after(scn: dict[str, Any], red_herrings: list[dict[str, Any]], res: dict[str, Any]
                       ) -> dict[str, Any]:
    """Score the final report (after the critic and retry) and the investigator's first report (before)."""
    after = score(scn, red_herrings, res)
    before_res = dict(res, report=res.get("report_initial") or res.get("report"),
                      totals=res.get("totals_initial") or res.get("totals"),
                      seconds=round(res["initial_seconds"], 1) if res.get("initial_seconds") else res.get("seconds"))
    before = score(scn, red_herrings, before_res)
    crit = res.get("critic") or {}
    first, final = crit.get("first") or {}, crit.get("final") or {}
    after["before"] = {k: before.get(k) for k in ("passed", "entity_ok", "category_ok", "report_entity",
                                                   "report_category", "jaccard", "red_herring", "confidence",
                                                   "tool_calls", "seconds", "tokens")}
    after["critic_first"] = first.get("verdict")
    after["critic_failed"] = first.get("failed") or []
    after["critic_final"] = final.get("verdict")
    after["retried"] = bool(crit.get("retried"))
    after["kept"] = crit.get("kept")
    return after


# ---------------------------------------------------------------------------------------------
# scoring (pure; unit-tested)
# ---------------------------------------------------------------------------------------------
def expected_categories(rc: dict[str, Any]) -> set[str]:
    cat = rc.get("category", "")
    for prefix, cats in CATEGORY_MAP:
        if cat == prefix or (prefix.endswith(".") and cat.startswith(prefix)):
            return cats
    if cat == "change.configuration":
        return {"certificate"} if rc.get("entity_type") == "certificate" else {"config"}
    return {"unknown"}


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return [str(obj)] if obj is not None else []


# Identifier keys that only say where the cause sits (rack, row, cluster). A red herring on the same rack shares them
# with any correct report, so they never count as naming the red herring; its event ids and entity still do.
LOCATION_KEYS = {"row", "rack", "cluster", "datacenter", "site", "zone", "location", "namespace"}


def _without_location(identifiers: Any) -> Any:
    if not isinstance(identifiers, dict):
        return identifiers
    return {k: v for k, v in identifiers.items() if str(k).lower().rsplit(".", 1)[-1] not in LOCATION_KEYS}


def _same_id(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 7 and long_.startswith(short) and all(c in "0123456789abcdef" for c in long_)


def score(scn: dict[str, Any], red_herrings: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    rep = result.get("report") or {}
    rc = rep.get("root_cause") or {}
    key_rc = scn.get("root_cause") or {}
    totals = result.get("totals") or {}
    out: dict[str, Any] = {
        "scenario": scn.get("scenario", "?"), "status": result.get("status"), "error": result.get("error"),
        "report_entity": rc.get("entity"), "report_category": rc.get("category"),
        "confidence": rep.get("confidence"), "tool_calls": totals.get("tool_calls"),
        "seconds": result.get("seconds"),
        "tokens": (totals.get("prompt_tokens") or 0) + (totals.get("completion_tokens") or 0),
        "prompt_tokens": totals.get("prompt_tokens"), "completion_tokens": totals.get("completion_tokens"),
        "job_id": result.get("job_id"), "seed": result.get("seed"),
    }
    if not key_rc:   # noise-only ticket: the right answer is "unknown" with low confidence
        ok = rc.get("category") == "unknown" and (rep.get("confidence") or 0) <= 0.3
        out.update(entity_ok=None, category_ok=ok, expected="unknown", jaccard=None, red_herring=False, passed=ok)
        return out

    ident = _strings(rc.get("identifiers") or {})
    key_ids = [v for k in ("change_id", "commit") if key_rc.get(k) for v in [key_rc[k]]] + \
              list(key_rc.get("change_ids") or [])
    entity_ok = (str(rc.get("entity", "")).strip().lower() == str(key_rc.get("entity", "")).strip().lower()) or \
        any(_same_id(i, k) for i in ident for k in key_ids)
    expected = expected_categories(key_rc)
    category_ok = rc.get("category") in expected

    key_chain = {c["event_id"] for c in scn.get("causal_chain", []) if c.get("event_id")}
    rep_chain = {c.get("event_id") for c in rep.get("causal_chain") or [] if c.get("event_id")}
    union = key_chain | rep_chain
    jaccard = round(len(key_chain & rep_chain) / len(union), 2) if union else 0.0

    cause_ids = set(rc.get("event_ids") or [])
    cause_names = {str(rc.get("entity", "")).lower(),
                   *(s.lower() for s in _strings(_without_location(rc.get("identifiers") or {})))}
    rh_hits = [h.get("type", "?") for h in red_herrings
               if (h.get("event_id") and h["event_id"] in cause_ids)
               or (h.get("entity") and str(h["entity"]).lower() in cause_names)]
    passed = bool(result.get("status") == "done" and entity_ok and category_ok and not rh_hits)
    out.update(entity_ok=entity_ok, category_ok=category_ok, expected="|".join(sorted(expected)),
               key_entity=key_rc.get("entity"), jaccard=jaccard, red_herring=bool(rh_hits),
               red_herring_types=rh_hits, passed=passed)
    return out


# ---------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------
def _yn(v: Any) -> str:
    return "n/a" if v is None else ("yes" if v else "no")


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _pass(v: Any) -> str:
    return "-" if v is None else ("**yes**" if v else "no")


def _critic(r: dict[str, Any]) -> str:
    first, final = r.get("critic_first"), r.get("critic_final")
    if not first:
        return "-"
    if not r.get("retried"):
        return first
    return f"{first} -> {final} (retried, kept {r.get('kept') or 'retry'})"


def markdown(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [f"# Triagent scorer: {meta['stamp']}", "",
             f"- investigator `{meta['models'].get('investigator')}`, critic `{meta['models'].get('critic')}`",
             f"- answer key `{meta['answer_key']}` (dcsim seed {meta['sim_seed']}), runs per scenario: "
             f"{meta['runs']}, model seed base {meta['seed']}", "",
             "Final pipeline output (investigator, critic review, retry only if on). Tool calls, seconds and tokens "
             "include the critic and the retry.", "",
             "| scenario | run | cause entity | category | chain overlap | red herring cited | confidence "
             "| tool calls | seconds | tokens | critic | pass |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["status"] != "done":
            lines.append(f"| {r['scenario']} | {r['run']} | {r['status']}: {str(r.get('error') or '')[:60]} "
                         f"| | | | | {_fmt(r.get('tool_calls'))} | {_fmt(r.get('seconds'), 0)} | {r.get('tokens') or '-'} "
                         f"| {_critic(r)} | no |")
            continue
        ent = f"{_yn(r['entity_ok'])} (`{r.get('report_entity')}`)"
        cat = f"{_yn(r['category_ok'])} (`{r.get('report_category')}`, want {r.get('expected')})"
        rh = "**HARD FAIL** " + ",".join(r.get("red_herring_types") or []) if r.get("red_herring") else "no"
        lines.append(f"| {r['scenario']} | {r['run']} | {ent} | {cat} | {_fmt(r.get('jaccard'))} | {rh} "
                     f"| {_fmt(r.get('confidence'))} | {_fmt(r.get('tool_calls'))} | {_fmt(r.get('seconds'), 0)} "
                     f"| {r.get('tokens'):,} | {_critic(r)} | {'**yes**' if r['passed'] else 'no'} |")

    lines += ["", "## Before and after the critic", "",
              "Before = the investigator's first report. After = the report the pipeline returns.", "",
              "| scenario | run | pass before | pass after | entity before -> after | category before -> after "
              "| chain overlap | confidence | critic (failed checks) | retried | tokens before -> after |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        b = r.get("before") or {}
        lines.append(
            f"| {r['scenario']} | {r['run']} | {_pass(b.get('passed'))} | {_pass(r.get('passed'))} "
            f"| `{b.get('report_entity')}` -> `{r.get('report_entity')}` "
            f"| {b.get('report_category')} -> {r.get('report_category')} "
            f"| {_fmt(b.get('jaccard'))} -> {_fmt(r.get('jaccard'))} "
            f"| {_fmt(b.get('confidence'))} -> {_fmt(r.get('confidence'))} "
            f"| {r.get('critic_first') or '-'}{' (' + ', '.join(r.get('critic_failed') or []) + ')' if r.get('critic_failed') else ''} "
            f"| {('kept ' + (r.get('kept') or 'retry')) if r.get('retried') else 'no'} "
            f"| {_fmt(b.get('tokens'))} -> {_fmt(r.get('tokens'))} |")

    lines += ["", "## Summary", "",
              "| scenario | pass before | pass after | cause entity | category | chain overlap (mean) | red herring "
              "| confidence (mean) | critic fail | retried | tool calls (mean) | seconds (mean) | tokens (mean) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["scenario"], []).append(r)

    def mean(xs: list[Any]) -> float | None:
        xs = [x for x in xs if isinstance(x, (int, float))]
        return sum(xs) / len(xs) if xs else None

    def summary_row(name: str, rs: list[dict[str, Any]]) -> str:
        n = len(rs)
        return (f"| {name} | {sum(bool((r.get('before') or {}).get('passed')) for r in rs)}/{n} "
                f"| {sum(bool(r.get('passed')) for r in rs)}/{n} "
                f"| {sum(bool(r.get('entity_ok')) for r in rs)}/{n} | {sum(bool(r.get('category_ok')) for r in rs)}/{n} "
                f"| {_fmt(mean([r.get('jaccard') for r in rs]))} | {sum(bool(r.get('red_herring')) for r in rs)}/{n} "
                f"| {_fmt(mean([r.get('confidence') for r in rs]))} "
                f"| {sum(r.get('critic_first') == 'fail' for r in rs)}/{n} | {sum(bool(r.get('retried')) for r in rs)}/{n} "
                f"| {_fmt(mean([r.get('tool_calls') for r in rs]), 1)} "
                f"| {_fmt(mean([r.get('seconds') for r in rs]), 0)} | {_fmt(mean([r.get('tokens') for r in rs]), 0)} |")

    for name, rs in by.items():
        lines.append(summary_row(name, rs))
    lines.append(summary_row("**all**", rows))
    failed = [r for r in rows if r["status"] != "done"]
    if failed:
        lines += ["", f"{len(failed)} run(s) did not finish: " +
                  ", ".join(f"{r['scenario']}#{r['run']} ({r['status']})" for r in failed)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------
def main() -> int:
    envf = env_file()
    ap = argparse.ArgumentParser(description="Score Triagent against the dcsim answer key.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--answer-key", type=Path, default=ROOT / "t" / "scenario.json")
    ap.add_argument("--runs", type=int, default=3, help="runs per scenario")
    ap.add_argument("--scenario", action="append", help="only these scenarios (repeatable); 'none' for noise tickets")
    ap.add_argument("--server", default="http://localhost:8080")
    ap.add_argument("--no-spawn", action="store_true", help="never start the server; fail if it is not running")
    ap.add_argument("--parallel", type=int, default=1, help="investigations in flight at once")
    ap.add_argument("--timeout", type=float, default=900, help="seconds per investigation")
    ap.add_argument("--seed", type=int, default=1000, help="model seed for run 1; run k uses seed+k-1")
    ap.add_argument("--es-url", default=os.environ.get("ES_URL") or envf.get("ES_URL") or "http://localhost:9200")
    ap.add_argument("--es-api-key", default=os.environ.get("ES_API_KEY") or envf.get("ES_API_KEY") or None)
    ap.add_argument("--namespace", default="dal1")
    ap.add_argument("--ingest", action="store_true", help="delete the dcsim data streams and reload all scenarios")
    ap.add_argument("--sim-seed", type=int, default=7, help="dcsim --seed for --ingest")
    ap.add_argument("--start", default=DEFAULT_START, help="dcsim --start for --ingest")
    ap.add_argument("--duration", default="12h", help="dcsim --duration for --ingest")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "t" / "scores")
    args = ap.parse_args()
    args.server = args.server.rstrip("/")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    es = ES(args.es_url, args.es_api_key)
    st, _ = es.call("GET", "/")
    if st != 200:
        raise SystemExit(f"Elasticsearch not reachable at {args.es_url} (docker compose up -d?)")
    key = load_key(es, args)
    ns = key.get("namespace", args.namespace)
    scenarios = [s for s in key.get("scenarios", []) if not args.scenario or s.get("scenario") in args.scenario]
    if not scenarios:
        raise SystemExit("no scenarios selected")

    proc = ensure_server(args, args.out_dir / f"{stamp}-server.log")
    try:
        _, health = http("GET", args.server + "/health")
        jobs = []
        for s in scenarios:
            doc = es.ticket_doc(ns, s["ticket"]["event_id"]) if s.get("ticket") else None
            if doc is None:
                print(f"skipping {s.get('scenario')}: no ticket", file=sys.stderr)
                continue
            base = payload_from_doc(doc, ns)
            jobs += [(s, base, run) for run in range(1, args.runs + 1)]
        tag = secrets.token_hex(2)
        print(f"{len(jobs)} investigations ({len(scenarios)} scenarios x {args.runs} runs), "
              f"{args.parallel} at a time", file=sys.stderr)

        rows: list[dict[str, Any]] = []
        with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
            futs = {pool.submit(run_one, args, base, run, tag): (s, run) for s, base, run in jobs}
            for fut in cf.as_completed(futs):
                s, run = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001 - one broken run must not lose the table
                    res = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "seconds": 0.0}
                row = dict(score_before_after(s, key.get("red_herrings", []), res), run=run)
                rows.append(row)
                print(f"  {row['scenario']:<18} run {run}: {row['status']:<7} "
                      f"before={(row.get('before') or {}).get('passed')} critic={row.get('critic_first')} "
                      f"retried={row.get('retried')} pass={row.get('passed')} "
                      f"entity={row.get('report_entity')} category={row.get('report_category')} "
                      f"tools={row.get('tool_calls')} {row.get('seconds')}s", file=sys.stderr)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    order = {s["scenario"]: i for i, s in enumerate(scenarios)}
    rows.sort(key=lambda r: (order.get(r["scenario"], 99), r["run"]))
    meta = {"stamp": stamp, "models": (health or {}).get("models", {}), "answer_key": str(args.answer_key),
            "sim_seed": key.get("seed"), "runs": args.runs, "seed": args.seed}
    md = markdown(rows, meta)
    sys.stdout.write(md)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{stamp}.md").write_text(md, encoding="utf-8")
    (args.out_dir / f"{stamp}.json").write_text(json.dumps({"meta": meta, "rows": rows}, indent=2, default=str),
                                                encoding="utf-8")
    print(f"\nwrote {args.out_dir / (stamp + '.md')}", file=sys.stderr)
    return 0 if rows and all(r["status"] == "done" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
