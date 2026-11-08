"""Expo push when a job finishes: title from the ticket, body from prompts/summarize.md on the small model.

    push(job, tokens=None) -> list[PushMessage dicts], one per device it was sent to

tokens defaults to the devices in Supabase (store.devices()). main.py passes the union of those and the tokens
registered with POST /devices since the server started, so pushes work with or without Supabase.

The body comes from the small model with reasoning off (TRIAGENT_MODEL_SMALL). If that call fails, a fixed sentence
built from the report is used instead; a push never waits on a retry loop. PushMessage.data carries job_id (the app
opens Incident detail on tap) and status. Expo answers per message; a DeviceNotRegistered error is logged in the
trace so a stale token is visible.

Set EXPO_ACCESS_TOKEN only if the Expo project has enhanced push security turned on.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

import httpx

import llm
import store
from contracts import PushMessage

log = logging.getLogger("triagent.notify")

EXPO_PUSH_URL = os.environ.get("EXPO_PUSH_URL") or "https://exp.host/--/api/v2/push/send"
PROMPT = (Path(__file__).parent / "prompts" / "summarize.md").read_text(encoding="utf-8")
BODY_MAX = 178          # what a lock screen shows before truncating
EXPO_BATCH = 100        # Expo accepts at most 100 messages per request


def title_for(job: Any) -> str:
    t = job.ticket
    head = f"{t.get('priority') or ''} {t.get('ticket_id')}".strip()
    rc = (job.report or {}).get("root_cause") or {}
    if job.status != "done" or not job.report:
        return f"{head}: investigation failed"
    if rc.get("category") in (None, "unknown"):
        return f"{head}: cause not confirmed"
    return f"{head}: cause found ({rc['category']})"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= BODY_MAX else text[:BODY_MAX - 1].rstrip() + "…"


def fallback_body(job: Any) -> str:
    if job.status != "done" or not job.report:
        return _clip(f"{job.ticket.get('title', '')}. Triage by hand: {job.error or 'no report produced'}")
    rc = job.report.get("root_cause") or {}
    return _clip(f"{rc.get('entity', '?')}: {rc.get('summary', '')}")


def summarize(job: Any) -> str:
    """One or two sentences for the notification body. Never raises."""
    if job.status != "done" or not job.report:
        return fallback_body(job)
    r = job.report
    rem = job.remediation or {}
    facts = {"ticket": {k: job.ticket.get(k) for k in ("title", "priority", "service", "host")},
             "problem": r.get("problem"), "root_cause": r.get("root_cause"), "confidence": r.get("confidence"),
             "first_remediation_step": (rem.get("immediate") or [None])[0]}
    try:
        res = llm.chat([{"role": "system", "content": PROMPT},
                        {"role": "user", "content": f"Report:\n{facts}"}],
                       role="small", thinking=False, max_tokens=200, trace=lambda d: job.trace.add("llm", **d))
        text = (res.text or "").strip().strip('"')
        return _clip(text) if text else fallback_body(job)
    except Exception as exc:  # noqa: BLE001 - the push still goes out with the fixed sentence
        log.warning("job %s: summarize failed (%s); using the fixed body", job.job_id, exc)
        return fallback_body(job)


def build(job: Any, tokens: Iterable[str]) -> list[dict[str, Any]]:
    title, body = title_for(job), summarize(job)
    return [PushMessage(to=t, title=title, body=body, data={"job_id": job.job_id, "status": job.status}).model_dump()
            for t in dict.fromkeys(tokens)]


def send(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """POST to Expo's push API. Returns Expo's per-message tickets ({status: ok|error, ...}) in message order."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if os.environ.get("EXPO_ACCESS_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['EXPO_ACCESS_TOKEN']}"
    tickets: list[dict[str, Any]] = []
    for i in range(0, len(messages), EXPO_BATCH):
        batch = [{**m, "sound": "default", "channelId": "default"} for m in messages[i:i + EXPO_BATCH]]
        r = httpx.post(EXPO_PUSH_URL, json=batch, headers=headers, timeout=15.0)
        if r.status_code >= 300:
            raise RuntimeError(f"Expo push {r.status_code}: {r.text[:300]}")
        tickets.extend(r.json().get("data") or [])
    return tickets


def push(job: Any, tokens: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Send the finished job to every device. Never raises; the outcome goes into the job's trace."""
    try:
        tokens = list(tokens) if tokens is not None else [d["expo_push_token"] for d in store.default().devices()]
        if not tokens:
            job.trace.add("info", message="push skipped: no registered devices")
            return []
        messages = build(job, tokens)
        tickets = send(messages)
        bad = [(m["to"], t) for m, t in zip(messages, tickets) if t.get("status") != "ok"]
        sent = len(messages) - len(bad)
        job.trace.add("info", message=f"push sent to {sent} of {len(messages)} devices: {messages[0]['title']} | "
                                      f"{messages[0]['body']}")
        for token, t in bad:
            err = (t.get("details") or {}).get("error") or t.get("message")
            job.trace.add("error", message=f"push to {token[:24]}... failed: {err}")
        return messages
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("job %s: push failed: %s", job.job_id, exc)
        job.trace.add("error", message=f"push not sent ({type(exc).__name__}: {exc})")
        return []
