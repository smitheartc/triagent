#!/usr/bin/env python3
"""Two Kibana dashboards for showing judges the simulated data center. Stdlib only, idempotent.

    python3 escluster/kibana_demo.py --kibana-url http://localhost:5601            # namespace dal1
    python3 escluster/kibana_demo.py --kibana-url http://localhost:5601 --namespace dal1 --rack B03

1. "Triagent: simulated data center"  the whole 12h window: what the cluster holds, log volume by data stream with
   every incident ticket marked, the change/deploy/login stream the agent has to pick a suspect from, and a physical
   signal (CRAC supply air) where one failure is visible.
2. "Triagent: one incident, rack <RACK>"  the rack B03 power failure from seed 7: the ticket only says "all hosts
   unreachable"; the PDU current climbs for two hours after feed B goes down for a firmware update, then feed A trips.
   This is what the agent reconstructs from the cluster.

It reads only the cluster (through Kibana), never scenario.json. It sets Kibana's display timezone to UTC (the
simulator and the dashboard text use UTC); --keep-timezone skips that. The incident dashboard's rack, PDU and time range
default to the seed 7 data the scorer loads (dcsim --seed 7 --start 2026-11-07T08:00:00Z --duration 12h); pass
--rack/--pdu/--incident-from/--incident-to for another dataset. Data views reuse es_setup.py's ids, so running
es_setup.py --kibana-url afterwards changes nothing.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from es_setup import DATA_VIEWS  # noqa: E402

ALL, LOGS, FACILITY, CHANGE, INCIDENTS = "dcsim-all", "triagent-logs", "dcsim-facility", "dcsim-change", "dcsim-incidents"
EXTRA_VIEWS = {LOGS: ("dcsim: logs", "logs-*-{ns}")}
TICKET_QUERY = 'event.code : "INC-CREATED"'


# --------------------------------------------------------------------------------------
# Kibana client
# --------------------------------------------------------------------------------------
class Kibana:
    def __init__(self, url: str, api_key: str | None, user: str | None):
        self.url = url.rstrip("/")
        self.headers = {"kbn-xsrf": "triagent"}
        if api_key:
            self.headers["Authorization"] = f"ApiKey {api_key}"
        elif user:
            self.headers["Authorization"] = "Basic " + base64.b64encode(user.encode()).decode()

    def call(self, method: str, path: str, body: bytes | None = None, ctype: str = "application/json") -> dict:
        req = urllib.request.Request(self.url + path, data=body, method=method,
                                     headers={**self.headers, **({"Content-Type": ctype} if body else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path}: {e.code} {e.read()[:500].decode(errors='replace')}")

    def data_view(self, vid: str, name: str, title: str) -> None:
        body = {"data_view": {"id": vid, "name": name, "title": title, "timeFieldName": "@timestamp"},
                "override": True}
        self.call("POST", "/api/data_views/data_view", json.dumps(body).encode())

    def import_objects(self, objects: list[dict]) -> dict:
        boundary = uuid.uuid4().hex
        ndjson = "\n".join(json.dumps(o) for o in objects).encode()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"triagent.ndjson\"\r\n"
                f"Content-Type: application/ndjson\r\n\r\n").encode() + ndjson + f"\r\n--{boundary}--\r\n".encode()
        return self.call("POST", "/api/saved_objects/_import?overwrite=true", body,
                         ctype=f"multipart/form-data; boundary={boundary}")


# --------------------------------------------------------------------------------------
# Lens building blocks
# --------------------------------------------------------------------------------------
def col_time() -> dict:
    return {"label": "@timestamp", "dataType": "date", "operationType": "date_histogram", "sourceField": "@timestamp",
            "isBucketed": True, "scale": "interval", "params": {"interval": "auto", "includeEmptyRows": True}}


def col_terms(field: str, label: str, size: int, order_by: str) -> dict:
    return {"label": label, "customLabel": True, "dataType": "string", "operationType": "terms",
            "sourceField": field, "isBucketed": True, "scale": "ordinal",
            "params": {"size": size, "orderBy": {"type": "column", "columnId": order_by}, "orderDirection": "desc",
                       "otherBucket": True, "missingBucket": False, "parentFormat": {"id": "terms"}}}


def col_count(label: str, kql: str | None = None) -> dict:
    c = {"label": label, "customLabel": True, "dataType": "number", "operationType": "count",
         "sourceField": "___records___", "isBucketed": False, "scale": "ratio", "params": {"emptyAsNull": False}}
    if kql:
        c["filter"] = {"query": kql, "language": "kuery"}
    return c


def col_metric(op: str, field: str, label: str) -> dict:
    return {"label": label, "customLabel": True, "dataType": "number", "operationType": op, "sourceField": field,
            "isBucketed": False, "scale": "ratio", "params": {"emptyAsNull": True}}


def col_static(value: float, label: str) -> dict:
    return {"label": label, "customLabel": True, "dataType": "number", "operationType": "static_value",
            "isStaticValue": True, "isBucketed": False, "scale": "ratio", "params": {"value": str(value)},
            "references": []}


def lens(oid: str, title: str, vis_type: str, layers: dict[str, tuple[str, dict, list[str]]], visualization: dict,
         query: str = "", annotation_views: dict[str, str] | None = None) -> dict:
    """layers: layer id -> (data view id, columns, column order)."""
    refs = [{"type": "index-pattern", "id": dv, "name": f"indexpattern-datasource-layer-{lid}"}
            for lid, (dv, _, _) in layers.items()]
    refs += [{"type": "index-pattern", "id": dv, "name": f"xy-visualization-layer-{lid}"}
             for lid, dv in (annotation_views or {}).items()]
    state = {"datasourceStates": {"formBased": {"layers": {
                 lid: {"columns": cols, "columnOrder": order, "incompleteColumns": {}, "sampling": 1}
                 for lid, (_, cols, order) in layers.items()}}},
             "visualization": visualization, "query": {"query": query, "language": "kuery"}, "filters": [],
             "adHocDataViews": {}, "internalReferences": []}
    # The migration versions say the state is already in today's shape; without them Kibana runs every Lens
    # migration since 7.x on it and the import fails with a 500.
    return {"type": "lens", "id": oid, "coreMigrationVersion": "8.8.0", "typeMigrationVersion": "8.9.0",
            "attributes": {"title": title, "description": "", "visualizationType": vis_type, "state": state},
            "references": refs}


def metric_tile(oid: str, title: str, dv: str, column: dict, subtitle: str) -> dict:
    return lens(oid, title, "lnsMetric", {"L": (dv, {"m": column}, ["m"])},
                {"layerId": "L", "layerType": "data", "metricAccessor": "m", "subtitle": subtitle})


def ticket_annotations(layer_id: str, labels: bool = True) -> dict:
    return {"layerId": layer_id, "layerType": "annotations", "ignoreGlobalFilters": True, "annotations": [{
        "type": "query", "id": f"{layer_id}-tickets", "label": "Incident ticket", "key": {"type": "point_in_time"},
        "timeField": "@timestamp", "filter": {"type": "kibana_query", "query": TICKET_QUERY, "language": "kuery"},
        "extraFields": ["ticket.id"], "textVisibility": labels, "textField": "ticket.id",
        "icon": "alert", "color": "#BD271E", "lineStyle": "dashed", "lineWidth": 1}]}


def xy(layers: list[dict], legend: str = "right", y_title: str | None = None) -> dict:
    v = {"legend": {"isVisible": True, "position": legend, "shouldTruncate": False}, "valueLabels": "hide",
         "fittingFunction": "Linear", "preferredSeriesType": layers[0].get("seriesType", "line"), "layers": layers,
         "axisTitlesVisibilitySettings": {"x": False, "yLeft": bool(y_title), "yRight": False},
         "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True}}
    if y_title:
        v["yTitle"] = y_title
    return v


def saved_search(oid: str, title: str, dv: str, query: str, columns: list[str]) -> dict:
    source = {"query": {"query": query, "language": "kuery"}, "filter": [],
              "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"}
    return {"type": "search", "id": oid, "attributes": {
                "title": title, "description": "", "columns": columns, "sort": [["@timestamp", "asc"]],
                "grid": {}, "hideChart": True, "isTextBasedQuery": False,
                "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(source)}},
            "references": [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern",
                            "id": dv}]}


def dashboard(oid: str, title: str, description: str, time_from: str, time_to: str,
              panels: list[tuple[dict | str, tuple[int, int, int, int]]]) -> dict:
    """panels: (saved object or markdown text, (x, y, w, h)) on Kibana's 48-column grid."""
    out, refs = [], []
    for n, (obj, (x, y, w, h)) in enumerate(panels):
        pid = str(n + 1)
        grid = {"x": x, "y": y, "w": w, "h": h, "i": pid}
        if isinstance(obj, str):
            out.append({"type": "visualization", "panelIndex": pid, "gridData": grid, "embeddableConfig": {
                "hidePanelTitles": True, "enhancements": {}, "savedVis": {
                    "title": "", "description": "", "type": "markdown", "uiState": {},
                    "params": {"fontSize": 12, "openLinksInNewTab": False, "markdown": obj},
                    "data": {"aggs": [], "searchSource": {"query": {"query": "", "language": "kuery"},
                                                          "filter": []}}}}})
            continue
        tile = obj["attributes"].get("visualizationType") == "lnsMetric"   # the metric shows its own title
        out.append({"type": obj["type"], "panelIndex": pid, "gridData": grid,
                    "embeddableConfig": {"enhancements": {}, **({"hidePanelTitles": True} if tile else {})},
                    "panelRefName": f"panel_{pid}"})
        refs.append({"name": f"{pid}:panel_{pid}", "type": obj["type"], "id": obj["id"]})
    return {"type": "dashboard", "id": oid, "attributes": {
                "title": title, "description": description, "timeRestore": True, "timeFrom": time_from,
                "timeTo": time_to, "refreshInterval": {"pause": True, "value": 0},
                "optionsJSON": json.dumps({"useMargins": True, "syncColors": False, "syncCursor": True,
                                           "syncTooltips": False, "hidePanelTitles": False}),
                "panelsJSON": json.dumps(out),
                "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"},
                                                                          "filter": []})}},
            "references": refs}


