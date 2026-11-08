"""Elasticsearch tools for the investigator, plus their OpenAI function schemas.

Every tool returns compact JSON. Raw _source never leaves this module. Caps (do not raise them to make one
scenario pass; fix the prompt or the ranking instead):
  search_events size <= 50, aggregate top <= 30, metric_series <= 60 points, find_changes size <= 50,
  message trimmed to 200 chars.

One ESTools instance per investigation: it knows the namespace and the ticket time (the default end of every
time window, because the investigation walks backward from the ticket).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

import topology

log = logging.getLogger("triagent.tools")

MAX_SEARCH = 50
MAX_TOP = 30
MAX_POINTS = 60
MSG_CHARS = 200
DOC_STR_CHARS = 300
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.@]+$")
_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")

# Shorthands the model tends to use for pivot fields.
ALIASES = {
    "host": "host.name", "hostname": "host.name", "service": "service.name", "observer": "observer.name",
    "device": "observer.name", "user": "user.name", "row": "labels.row", "rack": "labels.rack",
    "cluster": "labels.cluster", "code": "event.code", "event_code": "event.code", "action": "event.action",
    "level": "log.level", "change": "change.id", "change_id": "change.id", "pod": "kubernetes.pod.name",
    "source_ip": "source.ip", "commit": "cicd.commit", "dataset": "data_stream.dataset",
}
LEVELS = ["info", "warning", "error", "critical"]

# Compact row: CLAUDE.md fields first, then pivot ids only when present on the doc.
_ROW_FIELDS = ["event.code", "event.action", "log.level", "host.name", "service.name", "observer.name", "user.name"]
_ROW_EXTRA = ["change.id", "cicd.commit", "service.version", "source.ip", "kubernetes.pod.name"]
_ROW_SOURCE = ["@timestamp", "event.id", "message", "labels", *_ROW_FIELDS, *_ROW_EXTRA]
# get_doc drops these; they are constant or redundant and only cost tokens.
_DOC_DROP = ("data_stream.", "event.kind", "event.category", "event.type", "event.module", "event.severity",
             "event.dataset", "labels.dc", "labels.room", "host.hostname")

# find_changes sources: change management, CI/CD, sudo + logins, firewall rule changes.
_CHANGE_CODES_FILTER = {
    "security.auth": ["AUTH-SUDO", "AUTH-SSH-SUCCESS"],
    "security.firewall": ["FW-RULE-CHANGE"],
}
_ENTITY_FIELDS = ["host.name", "service.name", "observer.name", "user.name", "change.configuration_item",
                  "labels.row", "labels.rack", "labels.cluster", "cicd.pipeline"]


# ---------------------------------------------------------------------------------------------
# schemas (names are part of the prompts; rename everywhere or not at all)
# ---------------------------------------------------------------------------------------------
def _fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required or []}}}


_TIME = {
    "lookback": {"type": "string", "description": "Window length ending at `end`: 15m, 1h, 3h, 6h, 12h. Default 1h."},
    "end": {"type": "string", "description": "ISO-8601 end of the window. Default: the ticket time."},
    "start": {"type": "string", "description": "ISO-8601 start; overrides lookback."},
}
_FILTERS = {
    "filters": {"type": "object", "description": "Exact-match filters, field -> value or list of values. "
                "Keyword fields: host.name, service.name, observer.name, user.name, source.ip, change.id, "
                "kubernetes.pod.name, event.code, event.action, log.level, labels.row, labels.rack, labels.cluster.",
                "additionalProperties": True},
    "exclude": {"type": "object", "description": "Same shape as filters; matching docs are excluded.",
                "additionalProperties": True},
    "query": {"type": "string", "description": "Optional full-text query on message. All words must match; "
              "write OR between words to match any of them."},
    "dataset": {"type": "string", "description": "Data stream dataset or pattern, e.g. k8s.events, security.*, "
                "app.logs. Default: all."},
}
_EIDS = {"type": "array", "items": {"type": "string"}}

_SUBMIT_PROPS = {
    "problem": {"type": "string", "description": "What is broken, in one or two sentences, for the on-call engineer."},
    "root_cause": {"type": "object", "properties": {
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": ["cooling", "power", "network", "storage", "memory", "deploy",
                                                 "config", "certificate", "security", "unknown"]},
        "entity": {"type": "string", "description": "The entity where the cause happened (host, device, service, "
                   "PDU, CRAC, load balancer, user...)."},
        "entity_type": {"type": "string"},
        "identifiers": {"type": "object", "description": "IDs that pin the cause: change_id, commit, version, "
                        "file, line, user, interface, source_ip... Only values seen in retrieved docs.",
                        "additionalProperties": True},
        "event_ids": {**_EIDS, "description": "event.id values of the root-cause event(s)."},
    }, "required": ["summary", "category", "entity", "entity_type", "event_ids"]},
    "causal_chain": {"type": "array", "description": "Time-ordered, cause first, ending before the ticket.",
                     "items": {"type": "object", "properties": {
                         "timestamp": {"type": "string"}, "event_id": {"type": "string"},
                         "what": {"type": "string"}, "why_it_matters": {"type": "string"}},
                         "required": ["timestamp", "event_id", "what", "why_it_matters"]}},
    "rejected_candidates": {"type": "array", "items": {"type": "object", "properties": {
        "what": {"type": "string"}, "event_ids": _EIDS, "why_rejected": {"type": "string"}},
        "required": ["what", "event_ids", "why_rejected"]}},
    "affected": {"type": "object", "description": "Affected services, hosts, racks...", "additionalProperties": True},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "open_questions": {"type": "array", "items": {"type": "string"}},
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn("search_events", "Find log events. Returns compact rows (newest first by default), at most 50.",
        {**_FILTERS, **_TIME,
         "min_level": {"type": "string", "enum": LEVELS, "description": "Only events at or above this level."},
         "include_metrics": {"type": "boolean", "description": "Also search metrics-* docs. Default false."},
         "sort": {"type": "string", "enum": ["desc", "asc"], "description": "Default desc (walk backward)."},
         "size": {"type": "integer", "description": "Rows to return, default 20, max 50."}}),
    _fn("aggregate", "Count docs by a keyword field, with first/last seen time per value. Use before reading raw "
        "events: e.g. field=event.code to see what happened, field=host.name to see where.",
        {"field": {"type": "string", "description": "Keyword field to group by."}, **_FILTERS, **_TIME,
         "min_level": {"type": "string", "enum": LEVELS},
         "include_metrics": {"type": "boolean", "description": "Also count metrics-* docs. Default false."},
         "top": {"type": "integer", "description": "Buckets to return, default 10, max 30."}},
        ["field"]),
    _fn("metric_series", "Downsampled time series (<= 60 points) of one numeric metric field, with min/max/first/"
        "last. Use to see when something started trending.",
        {"dataset": {"type": "string", "description": "Metrics dataset, e.g. system.host, app.service, bmc.sensors."},
         "field": {"type": "string", "description": "Numeric field, e.g. datacenter.host.mem_used_pct."},
         "filters": _FILTERS["filters"], "stat": {"type": "string", "enum": ["avg", "max", "min", "sum"]},
         **_TIME},
        ["dataset", "field"]),
    _fn("find_changes", "Everything that changed: change tickets, deploys and config pushes, firewall rule changes "
        "(listed first, one row per change ticket with its lifecycle, one per deploy), then logins and sudo. "
        "At most 50 rows. Default lookback 6h.",
        {"entities": {"type": "array", "items": {"type": "string"}, "description": "Names to match against host, "
                      "service, device, user, change CI, row, rack, cluster. Omit to list all changes."},
         "filters": _FILTERS["filters"],
         "query": {"type": "string", "description": "Optional words to match in the message or change title; "
                   "any word matches."}, **_TIME,
         "size": {"type": "integer", "description": "Default 30, max 50."}}),
    _fn("expand_scope", "Neighbors of an entity from the topology: its rack, row, cluster, ToR switch, PDUs, CRAC, "
        "peer hosts, services on a host, hosts and load balancer of a service.",
        {"entity": {"type": "string", "description": "Host, service, device, rack, row or cluster name."}},
        ["entity"]),
    _fn("get_doc", "Full detail of one event by event.id (flattened fields, long strings trimmed).",
        {"event_id": {"type": "string"}}, ["event_id"]),
    _fn("note_finding", "Record an intermediate finding or a rejected hypothesis with its evidence. Shown live to "
        "the on-call engineer.",
        {"finding": {"type": "string"}, "event_ids": _EIDS,
         "kind": {"type": "string", "enum": ["symptom", "precursor", "candidate_cause", "rejected", "other"]}},
        ["finding"]),
    _fn("submit_report", "Submit the final report. Ends the investigation.", _SUBMIT_PROPS,
        ["problem", "root_cause", "causal_chain", "rejected_candidates", "confidence"]),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]
SUBMIT_SCHEMA = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "submit_report")

# Follow-ups (investigator.ask) end with answer_question instead of submit_report. Kept out of TOOL_SCHEMAS so the
# first investigation never sees it.
ANSWER_SCHEMA = _fn("answer_question", "Answer the engineer's follow-up question. Ends the follow-up.",
                    {"answer": {"type": "string", "description": "A direct answer in a few sentences, plain text."},
                     "event_ids": {**_EIDS, "description": "event.id values that support the answer; only ids "
                                   "you retrieved or that appear in the earlier report or trace."},
                     "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                    ["answer", "event_ids"])
ASK_TOOL_SCHEMAS = [t for t in TOOL_SCHEMAS if t["function"]["name"] != "submit_report"] + [ANSWER_SCHEMA]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
class ToolError(ValueError):
    pass


def parse_ts(value: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_dur(value: str | int | None, default: str) -> timedelta:
    if value is None or value == "":
        value = default
    if isinstance(value, (int, float)):
        return timedelta(minutes=float(value))
    m = _DUR_RE.match(str(value))
    if not m:
        raise ToolError(f"bad duration {value!r}; use e.g. 15m, 1h, 6h")
    n, unit = int(m.group(1)), m.group(2)
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def _field(name: str) -> str:
    name = ALIASES.get(name, name)
    if not _FIELD_RE.match(name):
        raise ToolError(f"bad field name {name!r}")
    return name


def _get(src: dict, dotted: str) -> Any:
    if dotted in src:  # flat dotted keys
        return src[dotted]
    cur: Any = src
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = obj
    return out


def _trim(s: Any, n: int) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[: n - 1] + "…"
    return s


def compact_row(src: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"ts": src.get("@timestamp"), "event.id": _get(src, "event.id")}
    for f in _ROW_FIELDS:
        v = _get(src, f)
        if v is not None:
            row[f] = v
    labels = {k: v for k, v in (src.get("labels") or {}).items() if k in ("row", "rack", "cluster") and v}
    if labels:
        row["labels"] = labels
    for f in _ROW_EXTRA:
        v = _get(src, f)
        if v is not None:
            row[f] = v[0] if isinstance(v, list) and len(v) == 1 else v
    if src.get("message") is not None:
        row["message"] = _trim(src["message"], MSG_CHARS)
    return row


def _terms_clauses(filters: dict[str, Any] | None) -> list[dict]:
    out = []
    for k, v in (filters or {}).items():
        f = _field(k)
        if v is None:
            continue
        if isinstance(v, list):
            if v:
                out.append({"terms": {f: [str(x) for x in v]}})
        else:
            out.append({"term": {f: str(v) if not isinstance(v, bool) else v}})
    return out


_OBJECT_ARGS = ("filters", "exclude")
_ARRAY_ARGS = ("entities", "event_ids")


def coerce_args(args: dict[str, Any]) -> dict[str, Any]:
    """Models sometimes send an object or array argument JSON-encoded as a string. Decode it, or fail clearly."""
    out = dict(args)
    for key in (*_OBJECT_ARGS, *_ARRAY_ARGS):
        val = out.get(key)
        if not isinstance(val, str):
            continue
        if not val.strip():
            out.pop(key)
            continue
        try:
            decoded = json.loads(val)
        except json.JSONDecodeError:
            if key in _ARRAY_ARGS:          # a bare name: treat as a one-item list
                out[key] = [val]
                continue
            raise ToolError(f"{key} must be an object like {{\"service.name\": \"x\"}}, got the string {val[:80]!r}")
        want = dict if key in _OBJECT_ARGS else list
        if key in _ARRAY_ARGS and isinstance(decoded, str):
            decoded = [decoded]
        if not isinstance(decoded, want):
            raise ToolError(f"{key} must be {'an object' if want is dict else 'an array'}, got {type(decoded).__name__}")
        out[key] = decoded
    for key in _OBJECT_ARGS:
        if key in out and out[key] is not None and not isinstance(out[key], dict):
            raise ToolError(f"{key} must be an object (field -> value or list of values)")
    return out


def text_query(query: str, operator: str, fields: list[str]) -> dict[str, Any]:
    """simple_query_string with the words OR / AND turned into its | / + operators (models write them as words)."""
    q = re.sub(r"\s+OR\s+", " | ", str(query))
    q = re.sub(r"\s+AND\s+", " + ", q)
    return {"simple_query_string": {"query": q, "fields": fields, "default_operator": operator, "lenient": True}}


# find_changes grouping: one row per change ticket / deploy, the effective doc as the representative.
_CHG_PREFERENCE = {"CHG-IMPLEMENTED": 0, "CHG-APPROVED": 1, "CHG-CREATED": 2, "CHG-CLOSED": 3}
_CHG_STATE = {"CHG-CREATED": "created", "CHG-APPROVED": "approved", "CHG-IMPLEMENTED": "implemented",
              "CHG-CLOSED": "closed"}
_REAL_CHANGE_FETCH = 200   # internal fetch to group from; rows returned stay <= MAX_SEARCH


def group_changes(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse change-ticket lifecycles and deploy start/success pairs into one compact row each."""
    rows: list[dict[str, Any]] = []
    tickets: dict[str, list[dict[str, Any]]] = {}
    deploys: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for src in sources:
        code = _get(src, "event.code")
        if code in _CHG_PREFERENCE and _get(src, "change.id"):
            tickets.setdefault(str(_get(src, "change.id")), []).append(src)
        elif code in ("CICD-DEPLOY-START", "CICD-DEPLOY-SUCCESS") and _get(src, "cicd.commit"):
            deploys.setdefault((str(_get(src, "service.name")), str(_get(src, "cicd.commit"))), []).append(src)
        else:
            rows.append(compact_row(src))
    for docs in tickets.values():
        docs.sort(key=lambda d: _CHG_PREFERENCE[_get(d, "event.code")])
        row = compact_row(docs[0])
        row["change.lifecycle"] = {_CHG_STATE[_get(d, "event.code")]: d.get("@timestamp")
                                   for d in sorted(docs, key=lambda d: d.get("@timestamp") or "")}
        others = [_get(d, "event.id") for d in docs[1:]]
        if others:
            row["other_event_ids"] = others
        rows.append(row)
    for docs in deploys.values():
        docs.sort(key=lambda d: 0 if _get(d, "event.code") == "CICD-DEPLOY-SUCCESS" else 1)
        row = compact_row(docs[0])
        start = next((d for d in docs if _get(d, "event.code") == "CICD-DEPLOY-START"), None)
        if start is not None and start is not docs[0]:
            row["deploy.started"] = start.get("@timestamp")
            row["other_event_ids"] = [_get(start, "event.id")]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------------------------
