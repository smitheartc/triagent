"""The only module that talks to a model. Nemotron on NVIDIA's hosted API, OpenAI-compatible.

Environment
  NVIDIA_BASE_URL               default https://integrate.api.nvidia.com/v1
  NVIDIA_API_KEY                fallback key for any role without its own
  NVIDIA_API_KEY_INVESTIGATOR   key for the investigator's model (per-model keys, as the hackathon issues them)
  NVIDIA_API_KEY_CRITIC         key for the critic's model
  NVIDIA_API_KEY_SMALL          key for the small model (remediation writer, push summarizer)
  TRIAGENT_MODEL_INVESTIGATOR   default nvidia/nemotron-3.5-super-vl-120b-a12b
  TRIAGENT_MODEL_CRITIC         default nvidia/nemotron-3.5-lightning-30b-a3b
  TRIAGENT_MODEL_SMALL          default nvidia/nemotron-3.5-lightning-30b-a3b
  TRIAGENT_ALLOW_ANY_MODEL=1    local experiments only: lift the nvidia/nemotron* restriction

Smoke test (real endpoint):  python3 server/llm.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

import openai
from openai import OpenAI

log = logging.getLogger("triagent.llm")

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODELS = {
    "investigator": "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "critic": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "small": "nvidia/nemotron-3.5-lightning-30b-a3b",
}
_ROLE_ENV = {"investigator": "TRIAGENT_MODEL_INVESTIGATOR", "critic": "TRIAGENT_MODEL_CRITIC",
             "small": "TRIAGENT_MODEL_SMALL"}
ATTEMPTS = 3                       # 5xx and connection errors
RATE_LIMIT_ATTEMPTS = 5            # 429: the account limit is per minute, so wait it out
BACKOFF_S = (2.0, 5.0)
RATE_LIMIT_BACKOFF_S = (10.0, 20.0, 30.0, 60.0)


class ModelNotAllowed(ValueError):
    pass


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict[str, Any]]           # [{"name", "arguments": dict, "id"}]
    usage: dict[str, int]                      # {"prompt_tokens", "completion_tokens"}
    latency_ms: int
    raw: Any = field(repr=False, default=None)
    structured: bool = True                    # False when tool_calls came from the text fallback
    model: str = ""

    def assistant_message(self) -> dict[str, Any]:
        """The assistant turn to append to the conversation before sending tool results."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls and self.structured:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                 for c in self.tool_calls]
        return msg


ROLES = tuple(_ROLE_ENV)


def model_for(role: str) -> str:
    return os.environ.get(_ROLE_ENV[role]) or DEFAULT_MODELS[role]


def key_for(role: str) -> str | None:
    """NVIDIA_API_KEY_<ROLE>, else NVIDIA_API_KEY. NVIDIA issues keys per model, so roles may need different keys."""
    return os.environ.get(f"NVIDIA_API_KEY_{role.upper()}") or os.environ.get("NVIDIA_API_KEY") or None


def check_model(model: str) -> None:
    if model.startswith("nvidia/nemotron"):
        return
    if os.environ.get("TRIAGENT_ALLOW_ANY_MODEL") == "1":
        log.warning("TRIAGENT_ALLOW_ANY_MODEL=1: using non-Nemotron model %s (local experiments only)", model)
        return
    raise ModelNotAllowed(
        f"model {model!r} is not a Nemotron model. The investigator and critic must run on nvidia/nemotron-* "
        f"(NVIDIA challenge rule). Set TRIAGENT_ALLOW_ANY_MODEL=1 only for local experiments.")


@lru_cache(maxsize=4)
def _client(base_url: str, api_key: str) -> OpenAI:
    # Retries are ours (see chat), so the SDK's are off.
    return OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=180.0)