# --------------------------------------------------------------------------------------
# the two dashboards
# --------------------------------------------------------------------------------------
OVERVIEW_TEXT = """### What the agent is searching
A deterministic simulator (`dcsim`) wrote **12 hours of one data center** into Elasticsearch: four rows of racks,
CRAC cooling units, dual-feed PDUs, top-of-rack switches, load balancers, compute hosts running Kubernetes, the apps
on them, badge readers, firewalls, change tickets and deploys. Every stream uses ECS fields, so the agent pivots on
`host.name`, `labels.rack`, `observer.name`, `user.name`, `change.id`.

**Nine failures are buried in it** (cooling, power, network optic, disk, memory leak, bad config push, expired
certificate, intrusion, disk fill), each with the quiet precursors a real one has, plus **red herrings**: a rack door
left open, harmless ECC errors, an unrelated credential rotation, a blocked brute force, a routine deploy.

The **tickets (red markers) carry symptoms only**, the way a pager alert does. The cause is never in the ticket; the
agent has to find it in the data below, cite the `event.id`s, and survive a second model checking those citations."""

INCIDENT_TEXT = """### One incident, start to finish: rack {rack}
**What the ticket says** (14:01): *Rack {rack}: all hosts unreachable (ping + BMC).* Nothing about power.

**What the cluster shows** (read the table bottom-left, top to bottom):
1. 11:38 change ticket: firmware upgrade on PDU **feed B**, feed B goes offline, the rack runs on feed A alone
2. 12:23 the firmware update **stalls**; feed B never comes back
3. the chart: **feed A current climbs** for two hours while new load lands in the same rack
4. 13:06 onward: 80% load warnings, then 95%, on {pdu}
5. 13:58 **feed A breaker trips**; every host in the rack drops; the ticket follows three minutes later

The answer the agent has to produce: *cause {pdu}, category power*, with the change, the stall, the warnings and the
trip as a cited causal chain, and the 80% warnings named as symptoms, not the cause (a metric threshold is never the
root cause)."""


