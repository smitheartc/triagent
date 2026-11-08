"""Mock Jira: turns INC-CREATED docs in Elasticsearch into issues, raises the webhook, takes the report comment back.

Standard library only. Run it next to the server; start order does not matter (webhooks retry until the server
answers, and tickets already in ES when the mock starts are processed too, unless --skip-existing).

    python3 server/jira_mock.py --es-url http://localhost:9200 --webhook http://localhost:8080/webhook

What it does
  - every --interval seconds, reads the INC-CREATED docs in logs-incident.tickets-* (all namespaces)
  - opens one issue per ticket doc, keyed by ticket.id; a new doc for a known ticket.id (a re-ingest or a second
    --stream run with the same seed) reopens the issue with no comments
  - --settle seconds after first seeing a ticket, POSTs the webhook payload (contracts.Ticket) built from the doc.
    Batch ingests land in timestamp order, so by then the docs before the ticket are searchable.
  - serves the ticket side of the integration (comment_payload() below):
        POST /issue/{ticket_id}/comment   {"body", "job_id", "status", "author"} -> 201 {"id", "issue_id", "created"}
        GET  /issue/{ticket_id}           the issue with its webhook state and comments
        GET  /issues                      every issue, newest first, without comment bodies
        GET  /health

Issues live in memory. The server's TICKET_BACKEND=mock (default) posts to JIRA_MOCK_URL (default
http://localhost:8082). Real Jira / SNOW would replace this process; the payload and comment shapes stay the same.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("jira_mock")

TICKET_INDEX = "logs-incident.tickets-*"
SOURCE_FIELDS = ["@timestamp", "event.id", "event.code", "ticket", "service.name", "host.name", "labels",
                 "data_stream.namespace"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# The comment exchange (main.patch_ticket -> this mock). Defined here, the side that serves it, rather than in
# contracts.py: the app never sees it, and contracts.py stays frozen while the app is built against it.
COMMENT_STATUSES = ("done", "failed")
COMMENT_RESPONSE_FIELDS = ("id", "issue_id", "created")


def comment_payload(body: str, job_id: str, status: str, author: str = "triagent") -> dict[str, Any]:
    """Body of POST /issue/{ticket_id}/comment. Jira's REST body is {"body": ...}; job_id and status ride along so
    the mock shows which investigation wrote the comment. Reply (201): {"id", "issue_id", "created"}."""
    if status not in COMMENT_STATUSES:
        raise ValueError(f"comment status must be one of {COMMENT_STATUSES}, not {status!r}")
    return {"body": body, "job_id": job_id, "status": status, "author": author}


def payload_from_doc(src: dict[str, Any], ns: str | None = None) -> dict[str, Any]:
    """The webhook payload (contracts.Ticket) for an INC-CREATED doc. scorer.py and tools_check.py use this too."""
    tk = src.get("ticket") or {}
    ns = ns or (src.get("data_stream") or {}).get("namespace") or "dal1"
    return {"ticket_id": tk.get("id"), "title": tk.get("title", ""), "description": tk.get("description", ""),
            "priority": tk.get("priority") or "P3", "created": src["@timestamp"],
            "service": (src.get("service") or {}).get("name"), "host": (src.get("host") or {}).get("name"),
            "labels": {k: v for k, v in (src.get("labels") or {}).items() if k in ("row", "rack", "cluster")},
            "namespace": ns, "source_event_id": src["event"]["id"]}


def _request(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
             timeout: float = 10.0) -> tuple[int, Any]:
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
        except ValueError:
            return e.code, raw.decode("utf-8", errors="replace")[:500]


class Mock:
    def __init__(self, es_url: str, webhook: str, es_api_key: str | None = None, index: str = TICKET_INDEX,
                 settle: float = 5.0) -> None:
        self.es_url = es_url.rstrip("/")
        self.webhook = webhook
        self.es_headers = {"Authorization": f"ApiKey {es_api_key}"} if es_api_key else {}
        self.index = index
        self.settle = settle
        self.issues: dict[str, dict[str, Any]] = {}
        self.seen_docs: set[str] = set()          # "<_index>/<_id>"; a re-ingest gets new _ids
        self.lock = threading.Lock()
        self._last_warn: dict[str, float] = {}

    # ---------------------------------------------------------------- Elasticsearch side
    def ticket_docs(self) -> list[dict[str, Any]]:
        body = {"size": 1000, "query": {"term": {"event.code": "INC-CREATED"}}, "sort": [{"@timestamp": "asc"}],
                "_source": SOURCE_FIELDS}
        st, res = _request("POST", f"{self.es_url}/{self.index}/_search?ignore_unavailable=true", body,
                           self.es_headers)
        if st != 200:
            raise RuntimeError(f"ES search {st}: {str(res)[:200]}")
        return res["hits"]["hits"]

    def skip_existing(self) -> int:
        hits = self.ticket_docs()
        self.seen_docs.update(f"{h['_index']}/{h['_id']}" for h in hits)
        return len(hits)

    def scan(self) -> list[str]:
        """Open (or reopen) an issue for every ticket doc not seen yet. Returns the ticket ids opened."""
        opened = []
        for h in self.ticket_docs():
            doc_key = f"{h['_index']}/{h['_id']}"
            if doc_key in self.seen_docs:
                continue
            self.seen_docs.add(doc_key)
            payload = payload_from_doc(h["_source"])
            tid = payload["ticket_id"]
            if not tid:
                continue
            with self.lock:
                reopened = tid in self.issues
                self.issues[tid] = {"id": tid, "title": payload["title"], "priority": payload["priority"],
                                    "status": "open", "created": payload["created"], "opened": now_iso(),
                                    "namespace": payload["namespace"], "source_event_id": payload["source_event_id"],
                                    "es_doc": doc_key, "payload": payload, "first_seen": time.time(),
                                    "webhook": {"state": "pending", "attempts": 0}, "comments": [],
                                    "reopened": reopened}
            log.info("%s %s [%s] %s", "reopened" if reopened else "opened", tid, payload["priority"], payload["title"])
            opened.append(tid)
        return opened

    # ---------------------------------------------------------------- webhook side
    def fire_pending(self) -> None:
        with self.lock:
            due = [i for i in self.issues.values()
                   if i["webhook"]["state"] == "pending" and time.time() - i["first_seen"] >= self.settle]
        for issue in due:
            self._fire(issue)

    def _fire(self, issue: dict[str, Any]) -> None:
        tid = issue["id"]
        try:
            st, body = _request("POST", self.webhook, issue["payload"], timeout=10.0)
        except (urllib.error.URLError, OSError) as exc:
            st, body = 0, str(exc)
        with self.lock:   # the HTTP handler reads webhook state under the same lock
            wh = issue["webhook"]
            wh["attempts"] += 1
            wh.pop("error", None)
            if st == 200 and isinstance(body, dict):
                wh.update(state="sent", job_id=body.get("job_id"), job_status=body.get("status"),
                          duplicate=bool(body.get("duplicate")), sent=now_iso())
            elif st == 0 or st >= 500:
                wh["error"] = f"{st or 'unreachable'}: {str(body)[:200]}"
            else:   # 4xx: the payload is wrong; retrying will not help
                wh.update(state="error", error=f"{st}: {str(body)[:300]}")
        if wh["state"] == "error":
            log.error("%s: webhook rejected %s", tid, wh["error"])
        elif wh["state"] == "pending":
            self._warn(tid, f"webhook for {tid} not delivered ({wh['error']}); retrying every poll")
        elif not body.get("duplicate"):
            log.info("%s -> webhook ok, job %s", tid, body.get("job_id"))
        elif body.get("status") in ("done", "failed"):
            log.warning("%s: the server already finished job %s (%s) for this exact ticket and will not comment "
                        "again. Restart the server to re-investigate.", tid, body.get("job_id"), body.get("status"))
        else:
            log.info("%s: the server is already running job %s for it; its comment will land here", tid,
                     body.get("job_id"))

    def _warn(self, key: str, msg: str, every: float = 30.0) -> None:
        if time.time() - self._last_warn.get(key, 0) >= every:
            self._last_warn[key] = time.time()
            log.warning(msg)

    def poll_forever(self, interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.scan()
            except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                self._warn("es", f"cannot read tickets from {self.es_url}: {exc}")
            self.fire_pending()
            stop.wait(interval)

    # ---------------------------------------------------------------- ticket API
    def add_comment(self, tid: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict) or not isinstance(body.get("body"), str) or not body["body"].strip():
            return 400, {"error": "body must be a JSON object with a non-empty 'body' string"}
        with self.lock:
            issue = self.issues.get(tid)
            if issue is None:
                return 404, {"error": f"no issue {tid}"}
            comment = {"id": str(len(issue["comments"]) + 1), "issue_id": tid, "created": now_iso(),
                       "author": body.get("author") or "triagent", "job_id": body.get("job_id"),
                       "status": body.get("status"), "body": body["body"]}
            issue["comments"].append(comment)
        first = body["body"].strip().splitlines()[0]
        log.info("%s <- comment %s from %s (job %s, %d lines): %s", tid, comment["id"], comment["author"],
                 comment["job_id"], body["body"].count("\n") + 1, first)
        return 201, {k: comment[k] for k in COMMENT_RESPONSE_FIELDS}

    def get_issue(self, tid: str) -> dict[str, Any] | None:
        with self.lock:
            issue = self.issues.get(tid)
            return json.loads(json.dumps({k: v for k, v in issue.items() if k != "first_seen"})) if issue else None

    def list_issues(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = [{"id": i["id"], "title": i["title"], "priority": i["priority"], "created": i["created"],
                     "namespace": i["namespace"], "webhook": dict(i["webhook"]), "comments": len(i["comments"]),
                     "reopened": i["reopened"]} for i in self.issues.values()]
        return sorted(rows, key=lambda r: r["created"], reverse=True)


def make_handler(mock: Mock) -> type[BaseHTTPRequestHandler]:
    comment_re = re.compile(r"^/issue/([^/]+)/comment/?$")
    issue_re = re.compile(r"^/issue/([^/]+)/?$")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            m = comment_re.match(path)
            if not m:
                return self._send(404, {"error": f"no route POST {path}"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            self._send(*mock.add_comment(m.group(1), body))

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._send(200, {"ok": True, "issues": len(mock.issues), "webhook": mock.webhook})
            if path in ("/issues", "/issues/"):
                return self._send(200, mock.list_issues())
            m = issue_re.match(path)
            if m:
                issue = mock.get_issue(m.group(1))
                return self._send(200, issue) if issue else self._send(404, {"error": f"no issue {m.group(1)}"})
            self._send(404, {"error": f"no route GET {path}"})

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s " + fmt, self.address_string(), *args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mock Jira for Triagent", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--es-url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    ap.add_argument("--es-api-key", default=os.environ.get("ES_API_KEY") or None)
    ap.add_argument("--webhook", default="http://localhost:8080/webhook", help="orchestrator webhook URL")
    ap.add_argument("--host", default="127.0.0.1", help="listen address (0.0.0.0 to serve the LAN)")
    ap.add_argument("--port", type=int, default=8082, help="8081 is taken by the Expo dev server")
    ap.add_argument("--index", default=TICKET_INDEX)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between ES polls")
    ap.add_argument("--settle", type=float, default=5.0, help="seconds between seeing a ticket and raising its webhook")
    ap.add_argument("--skip-existing", action="store_true",
                    help="ignore ticket docs already in ES at startup; only tickets ingested later raise webhooks")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s jira_mock: %(message)s")

    mock = Mock(args.es_url, args.webhook, args.es_api_key, args.index, args.settle)
    if args.skip_existing:
        try:
            log.info("skipping %d ticket docs already in %s", mock.skip_existing(), args.index)
        except (RuntimeError, urllib.error.URLError, OSError) as exc:
            log.error("--skip-existing: cannot read %s: %s", args.es_url, exc)
            return 2
    server = ThreadingHTTPServer((args.host, args.port), make_handler(mock))
    stop = threading.Event()
    poller = threading.Thread(target=mock.poll_forever, args=(args.interval, stop), daemon=True, name="poller")
    poller.start()
    log.info("listening on http://%s:%d, polling %s/%s every %.0fs, webhook %s", args.host, args.port,
             args.es_url, args.index, args.interval, args.webhook)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
