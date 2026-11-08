"""Supabase persistence: one row per job (contracts.SUPABASE_JOBS_SQL) plus the Expo push tokens.

Talks to Supabase's REST API (PostgREST) with httpx, which the server already depends on; the supabase-py package
would add a dependency for three HTTP calls. Off (every call a no-op) until SUPABASE_URL and SUPABASE_SECRET_KEY
(or the legacy name SUPABASE_SERVICE_KEY) are set in server/.env.

    python3 server/store.py --sql      # paste into the Supabase SQL Editor once (jobs table + devices + read policy)
    python3 server/store.py --check    # one round trip: insert, read back and delete a probe row

Who reads what
  server  writes jobs rows on every status change (save), reads them back at startup (load_jobs), and keeps device
          tokens in the devices table (save_device / devices) so pushes survive a server restart.
  app     reads jobs with the anon key (select policy below; realtime on jobs from contracts.SUPABASE_JOBS_SQL).
          It never writes: actions and push registration go through the server's HTTP API.

The devices table and the row-level security policy live here, not in contracts.py: the app never sees the devices
table, and contracts.py stays frozen while the app is built against it.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import SUPABASE_JOBS_SQL, supabase_row  # noqa: E402

log = logging.getLogger("triagent.store")

TIMEOUT_S = 10.0

# Run once after contracts.SUPABASE_JOBS_SQL. RLS on both tables: the anon key (the app's shared demo token) may
# only read jobs; the service key the server uses bypasses RLS. Without the select policy the anon key reads zero
# rows and no error, so an app wired to Supabase would show an empty Inbox.
STORE_SQL = """
alter table jobs enable row level security;
drop policy if exists "demo app reads jobs" on jobs;
create policy "demo app reads jobs" on jobs for select to anon, authenticated using (true);

create table if not exists devices (
  expo_push_token  text primary key,
  "user"           text not null default 'demo',
  platform         text,
  registered       timestamptz not null default now()
);
alter table devices enable row level security;   -- no policy: only the server's service key reaches it
"""


class Store:
    def __init__(self, url: str | None, key: str | None) -> None:
        self.url = (url or "").rstrip("/")
        self.key = key or ""
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None

    @classmethod
    def from_env(cls) -> "Store":
        return cls(os.environ.get("SUPABASE_URL"),
                   os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_KEY"))

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        h = {"apikey": self.key, "Content-Type": "application/json"}
        if self.key.startswith("eyJ"):   # legacy JWT service_role key; the newer sb_secret_ keys go in apikey only
            h["Authorization"] = f"Bearer {self.key}"
        if prefer:
            h["Prefer"] = prefer
        return h

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(base_url=f"{self.url}/rest/v1", timeout=TIMEOUT_S)
            return self._client

    def _call(self, method: str, path: str, *, prefer: str | None = None, **kw: Any) -> httpx.Response:
        r = self._http().request(method, path, headers=self._headers(prefer), **kw)
        if r.status_code >= 300:
            self.last_error = f"{method} {path} -> {r.status_code}: {r.text[:300]}"
            raise RuntimeError(self.last_error)
        self.last_error = None
        return r

    def _upsert(self, table: str, conflict: str, row: dict[str, Any]) -> None:
        self._call("POST", f"/{table}", params={"on_conflict": conflict}, json=row,
                   prefer="resolution=merge-duplicates,return=minimal")

    # ------------------------------------------------------------------ jobs
    def save(self, job: dict[str, Any]) -> None:
        """Upsert the jobs row for a Job JSON snapshot (main.Job.view()). Raises on a failed write."""
        if self.enabled:
            self._upsert("jobs", "job_id", supabase_row(job))

    def load_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        """The newest Job JSON documents, newest first."""
        if not self.enabled:
            return []
        r = self._call("GET", "/jobs", params={"select": "job", "order": "created.desc", "limit": str(limit)})
        return [row["job"] for row in r.json() if isinstance(row.get("job"), dict)]

    # ------------------------------------------------------------------ devices
    def save_device(self, reg: dict[str, Any]) -> None:
        if self.enabled:
            self._upsert("devices", "expo_push_token", {"expo_push_token": reg["expo_push_token"],
                                                        "user": reg.get("user") or "demo",
                                                        "platform": reg.get("platform")})

    def devices(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        r = self._call("GET", "/devices", params={"select": "expo_push_token,user,platform"})
        return list(r.json())

    def check(self) -> str:
        """Insert, read back and delete a probe jobs row. Returns a one-line result; raises on failure."""
        probe = {"job_id": "job-store-check", "ticket_id": "INC-CHECK", "status": "done", "created": 0.0,
                 "ticket": {"ticket_id": "INC-CHECK", "title": "store.py --check"}, "totals": {}}
        self.save(probe)
        r = self._call("GET", "/jobs", params={"select": "job_id,status", "job_id": "eq.job-store-check"})
        self._call("DELETE", "/jobs", params={"job_id": "eq.job-store-check"})
        if r.json() != [{"job_id": "job-store-check", "status": "done"}]:
            raise RuntimeError(f"probe row did not read back: {r.text[:200]}")
        return f"ok: wrote, read back and deleted a probe row in {self.url}"

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": self.url or None, "last_error": self.last_error}


_default: Store | None = None


def default() -> Store:
    global _default
    if _default is None:
        _default = Store.from_env()
    return _default


def save(job: Any) -> None:
    """contracts.py entry point: upsert one job's row. Accepts a main.Job or its Job JSON dict."""
    default().save(job.view() if hasattr(job, "view") else job)


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
    ap = argparse.ArgumentParser(description="Triagent Supabase store")
    ap.add_argument("--sql", action="store_true", help="print the SQL to run once in the Supabase SQL Editor")
    ap.add_argument("--check", action="store_true", help="round-trip a probe row with the configured keys")
    a = ap.parse_args()
    if a.sql:
        sys.stdout.write("-- contracts.SUPABASE_JOBS_SQL\n" + SUPABASE_JOBS_SQL + "\n-- store.STORE_SQL\n" + STORE_SQL)
    if a.check:
        s = Store.from_env()
        if not s.enabled:
            raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY are not set in server/.env")
        print(s.check())
    if not (a.sql or a.check):
        ap.print_help()