def build(ns: str, rack: str, pdu: str, day_from: str, day_to: str, inc_from: str, inc_to: str) -> list[dict]:
    p = f"triagent-{ns}-"
    tile = {
        "docs": metric_tile(p + "docs", "Documents", ALL, col_count("Documents"), "logs and metrics, 12h"),
        "streams": metric_tile(p + "streams", "Data streams", ALL,
                               col_metric("unique_count", "data_stream.dataset", "Data streams"), "ECS datasets"),
        "hosts": metric_tile(p + "hosts", "Hosts", ALL, col_metric("unique_count", "host.name", "Hosts"),
                             "plus PDUs, CRACs, switches"),
        "tickets": metric_tile(p + "tickets", "Incident tickets", INCIDENTS, col_count("Tickets", TICKET_QUERY),
                               "symptoms only"),
    }
    logs_time = lens(
        p + "logs-over-time", "Log events by data stream (tickets marked)", "lnsXY",
        {"L": (LOGS, {"t": col_time(), "ds": col_terms("data_stream.dataset", "Data stream", 10, "n"),
                      "n": col_count("Events")}, ["ds", "t", "n"])},
        xy([{"layerId": "L", "layerType": "data", "seriesType": "bar_stacked", "xAccessor": "t",
             "splitAccessor": "ds", "accessors": ["n"]},
            {**ticket_annotations("A"), "indexPatternId": LOGS}]),
        annotation_views={"A": LOGS})
    crac = lens(
        p + "crac-supply", "CRAC supply air temperature by unit", "lnsXY",
        {"L": (FACILITY, {"t": col_time(), "u": col_terms("observer.name", "CRAC unit", 8, "v"),
                          "v": col_metric("max", "datacenter.crac.supply_air_temp_c", "Supply air (C)")},
               ["u", "t", "v"])},
        xy([{"layerId": "L", "layerType": "data", "seriesType": "line", "xAccessor": "t", "splitAccessor": "u",
             "accessors": ["v"]},
            {**ticket_annotations("A", labels=False), "indexPatternId": INCIDENTS}], legend="right",
           y_title="Supply air (C)"),
        query="data_stream.dataset : \"facility.crac\"", annotation_views={"A": INCIDENTS})
    tickets = saved_search(p + "tickets", "Incident tickets (symptoms only)", INCIDENTS, TICKET_QUERY,
                           ["ticket.id", "ticket.priority", "ticket.title"])
    suspects = saved_search(
        p + "suspects", "Changes, deploys and IAM events: the suspects (and the red herrings)", ALL,
        'data_stream.dataset : ("change.management" or "cicd.deploy" or "security.iam")',
        ["event.code", "change.id", "service.name", "user.name", "message"])

    pdu_current = lens(
        p + "pdu-current", f"PDU current, rack {rack} (feed A vs feed B)", "lnsXY",
        {"L": (FACILITY, {"t": col_time(), "u": col_terms("observer.name", "PDU", 4, "v"),
                          "v": col_metric("max", "datacenter.pdu.current_a", "Current (A)")}, ["u", "t", "v"]),
         "R": (FACILITY, {"r1": col_static(30, "Breaker rating 30 A"), "r2": col_static(24, "80% continuous 24 A")},
               ["r1", "r2"])},
        xy([{"layerId": "L", "layerType": "data", "seriesType": "line", "xAccessor": "t", "splitAccessor": "u",
             "accessors": ["v"]},
            {"layerId": "R", "layerType": "referenceLine", "accessors": ["r1", "r2"], "yConfig": [
                {"forAccessor": "r1", "axisMode": "left", "color": "#BD271E", "lineStyle": "dashed",
                 "textVisibility": True},
                {"forAccessor": "r2", "axisMode": "left", "color": "#D6BF57", "lineStyle": "dotted",
                 "textVisibility": True}]},
            {**ticket_annotations("A"), "indexPatternId": INCIDENTS}], y_title="Amps"),
        query=f'data_stream.dataset : "facility.pdu" and observer.name : pdu-{rack.lower()}-*',
        annotation_views={"A": INCIDENTS})
    rack_events = lens(
        p + "rack-events", f"Rack {rack}: events per minute by stream", "lnsXY",
        {"L": (LOGS, {"t": col_time(), "ds": col_terms("data_stream.dataset", "Data stream", 8, "n"),
                      "n": col_count("Events")}, ["ds", "t", "n"])},
        xy([{"layerId": "L", "layerType": "data", "seriesType": "bar_stacked", "xAccessor": "t",
             "splitAccessor": "ds", "accessors": ["n"]},
            {**ticket_annotations("A"), "indexPatternId": INCIDENTS}]),
        query=f'labels.rack : "{rack}"', annotation_views={"A": INCIDENTS})
    timeline = saved_search(
        p + "rack-timeline", f"Rack {rack}: facility alarms, changes and the ticket", ALL,
        f'(labels.rack : "{rack}" or observer.name : pdu-{rack.lower()}-* or message : "{rack}") '
        'and data_stream.dataset : ("facility.alarms" or "change.management" or "physical.access" '
        'or "incident.tickets" or "cicd.deploy")',
        ["event.code", "observer.name", "user.name", "message"])

    overview = dashboard(
        p + "overview", f"Triagent: simulated data center ({ns})",
        "12h of dcsim data with nine hidden failures and red herrings; tickets carry symptoms only.",
        day_from, day_to,
        [(OVERVIEW_TEXT, (0, 0, 24, 15)),
         (tile["docs"], (24, 0, 12, 7)), (tile["streams"], (36, 0, 12, 7)),
         (tile["hosts"], (24, 7, 12, 8)), (tile["tickets"], (36, 7, 12, 8)),
         (logs_time, (0, 15, 48, 14)),
         (tickets, (0, 29, 20, 14)), (crac, (20, 29, 28, 14)),
         (suspects, (0, 43, 48, 16))])
    incident = dashboard(
        p + "incident", f"Triagent: one incident, rack {rack} ({ns})",
        f"The rack {rack} power failure: what the ticket says vs what the cluster shows.",
        inc_from, inc_to,
        [(INCIDENT_TEXT.format(rack=rack, pdu=pdu), (0, 0, 18, 22)),
         (pdu_current, (18, 0, 30, 22)),
         (timeline, (0, 22, 30, 18)), (rack_events, (30, 22, 18, 18))])
    return [*tile.values(), logs_time, crac, tickets, suspects, pdu_current, rack_events, timeline,
            overview, incident]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kibana-url", default="http://localhost:5601")
    ap.add_argument("--api-key", help="Kibana/Elastic API key (Elastic Cloud)")
    ap.add_argument("--user", help="user:password for basic auth")
    ap.add_argument("--namespace", default="dal1")
    ap.add_argument("--from", dest="day_from", default="2026-11-07T08:00:00.000Z")
    ap.add_argument("--to", dest="day_to", default="2026-11-07T20:00:00.000Z")
    ap.add_argument("--rack", default="B03", help="rack for the incident dashboard")
    ap.add_argument("--pdu", default="pdu-b03-a", help="the PDU that trips, named in the incident text")
    ap.add_argument("--incident-from", default="2026-11-07T11:15:00.000Z")
    ap.add_argument("--incident-to", default="2026-11-07T14:20:00.000Z")
    ap.add_argument("--keep-timezone", action="store_true",
                    help="leave Kibana's dateFormat:tz alone (default sets it to UTC so times match the dashboard text)")
    args = ap.parse_args()

    kb = Kibana(args.kibana_url, args.api_key, args.user)
    if not args.keep_timezone:
        kb.call("POST", "/api/kibana/settings", json.dumps({"changes": {"dateFormat:tz": "UTC"}}).encode())
    # dal1 keeps the shared data view ids es_setup.py creates. Any other namespace gets its own (dcsim-all-demo, ...),
    # so building its dashboards never repoints the dal1 ones.
    views = {**DATA_VIEWS, **EXTRA_VIEWS}
    suffix = "" if args.namespace == "dal1" else f"-{args.namespace}"
    for vid, (name, title) in views.items():
        kb.data_view(vid + suffix, f"{name} ({args.namespace})" if suffix else name, title.format(ns=args.namespace))
    objects = build(args.namespace, args.rack.upper(), args.pdu, args.day_from, args.day_to,
                    args.incident_from, args.incident_to)
    if suffix:
        text = json.dumps(objects)
        for vid in views:
            text = text.replace(f'"{vid}"', f'"{vid}{suffix}"')
        objects = json.loads(text)
    res = kb.import_objects(objects)
    if not res.get("success"):
        sys.exit("import failed: " + json.dumps(res.get("errors"), indent=1)[:3000])
    print(f"imported {res.get('successCount')} objects", file=sys.stderr)
    for d in (o for o in objects if o["type"] == "dashboard"):
        print(f"  {d['attributes']['title']}: {args.kibana_url.rstrip('/')}/app/dashboards#/view/{d['id']}")


if __name__ == "__main__":
    main()
