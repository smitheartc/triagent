#!/usr/bin/env python3
"""
es_setup.py - Elasticsearch schema for dcsim data streams.

Creates, idempotently and via the REST API only (stdlib urllib, no third-party deps):
  * ILM policies        dcsim-metrics-short / dcsim-logs-standard / dcsim-logs-long
  * component templates dcsim-ecs (shared ECS + labels), dcsim-datacenter (datacenter.* measurements)
  * index templates     one per data stream family, pattern <type>-<dataset>-*, data_stream: {}, priority 250
  * Kibana data views   per area (facility, compute, network, app, security, change, incidents, all) with --kibana-url

The list of data streams comes from event_catalog.json (written by dcsim.py) or, failing that, from
importing EVENTS out of ../sim/dcsim.py. Adding a dataset to EVENTS is enough: rerun this.

Run this BEFORE the first dcsim.py --es-url ingest. Templates only affect new backing indices, so after a
mapping change use --reset (deletes the dcsim data streams) and ingest again.

Examples
  python3 es_setup.py --es-url http://localhost:9200
  python3 es_setup.py --es-url http://localhost:9200 --kibana-url http://localhost:5601 --verify
  python3 es_setup.py --es-url https://es.example.com:9200 --es-api-key "$KEY" --reset
  python3 es_setup.py --dump es/                       # write every request body as JSON, apply nothing
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# --------------------------------------------------------------------------------------
# mapping building blocks
# --------------------------------------------------------------------------------------
KW = {"type": "keyword", "ignore_above": 1024}
F = {"type": "float"}
L = {"type": "long"}
IP = {"type": "ip"}
D = {"type": "date"}
B = {"type": "boolean"}
TXT = {"type": "match_only_text"}
TITLE = {"type": "text", "fields": {"keyword": KW}}
CK = {"type": "constant_keyword"}


def P(**fields):
    return {"properties": fields}


# Shared ECS fields used anywhere in dcsim.py (host, service, observer, user, source, change, cicd, ticket, ...).
ECS_PROPS = {
    "@timestamp": D,
    "data_stream": P(type=CK, dataset=CK, namespace=CK),
    "event": P(id=KW, kind=KW, category=KW, type=KW, action=KW, code=KW, dataset=KW, module=KW, severity=L),
    "log": P(level=KW),
    "labels": P(dc=KW, room=KW, row=KW, rack=KW, cluster=KW),
    "message": TXT,
    "host": P(name=KW, hostname=KW, ip=IP,
              cpu=P(usage=F),
              network=P(egress=P(bytes=L))),
    "service": P(name=KW, version=KW, environment=KW),
    "observer": P(name=KW, type=KW, ingress=P(interface=P(name=KW, alias=KW))),
    "user": P(name=KW),
    "source": P(ip=IP, bytes=L),
    "destination": P(ip=IP, port=L),
    "network": P(transport=KW),
    "rule": P(name=KW, action=KW),
    "process": P(name=KW, command_line={"type": "wildcard"}),
    "file": P(path=KW, device=KW),
    "error": P(message=TXT, type=KW, code=KW),
    "orchestrator": P(type=KW, namespace=KW, cluster=P(name=KW), resource=P(type=KW, name=KW)),
    "kubernetes": P(event=P(reason=KW), node=P(name=KW), pod=P(name=KW)),
    "change": P(id=KW, title=TITLE, type=KW, risk=KW, state=KW, configuration_item=KW,
                requested_by=KW, approved_by=KW),
    "cicd": P(pipeline=KW, repository=KW, commit=KW, previous_version=KW, strategy=KW,
              files_changed=P(path=KW, line=L, diff=TXT)),
    "ticket": P(id=KW, title=TITLE, priority=KW, status=KW, source=KW, assignment_group=KW, description=TXT),
    "tls": P(server=P(x509=P(subject=P(common_name=KW), issuer=P(common_name=KW),
                             not_after=D, serial_number=KW))),
    "threat": P(technique=P(id=KW, name=KW)),
}

# Custom measurements. Field names carry units (_c, _a, _ms, _pct, _dbm, _kw), matching dcsim.py.
DATACENTER_PROPS = {
    "bmc": P(inlet_temp_c=F, cpu_temp_c=F, fan_rpm=L, psu_watts=F, psu_status=KW, sensor=KW, reading=F, threshold=F),
    "host": P(mem_used_pct=F, var_used_pct=F, iowait_pct=F, load_1m=F, cpu_throttled=B),
    "crac": P(unit=KW, supply_air_temp_c=F, return_air_temp_c=F, compressor_status=KW,
              cooling_capacity_pct=F, fan_speed_pct=F),
    "pdu": P(unit=KW, feed=KW, state=KW, current_a=F, rated_a=F, load_pct=F, power_kw=F),
    "ups": P(unit=KW, load_kw=F, battery_pct=F, on_battery=B, input_voltage=F),
    "iface": P(device=KW, interface=KW, peer=KW, oper_status=KW, rx_bytes=L, tx_bytes=L,
               crc_errors=L, out_discards=L, rx_power_dbm=F),
    "lb": P(backend=KW, queue_depth=L, maxconn=L, tls_failures=L),
    "app": P(rps=F, p99_latency_ms=F, error_rate=F, http_5xx=L, gc_pause_p99_ms=F,
             pods_ready=L, pods_desired=L, load_balancer=KW),
    "smart": P(device=KW, model=KW, health=KW, reallocated_sectors=L, pending_sectors=L,
               power_on_hours=L, read_latency_ms=F, temperature_c=F),
    "raid": P(array=KW, state=KW, active_devices=L, progress_pct=F),
    "auth": P(method=KW),
    "iam": P(role=KW, policy=KW),
    "access": P(door=KW, direction=KW),
    "tls": P(endpoint=KW, days_remaining=F, hours_remaining=F),
}

# Safety net for fields added to dcsim.py later: strings become keyword, never text.
DYNAMIC_TEMPLATES = [
    {"strings_as_keyword": {"match_mapping_type": "string", "mapping": KW}},
]

# ILM tiers. Metrics are high volume and short lived; security/change/incident logs are kept longest.
ILM = {
    "dcsim-metrics-short": {"rollover": {"max_age": "1d", "max_primary_shard_size": "20gb"}, "delete_after": "3d"},
    "dcsim-logs-standard": {"rollover": {"max_age": "1d", "max_primary_shard_size": "20gb"}, "delete_after": "30d"},
    "dcsim-logs-long": {"rollover": {"max_age": "7d", "max_primary_shard_size": "20gb"}, "delete_after": "90d"},
}
LONG_LIVED = ("security.", "change.", "cicd.", "incident.", "physical.")

# Kibana data views by area. {ns} is replaced with --namespace.
DATA_VIEWS = {
    "dcsim-all":       ("dcsim: all",       "logs-*-{ns},metrics-*-{ns}"),
    "dcsim-facility":  ("dcsim: facility",  "metrics-facility.*-{ns},logs-facility.*-{ns},metrics-bmc.*-{ns},logs-bmc.*-{ns}"),
    "dcsim-compute":   ("dcsim: compute",   "metrics-system.*-{ns},logs-system.*-{ns},metrics-storage.*-{ns},logs-storage.*-{ns},logs-k8s.*-{ns}"),
    "dcsim-network":   ("dcsim: network",   "metrics-network.*-{ns},logs-network.*-{ns}"),
    "dcsim-app":       ("dcsim: app",       "metrics-app.*-{ns},logs-app.*-{ns},logs-tls.*-{ns}"),
    "dcsim-security":  ("dcsim: security",  "logs-security.*-{ns},logs-physical.*-{ns}"),
    "dcsim-change":    ("dcsim: change",    "logs-change.*-{ns},logs-cicd.*-{ns}"),
    "dcsim-incidents": ("dcsim: incidents", "logs-incident.*-{ns}"),
}


# --------------------------------------------------------------------------------------
# data stream discovery
# --------------------------------------------------------------------------------------
def load_streams(catalog_path: str) -> list[tuple[str, str]]:
    """Return sorted unique (type, dataset) pairs."""
    pairs = set()
    if catalog_path and os.path.exists(catalog_path):
        with open(catalog_path, encoding="utf-8") as fh:
            for entry in json.load(fh).values():
                pairs.add((entry["data_stream.type"], entry["data_stream.dataset"]))
        src = catalog_path
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        # dcsim.py lives in ../sim since this file moved to escluster/; keep the same-directory lookup too.
        sys.path[:0] = [here, os.path.join(os.path.dirname(here), "sim")]
        try:
            import dcsim  # noqa: WPS433
        except ImportError:
            sys.exit(f"no {catalog_path or 'event_catalog.json'} and dcsim.py not importable; run dcsim.py once or pass --catalog")
        for dtype, dataset, *_ in dcsim.EVENTS.values():
            pairs.add((dtype, dataset))
        src = "dcsim.EVENTS"
    print(f"{len(pairs)} data stream families from {src}", file=sys.stderr)
    return sorted(pairs)


# --------------------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------------------
def ilm_body(spec):
    return {"policy": {"phases": {
        "hot": {"min_age": "0ms", "actions": {"rollover": spec["rollover"]}},
        "delete": {"min_age": spec["delete_after"], "actions": {"delete": {}}}}}}


def component_bodies():
    return {
        "dcsim-ecs": {"template": {"mappings": {
            "dynamic_templates": DYNAMIC_TEMPLATES, "properties": ECS_PROPS}},
            "_meta": {"owner": "dcsim", "description": "shared ECS, labels and domain objects"}},
        "dcsim-datacenter": {"template": {"mappings": {"properties": {"datacenter": {"properties": DATACENTER_PROPS}}}},
                             "_meta": {"owner": "dcsim", "description": "datacenter.* measurements with units in names"}},
    }


def policy_for(dtype, dataset):
    if dtype == "metrics":
        return "dcsim-metrics-short"
    return "dcsim-logs-long" if dataset.startswith(LONG_LIVED) else "dcsim-logs-standard"


def index_template_body(dtype, dataset, replicas):
    return {
        "index_patterns": [f"{dtype}-{dataset}-*"],
        "data_stream": {},
        "priority": 250,  # above the built-in logs/metrics templates (100)
        "composed_of": ["dcsim-ecs", "dcsim-datacenter"],
        "template": {"settings": {
            "index.lifecycle.name": policy_for(dtype, dataset),
            "index.number_of_shards": 1,
            "index.number_of_replicas": replicas,
            "index.refresh_interval": "5s",
            "index.mapping.total_fields.limit": 2000,
            "index.codec": "best_compression"}},
        "_meta": {"owner": "dcsim", "dataset": dataset, "type": dtype},
    }


def template_name(dtype, dataset):
    return f"dcsim-{dtype}-{dataset}"


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
class Client:
    def __init__(self, base, args, kibana=False):
        self.base = base.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if kibana:
            self.headers["kbn-xsrf"] = "true"
        if args.es_api_key:
            self.headers["Authorization"] = f"ApiKey {args.es_api_key}"
        elif args.es_user:
            tok = base64.b64encode(f"{args.es_user}:{args.es_password or ''}".encode()).decode()
            self.headers["Authorization"] = f"Basic {tok}"
        self.ctx = ssl.create_default_context()
        if args.insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, method, path, body=None, ok=(200, 201), quiet_404=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code == 404 and quiet_404:
                return 404, {}
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = raw[:500].decode(errors="replace")
            sys.exit(f"{method} {path} -> HTTP {e.code}: {json.dumps(detail)[:800]}")
        except urllib.error.URLError as e:
            sys.exit(f"cannot reach {self.base}: {e.reason}")


# --------------------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------------------
def dump(dir_, name, body):
    os.makedirs(dir_, exist_ok=True)
    path = os.path.join(dir_, name + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    return path


def apply(es: Client | None, dump_dir, method, path, body, name):
    if dump_dir:
        print(f"  wrote {dump(dump_dir, name, body)}", file=sys.stderr)
        return
    es.call(method, path, body)
    print(f"  {method} {path}", file=sys.stderr)


def reset(es: Client, streams, ns):
    # One DELETE per stream: a comma list 404s as a whole when any one name is missing (a single-scenario ingest
    # creates only some streams), and then nothing at all is deleted.
    deleted = 0
    for t, d in streams:
        st, _ = es.call("DELETE", f"/_data_stream/{t}-{d}-{ns}?expand_wildcards=all", quiet_404=True)
        deleted += st != 404
    print(f"  deleted {deleted} dcsim data streams for namespace {ns}", file=sys.stderr)


def verify(es: Client, streams, ns):
    bad = 0
    for dtype, dataset in streams:
        idx = f"{dtype}-{dataset}-{ns}"
        _, sim = es.call("POST", f"/_index_template/_simulate_index/{idx}")
        t = sim.get("template", {})
        props = t.get("mappings", {}).get("properties", {})
        code = props.get("event", {}).get("properties", {}).get("code", {}).get("type")
        msg = props.get("message", {}).get("type")
        ilm = t.get("settings", {}).get("index", {}).get("lifecycle", {}).get("name", "")
        okay = code == "keyword" and msg == "match_only_text" and ilm.startswith("dcsim-")
        bad += not okay
        print(f"  {'ok  ' if okay else 'FAIL'} {idx:42s} event.code={code} message={msg} ilm={ilm or '-'}", file=sys.stderr)
    if bad:
        sys.exit(f"{bad} index name(s) would not get the dcsim template; check for higher-priority templates")
    print("  all index names resolve to dcsim templates", file=sys.stderr)


def kibana_views(kb: Client, ns):
    for vid, (name, title) in DATA_VIEWS.items():
        body = {"data_view": {"id": vid, "name": name, "title": title.format(ns=ns), "timeFieldName": "@timestamp"},
                "override": True}
        kb.call("POST", "/api/data_views/data_view", body)
        print(f"  data view {name}: {title.format(ns=ns)}", file=sys.stderr)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Create ILM policies, component/index templates and Kibana data views for dcsim.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--es-url", help="e.g. http://localhost:9200 (required unless --dump)")
    ap.add_argument("--es-user")
    ap.add_argument("--es-password")
    ap.add_argument("--es-api-key")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (local dev clusters only)")
    ap.add_argument("--kibana-url", help="e.g. http://localhost:5601; creates data views when given")
    ap.add_argument("--catalog", default="event_catalog.json", help="dcsim event catalog; falls back to importing dcsim.py")
    ap.add_argument("--namespace", default="dal1", help="data_stream.namespace used for --reset, --verify and data views")
    ap.add_argument("--replicas", type=int, default=0, help="index.number_of_replicas (0 for single-node)")
    ap.add_argument("--reset", action="store_true", help="delete existing dcsim data streams for --namespace first")
    ap.add_argument("--verify", action="store_true", help="simulate each index name and check the dcsim template wins")
    ap.add_argument("--dump", metavar="DIR", help="write all request bodies as JSON files instead of applying them")
    args = ap.parse_args()

    if not args.dump and not args.es_url:
        sys.exit("--es-url is required (or use --dump DIR)")
    if "-" in args.namespace or args.namespace != args.namespace.lower():
        sys.exit("--namespace must be lowercase without hyphens")

    streams = load_streams(args.catalog)
    es = None if args.dump else Client(args.es_url, args)
    if es:
        _, info = es.call("GET", "/")
        print(f"cluster {info.get('cluster_name')} version {info.get('version', {}).get('number')}", file=sys.stderr)
        if args.reset:
            reset(es, streams, args.namespace)

    print("ILM policies", file=sys.stderr)
    for name, spec in ILM.items():
        apply(es, args.dump, "PUT", f"/_ilm/policy/{name}", ilm_body(spec), f"ilm-{name}")

    print("component templates", file=sys.stderr)
    for name, body in component_bodies().items():
        apply(es, args.dump, "PUT", f"/_component_template/{name}", body, f"component-{name}")

    print("index templates", file=sys.stderr)
    for dtype, dataset in streams:
        name = template_name(dtype, dataset)
        apply(es, args.dump, "PUT", f"/_index_template/{name}", index_template_body(dtype, dataset, args.replicas),
              f"template-{name}")

    if es and args.verify:
        print("verify", file=sys.stderr)
        verify(es, streams, args.namespace)

    if args.kibana_url and not args.dump:
        print("kibana data views", file=sys.stderr)
        kibana_views(Client(args.kibana_url, args, kibana=True), args.namespace)

    print("done" if not args.dump else f"request bodies written to {args.dump}/ (apply with curl -XPUT ... -d @file)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