# ---------------------------------------------------------------------------------------------
# text fallback: some models emit the tool call as JSON in the message body. Keep this even when
# structured tool calls work; it is cheap insurance against a model or endpoint change.
# ---------------------------------------------------------------------------------------------
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str | None) -> str:
    if not text:
        return ""
    text = _THINK.sub("", text)
    # An unterminated <think> (truncated output) hides everything after it.
    if "<think>" in text.lower():
        text = text[: text.lower().index("<think>")]
    # Some templates emit only the closing tag (the opening one lives in the prompt).
    if "</think>" in text.lower():
        text = text[text.lower().rindex("</think>") + len("</think>"):]
    return text.strip()


def _json_objects(text: str):
    """Yield every top-level JSON object embedded in text, in order."""
    dec = json.JSONDecoder()
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = end


def parse_text_tool_call(text: str | None, tool_names: list[str] | set[str]) -> dict[str, Any] | None:
    """First JSON object in text (after removing <think> blocks) whose "name" is a known tool.

    Accepts {"name": ..., "arguments": {...}}, {"name": ..., "parameters": {...}}, and arguments given as a
    JSON string. Returns {"name", "arguments", "id"} or None.
    """
    body = strip_think(text)
    names = set(tool_names)
    for obj in _json_objects(body):
        cand = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        name = cand.get("name")
        if name not in names:
            continue
        args = cand.get("arguments", cand.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        return {"name": name, "arguments": args, "id": "text-0"}
    return None


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"_raw": raw}
    except json.JSONDecodeError:
        # Some models wrap or prefix the JSON; take the first object if there is one.
        obj = next(_json_objects(raw), None)
        return obj if obj is not None else {"_raw": raw}


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError)):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


def _retry_after(exc: Exception) -> float | None:
    """Seconds from a Retry-After header, capped at two minutes."""
    resp = getattr(exc, "response", None)
    value = resp.headers.get("retry-after") if resp is not None else None
    try:
        return min(max(float(value), 1.0), 120.0) if value else None
    except ValueError:
        return None


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, model: str | None = None,
         thinking: bool = True, max_tokens: int = 4096, seed: int | None = None,
         trace: Callable[[dict[str, Any]], None] | None = None, role: str = "investigator") -> ChatResult:
    """One chat completion against NVIDIA's endpoint, with retries and the text tool-call fallback.

    role (investigator | critic | small) picks the default model and the API key.
    trace, if given, is called once per successful call with
    {model, prompt_tokens, completion_tokens, latency_ms, tool_name}.
    """
    if role not in _ROLE_ENV:
        raise ValueError(f"unknown role {role!r}; roles are {ROLES}")
    model = model or model_for(role)
    check_model(model)
    api_key = key_for(role)
    if not api_key:
        raise RuntimeError(f"no API key for role {role!r}: set NVIDIA_API_KEY_{role.upper()} or NVIDIA_API_KEY "
                           f"(keys from build.nvidia.com)")
    client = _client(os.environ.get("NVIDIA_BASE_URL") or DEFAULT_BASE_URL, api_key)

    kwargs: dict[str, Any] = {
        "model": model, "messages": messages, "temperature": 1.0, "top_p": 0.95, "max_tokens": max_tokens,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}},
    }
    if tools:
        kwargs["tools"] = tools
    if seed is not None:
        kwargs["seed"] = seed

    resp = None
    attempt = 0
    while True:
        attempt += 1
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001 - classified below
            limited = isinstance(exc, openai.RateLimitError)
            limit = RATE_LIMIT_ATTEMPTS if limited else ATTEMPTS
            if attempt >= limit or not _retryable(exc):
                raise
            wait = _retry_after(exc) if limited else None
            if wait is None:
                table = RATE_LIMIT_BACKOFF_S if limited else BACKOFF_S
                wait = table[min(attempt - 1, len(table) - 1)]
            log.warning("model call failed (%s), attempt %d/%d, retrying in %.0fs", exc, attempt, limit, wait)
            time.sleep(wait)
    latency_ms = int((time.monotonic() - t0) * 1000)

    msg = resp.choices[0].message
    text = strip_think(msg.content)
    calls = [{"name": tc.function.name, "arguments": _parse_args(tc.function.arguments), "id": tc.id}
             for tc in (msg.tool_calls or []) if tc.type == "function"]
    structured = True
    if not calls and tools:
        names = [t["function"]["name"] for t in tools]
        fb = parse_text_tool_call(msg.content, names)
        if fb is not None:
            calls, structured = [fb], False
            log.info("tool call recovered from text: %s", fb["name"])

    usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
             "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0}
    if trace is not None:
        trace({"model": model, **usage, "latency_ms": latency_ms,
               "tool_name": calls[0]["name"] if calls else None})
    return ChatResult(text=text, tool_calls=calls, usage=usage, latency_ms=latency_ms, raw=resp,
                      structured=structured, model=model)


