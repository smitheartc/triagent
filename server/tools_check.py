"""No-LLM tool harness: run every tool against known tickets and print row counts.

Tickets come from the INC-CREATED docs in logs-incident.tickets-<namespace> (what jira_mock sees). The answer key
is never read. For each ticket the harness derives the scope the ticket itself names (service, host, rack, row,
cluster), then calls every tool the way an investigator would on its first pass.

    python3 server/tools_check.py                                # every ticket in the cluster
    python3 server/tools_check.py --ticket INC3356729            # one ticket
    python3 server/tools_check.py --sweep                        # also expand_scope on every device kind
    python3 server/tools_check.py --ticket-file t/ticket.json    # a webhook-shaped ticket file

Counts: search_events / find_changes = rows returned, aggregate = buckets, metric_series = points,
expand_scope = neighbor names, get_doc = 1 if the ticket doc is found, note_finding / submit_report = 1 if ok.
Exit status 1 if any tool returns 0 or an error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jira_mock import payload_from_doc  # noqa: E402
from tools import TOOL_NAMES, ESTools  # noqa: E402

COLUMNS = ["search_events", "aggregate", "metric_series", "find_changes", "expand_scope", "get_doc",
           "note_finding", "submit_report"]


def tickets_from_es(es_url: str, api_key: str | None, ns: str, only: str | None) -> list[dict[str, Any]]:
    t = ESTools(es_url, ns, api_key=api_key)
    try:
        flt: list[dict] = [{"term": {"event.code": "INC-CREATED"}}]
        if only:
            flt.append({"term": {"ticket.id": only}})
        res = t._search(f"logs-incident.tickets-{ns}", {"size": 100, "query": {"bool": {"filter": flt}},
                                                       "sort": [{"@timestamp": "asc"}]})
    finally:
        t.close()
    return [payload_from_doc(h["_source"], ns) for h in res["hits"]["hits"]]


def scope_of(ticket: dict[str, Any]) -> tuple[dict[str, Any], list[str], str | None]:
    """(filters, entities, primary entity) from what the ticket itself names.

    filters / primary: the most specific single scope (host, then service, then rack, row, cluster).
    entities: everything the ticket names, for find_changes (an investigator widens scope the same way).
    """
    labels = ticket.get("labels") or {}
    named = [("host.name", ticket.get("host")), ("service.name", ticket.get("service"))] + \
            [(f"labels.{lvl}", labels.get(lvl)) for lvl in ("rack", "row", "cluster")]
    named = [(f, v) for f, v in named if v]
    if not named:
        return {}, [], None
    field, primary = named[0]
    return {field: primary}, [v for _, v in named], primary


def metric_args(ticket: dict[str, Any], flt: dict[str, Any]) -> dict[str, Any]:
    if "service.name" in flt:
        return {"dataset": "app.service", "field": "datacenter.app.error_rate", "filters": flt}
    if "host.name" in flt:
        return {"dataset": "system.host", "field": "datacenter.host.mem_used_pct", "filters": flt}
    if flt:
        return {"dataset": "bmc.sensors", "field": "datacenter.bmc.inlet_temp_c", "filters": flt}
    return {"dataset": "app.service", "field": "datacenter.app.error_rate"}


def count(name: str, res: Any) -> int | str:
    if not isinstance(res, dict):
        return "?"
    if res.get("error"):
        return "ERR"
    if name in ("search_events", "find_changes"):
        return res.get("returned", 0)
    if name == "aggregate":
        return len(res.get("buckets", []))
    if name == "metric_series":
        return len(res.get("points", []))
    if name == "expand_scope":
        n = 0
        for k, v in res.items():
            if k in ("entity", "type", "found", "note", "also_seen_as"):
                continue
            n += len(v) if isinstance(v, list) else 1
        return n
    if name == "get_doc":
        return 1 if res.get("found") else 0
    if name == "note_finding":
        return 1 if res.get("ok") else 0
    if name == "submit_report":
        return 1 if res.get("problem") else 0
    return "?"


def check_ticket(tools: ESTools, ticket: dict[str, Any], verbose: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    flt, ents, primary = scope_of(ticket)
    calls = {
        "search_events": {"filters": flt, "lookback": "1h", "size": 50},
        "aggregate": {"field": "event.code", "filters": flt, "lookback": "3h", "top": 30},
        "metric_series": {**metric_args(ticket, flt), "lookback": "6h"},
        "find_changes": {"entities": ents, "lookback": "6h", "size": 50} if ents else {"lookback": "6h"},
        "expand_scope": {"entity": primary or "unknown"},
        "get_doc": {"event_id": ticket["source_event_id"]},
        "note_finding": {"finding": "harness", "event_ids": [ticket["source_event_id"]], "kind": "other"},
        "submit_report": {"problem": "harness", "root_cause": {}, "causal_chain": [], "rejected_candidates": [],
                          "confidence": 0.0},
    }
    counts, results = {}, {}
    for name in COLUMNS:
        res = tools.dispatch(name, calls[name])
        counts[name], results[name] = count(name, res), res
        if verbose:
            print(f"    {name} {json.dumps(calls[name])} -> {json.dumps(res, default=str)[:300]}")
    return counts, results


def sweep(tools: ESTools) -> list[tuple[str, Any]]:
    """expand_scope on one entity of every kind, discovered from the data."""
    def first(field: str, prefix: str = "") -> str | None:
        flt = [{"prefix": {field: prefix}}] if prefix else []
        res = tools._search(f"logs-*-{tools.ns},metrics-*-{tools.ns}", {
            "size": 0, "query": {"bool": {"filter": flt}}, "aggs": {"x": {"terms": {"field": field, "size": 1}}}})
        b = res["aggregations"]["x"]["buckets"]
        return str(b[0]["key"]) if b else None

    names = [first("host.name"), first("service.name"), first("labels.rack"), first("labels.row"),
             first("labels.cluster"), first("user.name"), first("observer.name", "tor-"),
             first("observer.name", "pdu-"), first("observer.name", "crac-"), "lb-1", "lb-2", "fw-edge-1",
             "bastion-1", "ups-1", "spine-1", "spine-2"]
    out = []
    for n in [n for n in names if n]:
        res = tools.dispatch("expand_scope", {"entity": n})
        out.append((n, count("expand_scope", res), res))
    return out


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--es-url", default=os.environ.get("ES_URL", "http://localhost:9200"))
    ap.add_argument("--es-api-key", default=os.environ.get("ES_API_KEY") or None)
    ap.add_argument("--namespace", default="dal1")
    ap.add_argument("--ticket", help="only this ticket id")
    ap.add_argument("--ticket-file", help="webhook-shaped ticket JSON instead of tickets from ES")
    ap.add_argument("--sweep", action="store_true", help="also run expand_scope on every device kind")
    ap.add_argument("-v", "--verbose", action="store_true", help="print each call and a truncated result")
    args = ap.parse_args()

    assert sorted(COLUMNS) == sorted(TOOL_NAMES), "harness out of date with tools.TOOL_NAMES"
    if args.ticket_file:
        tickets = [json.loads(Path(args.ticket_file).read_text())]
    else:
        tickets = tickets_from_es(args.es_url, args.es_api_key, args.namespace, args.ticket)
    if not tickets:
        print("no INC-CREATED tickets found; ingest dcsim data first")
        return 1

    failed = False
    short = {"search_events": "search", "aggregate": "agg", "metric_series": "series", "find_changes": "changes",
             "expand_scope": "scope", "get_doc": "doc", "note_finding": "note", "submit_report": "submit"}
    print(f"{'ticket':<11} {'scope':<28} " + " ".join(f"{short[c]:>7}" for c in COLUMNS) + "  title")
    for tk in tickets:
        tools = ESTools(args.es_url, tk.get("namespace") or args.namespace, anchor=tk["created"],
                        api_key=args.es_api_key)
        try:
            flt, _, _ = scope_of(tk)
            if args.verbose:
                print(f"  {tk['ticket_id']}: {tk['title']}")
            counts, _ = check_ticket(tools, tk, args.verbose)
        finally:
            tools.close()
        bad = [c for c in COLUMNS if counts[c] in (0, "ERR", "?")]
        failed |= bool(bad)
        scope = ",".join(f"{k.split('.')[-1] if k.startswith('labels') else k.split('.')[0]}={v}"
                         for k, v in flt.items()) or "(none)"
        print(f"{tk['ticket_id']:<11} {scope[:28]:<28} " + " ".join(f"{str(counts[c]):>7}" for c in COLUMNS)
              + f"  {tk['title'][:60]}" + (f"   <-- EMPTY: {', '.join(bad)}" if bad else ""))

    if args.sweep:
        tools = ESTools(args.es_url, args.namespace, api_key=args.es_api_key)
        try:
            print("\nexpand_scope sweep (neighbor names per entity kind):")
            for name, n, res in sweep(tools):
                failed |= n in (0, "ERR", "?")
                keys = {k: (len(v) if isinstance(v, list) else v) for k, v in res.items() if k != "entity"}
                print(f"  {name:<14} {str(n):>4}  {json.dumps(keys)[:150]}" + ("   <-- EMPTY" if n in (0, "ERR") else ""))
        finally:
            tools.close()

    print("\nall tools returned rows" if not failed else "\nsome tools returned nothing (see EMPTY)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
