"""tuning_knobs: today's values, env override rules, and critic_retry=0 (no network)."""
from __future__ import annotations

import asyncio

import critic
import investigator
import llm
import main
import tuning_knobs
from tuning_knobs import KNOBS

TICKET = {"ticket_id": "INC1", "created": "2026-11-07T11:05:00Z", "title": "auth-svc 5xx", "namespace": "dal1",
          "labels": {}}


def test_knobs_hold_todays_values():
    assert KNOBS == {"max_tool_calls": 20, "critic_enabled": True, "critic_retry": 0, "thinking_investigator": True,
                     "thinking_critic": True, "temperature": 1.0}
    assert 8 <= KNOBS["max_tool_calls"] <= 20 and 0.6 <= KNOBS["temperature"] <= 1.0


def test_settings_use_knobs_when_env_unset_or_empty(monkeypatch):
    monkeypatch.setitem(KNOBS, "max_tool_calls", 13)
    monkeypatch.setitem(KNOBS, "critic_enabled", False)
    monkeypatch.setitem(KNOBS, "critic_retry", 0)
    for value in (None, "", "  "):
        for name in ("TRIAGENT_MAX_TOOL_CALLS", "TRIAGENT_CRITIC"):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        s = main.Settings.from_env()
        assert (s.max_tool_calls, s.critic_enabled, s.critic_retry) == (13, False, 0)


def test_env_wins_when_set(monkeypatch):
    monkeypatch.setitem(KNOBS, "max_tool_calls", 13)
    monkeypatch.setitem(KNOBS, "critic_enabled", True)
    monkeypatch.setenv("TRIAGENT_MAX_TOOL_CALLS", "9")
    monkeypatch.setenv("TRIAGENT_CRITIC", "0")
    s = main.Settings.from_env()
    assert (s.max_tool_calls, s.critic_enabled) == (9, False)
    monkeypatch.setitem(KNOBS, "critic_enabled", False)
    monkeypatch.setenv("TRIAGENT_CRITIC", "1")
    assert main.Settings.from_env().critic_enabled is True


def _capture_chat(monkeypatch, text="ok"):
    seen = []

    def fake_chat(messages, tools=None, model=None, thinking=True, max_tokens=4096, seed=None, trace=None,
                  role="investigator"):
        seen.append({"thinking": thinking, "role": role})
        return llm.ChatResult(text=text, tool_calls=[], structured=True, raw=None, model=model or "m",
                              usage={"prompt_tokens": 1, "completion_tokens": 1}, latency_ms=1)
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen


def test_temperature_reads_knobs(monkeypatch):
    sent = {}

    class FakeCompletions:
        def create(self, **kw):
            sent.update(kw)
            msg = type("M", (), {"content": "ok", "tool_calls": None})()
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()], "usage": usage})()

    fake = type("Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()})()
    monkeypatch.setattr(llm, "_client", lambda *a: fake)
    llm.chat([{"role": "user", "content": "x"}], role="small")
    assert sent["temperature"] == 1.0
    monkeypatch.setitem(KNOBS, "temperature", 0.7)
    llm.chat([{"role": "user", "content": "x"}], role="small")
    assert sent["temperature"] == 0.7


def test_thinking_flags_read_knobs(monkeypatch):
    seen = _capture_chat(monkeypatch)
    loop = investigator._Loop(TICKET, None, type("T", (), {"add": lambda *a, **k: {}})(), 20, "nvidia/nemotron-x",
                              None)
    loop.chat([])
    monkeypatch.setitem(KNOBS, "thinking_investigator", False)
    loop.chat([])
    assert [s["thinking"] for s in seen] == [True, False]
    assert tuning_knobs.thinking_critic() is True
    monkeypatch.setitem(KNOBS, "thinking_critic", False)
    assert tuning_knobs.thinking_critic() is False


def _report(category="deploy"):
    return {"problem": "p", "root_cause": {"summary": "s", "category": category, "entity": "lb-1",
                                           "entity_type": "load_balancer", "identifiers": {}, "event_ids": ["a"]},
            "causal_chain": [], "rejected_candidates": [], "affected": {}, "confidence": 0.9, "open_questions": []}


def _run(monkeypatch, tmp_path, retry):
    calls = []

    def fake_investigate(ticket, es, trace, max_calls, seed=None, feedback=None):
        calls.append(feedback)
        return _report()

    reviews = []

    def fake_critic(*a):
        reviews.append(1)
        return {"verdict": "fail", "failed": ["category_matches"],
                "checks": {"category_matches": {"ok": False, "detail": "push is config"}},
                "corrections": ["Use category config."]}
    monkeypatch.setattr(investigator, "run", fake_investigate)
    monkeypatch.setattr(critic, "run", fake_critic)
    monkeypatch.setitem(KNOBS, "critic_retry", retry)
    monkeypatch.delenv("TRIAGENT_CRITIC", raising=False)
    s = main.Settings.from_env()
    s = main.Settings(**{**s.__dict__, "jobs_dir": tmp_path, "critic_enabled": True, "ticket_backend": "none"})

    async def go():
        job = main.Job(job_id="job-k", ticket=dict(TICKET), loop=asyncio.get_running_loop())
        await main.run_pipeline(job, s)
        return job
    return asyncio.run(go()), calls, reviews


def test_critic_retry_zero_never_reruns_the_investigator(monkeypatch, tmp_path):
    job, calls, reviews = _run(monkeypatch, tmp_path, 0)
    assert job.status == "done"
    assert calls == [None] and len(reviews) == 1
    assert job.critic["retried"] is False and job.critic["first"]["verdict"] == "fail"
    assert job.report["open_questions"][-1].startswith("Reviewer: category_matches failed: push is config")
    assert job.report["root_cause"] == job.report_initial["root_cause"]


def test_critic_retry_one_still_retries(monkeypatch, tmp_path):
    job, calls, reviews = _run(monkeypatch, tmp_path, 1)
    assert len(calls) == 2 and len(reviews) == 2 and job.critic["retried"] is True
