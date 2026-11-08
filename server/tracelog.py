"""Investigation trace: an append-only list of timestamped entries.

Named tracelog (not trace) so it does not shadow the stdlib `trace` module.

Entry kinds written today:
  status   job status changes                       {status}
  llm      one model call                           {model, prompt_tokens, completion_tokens, latency_ms, tool_name}
  tool     one tool call                            {name, arguments, result (truncated), call_no}
  finding  a note_finding call                      {finding fields as sent by the model}
  verify   event id verification against ES         {kept, dropped}
  info     anything else worth showing               {message}
  error    a failure                                 {message}
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable


class Trace:
    def __init__(self, on_add: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._on_add = on_add
        # Pipeline stage stamped on every entry (investigate, critic, retry, critic_retry). Set between stages,
        # never while a worker thread is writing.
        self.phase: str | None = None

    def add(self, kind: str, /, **data: Any) -> dict[str, Any]:
        # seq, ts and kind are reserved; a payload key with the same name is kept under a "data_" prefix.
        payload = {(f"data_{k}" if k in ("seq", "ts", "kind", "phase") else k): v for k, v in data.items()}
        with self._lock:
            entry = {"seq": len(self._entries) + 1,
                     "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                     "kind": kind, **({"phase": self.phase} if self.phase else {}), **payload}
            self._entries.append(entry)
        if self._on_add is not None:
            self._on_add(entry)
        return entry

    def restore(self, entries: list[dict[str, Any]]) -> None:
        """Replace the entries with ones saved earlier (a job loaded from Supabase). New entries continue the seq."""
        with self._lock:
            self._entries = [dict(e) for e in sorted(entries, key=lambda e: e.get("seq", 0))]
            for i, e in enumerate(self._entries, 1):
                e["seq"] = i

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def totals(self, phases: set[str] | None = None) -> dict[str, int]:
        """Token and call counts for cost reporting (judges ask for cost per investigation).

        phases limits the count to entries stamped with those pipeline stages.
        """
        out = {"llm_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "llm_latency_ms": 0}
        seen_calls: set[tuple[str | None, int]] = set()
        for e in self.entries():
            if phases is not None and e.get("phase") not in phases:
                continue
            if e["kind"] == "llm":
                out["llm_calls"] += 1
                out["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
                out["completion_tokens"] += int(e.get("completion_tokens") or 0)
                out["llm_latency_ms"] += int(e.get("latency_ms") or 0)
            elif e["kind"] == "tool":
                # A corrected submit_report reuses its call_no; count each budgeted call once.
                no = e.get("call_no")
                key = (e.get("phase"), no)
                if no is None or key not in seen_calls:
                    out["tool_calls"] += 1
                if no is not None:
                    seen_calls.add(key)
        return out