class ESTools:
    """ES tool implementations bound to one investigation (namespace + ticket time)."""

    def __init__(self, es_url: str, namespace: str = "dal1", anchor: str | datetime | None = None,
                 api_key: str | None = None, timeout: float = 30.0, client: httpx.Client | None = None) -> None:
        if not re.match(r"^[a-z0-9_.]+$", namespace):
            raise ValueError(f"bad namespace {namespace!r}")
        self.ns = namespace
        if isinstance(anchor, str):
            anchor = parse_ts(anchor)
        self.anchor = anchor or datetime.now(timezone.utc)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"ApiKey {api_key}"
        self.http = client or httpx.Client(base_url=es_url.rstrip("/"), headers=headers, timeout=timeout)

    def close(self) -> None:
        self.http.close()

    # ---- plumbing --------------------------------------------------------------------------
    def _search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.http.post(f"/{index}/_search", params={"ignore_unavailable": "true", "allow_no_indices": "true"},
                           json=body)
        if r.status_code >= 400:
            raise ToolError(f"elasticsearch {r.status_code}: {r.text[:300]}")
        return r.json()

    def _index(self, dataset: str | None, kind: str = "logs") -> str:
        ds = (dataset or "*").strip()
        for p in ("logs-", "metrics-"):
            if ds.startswith(p):
                kind, ds = p[:-1], ds[len(p):]
        ds = ds.removesuffix(f"-{self.ns}")
        if not re.match(r"^[a-z0-9_.*]+$", ds):
            raise ToolError(f"bad dataset {dataset!r}")
        kinds = ["logs", "metrics"] if kind == "all" else [kind]
        return ",".join(f"{k}-{ds}-{self.ns}" for k in kinds)

    def _window(self, args: dict[str, Any], default_lookback: str = "1h") -> tuple[str, str]:
        end = parse_ts(args["end"]) if args.get("end") else self.anchor
        start = parse_ts(args["start"]) if args.get("start") else end - parse_dur(args.get("lookback"), default_lookback)
        if start >= end:
            raise ToolError("start must be before end")
        return iso(start), iso(end)

    def _bool(self, args: dict[str, Any], default_lookback: str = "1h") -> tuple[dict, str, str]:
        start, end = self._window(args, default_lookback)
        filt: list[dict] = [{"range": {"@timestamp": {"gte": start, "lte": end}}}]
        filt += _terms_clauses(args.get("filters"))
        if args.get("min_level"):
            lvl = str(args["min_level"]).lower()
            if lvl not in LEVELS:
                raise ToolError(f"min_level must be one of {LEVELS}")
            filt.append({"terms": {"log.level": LEVELS[LEVELS.index(lvl):]}})
        must = [text_query(args["query"], "and", ["message"])] if args.get("query") else []
        q = {"bool": {"filter": filt, "must": must, "must_not": _terms_clauses(args.get("exclude"))}}
        return q, start, end

    # ---- search_events ---------------------------------------------------------------------
    def search_events(self, args: dict[str, Any]) -> dict[str, Any]:
        size = max(1, min(int(args.get("size") or 20), MAX_SEARCH))
        q, start, end = self._bool(args)
        kind = "all" if args.get("include_metrics") else "logs"
        order = "asc" if str(args.get("sort", "desc")).lower() == "asc" else "desc"
        res = self._search(self._index(args.get("dataset"), kind), {
            "size": size, "query": q, "_source": _ROW_SOURCE, "track_total_hits": True,
            "sort": [{"@timestamp": order}]})
        hits = res["hits"]["hits"]
        return {"window": [start, end], "total": res["hits"]["total"]["value"], "returned": len(hits),
                "rows": [compact_row(h["_source"]) for h in hits]}

    # ---- aggregate -------------------------------------------------------------------------
    def aggregate(self, args: dict[str, Any]) -> dict[str, Any]:
        field = _field(args.get("field") or "")
        top = max(1, min(int(args.get("top") or 10), MAX_TOP))
        q, start, end = self._bool(args)
        kind = "all" if args.get("include_metrics") else "logs"
        res = self._search(self._index(args.get("dataset"), kind), {
            "size": 0, "query": q, "track_total_hits": True,
            "aggs": {"by": {"terms": {"field": field, "size": top},
                            "aggs": {"first": {"min": {"field": "@timestamp"}},
                                     "last": {"max": {"field": "@timestamp"}}}}}})
        agg = res["aggregations"]["by"]
        return {"window": [start, end], "field": field, "total_docs": res["hits"]["total"]["value"],
                "other_docs": agg.get("sum_other_doc_count", 0),
                "buckets": [{"value": b.get("key_as_string", b["key"]), "count": b["doc_count"],
                             "first": b["first"].get("value_as_string"), "last": b["last"].get("value_as_string")}
                            for b in agg["buckets"]]}

    # ---- metric_series ---------------------------------------------------------------------
    def metric_series(self, args: dict[str, Any]) -> dict[str, Any]:
        field = _field(args.get("field") or "")
        stat = str(args.get("stat") or "avg").lower()
        if stat not in ("avg", "max", "min", "sum"):
            raise ToolError("stat must be avg, max, min or sum")
        start, end = self._window(args, "3h")
        span = (parse_ts(end) - parse_ts(start)).total_seconds()
        interval = max(60, int(-(-span // MAX_POINTS)))
        interval = -(-interval // 60) * 60  # whole minutes
        filt = [{"range": {"@timestamp": {"gte": start, "lte": end}}}, *_terms_clauses(args.get("filters")),
                {"exists": {"field": field}}]
        res = self._search(self._index(args.get("dataset"), "metrics"), {
            "size": 0, "query": {"bool": {"filter": filt}}, "track_total_hits": True,
            "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": f"{interval}s",
                                              "min_doc_count": 1},
                           "aggs": {"v": {stat: {"field": field}}}},
                     "all": {"stats": {"field": field}}}})
        aggs = res.get("aggregations") or {}
        pts = [[b["key_as_string"][:16] + "Z", round(b["v"]["value"], 3)]
               for b in (aggs.get("t") or {}).get("buckets", []) if b["v"]["value"] is not None][-MAX_POINTS:]
        st = aggs.get("all") or {}
        out: dict[str, Any] = {"window": [start, end], "field": field, "stat": stat, "interval_s": interval,
                               "docs": res["hits"]["total"]["value"], "points": pts}
        if pts:
            out["summary"] = {"min": st.get("min"), "max": st.get("max"), "first": pts[0][1], "last": pts[-1][1],
                              "max_at": max(pts, key=lambda p: p[1])[0]}
        else:
            out["note"] = "no numeric values; check dataset and field names"
        return out

    # ---- find_changes ----------------------------------------------------------------------
    def find_changes(self, args: dict[str, Any]) -> dict[str, Any]:
        """Change tickets, deploys, config pushes and firewall rule changes first (one row per change ticket and
        per deploy), then logins and sudo fill the remaining rows. Newest first within the page."""
        size = max(1, min(int(args.get("size") or 30), MAX_SEARCH))
        start, end = self._window(args, "6h")
        base: list[dict] = [{"range": {"@timestamp": {"gte": start, "lte": end}}}, *_terms_clauses(args.get("filters"))]
        ents = [str(e) for e in (args.get("entities") or []) if e]
        if ents:
            base.append({"bool": {"should": [{"terms": {f: ents}} for f in _ENTITY_FIELDS], "minimum_should_match": 1}})
        must = [text_query(args["query"], "or", ["message", "change.title"])] if args.get("query") else []
        ns = self.ns
        real_index = f"logs-change.*-{ns},logs-cicd.*-{ns},logs-security.firewall-{ns}"
        real_filter = base + [{"bool": {"should": [
            {"bool": {"must_not": {"term": {"data_stream.dataset": "security.firewall"}}}},
            {"terms": {"event.code": _CHANGE_CODES_FILTER["security.firewall"]}}], "minimum_should_match": 1}}]
        real = self._search(real_index, {"size": _REAL_CHANGE_FETCH, "query": {"bool": {"filter": real_filter,
                                                                                       "must": must}},
                                         "_source": _ROW_SOURCE, "track_total_hits": True,
                                         "sort": [{"@timestamp": "desc"}]})
        rows = group_changes([h["_source"] for h in real["hits"]["hits"]])
        rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
        rows = rows[:size]

        auth_total, auth_rows = 0, []
        room = size - len(rows)
        auth_filter = base + [{"terms": {"event.code": _CHANGE_CODES_FILTER["security.auth"]}}]
        auth = self._search(f"logs-security.auth-{ns}", {
            "size": max(room, 0), "query": {"bool": {"filter": auth_filter, "must": must}},
            "_source": _ROW_SOURCE, "track_total_hits": True, "sort": [{"@timestamp": "desc"}]})
        auth_total = auth["hits"]["total"]["value"]
        auth_rows = [compact_row(h["_source"]) for h in auth["hits"]["hits"]] if room > 0 else []
        out_rows = rows + sorted(auth_rows, key=lambda r: r.get("ts") or "", reverse=True)
        real_total = real["hits"]["total"]["value"]
        return {"window": [start, end], "total": real_total + auth_total, "returned": len(out_rows),
                "counts": {"change_docs": real_total, "change_rows": len(rows), "logins_and_sudo": auth_total,
                           "logins_and_sudo_shown": len(auth_rows)},
                "note": "changes, deploys and firewall rule changes are listed first, one row per change ticket "
                        "(change.lifecycle) and per deploy; logins and sudo fill the remaining rows",
                "rows": out_rows}

    # ---- expand_scope ----------------------------------------------------------------------
    def expand_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        entity = str(args.get("entity") or "").strip()
        if not entity:
            raise ToolError("entity is required")
        return topology.neighbors(self._search, self.ns, entity)

    # ---- get_doc ---------------------------------------------------------------------------
    def get_doc(self, args: dict[str, Any] | str) -> dict[str, Any]:
        event_id = args if isinstance(args, str) else str(args.get("event_id") or "")
        if not event_id:
            raise ToolError("event_id is required")
        res = self._search(f"logs-*-{self.ns},metrics-*-{self.ns}",
                           {"size": 1, "query": {"term": {"event.id": event_id}}})
        hits = res["hits"]["hits"]
        if not hits:
            return {"found": False, "event_id": event_id}
        src = hits[0]["_source"]
        flat = {k: _trim(v, DOC_STR_CHARS) for k, v in _flatten(src).items()
                if not k.startswith(_DOC_DROP) and k not in _DOC_DROP}
        flat["index"] = f"{_get(src, 'data_stream.type')}-{_get(src, 'data_stream.dataset')}-{self.ns}"
        return {"found": True, "doc": flat}

    def exists(self, event_id: str) -> bool:
        return bool(self.get_doc(event_id).get("found"))

    # ---- catalog for the user message ------------------------------------------------------
    def catalog(self) -> list[dict[str, Any]]:
        """[{code, index, action, count}] for every event.code present in this namespace."""
        res = self._search(f"logs-*-{self.ns},metrics-*-{self.ns}", {
            "size": 0, "aggs": {"c": {"terms": {"field": "event.code", "size": 500}, "aggs": {
                "t": {"terms": {"field": "data_stream.type", "size": 1}},
                "d": {"terms": {"field": "data_stream.dataset", "size": 1}},
                "a": {"terms": {"field": "event.action", "size": 1}}}}}})
        out = []
        for b in res["aggregations"]["c"]["buckets"]:
            t = b["t"]["buckets"][0]["key"] if b["t"]["buckets"] else "?"
            d = b["d"]["buckets"][0]["key"] if b["d"]["buckets"] else "?"
            a = b["a"]["buckets"][0]["key"] if b["a"]["buckets"] else ""
            out.append({"code": b["key"], "index": f"{t}-{d}-{self.ns}", "action": a, "count": b["doc_count"]})
        return sorted(out, key=lambda r: (r["index"], r["code"]))

    def metric_fields(self) -> dict[str, list[str]]:
        """{dataset: [numeric datacenter.* / host.* fields]} for metric_series, from field caps."""
        out: dict[str, list[str]] = {}
        for row in self.catalog():
            if not row["index"].startswith("metrics-"):
                continue
            r = self.http.get(f"/{row['index']}/_field_caps", params={"fields": "datacenter.*,host.cpu.*",
                                                                         "include_empty_fields": "false"})
            if r.status_code >= 400:
                continue
            fields = sorted(f for f, caps in r.json().get("fields", {}).items()
                            if any(t in caps for t in ("float", "long", "double", "scaled_float", "integer")))
            if fields:
                out[row["index"].split("-")[1]] = fields
        return out

    # ---- dispatch --------------------------------------------------------------------------
    def dispatch(self, name: str, arguments: dict[str, Any] | None) -> Any:
        try:
            args = coerce_args(arguments or {})
            if name == "note_finding":
                return {"ok": True}
            if name == "submit_report":
                return args
            fn = {"search_events": self.search_events, "aggregate": self.aggregate,
                  "metric_series": self.metric_series, "find_changes": self.find_changes,
                  "expand_scope": self.expand_scope, "get_doc": self.get_doc}.get(name)
            if fn is None:
                return {"error": f"unknown tool {name!r}; tools are {TOOL_NAMES}"}
            return fn(args)
        except (ToolError, ValueError, TypeError, KeyError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        except httpx.HTTPError as exc:
            log.warning("ES request failed in %s: %s", name, exc)
            return {"error": f"elasticsearch unreachable: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a tool bug must not end the investigation
            log.exception("tool %s crashed on %r", name, arguments)
            return {"error": f"tool {name} failed: {type(exc).__name__}: {exc}"}
