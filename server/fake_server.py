"""The real Triagent HTTP API with scripted investigations: no Elasticsearch, no NVIDIA keys.

For building the app (or anything that consumes the API) in parallel with the backend. It is built by
main.create_app() from the same routes, with the pipeline swapped for fakes.fake_pipeline, so routes, shapes and SSE
framing are identical to production.

    cd server && pip install -r requirements.txt
    uvicorn fake_server:app --port 8080 --host 0.0.0.0        # --host so a phone on the LAN can reach it
    TRIAGENT_FAKE_SPEED=5 uvicorn fake_server:app --port 8080  # play investigations 5x faster

On startup it holds: a finished cooling investigation, a finished config investigation that went through a critic
retry, a failed job, and one live job streaming its trace (about a minute at speed 1). POST /investigate (or
/webhook) with any Ticket starts a new scripted investigation; the template is picked from the ticket text
(thermal/NotReady -> cooling, 5xx/LB -> config, crashloop/OOM/deploy -> deploy).

    curl -s localhost:8080/jobs | python3 -m json.tool
    curl -N localhost:8080/jobs/job-fakelive0001/events
    curl -s -XPOST localhost:8080/investigate -H 'content-type: application/json' \\
         -d '{"ticket_id":"INC1","title":"checkout-api: pods CrashLoopBackOff","created":"2026-11-08T10:00:00Z"}'
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import fakes
import main

REPO = main.REPO_ROOT


def build_app(settings: main.Settings | None = None) -> "main.FastAPI":
    """A fake Triagent app: same routes as main.py, scripted pipeline, preloaded jobs."""
    settings = settings or dataclasses.replace(main.Settings.from_env(), fake=True, jobs_dir=REPO / "t" / "fake_jobs",
                                               scores_dir=REPO / "t" / "fake_scores")
    application = main.create_app(settings=dataclasses.replace(settings, fake=True), pipeline=fakes.fake_pipeline)

    async def _startup() -> None:
        scores = application.state.settings.scores_dir
        scores.mkdir(parents=True, exist_ok=True)
        if not any(scores.glob("*.json")):
            (scores / "20261108T000000Z.json").write_text(json.dumps(fakes.sample_score_run(), indent=2),
                                                          encoding="utf-8")
        await fakes.preload(application.state.registry, asyncio.get_running_loop(), main.Job)

    application.router.on_startup.append(_startup)
    return application


app = build_app()
