"""In-memory stand-ins so every section can be built and tested without Elasticsearch or NVIDIA.

For the app (via fake_server.py)
  TEMPLATES          three scripted investigations (cooling pass; config with a critic retry; deploy pass) whose
                     trace, report, critic and remediation match contracts.py exactly
  fake_pipeline      plays a template onto a main.Job with realistic pacing (TRIAGENT_FAKE_SPEED, default 1)
  fake_ask           answers a follow-up (POST /jobs/{id}/ask) from the job's own report, same Action states
  preload(registry)  fills a registry with finished, retried, failed and one live job
  sample_score_run() a ScoreRun shaped like the scorer's output

For backend sections (investigator, critic, remediate, scorer work without the cluster or the API)
  FakeTools          the tools.ESTools dispatch() interface over in-memory dcsim docs, same argument names and caps
  FakeTools.from_dcsim(...)  generates a small deterministic dcsim run into t/fixtures/ and loads it
  fake_chat(script)  a drop-in for llm.chat that replays scripted replies (tool calls or text)

    python3 server/fakes.py            # build the dcsim fixture and print a FakeTools smoke summary
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import llm
import tools as real_tools
from contracts import MAX_AGG_TOP, MAX_SEARCH_SIZE, MAX_SERIES_POINTS, TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "t" / "fixtures"
UTC = timezone.utc


def eid(n: int) -> str:
    """Deterministic event ids for scripted jobs (shaped like dcsim's uuids)."""
    return f"00000000-0000-4000-8000-{n:012d}"


# =============================================================================================
# scripted investigations for the app
# =============================================================================================
def _llm(tool: str | None, prompt: int, completion: int, ms: int) -> tuple[str, dict[str, Any]]:
    return "llm", {"model": "nvidia/nemotron-3.5-super-vl-120b-a12b", "prompt_tokens": prompt,
                   "completion_tokens": completion, "latency_ms": ms, "tool_name": tool}


def _critic_llm(prompt: int, completion: int, ms: int) -> tuple[str, dict[str, Any]]:
    return "llm", {"model": "nvidia/nemotron-3-ultra-550b-a55b", "prompt_tokens": prompt,
                   "completion_tokens": completion, "latency_ms": ms, "tool_name": None}


def _tool(no: int, name: str, args: dict[str, Any], result: Any) -> tuple[str, dict[str, Any]]:
    return "tool", {"call_no": no, "name": name, "arguments": args,
                    "result": json.dumps(result, separators=(",", ":"))[:500]}


def _row(n: int, ts: str, code: str, msg: str, **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "event.id": eid(n), "event.code": code, "log.level": extra.pop("level", "info"),
            **extra, "message": msg}


def _checks(failed: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    failed = failed or {}
    src = {"ids_exist": "code", "chain_time_order": "code", "adjacent_steps_linked": "code"}
    return {name: {"ok": name not in failed, "detail": failed.get(name, "ok"), "source": src.get(name, "model")}
            for name in ("ids_exist", "chain_time_order", "root_cause_concrete", "adjacent_steps_linked",
                         "rejections_listed", "category_matches", "confidence_consistent")}


def _review(failed: dict[str, str] | None = None, corrections: list[str] | None = None, cited: int = 5) -> dict:
    return {"verdict": "fail" if failed else "pass", "failed": list(failed or {}), "checks": _checks(failed),
            "corrections": corrections or [], "model": "nvidia/nemotron-3-ultra-550b-a55b",
            "docs_checked": cited, "docs_cited": cited}


COOLING = {
    "key": "cooling",
    "match": ("notready", "thermal", "throttl", "crac", "temperature"),
    "ticket": {"ticket_id": "INC6376805", "title": "KubeNodeNotReady: 18 nodes NotReady in k8s-prod-2",
               "description": "KubeNodeNotReady firing for 18 nodes in k8s-prod-2 (rows C and D).", "priority": "P1",
               "created": "2026-11-07T16:53:00.000Z", "service": None, "host": "cmp-c02-01",
               "labels": {"row": "C", "rack": "C02", "cluster": "k8s-prod-2"}, "namespace": "dal1",
               "source_event_id": eid(100)},
    "investigate": [
        (1.0, *_llm("aggregate", 6339, 412, 6100)),
        (0.5, *_tool(1, "aggregate", {"field": "event.code", "filters": {"labels.row": "C"}, "lookback": "1h"},
                     {"buckets": [{"value": "K8S-NODE-NOTREADY", "count": 18}, {"value": "SYS-THERMAL-SHUTDOWN",
                                                                                "count": 18}]})),
        (0.5, *_tool(2, "find_changes", {"entities": ["C", "k8s-prod-2"], "lookback": "6h"},
                     {"total": 14, "returned": 14, "rows": [_row(101, "2026-11-07T15:02:00.000Z", "CHG-IMPLEMENTED",
                                                                 "CHG0005788 implemented: Rotate API credentials for "
                                                                 "metrics exporter", user="mkhan")]})),
        (0.3, "assistant", {"text": "Row C hosts shut down thermally; no change touches row C. Checking the CRAC."}),
        (4.0, *_llm("search_events", 12880, 530, 9200)),
        (0.5, *_tool(3, "search_events", {"dataset": "facility.alarms", "filters": {"labels.row": "C"},
                                          "lookback": "6h"},
                     {"total": 3, "rows": [_row(102, "2026-11-07T13:50:00.000Z", "FAC-CRAC-HIGH-PRESSURE",
                                                "crac-c: compressor 1 high head pressure", observer="crac-c",
                                                level="warning"),
                                           _row(103, "2026-11-07T16:21:00.000Z", "FAC-CRAC-COMPRESSOR-TRIP",
                                                "crac-c: compressor 1 tripped on high pressure lockout",
                                                observer="crac-c", level="error")]})),
        (0.5, "finding", {"finding": "crac-c compressor high head pressure from 13:50, trip at 16:21",
                          "event_ids": [eid(102), eid(103)], "finding_kind": "candidate_cause"}),
        (4.0, *_llm("metric_series", 19210, 488, 8800)),
        (0.5, *_tool(4, "metric_series", {"dataset": "bmc.sensors", "field": "datacenter.bmc.inlet_temp_c",
                                          "filters": {"labels.row": "C"}, "lookback": "6h"},
                     {"points": [["2026-11-07T13:00Z", 22.1], ["2026-11-07T16:30Z", 38.9]],
                      "summary": {"min": 21.8, "max": 41.2}})),
        (0.5, "finding", {"finding": "Row C inlet temperature climbs from 22 to 39 C after the trip",
                          "event_ids": [eid(104)], "finding_kind": "precursor"}),
        (0.5, "finding", {"finding": "Rejected CHG0005788: credential rotation, unrelated to row C cooling",
                          "event_ids": [eid(101)], "finding_kind": "rejected"}),
        (5.0, *_llm("submit_report", 26510, 1290, 14100)),
        (0.3, "tool", {"call_no": 5, "name": "submit_report", "arguments": {"problem": "..."}, "result": "(submitted)"}),
        (0.3, "verify", {"checked": 6, "dropped": [], "unverified": []}),
    ],
    "report": {
        "problem": "18 hosts in rows C and D of k8s-prod-2 shut down thermally and went NotReady.",
        "root_cause": {"summary": "CRAC unit crac-c lost compressor 1 to a high head pressure lockout at 16:21 after "
                                  "warning since 13:50; row C inlet temperatures rose until hosts throttled and shut "
                                  "down.", "category": "cooling", "entity": "crac-c", "entity_type": "crac_unit",
                       "identifiers": {"row": "C", "compressor": "1"}, "event_ids": [eid(102), eid(103)]},
        "causal_chain": [
            {"timestamp": "2026-11-07T13:50:00.000Z", "event_id": eid(102), "what": "crac-c high head pressure",
             "why_it_matters": "first sign of the compressor fault"},
            {"timestamp": "2026-11-07T16:21:00.000Z", "event_id": eid(103), "what": "crac-c compressor trip",
             "why_it_matters": "row C loses cooling capacity"},
            {"timestamp": "2026-11-07T16:40:00.000Z", "event_id": eid(104), "what": "row C inlet above 35 C",
             "why_it_matters": "hosts begin thermal throttling"},
            {"timestamp": "2026-11-07T16:50:00.000Z", "event_id": eid(105), "what": "first thermal shutdown",
             "why_it_matters": "nodes drop out and go NotReady"}],
        "rejected_candidates": [{"what": "CHG0005788 credential rotation", "event_ids": [eid(101)],
                                 "why_rejected": "touches the metrics exporter, not row C or its cooling"}],
        "affected": {"rows": ["C", "D"], "cluster": "k8s-prod-2", "hosts": 18},
        "confidence": 0.95, "open_questions": []},
    "critic": [(2.0, *_critic_llm(5620, 610, 13000))],
    "critic_first": _review(cited=6),
    "remediation": {
        "covered": True, "category": "cooling", "runbook": "runbooks/cooling.md", "source": "model",
        "entity_types": ["CRAC unit", "row", "rack", "host"],
        "immediate": ["In the BMS console, open the alarms for CRAC unit `crac-c` and acknowledge them; read the "
                      "compressor status and supply air temperature.",
                      "In the BMS console, lower the supply setpoint and raise fan speed on the neighbouring CRAC "
                      "units to carry row C.",
                      "Move workloads off throttling host cmp-c02-01: `kubectl cordon cmp-c02-01` then "
                      "`kubectl drain cmp-c02-01 --ignore-daemonsets --delete-emptydir-data`."],
        "verify": ["BMS trend for CRAC unit crac-c shows supply air back under 20 °C and the compressor running.",
                   "`ipmitool -I lanplus -H cmp-c02-01-bmc sdr type Temperature` shows inlet under 27 °C."],
        "prevent": ["Keep N+1 CRAC capacity per row and test failover quarterly."],
        "rollback_risk": "Repeated compressor resets on a faulted unit can destroy the compressor."},
}

CONFIG = {
    "key": "config",
    "match": ("5xx", "error rate", "503", "haproxy", "lb-"),
    "ticket": {"ticket_id": "INC7416175", "title": "web-frontend: HTTP 5xx error rate above 25%",
               "description": "Error-rate SLO alert for web-frontend behind lb-1.", "priority": "P1",
               "created": "2026-11-07T16:19:00.000Z", "service": "web-frontend", "host": None, "labels": {},
               "namespace": "dal1", "source_event_id": eid(200)},
    "investigate": [
        (1.0, *_llm("find_changes", 6120, 380, 5400)),
        (0.5, *_tool(1, "find_changes", {"entities": ["web-frontend", "lb-1"], "lookback": "3h"},
                     {"total": 6, "rows": [_row(201, "2026-11-07T15:13:00.000Z", "CICD-CONFIG-PUSH",
                                                "haproxy/conf.d/backends.cfg: backend web-frontend maxconn 2000 -> 200",
                                                user="jdoe", **{"change.id": "CHG0005604"})]})),
        (0.3, "finding", {"finding": "Config push lowered web-frontend maxconn 2000 -> 200 on lb-1 at 15:13",
                          "event_ids": [eid(201)], "finding_kind": "candidate_cause"}),
        (4.0, *_llm("search_events", 14100, 520, 8100)),
        (0.5, *_tool(2, "search_events", {"dataset": "network.lb", "filters": {"observer.name": "lb-1"},
                                          "lookback": "3h"},
                     {"total": 9, "rows": [_row(202, "2026-11-07T15:14:15.000Z", "LB-RELOAD",
                                                "haproxy: reloaded", observer="lb-1"),
                                           _row(203, "2026-11-07T15:46:00.000Z", "LB-QUEUE-HIGH",
                                                "backend web-frontend queue 412", observer="lb-1", level="warning")]})),
        (5.0, *_llm("submit_report", 20500, 1100, 12400)),
        (0.3, "tool", {"call_no": 3, "name": "submit_report", "arguments": {"problem": "..."}, "result": "(submitted)"}),
        (0.3, "verify", {"checked": 4, "dropped": [], "unverified": []}),
    ],
    "report": {
        "problem": "web-frontend returns 5xx above 25% through lb-1.",
        "root_cause": {"summary": "Config push under CHG0005604 lowered the web-frontend backend maxconn on lb-1 "
                                  "from 2000 to 200; requests queued and timed out as traffic grew.",
                       "category": "config", "entity": "lb-1", "entity_type": "load_balancer",
                       "identifiers": {"change_id": "CHG0005604", "file": "haproxy/conf.d/backends.cfg",
                                       "user": "jdoe"}, "event_ids": [eid(201)]},
        "causal_chain": [
            {"timestamp": "2026-11-07T15:13:00.000Z", "event_id": eid(201), "what": "maxconn lowered to 200",
             "why_it_matters": "caps concurrent backend connections"},
            {"timestamp": "2026-11-07T15:14:15.000Z", "event_id": eid(202), "what": "lb-1 reload",
             "why_it_matters": "new limit takes effect"},
            {"timestamp": "2026-11-07T15:46:00.000Z", "event_id": eid(203), "what": "backend queue high",
             "why_it_matters": "requests wait for a free slot"}],
        "rejected_candidates": [{"what": "search v3.29.5 deploy", "event_ids": [eid(204)],
                                 "why_rejected": "different service and load balancer"}],
        "affected": {"service": "web-frontend", "load_balancer": "lb-1"}, "confidence": 0.95, "open_questions": []},
    "critic": [(2.0, *_critic_llm(4900, 540, 11800))],
    "critic_first": _review({"confidence_consistent": "0.95 while open_questions are empty but the chain stops "
                                                      "before the 5xx spike"},
                            ["Add the first 5xx error event after the queue grew and keep confidence consistent."], 4),
    "retry": {
        "steps": [
            (1.0, *_llm("search_events", 9800, 400, 7200)),
            (0.5, *_tool(1, "search_events", {"filters": {"service.name": "web-frontend"}, "min_level": "error",
                                              "lookback": "1h"},
                         {"total": 31, "rows": [_row(205, "2026-11-07T16:16:00.000Z", "LB-NO-SERVER",
                                                     "503: backend web-frontend has no server available",
                                                     observer="lb-1", level="error")]})),
            (4.0, *_llm("submit_report", 15200, 1050, 11900)),
            (0.3, "tool", {"call_no": 2, "name": "submit_report", "arguments": {"problem": "..."},
                           "result": "(submitted)"}),
            (0.3, "verify", {"checked": 5, "dropped": [], "unverified": []}),
        ],
        "report_patch": {"causal_chain_append": {"timestamp": "2026-11-07T16:16:00.000Z", "event_id": eid(205),
                                                 "what": "first 503 no server available",
                                                 "why_it_matters": "the user-visible failure"},
                         "confidence": 0.9},
        "critic": [(2.0, *_critic_llm(5300, 480, 10200))],
        "critic_final": _review(cited=5),
        "kept": "retry",
    },
    "remediation": {
        "covered": True, "category": "config", "runbook": "runbooks/config.md", "source": "model",
        "entity_types": ["load balancer", "service", "host", "configuration file", "change", "commit"],
        "immediate": ["Identify the change: change `CHG0005604`, file `haproxy/conf.d/backends.cfg`; read the diff "
                      "with `git show <commit>`.",
                      "On load balancer lb-1, restore the previous file, validate and reload: "
                      "`haproxy -c -f /etc/haproxy/haproxy.cfg` then `systemctl reload haproxy`."],
        "verify": ["`haproxy -c -f /etc/haproxy/haproxy.cfg` shows the old maxconn and the 5xx rate returns to "
                   "baseline."],
        "prevent": ["Validate configuration in CI and require peer review for production config."],
        "rollback_risk": "A reload of load balancer lb-1 briefly drops idle connections."},
}

DEPLOY = {
    "key": "deploy",
    "match": ("crashloop", "oom", "memory", "deploy", "release"),
    "ticket": {"ticket_id": "INC4868166", "title": "web-frontend: pods CrashLoopBackOff, 5xx above SLO",
               "description": "KubePodCrashLooping and error-rate alerts for web-frontend in k8s-prod-1.",
               "priority": "P1", "created": "2026-11-07T18:02:00.000Z", "service": "web-frontend", "host": None,
               "labels": {}, "namespace": "dal1", "source_event_id": eid(300)},
    "investigate": [
        (1.0, *_llm("find_changes", 6050, 350, 5200)),
        (0.5, *_tool(1, "find_changes", {"entities": ["web-frontend"], "lookback": "6h"},
                     {"total": 4, "rows": [_row(301, "2026-11-07T15:03:00.000Z", "CICD-DEPLOY-SUCCESS",
                                                "Deploy web-frontend v1.4.0 completed: 6/6 pods updated",
                                                service="web-frontend", **{"service.version": "v1.4.0"})]})),
        (4.0, *_llm("metric_series", 12900, 470, 9100)),
        (0.5, *_tool(2, "metric_series", {"dataset": "system.host", "field": "datacenter.host.mem_used_pct",
                                          "filters": {"service.name": "web-frontend"}, "lookback": "6h"},
                     {"points": [["2026-11-07T15:00Z", 51.0], ["2026-11-07T17:55Z", 97.4]]})),
        (0.5, "finding", {"finding": "Memory climbs linearly from the v1.4.0 rollout at 15:03",
                          "event_ids": [eid(301)], "finding_kind": "candidate_cause"}),
        (5.0, *_llm("submit_report", 19800, 1150, 12000)),
        (0.3, "tool", {"call_no": 3, "name": "submit_report", "arguments": {"problem": "..."}, "result": "(submitted)"}),
        (0.3, "verify", {"checked": 4, "dropped": [], "unverified": []}),
    ],
    "report": {
        "problem": "web-frontend pods are OOMKilled and crash-looping; 5xx above SLO.",
        "root_cause": {"summary": "Release v1.4.0 of web-frontend leaks heap; memory grew until containers were "
                                  "OOMKilled.", "category": "deploy", "entity": "web-frontend",
                       "entity_type": "service", "identifiers": {"version": "v1.4.0", "previous_version": "v1.3.1",
                                                                 "change_id": "CHG0005636"},
                       "event_ids": [eid(301)]},
        "causal_chain": [
            {"timestamp": "2026-11-07T15:03:00.000Z", "event_id": eid(301), "what": "v1.4.0 rollout complete",
             "why_it_matters": "memory growth starts"},
            {"timestamp": "2026-11-07T17:31:00.000Z", "event_id": eid(302), "what": "first OOMKilled",
             "why_it_matters": "pods start restarting"},
            {"timestamp": "2026-11-07T17:59:00.000Z", "event_id": eid(303), "what": "CrashLoopBackOff",
             "why_it_matters": "too few healthy pods"}],
        "rejected_candidates": [{"what": "auth-svc v2.6.4 deploy", "event_ids": [eid(304)],
                                 "why_rejected": "different service; no memory growth there"}],
        "affected": {"service": "web-frontend", "cluster": "k8s-prod-1"}, "confidence": 0.9, "open_questions": []},
    "critic": [(2.0, *_critic_llm(4700, 450, 9800))],
    "critic_first": _review(cited=4),
    "remediation": {
        "covered": True, "category": "deploy", "runbook": "runbooks/deploy.md", "source": "model",
        "entity_types": ["service", "deployment", "version", "commit"],
        "immediate": ["Confirm the rollout for service web-frontend: `kubectl -n prod rollout history "
                      "deployment/web-frontend`.",
                      "Roll service web-frontend back to v1.3.1: `kubectl -n prod rollout undo "
                      "deployment/web-frontend`."],
        "verify": ["`kubectl -n prod rollout status deployment/web-frontend` completes on v1.3.1."],
        "prevent": ["Canary new versions and roll back automatically on SLO burn."],
        "rollback_risk": "Rolling back breaks if v1.4.0 ran a non-reversible migration."},
}

TEMPLATES = [COOLING, CONFIG, DEPLOY]


def pick_template(ticket: dict[str, Any]) -> dict[str, Any]:
    """Most specific first: thermal/NotReady -> cooling, crashloop/OOM/deploy -> deploy, 5xx/LB -> config."""
    text = f"{ticket.get('title', '')} {ticket.get('description', '')}".lower()
    for tpl in (COOLING, DEPLOY, CONFIG):
        if any(w in text for w in tpl["match"]):
            return tpl
    return TEMPLATES[sum(map(ord, str(ticket.get("ticket_id", "")))) % len(TEMPLATES)]


def _speed() -> float:
    try:
        return max(0.01, float(os.environ.get("TRIAGENT_FAKE_SPEED", "1")))
    except ValueError:
        return 1.0


async def _play(job: Any, steps: list[tuple], speed: float | None) -> None:
    for delay, kind, fields in steps:
        if speed:
            await asyncio.sleep(delay / speed)
        job.trace.add(kind, **json.loads(json.dumps(fields)))


def _retry_report(tpl: dict[str, Any]) -> dict[str, Any]:
    rep = json.loads(json.dumps(tpl["report"]))
    patch = tpl["retry"]["report_patch"]
    if "causal_chain_append" in patch:
        rep["causal_chain"].append(patch["causal_chain_append"])
    if "confidence" in patch:
        rep["confidence"] = patch["confidence"]
    return rep


async def fake_ask(job: Any, action: dict[str, Any], settings: Any, speed: float | None = None) -> None:
    """Follow-up with the same Action states as main.run_ask: queued -> answering -> answered. The answer is
    built from the job's report: a question naming a rejected candidate gets that candidate's reasoning and ids."""
    speed = _speed() if speed is None else speed
    report = job.report or {}
    q = (action.get("question") or "").lower()
    rejected = report.get("rejected_candidates") or []
    pick = next((r for r in rejected if any(w in q for w in re.findall(r"[a-z0-9][a-z0-9-]{3,}",
                                                                      r.get("what", "").lower()))), None)
    pick = pick or (rejected[0] if rejected and ("reject" in q or "rule" in q or "why not" in q) else None)
    if pick:
        ids = list(pick.get("event_ids") or [])
        answer = f"I ruled out {pick['what']}: {pick['why_rejected']}"
    else:
        rc = report.get("root_cause") or {}
        ids = list(rc.get("event_ids") or [])
        answer = f"The cause is {rc.get('entity', 'unknown')}: {rc.get('summary', '')}"
    job.trace.phase = "ask"
    try:
        action["state"] = "answering"
        job.trace.add("action", action=dict(action))
        job.trace.add("info", message="follow-up started (fake)")
        steps = [(1.5, "llm", {"model": "fake/nemotron-super", "prompt_tokens": 9000, "completion_tokens": 300,
                               "latency_ms": 1400, "tool_name": "get_doc" if ids else "answer_question"})]
        if ids:
            steps.append((0.5, "tool", {"call_no": 1, "name": "get_doc", "arguments": {"event_id": ids[0]},
                                        "result": json.dumps({"found": True, "event_id": ids[0]})}))
        steps.append((1.0, "tool", {"call_no": len(steps), "name": "answer_question",
                                    "arguments": {"answer": answer, "event_ids": ids}, "result": "(answered)"}))
        await _play(job, steps, speed)
        action.update(state="answered", answer=answer, event_ids=ids)
    finally:
        job.trace.add("action", action=dict(action))
        job.trace.phase = None


async def fake_pipeline(job: Any, settings: Any, speed: float | None = None, fail_after: int | None = None) -> None:
    """Play a scripted investigation onto job, in the same order and statuses as main.run_pipeline.

    speed: playback multiplier (None reads TRIAGENT_FAKE_SPEED; 0 means instant). fail_after: raise after N steps.
    """
    tpl = pick_template(job.ticket)
    speed = _speed() if speed is None else speed
    try:
        job.set_status("running")
        job.trace.phase = "investigate"
        job.trace.add("info", message="investigation started (fake): template=" + tpl["key"])
        steps = tpl["investigate"] if fail_after is None else tpl["investigate"][:fail_after]
        await _play(job, steps, speed)
        if fail_after is not None:
            raise RuntimeError("RateLimitError: 429 Too Many Requests (fake failure)")
        job.report = json.loads(json.dumps(tpl["report"]))
        job.report_initial = job.report
        job.initial_seconds = time.time() - job.created
        job.totals_initial = job.trace.totals({"investigate"})

        job.trace.phase = "critic"
        job.set_status("critic")
        await _play(job, tpl["critic"], speed)
        first = tpl["critic_first"]
        job.trace.add("critic", verdict=first["verdict"], failed=first["failed"], corrections=first["corrections"])
        job.critic = {"first": first, "final": first, "retried": False}
        if first["verdict"] == "fail" and "retry" in tpl:
            retry = tpl["retry"]
            job.trace.phase = "retry"
            job.set_status("running", retry=True)
            await _play(job, retry["steps"], speed)
            job.report = _retry_report(tpl)
            job.trace.phase = "critic_retry"
            job.set_status("critic", retry=True)
            await _play(job, retry["critic"], speed)
            final = retry["critic_final"]
            job.trace.add("critic", verdict=final["verdict"], failed=final["failed"], corrections=final["corrections"])
            job.critic = {"first": first, "final": final, "retried": True, "kept": retry["kept"]}
        job.trace.phase = None

        job.set_status("remediating")
        if speed:
            await asyncio.sleep(1.5 / speed)
        rem = tpl["remediation"]
        job.trace.add("llm", model="nvidia/nemotron-3.5-lightning-30b-a3b", prompt_tokens=2400,
                      completion_tokens=380, latency_ms=2900, tool_name=None)
        job.trace.add("remediation", runbook=rem["runbook"], covered=rem["covered"], source=rem["source"],
                      steps=len(rem["immediate"]))
        job.remediation = rem
        job.finished = time.time()
        job.set_status("done", totals=job.trace.totals())
    except Exception as exc:  # noqa: BLE001 - mirror main.run_pipeline: always end terminal
        job.error = f"{type(exc).__name__}: {exc}"
        job.trace.phase = None
        job.trace.add("error", message=job.error)
        job.finished = time.time()
        job.set_status("failed")


async def preload(registry: Any, loop: asyncio.AbstractEventLoop, job_cls: Any) -> list[Any]:
    """Finished jobs for every template, one failed job, and one live job that streams for the Live screen."""
    jobs = []
    now = time.time()
    plan = [(COOLING, 2400, None), (CONFIG, 1500, None), (DEPLOY, 900, 3)]
    for i, (tpl, ago, fail_after) in enumerate(plan):
        ticket = dict(tpl["ticket"])
        job = job_cls(job_id=f"job-fake{i:08d}", ticket=ticket, loop=loop)
        job.created = now - ago
        job.set_status("queued")
        await fake_pipeline(job, None, speed=0, fail_after=fail_after)
        job.finished = job.created + 60 + 40 * i
        registry.jobs[job.job_id] = job
        registry.by_ticket[ticket["ticket_id"]] = job.job_id
        jobs.append(job)
    live_ticket = dict(DEPLOY["ticket"], ticket_id="INC9000001", created=datetime.now(UTC).isoformat()
                       .replace("+00:00", "Z"))
    live = job_cls(job_id="job-fakelive0001", ticket=live_ticket, loop=loop)
    live.set_status("queued")
    registry.jobs[live.job_id] = live
    registry.by_ticket[live_ticket["ticket_id"]] = live.job_id
    task = loop.create_task(fake_pipeline(live, None))
    registry.tasks.add(task)
    task.add_done_callback(registry.tasks.discard)
    jobs.append(live)
    return jobs


def sample_score_run() -> dict[str, Any]:
    """A ScoreRun shaped like server/scorer.py output (two scenarios, one run each)."""
    def row(scenario: str, passed: bool, entity: str, category: str, tokens: int, secs: float, retried: bool) -> dict:
        return {"scenario": scenario, "run": 1, "status": "done", "passed": passed, "entity_ok": passed,
                "category_ok": True, "report_entity": entity, "report_category": category, "jaccard": 0.4,
                "red_herring": False, "confidence": 0.9, "tool_calls": 14, "seconds": secs, "tokens": tokens,
                "critic_first": "fail" if retried else "pass", "critic_final": "pass", "retried": retried,
                "kept": "retry" if retried else None,
                "before": {"passed": passed, "report_entity": entity, "tokens": tokens // 2}}
    return {"meta": {"stamp": "20261108T000000Z", "runs": 1, "models": {"investigator": "fake", "critic": "fake"},
                     "fake": True},
            "rows": [row("cooling_failure", True, "crac-c", "cooling", 132000, 52.0, False),
                     row("bad_config_change", True, "lb-1", "config", 251000, 140.0, True)]}


# =============================================================================================
# FakeTools: ESTools.dispatch() over in-memory dcsim docs
# =============================================================================================
_LEVELS = real_tools.LEVELS


def _match_value(have: Any, want: Any) -> bool:
    haves = {str(x) for x in (have if isinstance(have, list) else [have]) if x is not None}
    wants = {str(x) for x in (want if isinstance(want, list) else [want])}
    return bool(haves & wants)


def _text_match(msg: str, query: str, operator: str) -> bool:
    msg = (msg or "").lower()
    if re.search(r"\s(OR|\|)\s", query):
        return any(w.lower() in msg for w in re.split(r"\s+(?:OR|\|)\s+", query) if w)
    words = [w.lower() for w in query.split() if w not in ("AND", "+")]
    return any(w in msg for w in words) if operator == "or" else all(w in msg for w in words)


class FakeTools:
    """Same dispatch(name, args) contract, argument names, caps and result shapes as tools.ESTools.

    It filters in Python instead of querying Elasticsearch: exact-match filters, simple text matching, no scoring.
    expand_scope is derived from the docs' labels and the naming scheme, like topology.py.
    """

    def __init__(self, docs: list[dict[str, Any]], namespace: str = "dal1", anchor: str | None = None) -> None:
        self.ns = namespace
        self.docs = sorted(docs, key=lambda d: d["@timestamp"])
        self.by_id = {d["event"]["id"]: d for d in self.docs}
        self.anchor = real_tools.parse_ts(anchor) if anchor else real_tools.parse_ts(self.docs[-1]["@timestamp"])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @classmethod
    def from_file(cls, path: str | Path, **kw: Any) -> "FakeTools":
        with open(path, encoding="utf-8") as fh:
            return cls([json.loads(ln) for ln in fh if ln.strip()], **kw)

    @classmethod
    def from_dcsim(cls, scenario: str = "memory_leak", seed: int = 7, start: str = "2026-11-07T08:00:00Z",
                   duration: str = "6h", **kw: Any) -> "FakeTools":
        """Generate (once) a small deterministic dcsim run into t/fixtures/ and load it."""
        FIXTURES.mkdir(parents=True, exist_ok=True)
        stem = FIXTURES / f"{scenario}-s{seed}"
        events = stem.with_suffix(".ndjson")
        if not events.exists():
            subprocess.run([sys.executable, str(ROOT / "sim" / "dcsim.py"), "--scenario", scenario, "--seed", str(seed),
                            "--start", start, "--duration", duration, "--rows", "2", "--racks-per-row", "2",
                            "--hosts-per-rack", "3", "--format", "docs", "--out", str(events),
                            "--answer-key", str(stem) + "-key.json", "--catalog", str(stem) + "-catalog.json"],
                           check=True, stderr=subprocess.DEVNULL)
        return cls.from_file(events, **kw)

    # ---- selection --------------------------------------------------------------------------
    def _window(self, args: dict[str, Any], default: str) -> tuple[datetime, datetime]:
        end = real_tools.parse_ts(args["end"]) if args.get("end") else self.anchor
        start = real_tools.parse_ts(args["start"]) if args.get("start") else \
            end - real_tools.parse_dur(args.get("lookback"), default)
        return start, end

    def _select(self, args: dict[str, Any], default: str = "1h", kind: str = "logs",
                datasets: tuple[str, ...] | None = None, operator: str = "and") -> list[dict[str, Any]]:
        start, end = self._window(args, default)
        ds_pat = (args.get("dataset") or "*").removeprefix("logs-").removeprefix("metrics-")
        rx = re.compile("^" + re.escape(ds_pat).replace(r"\*", ".*") + "$")
        flt = {real_tools.ALIASES.get(k, k): v for k, v in (args.get("filters") or {}).items() if v is not None}
        exc = {real_tools.ALIASES.get(k, k): v for k, v in (args.get("exclude") or {}).items() if v is not None}
        lvl = args.get("min_level")
        out = []
        for d in self.docs:
            ts = real_tools.parse_ts(d["@timestamp"])
            if ts < start or ts > end:
                continue
            dtype, dataset = d["data_stream"]["type"], d["data_stream"]["dataset"]
            if kind != "all" and dtype != kind:
                continue
            if datasets is not None:
                if dataset not in datasets:
                    continue
            elif not rx.match(dataset):
                continue
            if any(not _match_value(real_tools._get(d, f), v) for f, v in flt.items()):
                continue
            if any(_match_value(real_tools._get(d, f), v) for f, v in exc.items()):
                continue
            if lvl and d["log"]["level"] not in _LEVELS[_LEVELS.index(lvl):]:
                continue
            if args.get("query") and not _text_match(d.get("message", ""), str(args["query"]), operator):
                continue
            out.append(d)
        return out

    # ---- tools ------------------------------------------------------------------------------
    def search_events(self, args: dict[str, Any]) -> dict[str, Any]:
        size = max(1, min(int(args.get("size") or 20), MAX_SEARCH_SIZE))
        docs = self._select(args, kind="all" if args.get("include_metrics") else "logs")
        docs.sort(key=lambda d: d["@timestamp"], reverse=str(args.get("sort", "desc")) != "asc")
        start, end = self._window(args, "1h")
        return {"window": [real_tools.iso(start), real_tools.iso(end)], "total": len(docs),
                "returned": min(size, len(docs)), "rows": [real_tools.compact_row(d) for d in docs[:size]]}

    def aggregate(self, args: dict[str, Any]) -> dict[str, Any]:
        field = real_tools.ALIASES.get(args.get("field") or "", args.get("field") or "")
        top = max(1, min(int(args.get("top") or 10), MAX_AGG_TOP))
        docs = self._select(args, kind="all" if args.get("include_metrics") else "logs")
        counts: Counter = Counter()
        first: dict[str, str] = {}
        last: dict[str, str] = {}
        for d in docs:
            v = real_tools._get(d, field)
            for x in v if isinstance(v, list) else [v]:
                if x is None:
                    continue
                k = str(x)
                counts[k] += 1
                first.setdefault(k, d["@timestamp"])
                last[k] = d["@timestamp"]
        buckets = counts.most_common(top)
        start, end = self._window(args, "1h")
        return {"window": [real_tools.iso(start), real_tools.iso(end)], "field": field, "total_docs": len(docs),
                "other_docs": sum(counts.values()) - sum(n for _, n in buckets),
                "buckets": [{"value": k, "count": n, "first": first[k], "last": last[k]} for k, n in buckets]}

    def metric_series(self, args: dict[str, Any]) -> dict[str, Any]:
        field = args.get("field") or ""
        stat = str(args.get("stat") or "avg")
        docs = [d for d in self._select(args, default="3h", kind="metrics")
                if isinstance(real_tools._get(d, field), (int, float))]
        start, end = self._window(args, "3h")
        step = max(60, int(-(-(end - start).total_seconds() // MAX_SERIES_POINTS)))
        step = -(-step // 60) * 60
        buckets: dict[int, list[float]] = {}
        for d in docs:
            i = int((real_tools.parse_ts(d["@timestamp"]) - start).total_seconds() // step)
            buckets.setdefault(i, []).append(float(real_tools._get(d, field)))
        agg = {"avg": lambda xs: sum(xs) / len(xs), "max": max, "min": min, "sum": sum}[stat]
        pts = [[real_tools.iso(start + timedelta(seconds=i * step))[:16] + "Z", round(agg(v), 3)]
               for i, v in sorted(buckets.items())][-MAX_SERIES_POINTS:]
        out = {"window": [real_tools.iso(start), real_tools.iso(end)], "field": field, "stat": stat,
               "interval_s": step, "docs": len(docs), "points": pts}
        if pts:
            vals = [p[1] for p in pts]
            out["summary"] = {"min": min(vals), "max": max(vals), "first": vals[0], "last": vals[-1],
                              "max_at": max(pts, key=lambda p: p[1])[0]}
        else:
            out["note"] = "no numeric values; check dataset and field names"
        return out

    def find_changes(self, args: dict[str, Any]) -> dict[str, Any]:
        size = max(1, min(int(args.get("size") or 30), MAX_SEARCH_SIZE))
        ents = {str(e) for e in args.get("entities") or [] if e}

        def scoped(d: dict[str, Any]) -> bool:
            return not ents or any(_match_value(real_tools._get(d, f), list(ents)) for f in real_tools._ENTITY_FIELDS)
        real = [d for d in self._select(args, "6h", datasets=("change.management", "cicd.deploy", "security.firewall"),
                                        operator="or")
                if scoped(d) and (d["data_stream"]["dataset"] != "security.firewall"
                                  or d["event"]["code"] == "FW-RULE-CHANGE")]
        rows = sorted(real_tools.group_changes(real), key=lambda r: r.get("ts") or "", reverse=True)[:size]
        auth = [d for d in self._select(args, "6h", datasets=("security.auth",), operator="or")
                if scoped(d) and d["event"]["code"] in ("AUTH-SUDO", "AUTH-SSH-SUCCESS")]
        auth.sort(key=lambda d: d["@timestamp"], reverse=True)
        room = max(0, size - len(rows))
        start, end = self._window(args, "6h")
        return {"window": [real_tools.iso(start), real_tools.iso(end)], "total": len(real) + len(auth),
                "returned": len(rows) + min(room, len(auth)),
                "counts": {"change_docs": len(real), "change_rows": len(rows), "logins_and_sudo": len(auth),
                           "logins_and_sudo_shown": min(room, len(auth))},
                "rows": rows + [real_tools.compact_row(d) for d in auth[:room]]}

    def expand_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        entity = str(args.get("entity") or "")
        hosts = {real_tools._get(d, "host.name"): d.get("labels", {}) for d in self.docs if real_tools._get(d, "host.name")}
        if entity in hosts:
            lab = hosts[entity]
            rack, row = lab.get("rack"), lab.get("row")
            return {"entity": entity, "type": "host", "row": row, "rack": rack, "cluster": lab.get("cluster"),
                    "rack_peers": sorted(h for h, l in hosts.items() if l.get("rack") == rack and h != entity),
                    "devices": [f"tor-{rack.lower()}", f"pdu-{rack.lower()}-a", f"pdu-{rack.lower()}-b",
                                f"crac-{row.lower()}"] if rack and row else []}
        svc_hosts = sorted({real_tools._get(d, "host.name") for d in self.docs
                            if real_tools._get(d, "service.name") == entity and real_tools._get(d, "host.name")})
        if svc_hosts:
            return {"entity": entity, "type": "service", "hosts": svc_hosts,
                    "racks": sorted({hosts[h].get("rack") for h in svc_hosts if hosts[h].get("rack")})}
        for lvl in ("rack", "row", "cluster"):
            members = sorted(h for h, l in hosts.items() if str(l.get(lvl, "")).lower() == entity.lower())
            if members:
                return {"entity": entity, "type": lvl, "hosts": members}
        return {"entity": entity, "found": False, "note": "no docs carry this name"}

    def get_doc(self, args: dict[str, Any] | str) -> dict[str, Any]:
        event_id = args if isinstance(args, str) else str(args.get("event_id") or "")
        d = self.by_id.get(event_id)
        if d is None:
            return {"found": False, "event_id": event_id}
        flat = {k: real_tools._trim(v, real_tools.DOC_STR_CHARS) for k, v in real_tools._flatten(d).items()
                if not k.startswith(real_tools._DOC_DROP) and k not in real_tools._DOC_DROP}
        flat["index"] = f"{d['data_stream']['type']}-{d['data_stream']['dataset']}-{self.ns}"
        return {"found": True, "doc": flat}

    def catalog(self) -> list[dict[str, Any]]:
        c = Counter((d["event"]["code"], f"{d['data_stream']['type']}-{d['data_stream']['dataset']}-{self.ns}",
                     d["event"]["action"]) for d in self.docs)
        return sorted(({"code": k[0], "index": k[1], "action": k[2], "count": n} for k, n in c.items()),
                      key=lambda r: (r["index"], r["code"]))

    def metric_fields(self) -> dict[str, list[str]]:
        out: dict[str, set[str]] = {}
        for d in self.docs:
            if d["data_stream"]["type"] == "metrics":
                out.setdefault(d["data_stream"]["dataset"], set()).update(
                    k for k, v in real_tools._flatten(d.get("datacenter", {}), "datacenter.").items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool))
        return {k: sorted(v) for k, v in out.items()}

    def dispatch(self, name: str, arguments: dict[str, Any] | None) -> Any:
        self.calls.append((name, dict(arguments or {})))
        try:
            args = real_tools.coerce_args(arguments or {})
            if name == "note_finding":
                return {"ok": True}
            if name == "submit_report":
                return args
            if name not in TOOL_NAMES:
                return {"error": f"unknown tool {name!r}; tools are {list(TOOL_NAMES)}"}
            return getattr(self, name)(args)
        except (real_tools.ToolError, ValueError, TypeError, KeyError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


# =============================================================================================
# fake_chat: a drop-in for llm.chat
# =============================================================================================
def fake_chat(script: list[Any], record: list[dict[str, Any]] | None = None):
    """Return a function with llm.chat's signature that replays script items in order.

    Items: {"name": tool, "arguments": {...}} (one tool call), a list of those (parallel calls), a str (plain text),
    or an llm.ChatResult. When the script runs out it answers with plain text.
    Use with pytest: monkeypatch.setattr(llm, "chat", fakes.fake_chat([...])).
    """
    items = list(script)

    def chat(messages, tools=None, model=None, thinking=True, max_tokens=4096, seed=None, trace=None,
             role="investigator"):
        if record is not None:
            record.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []],
                           "role": role, "thinking": thinking, "seed": seed})
        item = items.pop(0) if items else "Nothing further."
        if isinstance(item, llm.ChatResult):
            res = item
        else:
            calls = item if isinstance(item, list) else [item] if isinstance(item, dict) else []
            res = llm.ChatResult(text=item if isinstance(item, str) else "",
                                 tool_calls=[{"id": f"call{i}", "name": c["name"], "arguments": c.get("arguments", {})}
                                             for i, c in enumerate(calls)],
                                 usage={"prompt_tokens": sum(len(str(m.get("content", ""))) for m in messages) // 4,
                                        "completion_tokens": 50}, latency_ms=5, model=model or f"fake/{role}")
        if trace is not None:
            trace({"model": res.model or f"fake/{role}", **res.usage, "latency_ms": res.latency_ms,
                   "tool_name": res.tool_calls[0]["name"] if res.tool_calls else None})
        return res
    return chat


if __name__ == "__main__":
    t = FakeTools.from_dcsim()
    print(f"FakeTools: {len(t.docs)} docs, {len(t.catalog())} event codes, anchor {real_tools.iso(t.anchor)}")
    for name, args in [("aggregate", {"field": "event.code", "lookback": "6h", "top": 5}),
                       ("find_changes", {"lookback": "6h", "size": 5}),
                       ("search_events", {"min_level": "error", "lookback": "6h", "size": 3})]:
        r = t.dispatch(name, args)
        print(f"  {name}: " + json.dumps(r)[:200])
