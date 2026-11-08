"""Tunable constants for the triage pipeline, in one place so a tuning run can change them as a one-file diff.

Read at call time by the modules that use them (main.Settings, main.run_pipeline, investigator, critic, llm). The
server reads them once per start, so a change needs a server restart to take effect.

An environment variable with the same meaning still wins when it is set and non-empty (TRIAGENT_MAX_TOOL_CALLS,
TRIAGENT_CRITIC), so existing .env files keep working; an empty value means "use KNOBS".

Ranges a tuning run must respect:
  max_tool_calls         int, 8..20 (never above 20)
  critic_enabled         bool
  critic_retry           0 or 1: 1 = one investigator retry when the critic fails the report; 0 = keep the critic's
                         review and append its failed checks to open_questions, never re-run the investigator
  thinking_investigator  bool, reasoning on for the investigator's chat calls
  thinking_critic        bool, reasoning on for the critic's first chat call
  temperature            float, 0.6..1.0, every chat call
"""
from __future__ import annotations

import os

KNOBS: dict[str, object] = {
    "max_tool_calls": 20,
    "critic_enabled": True,
    "critic_retry": 0,
    "thinking_investigator": True,
    "thinking_critic": True,
    "temperature": 1.0,
}


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value.strip() != "" else None


def max_tool_calls() -> int:
    """TRIAGENT_MAX_TOOL_CALLS when set and non-empty, else KNOBS."""
    value = _env("TRIAGENT_MAX_TOOL_CALLS")
    return int(value) if value is not None else int(KNOBS["max_tool_calls"])  # type: ignore[arg-type]


def critic_enabled() -> bool:
    """TRIAGENT_CRITIC when set and non-empty ("0" disables, anything else enables), else KNOBS."""
    value = _env("TRIAGENT_CRITIC")
    return value.strip() != "0" if value is not None else bool(KNOBS["critic_enabled"])


def critic_retry() -> int:
    return int(KNOBS["critic_retry"])  # type: ignore[arg-type]


def thinking_investigator() -> bool:
    return bool(KNOBS["thinking_investigator"])


def thinking_critic() -> bool:
    return bool(KNOBS["thinking_critic"])


def temperature() -> float:
    return float(KNOBS["temperature"])  # type: ignore[arg-type]