def _key_source(role: str) -> str:
    return f"NVIDIA_API_KEY_{role.upper()}" if os.environ.get(f"NVIDIA_API_KEY_{role.upper()}") else "NVIDIA_API_KEY"


def _smoke() -> int:
    """Investigator: plain completion + tool call (exit code depends on this). Critic, small: plain completion."""
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    out = sys.stdout
    model = model_for("investigator")
    out.write(f"investigator [{model}] key from {_key_source('investigator')}\n")

    try:
        r = chat([{"role": "user", "content": "Reply with exactly the word: ready"}], role="investigator",
                 thinking=False, max_tokens=64)
    except Exception as exc:  # noqa: BLE001 - a smoke test should say what failed, not dump a traceback
        out.write(f"  FAILED: {type(exc).__name__}: {str(exc)[:200]}\n")
        return 1
    out.write(f"  plain completion {r.latency_ms}ms tokens={r.usage}: {r.text[:80]!r}\n")

    get_time = {"type": "function", "function": {
        "name": "get_time", "description": "Return the current UTC time.",
        "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": []}}}
    r = chat([{"role": "system", "content": "You can call tools. Use get_time to answer time questions."},
              {"role": "user", "content": "What time is it in UTC? Use the tool."}],
             tools=[get_time], role="investigator", thinking=True, max_tokens=2048)
    status = 1
    if r.tool_calls:
        how = "structured" if r.structured else "text fallback"
        out.write(f"  tool call: {how} -> {r.tool_calls[0]['name']}({r.tool_calls[0]['arguments']}) "
                  f"{r.latency_ms}ms tokens={r.usage}\n")
        # Round-trip the result so we know the follow-up turn is accepted too.
        msgs = [{"role": "user", "content": "What time is it in UTC? Use the tool."}, r.assistant_message()]
        now = datetime.now(timezone.utc).isoformat()
        msgs.append({"role": "tool", "tool_call_id": r.tool_calls[0]["id"], "content": now} if r.structured
                    else {"role": "user", "content": f"get_time returned: {now}"})
        r2 = chat(msgs, tools=[get_time], role="investigator", thinking=False, max_tokens=256)
        out.write(f"  follow-up after tool result: {r2.text[:120]!r}\n")
        status = 0
    else:
        out.write(f"  tool call: NONE (neither structured nor text fallback). text={r.text[:200]!r}\n")

    # Critic and small: informative only (their modules are later build steps).
    for role in ("critic", "small"):
        out.write(f"{role} [{model_for(role)}] key from {_key_source(role)}\n")
        try:
            r = chat([{"role": "user", "content": "Reply with exactly the word: ready"}], role=role,
                     thinking=False, max_tokens=64)
            out.write(f"  plain completion {r.latency_ms}ms tokens={r.usage}: {r.text[:80]!r}\n")
        except Exception as exc:  # noqa: BLE001 - report and keep going
            out.write(f"  FAILED: {type(exc).__name__}: {str(exc)[:160]}\n")
    return status


if __name__ == "__main__":
    raise SystemExit(_smoke())
