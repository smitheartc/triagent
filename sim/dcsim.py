#!/usr/bin/env python3
"""
dcsim.py - Data center telemetry & log simulator for Elasticsearch.

Generates a realistic, causally linked stream of metrics and logs for a simulated
data center. One or more failure scenarios are seeded into the stream; each is
telegraphed by precursor signals (ramps, warnings, admin changes) before it
fails and an incident ticket is raised. Background noise and deliberate red
herrings make root-cause analysis non-trivial.

Outputs
  * NDJSON for the Elasticsearch _bulk API (default) or plain one-doc-per-line
  * optional direct indexing into Elasticsearch (data streams, op_type=create)
  * scenario.json      - answer key: root cause, causal chain, ticket, red herrings
  * event_catalog.json - every event code -> data stream, kind, category, count

Every document carries
  data_stream.{type,dataset,namespace}   -> target index <type>-<dataset>-<namespace>
  event.{code,action,kind,category,type,dataset,module,severity,id}
  labels.{dc,room,row,rack,cluster}      -> shared location keys for correlation

Examples
  python3 dcsim.py --list-scenarios
  python3 dcsim.py --scenario cooling_failure --seed 7
  python3 dcsim.py --scenario all --duration 12h
  python3 dcsim.py --scenario bad_config_change --es-url https://localhost:9200 \
      --es-api-key $KEY
  python3 dcsim.py --scenario memory_leak --stream --speed 60 --out -   # live, 60x
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
VOLTS = 208.0
PDU_RATED_A = 30.0
PDU_WARN_A = 24.0  # 80% continuous-load rule
SEV = {"debug": 7, "info": 6, "notice": 5, "warning": 4, "error": 3, "critical": 2, "alert": 1}

# --------------------------------------------------------------------------------------
# Event catalog. Every emitted document references exactly one code from this table, so
# each kind of event is differentiable by event.code / event.action and lands in a fixed
# data stream.   code: (data_stream.type, dataset, event.kind, event.category, event.type, event.action)
# --------------------------------------------------------------------------------------
EVENTS = {
    # ---- metrics -------------------------------------------------------------------
    "MET-BMC":   ("metrics", "bmc.sensors",       "metric", "host",    "info", "bmc-sensor-reading"),
    "MET-HOST":  ("metrics", "system.host",       "metric", "host",    "info", "host-metrics"),
    "MET-CRAC":  ("metrics", "facility.crac",     "metric", "host",    "info", "crac-reading"),
    "MET-PDU":   ("metrics", "facility.pdu",      "metric", "host",    "info", "pdu-reading"),
    "MET-UPS":   ("metrics", "facility.ups",      "metric", "host",    "info", "ups-reading"),
    "MET-IFACE": ("metrics", "network.interface", "metric", "network", "info", "interface-counters"),
    "MET-APP":   ("metrics", "app.service",       "metric", "web",     "info", "service-metrics"),
    "MET-SMART": ("metrics", "storage.smart",     "metric", "host",    "info", "smart-reading"),
    # ---- BMC system event log ------------------------------------------------------
    "BMC-INLET-WARN": ("logs", "bmc.sel", "event", "host", "info", "inlet-temp-upper-noncritical"),
    "BMC-INLET-CRIT": ("logs", "bmc.sel", "alert", "host", "info", "inlet-temp-upper-critical"),
    # ---- OS syslog -----------------------------------------------------------------
    "SYS-THERMAL-THROTTLE": ("logs", "system.syslog", "event", "host", "info",   "cpu-thermal-throttle"),
    "SYS-THERMAL-SHUTDOWN": ("logs", "system.syslog", "event", "host", "end",    "thermal-shutdown"),
    "SYS-EDAC-CE":          ("logs", "system.syslog", "event", "host", "error",  "edac-corrected-memory-error"),
    "SYS-CRON":             ("logs", "system.syslog", "event", "process", "info", "cron-job"),
    "SYS-NTP-SYNC":         ("logs", "system.syslog", "event", "host", "info",   "ntp-sync"),
    "SYS-SYSTEMD":          ("logs", "system.syslog", "event", "process", "info", "systemd-unit"),
    "SYS-BOOT":             ("logs", "system.syslog", "event", "host", "start",  "host-boot"),
    "SYS-IO-ERROR":         ("logs", "system.syslog", "event", "host", "error",  "block-io-error"),
    "SYS-FS-RO":            ("logs", "system.syslog", "event", "file", "error",  "filesystem-remount-ro"),
    "SYS-DISK-SPACE-WARN":  ("logs", "system.syslog", "event", "file", "info",   "disk-space-low"),
    "SYS-DISK-SPACE-CRIT":  ("logs", "system.syslog", "event", "file", "error",  "disk-space-critical"),
    "SYS-OOM-KILL":         ("logs", "system.syslog", "event", "process", "end", "oom-kill"),
    # ---- storage / RAID ------------------------------------------------------------
    "RAID-DISK-FAIL":        ("logs", "storage.raid", "event", "host", "error",  "raid-member-failed"),
    "RAID-REBUILD-START":    ("logs", "storage.raid", "event", "host", "start",  "raid-rebuild-start"),
    "RAID-REBUILD-PROGRESS": ("logs", "storage.raid", "event", "host", "info",   "raid-rebuild-progress"),
    "RAID-ARRAY-FAIL":       ("logs", "storage.raid", "alert", "host", "error",  "raid-array-failed"),
    # ---- kubernetes events ---------------------------------------------------------
    "K8S-SCHEDULED":       ("logs", "k8s.events", "event", "process", "info",  "pod-scheduled"),
    "K8S-PULLED":          ("logs", "k8s.events", "event", "process", "info",  "image-pulled"),
    "K8S-STARTED":         ("logs", "k8s.events", "event", "process", "start", "container-started"),
    "K8S-SCALED":          ("logs", "k8s.events", "event", "process", "change", "replicaset-scaled"),
    "K8S-BACKOFF":         ("logs", "k8s.events", "event", "process", "error", "container-backoff"),
    "K8S-NODE-NOTREADY":   ("logs", "k8s.events", "event", "host",    "end",   "node-not-ready"),
    "K8S-NODE-READY":      ("logs", "k8s.events", "event", "host",    "start", "node-ready"),
    "K8S-EVICTED":         ("logs", "k8s.events", "event", "process", "end",   "pod-evicted"),
    "K8S-OOMKILLED":       ("logs", "k8s.events", "event", "process", "end",   "container-oomkilled"),
    "K8S-CRASHLOOP":       ("logs", "k8s.events", "event", "process", "error", "crashloop-backoff"),
    "K8S-DISK-PRESSURE":   ("logs", "k8s.events", "event", "host",    "info",  "node-disk-pressure"),
    "K8S-CERT-RENEW-FAIL": ("logs", "k8s.events", "event", "configuration", "error", "certificate-renewal-failed"),
    # ---- network devices -----------------------------------------------------------
    "NET-XCVR-RX-LOW": ("logs", "network.device", "event", "network", "info",  "transceiver-rx-power-low"),
    "NET-LINK-DOWN":   ("logs", "network.device", "event", "network", "end",   "link-down"),
    "NET-LINK-UP":     ("logs", "network.device", "event", "network", "start", "link-up"),
    "NET-ERRDISABLE":  ("logs", "network.device", "event", "network", "end",   "port-err-disabled"),
    "NET-BGP-DOWN":    ("logs", "network.device", "event", "network", "end",   "bgp-session-down"),
    "NET-BGP-UP":      ("logs", "network.device", "event", "network", "start", "bgp-session-up"),
    # ---- load balancers ------------------------------------------------------------
    "LB-RELOAD":     ("logs", "network.lb", "event", "configuration", "change", "lb-config-reload"),
    "LB-QUEUE-HIGH": ("logs", "network.lb", "event", "web",     "info",  "lb-backend-queue-high"),
    "LB-NO-SERVER":  ("logs", "network.lb", "event", "web",     "error", "lb-no-server-available"),
    "LB-TLS-FAIL":   ("logs", "network.lb", "event", "network", "error", "lb-tls-handshake-failure"),
    # ---- application logs ----------------------------------------------------------
    "APP-ERROR":     ("logs", "app.logs", "event", "web",     "error", "app-request-errors"),
    "APP-GC-WARN":   ("logs", "app.logs", "event", "process", "info",  "app-gc-overhead"),
    "APP-OOM":       ("logs", "app.logs", "event", "process", "error", "app-out-of-memory"),
    "APP-DISK-FULL": ("logs", "app.logs", "event", "file",    "error", "app-write-no-space"),
    # ---- security ------------------------------------------------------------------
    "AUTH-SSH-SUCCESS":  ("logs", "security.auth",     "event", "authentication", "start",   "ssh-login-success"),
    "AUTH-SSH-FAIL":     ("logs", "security.auth",     "event", "authentication", "denied",  "ssh-login-failure"),
    "AUTH-SUDO":         ("logs", "security.auth",     "event", "process",        "admin",   "sudo-command"),
    "FW-DENY":           ("logs", "security.firewall", "event", "network",        "denied",  "firewall-deny"),
    "FW-ALLOW":          ("logs", "security.firewall", "event", "network",        "allowed", "firewall-allow"),
    "FW-RULE-CHANGE":    ("logs", "security.firewall", "event", "configuration",  "change",  "firewall-rule-change"),
    "FW-AUTOBLOCK":      ("logs", "security.firewall", "event", "network",        "denied",  "firewall-auto-block"),
    "IAM-ROLE-CREATE":   ("logs", "security.iam",      "event", "iam",            "creation", "iam-role-created"),
    "IAM-POLICY-ATTACH": ("logs", "security.iam",      "event", "iam",            "change",  "iam-policy-attached"),
    "IDS-EXFIL":         ("logs", "security.alerts",   "alert", "intrusion_detection", "indicator", "ids-possible-exfiltration"),
    # ---- change management / CI-CD -------------------------------------------------
    "CHG-CREATED":         ("logs", "change.management", "event", "configuration", "creation", "change-created"),
    "CHG-APPROVED":        ("logs", "change.management", "event", "configuration", "change",   "change-approved"),
    "CHG-IMPLEMENTED":     ("logs", "change.management", "event", "configuration", "change",   "change-implemented"),
    "CHG-CLOSED":          ("logs", "change.management", "event", "configuration", "end",      "change-closed"),
    "CICD-DEPLOY-START":   ("logs", "cicd.deploy", "event", "package",       "start",        "deploy-started"),
    "CICD-DEPLOY-SUCCESS": ("logs", "cicd.deploy", "event", "package",       "installation", "deploy-succeeded"),
    "CICD-CONFIG-PUSH":    ("logs", "cicd.deploy", "event", "configuration", "change",       "config-pushed"),
    # ---- facility alarms (BMS / DCIM) ----------------------------------------------
    "FAC-CRAC-HIGH-PRESSURE":   ("logs", "facility.alarms", "alert", "host", "info",   "crac-high-head-pressure"),
    "FAC-CRAC-COMPRESSOR-TRIP": ("logs", "facility.alarms", "alert", "host", "error",  "crac-compressor-trip"),
    "FAC-SUPPLY-TEMP-HIGH":     ("logs", "facility.alarms", "alert", "host", "info",   "crac-supply-temp-high"),
    "FAC-PDU-LOAD-WARN":        ("logs", "facility.alarms", "alert", "host", "info",   "pdu-load-warning"),
    "FAC-PDU-LOAD-CRIT":        ("logs", "facility.alarms", "alert", "host", "error",  "pdu-load-critical"),
    "FAC-PDU-FEED-OFFLINE":     ("logs", "facility.alarms", "alert", "host", "end",    "pdu-feed-offline"),
    "FAC-PDU-FW-STALLED":       ("logs", "facility.alarms", "alert", "host", "error",  "pdu-firmware-update-stalled"),
    "FAC-PDU-BREAKER-TRIP":     ("logs", "facility.alarms", "alert", "host", "end",    "pdu-breaker-trip"),
    "FAC-FILTER-DUE":           ("logs", "facility.alarms", "event", "host", "info",   "maintenance-filter-due"),
    "FAC-DOOR-AJAR":            ("logs", "facility.alarms", "alert", "host", "info",   "rack-door-ajar"),
    # ---- physical access -----------------------------------------------------------
    "BADGE-GRANTED": ("logs", "physical.access", "event", "authentication", "allowed", "badge-access-granted"),
    "BADGE-DENIED":  ("logs", "physical.access", "event", "authentication", "denied",  "badge-access-denied"),
    # ---- certificates / tickets ----------------------------------------------------
    "TLS-CERT-STATUS": ("logs", "tls.certs",        "state", "configuration", "info",     "certificate-scan"),
    "INC-CREATED":     ("logs", "incident.tickets", "event", "configuration", "creation", "incident-created"),
}

SERVICE_DEFS = [  # name, load balancer, internet-facing, base rps, base p99 ms
    ("web-frontend", "lb-1", True, 1800, 120),
    ("checkout-api", "lb-1", True, 450, 180),
    ("auth-svc", "lb-1", True, 900, 60),
    ("payments", "lb-2", False, 220, 250),
    ("search", "lb-2", False, 1200, 140),
    ("inventory", "lb-2", False, 380, 90),
]
ADMINS = {"jdoe": "10.50.0.11", "asmith": "10.50.0.12", "mkhan": "10.50.0.13",
          "rlee": "10.50.0.14", "tnguyen": "10.50.0.15"}
TECHS = ["ljones", "pbrown", "cwhite"]


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def parse_dur(s) -> timedelta:
    s = str(s).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in mult:
        return timedelta(seconds=float(s[:-1]) * mult[s[-1]])
    return timedelta(seconds=float(s))


def ramp(ts, start, end, shape="linear", k=4.0) -> float:
    """0 before start, 1 after end; linear / exponential / logarithmic in between."""
    if ts <= start:
        return 0.0
    if ts >= end:
        return 1.0
    x = (ts - start) / (end - start)
    if shape == "exp":
        return (math.exp(k * x) - 1) / (math.exp(k) - 1)
    if shape == "log":
        return math.log1p(k * x) / math.log1p(k)
    return x


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def h6(*parts, n=6) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:n]


def iso(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def mins(m):
    return timedelta(minutes=m)


# --------------------------------------------------------------------------------------
# topology
# --------------------------------------------------------------------------------------
class Topology:
    def __init__(self, rng, dc, rows, racks_per_row, hosts_per_rack):
        self.dc, self.room = dc, "hall-1"
        self.rows = [chr(65 + i) for i in range(rows)]
        self.racks, self.hosts, self.loc = {}, {}, {}
        self.hosts_per_rack = hosts_per_rack
        for ri, row in enumerate(self.rows):
            cluster = f"k8s-prod-{ri // 2 + 1}"
            self.loc[f"crac-{row.lower()}"] = {"row": row}
            for k in range(1, racks_per_row + 1):
                rack = f"{row}{k:02d}"
                rl = rack.lower()
                half = max(1.0, (racks_per_row - 1) / 2)
                pos = abs((k - 1) - (racks_per_row - 1) / 2) / half  # 0 = middle of row
                r = {"name": rack, "row": row, "cluster": cluster, "idx": k, "tor": f"tor-{rl}",
                     "pdus": [f"pdu-{rl}-a", f"pdu-{rl}-b"], "heat_factor": 1.15 - 0.3 * pos, "hosts": []}
                self.racks[rack] = r
                lab = {"row": row, "rack": rack, "cluster": cluster}
                for e in [r["tor"], *r["pdus"]]:
                    self.loc[e] = dict(lab)
                for j in range(1, hosts_per_rack + 1):
                    self.add_host(rng, rack, j)
        self.clusters = sorted({r["cluster"] for r in self.racks.values()})
        first = next(iter(self.racks.values()))
        for appliance in ("lb-1", "lb-2", "bastion-1", "fw-edge-1"):
            self.loc[appliance] = {"row": first["row"], "rack": first["name"]}
        self.loc["ups-1"] = {}

    def add_host(self, rng, rack, j, online_from=None, kind="cmp", **extra):
        r = self.racks[rack]
        name = f"{kind}-{rack.lower()}-{j:02d}"
        ri = self.rows.index(r["row"])
        h = {"name": name, "rack": rack, "row": r["row"], "cluster": r["cluster"],
             "ip": f"10.{10 + ri}.{r['idx']}.{10 + j}", "port": f"eth1/{j}",
             "base_cpu": rng.uniform(0.25, 0.55), "base_mem": rng.uniform(38, 60),
             "base_var": rng.uniform(30, 60), "inlet_bias": rng.uniform(-0.8, 0.8),
             "disks": ["sda", "sdb", "sdc", "sdd"],
             "realloc_base": {d: rng.choice([0, 0, 0, 0, 1, 2, 4]) for d in ["sda", "sdb", "sdc", "sdd"]},
             "power_on_hours": rng.randint(4000, 38000), "online_from": online_from, "kind": kind}
        h.update(extra)
        self.hosts[name] = h
        r["hosts"].append(name)
        self.loc[name] = {"row": r["row"], "rack": rack, "cluster": r["cluster"]}
        return h


# --------------------------------------------------------------------------------------
# simulator core
# --------------------------------------------------------------------------------------
class Sim:
    def __init__(self, args):
        self.rng = random.Random(args.seed)
        self.idrng = random.Random((args.seed or 0) ^ 0x5EED5EED)
        self.tick = parse_dur(args.tick)
        self.tick_s = self.tick.total_seconds()
        dur = parse_dur(args.duration)
        if args.start:
            self.start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).astimezone(UTC)
        else:
            self.start = (datetime.now(UTC) - dur).replace(second=0, microsecond=0)
        self.end = self.start + dur
        self.ns = args.namespace
        self.topo = Topology(self.rng, args.dc, args.rows, args.racks_per_row, args.hosts_per_rack)
        self.base_hosts = list(self.topo.hosts)
        self.docs, self.event_log, self.where_of = [], [], {}
        self.stats = Counter()
        self.mods = defaultdict(dict)
        self.flags = {}
        self.down, self.prev_down, self.down_reason = set(), set(), {}
        self.host_state = {}
        self._last, self._once = {}, set()
        self.tick_i = 0
        self.tmap = lambda dt: dt
        self._chg_seq = self.rng.randint(4000, 8000)
        self._build_services()
        self.scenarios, self.red = [], None

    # ---- world setup ---------------------------------------------------------------
    def _build_services(self):
        r = self.rng
        self.services, self.versions, self.certs = {}, {}, {}
        for i, (name, lb, ext, rps, p99) in enumerate(SERVICE_DEFS):
            cluster = self.topo.clusters[i % len(self.topo.clusters)]
            pool = [h for h in self.base_hosts if self.topo.hosts[h]["cluster"] == cluster]
            hosts = sorted(r.sample(pool, min(6, len(pool))))
            self.services[name] = {"name": name, "lb": lb, "external": ext, "rps": rps, "p99": p99,
                                   "cluster": cluster, "hosts": hosts}
            self.versions[name] = [(self.start - timedelta(days=r.randint(2, 20)),
                                    f"v{r.randint(1, 4)}.{r.randint(0, 30)}.{r.randint(0, 9)}")]
            if ext:
                self.certs[name] = {"domain": f"{name}.example.com", "lb": lb, "issuer": "dc-internal-acme-ca",
                                    "serial": h6(name, "cert", n=16),
                                    "not_after": self.start + timedelta(days=r.randint(20, 300))}

    def version_at(self, svc, ts):
        v = self.versions[svc][0][1]
        for t, ver in self.versions[svc]:
            if t <= ts:
                v = ver
        return v

    def bump(self, ver, minor=True):
        a, b, c = (int(x) for x in ver.lstrip("v").split("."))
        return f"v{a}.{b + 1}.0" if minor else f"v{a}.{b}.{c + 1}"

    def pod(self, svc, host, ts):
        ver = self.version_at(svc, ts)
        return f"{svc}-{h6(svc, ver, n=9)}-{h6(svc, ver, host, n=5)}"

    def uid(self):
        return str(uuid.UUID(int=self.idrng.getrandbits(128), version=4))

    def sha(self):
        return "%040x" % self.rng.getrandbits(160)

    def new_change_id(self):
        self._chg_seq += self.rng.randint(1, 40)
        return f"CHG{self._chg_seq:07d}"

    def public_ip(self):
        net = self.rng.choice(["203.0.113", "198.51.100", "192.0.2"])  # RFC 5737 test nets
        return f"{net}.{self.rng.randint(2, 254)}"

    def is_online(self, h, ts):
        return h["online_from"] is None or ts >= h["online_from"]

    # ---- per-tick modifiers & state --------------------------------------------------
    def mod(self, ent, key, val):
        d = self.mods[ent]
        d[key] = d.get(key, 0.0) + val

    def setm(self, ent, key, val):
        self.mods[ent][key] = val

    def m(self, ent, key, default=0.0):
        return self.mods.get(ent, {}).get(key, default)

    def every(self, key, ts, seconds):
        last = self._last.get(key)
        if last is None or (ts - last).total_seconds() >= seconds:
            self._last[key] = ts
            return True
        return False

    def once(self, key):
        if key in self._once:
            return False
        self._once.add(key)
        return True

    def poisson(self, per_hour):
        lam = per_hour * self.tick_s / 3600.0
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= L:
                return k
            k += 1

    def diurnal(self, ts):
        local_h = (ts.hour + ts.minute / 60 - 6) % 24  # US Central-ish
        return 0.75 + 0.35 * math.sin(2 * math.pi * (local_h - 10) / 24)

    def crossed(self, ts, period_s):
        if self.tick_i == 0:
            return True
        return int(ts.timestamp() // period_s) != int((ts - self.tick).timestamp() // period_s)

    # ---- document emission ------------------------------------------------------------
    def emit(self, ts, code, message, level="info", where=None, jitter=0.0, **fields):
        dtype, dataset, kind, cat, etype, action = EVENTS[code]
        if jitter:
            ts = ts + timedelta(seconds=self.rng.uniform(0, jitter))
        labels = {"dc": self.topo.dc, "room": self.topo.room}
        doc = {"@timestamp": ts,
               "data_stream": {"type": dtype, "dataset": dataset, "namespace": self.ns},
               "event": {"id": self.uid(), "kind": kind, "category": [cat], "type": [etype],
                         "action": action, "code": code, "dataset": dataset,
                         "module": dataset.split(".")[0], "severity": SEV[level]},
               "log": {"level": level},
               "labels": labels}
        if message is not None:
            doc["message"] = message
        if where:
            labels.update(self.topo.loc.get(where, {}))
            if where in self.topo.hosts:
                h = self.topo.hosts[where]
                doc["host"] = {"name": where, "hostname": where, "ip": [h["ip"]]}
        merge(doc, fields)
        self.docs.append(doc)
        self.stats[code] += 1
        if dtype == "logs":
            svc = fields.get("service", {}).get("name") if isinstance(fields.get("service"), dict) else None
            self.event_log.append((ts, code, where, svc))
        self.where_of[doc["event"]["id"]] = where
        return doc

    def k8s(self, ts, code, msg, level, host, svc=None, reason=None, pod=None, jitter=0.0, **extra):
        cluster = self.topo.hosts[host]["cluster"] if host in self.topo.hosts else None
        f = {"orchestrator": {"type": "kubernetes", "cluster": {"name": cluster}, "namespace": "prod"},
             "kubernetes": {"event": {"reason": reason or EVENTS[code][5]}, "node": {"name": host}}}
        if pod:
            f["kubernetes"]["pod"] = {"name": pod}
            f["orchestrator"]["resource"] = {"type": "pod", "name": pod}
        if svc:
            f["service"] = {"name": svc}
        merge(f, extra)
        return self.emit(ts, code, msg, level, host, jitter=jitter, **f)

    def emit_change(self, chg, title, owner, ci, created, approved=None, implemented=None, closed=None,
                    risk="low", ctype="normal", approver=None):
        approver = approver or self.rng.choice([a for a in ADMINS if a != owner])
        base = {"id": chg, "title": title, "type": ctype, "risk": risk, "configuration_item": ci,
                "requested_by": owner}
        out = {}
        for key, code, ts, state, who, lvl in (
                ("created", "CHG-CREATED", created, "new", owner, "info"),
                ("approved", "CHG-APPROVED", approved, "scheduled", approver, "info"),
                ("implemented", "CHG-IMPLEMENTED", implemented, "implemented", owner, "info"),
                ("closed", "CHG-CLOSED", closed, "closed", owner, "info")):
            if ts is None:
                continue
            c = dict(base, state=state)
            if key != "created":
                c["approved_by"] = approver
            out[key] = self.emit(ts, code, f"{chg} {state}: {title} [CI: {ci}]", lvl, ci,
                                 change=c, user={"name": who})
        return out

    def emit_deploy(self, ts, svc, ver, user, commit, duration_s=240, repo=None):
        prev = self.version_at(svc, ts)
        info = {"pipeline": f"{svc}-release", "repository": repo or f"apps/{svc}", "commit": commit,
                "previous_version": prev, "strategy": "rolling"}
        a = self.emit(ts, "CICD-DEPLOY-START", f"Deploy {svc} {prev} -> {ver} started by {user} (commit {commit[:8]})",
                      "info", None, labels={"cluster": self.services[svc]["cluster"]},
                      service={"name": svc, "version": ver}, user={"name": user}, cicd=info)
        done = ts + timedelta(seconds=duration_s)
        b = self.emit(done, "CICD-DEPLOY-SUCCESS", f"Deploy {svc} {ver} completed: {len(self.services[svc]['hosts'])}/"
                      f"{len(self.services[svc]['hosts'])} pods updated", "info", None,
                      labels={"cluster": self.services[svc]["cluster"]},
                      service={"name": svc, "version": ver}, user={"name": user}, cicd=info)
        self.versions[svc].append((done, ver))
        self.versions[svc].sort()
        return a, b

    # ---- main loop ---------------------------------------------------------------------
    def run(self):
        self.schedule_background()
        ts = self.start
        while ts < self.end:
            self.mods = defaultdict(dict)
            for sc in self.scenarios:
                sc.apply(ts)
            if self.red:
                self.red.apply(ts)
            self.emit_hosts(ts)
            self.emit_facility(ts)
            self.emit_network(ts)
            self.emit_services(ts)
            self.emit_storage(ts)
            self.emit_certs(ts)
            self.emit_noise(ts)
            self.tick_i += 1
            ts += self.tick
        self.docs = [d for d in self.docs if self.start <= d["@timestamp"] < self.end]
        self.docs.sort(key=lambda d: d["@timestamp"])

    # ---- emitters ------------------------------------------------------------------------
    def emit_hosts(self, ts):
        g, T, d = self.rng.gauss, self.tick_s, self.diurnal(ts)
        online = [h for h in self.topo.hosts.values() if self.is_online(h, ts)]
        tot = Counter(h["cluster"] for h in online)
        dn = Counter(h["cluster"] for h in online if h["name"] in self.down)
        self.host_state = {}
        for h in online:
            n = h["name"]
            if n in self.down:
                continue
            shift = 0.6 * dn[h["cluster"]] / max(1, tot[h["cluster"]])  # load moved off dead nodes
            cpu = clamp(h["base_cpu"] * d + g(0, 0.03) + self.m(n, "cpu") + shift, 0.02, 1.0)
            inlet = 22 + h["inlet_bias"] + g(0, 0.3) + self.m(n, "inlet")
            ctemp = inlet + 15 + cpu * 45 + max(0, inlet - 22) * 0.8 + g(0, 0.8)
            fan = clamp(4800 + max(0, ctemp - 55) * 110 + max(0, inlet - 24) * 250 + g(0, 60), 3000, 16500)
            mem = clamp(h["base_mem"] + g(0, 0.6) + self.m(n, "mem"), 1, 99.9)
            var = clamp(h["base_var"] + (ts - self.start).total_seconds() / 3600 * 0.05 + self.m(n, "var"), 0, 100)
            iow = clamp(0.01 + abs(g(0, 0.004)) + self.m(n, "iowait"), 0, 1)
            watts = 180 + 300 * cpu + self.m(n, "watts")
            egress = (2e6 + 8e6 * cpu) * abs(g(1, 0.05)) + self.m(n, "egress_bps")
            err = self.m(n, "err")
            throttled = ctemp >= 92

            self.emit(ts, "MET-BMC", None, where=n, jitter=2, datacenter={"bmc": {
                "inlet_temp_c": round(inlet, 2), "cpu_temp_c": round(ctemp, 2), "fan_rpm": int(fan),
                "psu_watts": round(watts, 1), "psu_status": "ok"}})
            self.emit(ts, "MET-HOST", None, where=n, jitter=2,
                      host={"cpu": {"usage": round(cpu, 4)}, "network": {"egress": {"bytes": int(egress * T)}}},
                      datacenter={"host": {"mem_used_pct": round(mem, 2), "var_used_pct": round(var, 2),
                                           "iowait_pct": round(iow * 100, 2), "load_1m": round(cpu * 32 + iow * 20, 2),
                                           "cpu_throttled": throttled}})
            if inlet >= 38 and self.every((n, "inlet-crit"), ts, 600):
                self.emit(ts, "BMC-INLET-CRIT", f"Inlet Temp | Upper Critical going high | Reading {inlet:.0f} C > Threshold 38 C",
                          "critical", n, jitter=30, datacenter={"bmc": {"sensor": "Inlet Temp", "reading": round(inlet, 1), "threshold": 38}})
            elif inlet >= 32 and self.every((n, "inlet-warn"), ts, 900):
                self.emit(ts, "BMC-INLET-WARN", f"Inlet Temp | Upper Non-critical going high | Reading {inlet:.0f} C > Threshold 32 C",
                          "warning", n, jitter=30, datacenter={"bmc": {"sensor": "Inlet Temp", "reading": round(inlet, 1), "threshold": 32}})
            if ctemp >= 100:
                self.emit(ts, "SYS-THERMAL-SHUTDOWN", f"kernel: CPU0: Package temperature above critical ({ctemp:.0f} C), "
                          "shutting down to prevent damage", "critical", n, jitter=20, process={"name": "kernel"})
                self.down.add(n)
                self.down_reason[n] = "thermal"
                continue
            if throttled and self.every((n, "throttle"), ts, 300):
                self.emit(ts, "SYS-THERMAL-THROTTLE", f"kernel: CPU{self.rng.randint(0, 63)}: Core temperature above threshold "
                          f"({ctemp:.0f} C), cpu clock throttled", "warning", n, jitter=30, process={"name": "kernel"})
            if var >= 95 and self.every((n, "var-crit"), ts, 600):
                self.emit(ts, "SYS-DISK-SPACE-CRIT", f"/var filesystem {var:.1f}% full", "error", n, jitter=30,
                          file={"path": "/var"}, datacenter={"host": {"var_used_pct": round(var, 2)}})
            elif var >= 85 and self.every((n, "var-warn"), ts, 1200):
                self.emit(ts, "SYS-DISK-SPACE-WARN", f"/var filesystem {var:.1f}% full", "warning", n, jitter=30,
                          file={"path": "/var"}, datacenter={"host": {"var_used_pct": round(var, 2)}})
            if var >= 90 and self.once((n, "diskpressure")):
                self.k8s(ts, "K8S-DISK-PRESSURE", f"Node {n} status is now: NodeHasDiskPressure", "warning", n,
                         reason="NodeHasDiskPressure", jitter=30)
            if var >= 99.5:
                err += 0.7
            self.host_state[n] = {"cpu": cpu, "throttled": throttled, "mem": mem, "var": var, "iowait": iow,
                                  "pkt": self.m(n, "pkt_loss"), "err": err, "watts": watts}
        for n in sorted(self.down - self.prev_down):
            self.k8s(ts + timedelta(seconds=40), "K8S-NODE-NOTREADY",
                     f"Node {n} status is now: NodeNotReady (kubelet stopped posting node status)", "error", n,
                     reason="NodeNotReady")
        self.prev_down = set(self.down)

    def emit_facility(self, ts):
        g = self.rng.gauss
        for row in self.topo.rows:
            crac = f"crac-{row.lower()}"
            cpus = [s["cpu"] for n, s in self.host_state.items() if self.topo.hosts[n]["row"] == row]
            sup = 18 + g(0, 0.15) + self.m(crac, "supply")
            ret = sup + 8 + 6 * (sum(cpus) / len(cpus) if cpus else 0)
            cap = clamp(100 - self.m(crac, "capacity_loss"), 0, 100)
            self.emit(ts, "MET-CRAC", None, where=crac, jitter=2, observer={"name": crac, "type": "crac"},
                      datacenter={"crac": {"unit": crac, "supply_air_temp_c": round(sup, 2), "return_air_temp_c": round(ret, 2),
                                           "compressor_status": self.flags.get((crac, "status"), "running"),
                                           "cooling_capacity_pct": round(cap, 1),
                                           "fan_speed_pct": round(clamp(55 + (sup - 18) * 4 + g(0, 1), 0, 100), 1)}})
        total_w = 0
        for rack in self.topo.racks.values():
            watts = sum(self.host_state[n]["watts"] for n in rack["hosts"] if n in self.host_state)
            total_w += watts
            live = [p for p in rack["pdus"] if not self.flags.get((p, "offline")) and not self.flags.get((p, "tripped"))]
            for p in rack["pdus"]:
                amps = watts / VOLTS / len(live) * abs(g(1, 0.01)) if p in live else 0.0
                state = "tripped" if self.flags.get((p, "tripped")) else "offline" if self.flags.get((p, "offline")) else "on"
                self.emit(ts, "MET-PDU", None, where=p, jitter=2, observer={"name": p, "type": "pdu"},
                          datacenter={"pdu": {"unit": p, "feed": p[-1].upper(), "current_a": round(amps, 2), "rated_a": PDU_RATED_A,
                                              "load_pct": round(amps / PDU_RATED_A * 100, 1), "power_kw": round(amps * VOLTS / 1000, 3),
                                              "state": state}})
                if amps >= 28.5 and self.every((p, "crit"), ts, 300):
                    self.emit(ts, "FAC-PDU-LOAD-CRIT", f"{p}: branch circuit load {amps:.1f} A ({amps / PDU_RATED_A:.0%} of breaker rating)",
                              "critical", p, jitter=20, observer={"name": p, "type": "pdu"},
                              datacenter={"pdu": {"unit": p, "current_a": round(amps, 2), "rated_a": PDU_RATED_A}})
                elif amps >= PDU_WARN_A and self.every((p, "warn"), ts, 600):
                    self.emit(ts, "FAC-PDU-LOAD-WARN", f"{p}: branch circuit load {amps:.1f} A exceeds 80% continuous rating",
                              "warning", p, jitter=20, observer={"name": p, "type": "pdu"},
                              datacenter={"pdu": {"unit": p, "current_a": round(amps, 2), "rated_a": PDU_RATED_A}})
        if self.tick_i % 5 == 0:
            self.emit(ts, "MET-UPS", None, where="ups-1", jitter=2, observer={"name": "ups-1", "type": "ups"},
                      datacenter={"ups": {"unit": "ups-1", "load_kw": round(total_w * 1.08 / 1000, 2), "battery_pct": 100,
                                          "on_battery": False, "input_voltage": round(480 + g(0, 1.5), 1)}})

    def emit_network(self, ts):
        g, T = self.rng.gauss, self.tick_s
        for rack in self.topo.racks.values():
            tor = rack["tor"]
            sts = [self.host_state[n] for n in rack["hosts"] if n in self.host_state]
            rack_bps = sum(2e6 + 8e6 * s["cpu"] for s in sts)
            for port, spine in (("eth1/49", "spine-1"), ("eth1/50", "spine-2")):
                down = bool(self.flags.get((tor, port, "down")))
                crc = 0 if down else int(self.m(tor, f"crc:{port}") * abs(g(1, 0.15))) + (1 if self.rng.random() < 0.01 else 0)
                rx = -40.0 if down else -2.4 + g(0, 0.08) + self.m(tor, f"rxpow:{port}")
                share = 0 if down else (1.0 if self.flags.get((tor, "eth1/49" if port == "eth1/50" else "eth1/50", "down")) else 0.5)
                self.emit(ts, "MET-IFACE", None, where=tor, jitter=2,
                          observer={"name": tor, "type": "switch", "ingress": {"interface": {"name": port, "alias": f"uplink-{spine}"}}},
                          datacenter={"iface": {"device": tor, "interface": port, "peer": spine, "oper_status": "down" if down else "up",
                                                "rx_bytes": int(rack_bps * share * T), "tx_bytes": int(rack_bps * share * 0.7 * T),
                                                "crc_errors": crc, "out_discards": int(self.m(tor, f"drops:{port}")),
                                                "rx_power_dbm": round(rx, 2)}})
            if self.tick_i % 5 == 0:
                for n in rack["hosts"]:
                    h = self.topo.hosts[n]
                    if not self.is_online(h, ts):
                        continue
                    st = self.host_state.get(n)
                    self.emit(ts, "MET-IFACE", None, where=tor, jitter=2,
                              observer={"name": tor, "type": "switch", "ingress": {"interface": {"name": h["port"], "alias": n}}},
                              datacenter={"iface": {"device": tor, "interface": h["port"], "peer": n,
                                                    "oper_status": "up" if st else "down",
                                                    "rx_bytes": int((2e6 + 8e6 * st["cpu"]) * T) if st else 0,
                                                    "tx_bytes": int((1.5e6 + 6e6 * st["cpu"]) * T) if st else 0,
                                                    "crc_errors": 0, "out_discards": 0, "rx_power_dbm": round(-2.1 + g(0, 0.05), 2)}})

    def emit_services(self, ts):
        g, d, T = self.rng.gauss, self.diurnal(ts), self.tick_s
        for svc in self.services.values():
            name = svc["name"]
            nb = len(svc["hosts"])
            sts = [self.host_state[n] for n in svc["hosts"] if n in self.host_state]
            down_frac = 1 - len(sts) / nb
            avg = (lambda k: sum(s[k] for s in sts) / len(sts)) if sts else (lambda k: 0.0)
            thr = sum(1 for s in sts if s["throttled"]) / nb
            rps = svc["rps"] * d * abs(g(1, 0.03)) * self.m(name, "rps_mult", 1.0)
            p99 = (svc["p99"] * (1 + 1.8 * thr) + 900 * down_frac + 1500 * avg("iowait") + 4000 * avg("pkt")
                   + self.m(name, "latency") + g(0, svc["p99"] * 0.05))
            err = 0.002 + abs(g(0, 0.0008)) + 0.5 * down_frac + 2 * avg("pkt") + avg("err") + self.m(name, "err")
            err = 1.0 if not sts else clamp(err, 0, 1)
            crashing = int(self.m(name, "crashing"))
            self.emit(ts, "MET-APP", None, jitter=2, labels={"cluster": svc["cluster"]},
                      service={"name": name, "version": self.version_at(name, ts), "environment": "prod"},
                      datacenter={"app": {"rps": round(rps, 1), "p99_latency_ms": round(max(5, p99), 1),
                                          "error_rate": round(err, 4), "http_5xx": int(rps * T * err),
                                          "gc_pause_p99_ms": round(max(1, 12 + g(0, 2) + self.m(name, "gc")), 1),
                                          "pods_ready": max(0, len(sts) - crashing), "pods_desired": nb,
                                          "load_balancer": svc["lb"]}})
            if err >= 0.03 and self.every((name, "apperr"), ts, 60):
                msg = self.flags.get((name, "errmsg"), "upstream connect error or disconnect/reset before headers")
                self.emit(ts, "APP-ERROR", f"{name}: {int(rps * T * err)} failed requests in last {int(T)}s: {msg}", "error",
                          None, jitter=20, labels={"cluster": svc["cluster"]},
                          service={"name": name, "version": self.version_at(name, ts)},
                          error={"message": msg}, datacenter={"app": {"error_rate": round(err, 4)}})

    def emit_storage(self, ts):
        if self.tick_i % 10:
            return
        g = self.rng.gauss
        for n in self.host_state:
            h = self.topo.hosts[n]
            for dsk in h["disks"]:
                failed = bool(self.flags.get((n, dsk, "failed")))
                realloc = h["realloc_base"][dsk] + int(self.m(n, f"realloc:{dsk}"))
                pending = int(self.m(n, f"pending:{dsk}"))
                health = "FAILED" if failed else ("PRE-FAIL" if realloc > 500 else "PASSED")
                self.emit(ts, "MET-SMART", None, where=n, jitter=5,
                          datacenter={"smart": {"device": f"/dev/{dsk}", "model": "ST4000NM0035", "health": health,
                                                "reallocated_sectors": realloc, "pending_sectors": pending,
                                                "read_latency_ms": None if failed else round(3.5 + abs(g(0, 0.5)) + self.m(n, f"lat:{dsk}"), 2),
                                                "temperature_c": round(31 + g(0, 1) + self.m(n, "inlet") * 0.6, 1),
                                                "power_on_hours": h["power_on_hours"] + int((ts - self.start).total_seconds() // 3600)}})

    def emit_certs(self, ts):
        if not self.crossed(ts, 3600):
            return
        for svc, c in self.certs.items():
            rem = (c["not_after"] - ts).total_seconds()
            days = rem / 86400
            if rem <= 0:
                lvl, msg = "critical", f"{c['domain']}: certificate EXPIRED {abs(rem) / 3600:.1f}h ago"
            elif days < 1:
                lvl, msg = "error", f"{c['domain']}: certificate expires in {rem / 3600:.1f} hours"
            elif days < 14:
                lvl, msg = "warning", f"{c['domain']}: certificate expires in {days:.1f} days"
            else:
                lvl, msg = "info", f"{c['domain']}: certificate valid, {days:.0f} days remaining"
            self.emit(ts, "TLS-CERT-STATUS", msg, lvl, c["lb"], jitter=60, service={"name": svc},
                      tls={"server": {"x509": {"subject": {"common_name": [c["domain"]]}, "issuer": {"common_name": [c["issuer"]]},
                                               "not_after": c["not_after"], "serial_number": c["serial"]}}},
                      datacenter={"tls": {"endpoint": f"{c['domain']}:443", "days_remaining": round(days, 3),
                                          "hours_remaining": round(rem / 3600, 2)}})

    # ---- background noise ------------------------------------------------------------------
    def schedule_background(self):
        """Routine changes and deploys that have nothing to do with any failure."""
        r, span = self.rng, (self.end - self.start)
        routine = [("Patch openssh on bastion-1", "bastion-1"), ("Rotate syslog retention to 30d on log collectors", "fw-edge-1"),
                   ("Replace air filters row {row}", "crac-{rowl}"), ("Update NTP servers on spine switches", "fw-edge-1"),
                   ("Add monitoring for UPS-1 bypass state", "ups-1")]
        for title, ci in r.sample(routine, 3):
            row = r.choice(self.topo.rows)
            title, ci = title.format(row=row), ci.format(rowl=row.lower())
            t = self.start + span * r.uniform(0.05, 0.8)
            self.emit_change(self.new_change_id(), title, r.choice(list(ADMINS)), ci, created=t - mins(r.randint(60, 600)),
                             approved=t - mins(r.randint(10, 50)), implemented=t, closed=t + mins(r.randint(20, 90)))
        for svc in r.sample(list(self.services), 3):
            t = self.start + span * r.uniform(0.05, 0.6)
            self.emit_deploy(t, svc, self.bump(self.version_at(svc, t), minor=False), "svc-deploy", self.sha())

    def emit_noise(self, ts):
        r, T = self.rng, self.tick_s
        hosts = self.base_hosts
        for _ in range(self.poisson(6)):  # routine admin logins (publickey from VPN)
            a = r.choice(list(ADMINS))
            h = r.choice(hosts)
            self.emit(ts, "AUTH-SSH-SUCCESS", f"sshd: Accepted publickey for {a} from {ADMINS[a]} port {r.randint(40000, 65000)} ssh2",
                      "info", h, jitter=T, user={"name": a}, source={"ip": ADMINS[a]}, datacenter={"auth": {"method": "publickey"}})
            if r.random() < 0.4:
                cmd = r.choice(["/usr/bin/systemctl status kubelet", "/usr/bin/journalctl -u containerd", "/usr/bin/apt list --upgradable"])
                self.emit(ts, "AUTH-SUDO", f"sudo: {a} : TTY=pts/0 ; USER=root ; COMMAND={cmd}", "info", h, jitter=T,
                          user={"name": a}, process={"command_line": cmd})
        for _ in range(self.poisson(1)):  # password typo then success
            a = r.choice(list(ADMINS))
            self.emit(ts, "AUTH-SSH-FAIL", f"sshd: Failed password for {a} from {ADMINS[a]} port {r.randint(40000, 65000)} ssh2",
                      "warning", "bastion-1", jitter=T / 2, user={"name": a}, source={"ip": ADMINS[a]}, datacenter={"auth": {"method": "password"}})
        for _ in range(self.poisson(40)):  # internet scanners hitting the edge
            ip, port = self.public_ip(), r.choice([22, 23, 445, 3389, 5900, 6379, 9200])
            self.emit(ts, "FW-DENY", f"DENY tcp {ip}:{r.randint(1024, 65000)} -> 198.18.0.10:{port} (policy: edge-default-deny)",
                      "info", "fw-edge-1", jitter=T, source={"ip": ip}, destination={"ip": "198.18.0.10", "port": port},
                      network={"transport": "tcp"}, rule={"name": "edge-default-deny"})
        for _ in range(self.poisson(18)):  # normal k8s churn
            svc = r.choice(list(self.services.values()))
            h = r.choice(svc["hosts"])
            pod = self.pod(svc["name"], h, ts)
            code, msg = r.choice([("K8S-SCHEDULED", f"Successfully assigned prod/{pod} to {h}"),
                                  ("K8S-PULLED", f"Container image \"registry.local/{svc['name']}:{self.version_at(svc['name'], ts)}\" already present on machine"),
                                  ("K8S-STARTED", f"Started container {svc['name']}")])
            self.k8s(ts, code, msg, "info", h, svc["name"], pod=pod, jitter=T)
        for _ in range(self.poisson(1)):  # benign flaky batch job
            h = r.choice(hosts)
            self.k8s(ts, "K8S-BACKOFF", "Back-off restarting failed container report-cron", "warning", h, "report-cron",
                     reason="BackOff", pod=f"report-cron-{h6(ts.date(), n=5)}", jitter=T)
        for _ in range(self.poisson(12)):
            h = r.choice(hosts)
            code, msg = r.choice([("SYS-CRON", "CRON[{p}]: (root) CMD (/usr/lib/sysstat/sa1 1 1)"),
                                  ("SYS-NTP-SYNC", "chronyd[{p}]: Selected source 10.0.0.123 (ntp1.dal1.internal)"),
                                  ("SYS-SYSTEMD", "systemd[1]: Started Daily apt download activities."),
                                  ("SYS-SYSTEMD", "systemd[1]: logrotate.service: Succeeded.")])
            self.emit(ts, code, msg.format(p=r.randint(1000, 99999)), "info", h, jitter=T)
        for _ in range(self.poisson(0.4)):
            self.edac(ts + timedelta(seconds=r.uniform(0, T)), r.choice(hosts))
        for _ in range(self.poisson(3)):
            who = r.choice(TECHS + list(ADMINS))
            door = r.choice(["hall-1-main", "hall-1-cage-2", "loading-dock", "noc"])
            self.emit(ts, "BADGE-GRANTED", f"Access granted: {who} at {door}", "info", None, jitter=T,
                      user={"name": who}, datacenter={"access": {"door": door, "direction": r.choice(["in", "out"])}})
        for _ in range(self.poisson(0.15)):
            self.emit(ts, "BADGE-DENIED", f"Access denied: visitor-badge-{r.randint(10, 99)} at hall-1-cage-2 (not authorized)",
                      "warning", None, jitter=T, datacenter={"access": {"door": "hall-1-cage-2", "direction": "in"}})
        for _ in range(self.poisson(0.2)):
            crac = f"crac-{r.choice(self.topo.rows).lower()}"
            self.emit(ts, "FAC-FILTER-DUE", f"{crac}: air filter differential pressure high, replacement due within 14 days",
                      "info", crac, jitter=T, observer={"name": crac, "type": "crac"})

    def edac(self, ts, host):
        self.emit(ts, "SYS-EDAC-CE", f"kernel: EDAC MC0: 1 CE memory read error on CPU_SrcID#0_Ha#0_Chan#{self.rng.randint(0, 3)}"
                  f"_DIMM#{self.rng.randint(0, 1)} (corrected)", "warning", host, process={"name": "kernel"})


# --------------------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------------------
class Scenario:
    name = ""
    title = ""
    default_lead = "3h"

    def __init__(self, sim: Sim, fail_at, lead):
        self.sim, self.rng = sim, sim.rng
        self.fail_at = fail_at
        self.pre = max(sim.start + mins(5), fail_at - lead)
        self.L = self.fail_at - self.pre
        self.chain, self.emergent = [], []
        self._done = set()
        self.ticket, self.root, self.affected = None, {}, {}
        self.services, self.rows, self.hosts_scope = set(), set(), set()
        self.setup()

    # helpers
    def once(self, key):
        if key in self._done:
            return False
        self._done.add(key)
        return True

    def at(self, ts, when, key):
        return ts >= when and self.once(key)

    def f(self, ts, start=None, end=None, shape="linear", k=4.0):
        return ramp(ts, start or self.pre, end or self.fail_at, shape, k)

    def add_chain(self, doc, what):
        self.chain.append({"time": doc["@timestamp"], "event_code": doc["event"]["code"],
                           "dataset": doc["data_stream"]["dataset"], "event_id": doc["event"]["id"],
                           "entity": self.sim.where_of.get(doc["event"]["id"]) or (doc.get("service") or {}).get("name")
                           or (doc.get("change") or {}).get("configuration_item") or (doc.get("user") or {}).get("name"),
                           "what": what})
        return doc

    def ev(self, ts, code, msg, level="info", where=None, chain=None, **fields):
        doc = self.sim.emit(ts, code, msg, level, where, **fields)
        if chain:
            self.add_chain(doc, chain)
        return doc

    def ticket_at(self, ts, when, title, priority, where=None, service=None, desc=""):
        if not self.at(ts, when, "ticket"):
            return
        tid = f"INC{self.rng.randint(1000000, 9999999)}"
        f = {"ticket": {"id": tid, "title": title, "priority": priority, "status": "new", "source": "alertmanager",
                        "assignment_group": "dc-ops-oncall", "description": desc}}
        if service:
            f["service"] = {"name": service}
        doc = self.sim.emit(when, "INC-CREATED", f"{tid} opened [{priority}]: {title}", "error", where, **f)
        self.ticket = {"id": tid, "created": when, "title": title, "priority": priority, "event_id": doc["event"]["id"]}

    def apply(self, ts):
        pass

    def answer(self):
        chain = list(self.chain)
        for code, desc, pred in self.emergent:
            hits = [(t, w or sv) for t, c, w, sv in self.sim.event_log
                    if c == code and t >= self.pre and (pred(w) or (sv is not None and pred(sv)))]
            if hits:
                t, w = min(hits, key=lambda x: x[0])
                chain.append({"time": t, "event_code": code, "dataset": EVENTS[code][1], "entity": w,
                              "what": desc, "occurrences": len(hits)})
        chain.sort(key=lambda c: c["time"])
        return {"scenario": self.name, "title": self.title, "precursor_start": self.pre, "failure_time": self.fail_at,
                "ticket": self.ticket, "root_cause": self.root, "affected": self.affected,
                "metric_signals": self.signals(), "causal_chain": chain}

    def signals(self):
        """Metric series that telegraph the failure: (index, field, filter, description)."""
        return []

    def sig(self, dataset, field, flt, desc, dtype="metrics"):
        return {"index": f"{dtype}-{dataset}-{self.sim.ns}", "field": field, "filter": flt, "description": desc}

    def in_rows(self, rows):
        return lambda w: self.sim.topo.loc.get(w, {}).get("row") in rows

    def in_set(self, s):
        return lambda w: w in s


class CoolingFailure(Scenario):
    name = "cooling_failure"
    title = "CRAC compressor failure -> row overheats -> thermal throttling -> host shutdowns -> load cascade"
    default_lead = "3h"

    def setup(self):
        s, r = self.sim, self.rng
        self.row = r.choice(s.topo.rows)
        self.crac = f"crac-{self.row.lower()}"
        i = s.topo.rows.index(self.row)
        self.neighbors = [s.topo.rows[j] for j in (i - 1, i + 1) if 0 <= j < len(s.topo.rows)]
        self.peak = 23.0
        self.rows = {self.row}
        self.cluster = s.topo.racks[f"{self.row}01"]["cluster"]
        self.services = {n for n, v in s.services.items() if v["cluster"] == self.cluster}
        self.root = {"summary": f"Compressor 2 on {self.crac} hit high head pressure and tripped, leaving row {self.row} "
                                f"with partial cooling. Inlet temps climbed until CPUs throttled and hosts shut down "
                                f"thermally; their load shifted to the rest of {self.cluster}.",
                     "category": "facility.cooling", "entity": self.crac, "entity_type": "crac_unit",
                     "location": {"dc": s.topo.dc, "room": s.topo.room, "row": self.row}}
        rowp = self.in_rows({self.row})
        self.emergent = [("BMC-INLET-WARN", "First inlet-temperature warning in the affected row", rowp),
                         ("SYS-THERMAL-THROTTLE", "First CPU thermal throttling in the affected row", rowp),
                         ("SYS-THERMAL-SHUTDOWN", "First host thermal shutdown (FAILURE)", rowp),
                         ("K8S-NODE-NOTREADY", "Kubernetes marks nodes NotReady; pods reschedule", rowp),
                         ("APP-ERROR", "Service errors from lost capacity", self.in_set(self.services))]

    def signals(self):
        return [self.sig("facility.crac", "datacenter.crac.supply_air_temp_c", {"observer.name": self.crac}, "Supply air temp rises exponentially"),
                self.sig("facility.crac", "datacenter.crac.cooling_capacity_pct", {"observer.name": self.crac}, "Cooling capacity drops"),
                self.sig("bmc.sensors", "datacenter.bmc.inlet_temp_c", {"labels.row": self.row}, "Inlet temps climb across the row"),
                self.sig("bmc.sensors", "datacenter.bmc.fan_rpm", {"labels.row": self.row}, "Fans ramp toward max"),
                self.sig("system.host", "host.cpu.usage", {"labels.cluster": self.cluster}, "Load shifts to surviving nodes after shutdowns")]

    def apply(self, ts):
        s = self.sim
        if ts < self.pre:
            return
        off = self.peak * self.f(ts, shape="exp", k=3) if ts <= self.fail_at else \
            self.peak + 0.25 * (ts - self.fail_at).total_seconds() / 60
        off = min(off, 32)
        s.mod(self.crac, "supply", off)
        s.mod(self.crac, "capacity_loss", min(100, 30 + off * 2.5) if ts >= self.pre + self.L * 0.35 else off * 2)
        for rack in s.topo.racks.values():
            hf = rack["heat_factor"] if rack["row"] == self.row else (0.25 if rack["row"] in self.neighbors else 0)
            if hf:
                for h in rack["hosts"]:
                    s.mod(h, "inlet", off * hf)
        obs = {"name": self.crac, "type": "crac"}
        if self.at(ts, self.pre, "hp"):
            self.ev(self.pre, "FAC-CRAC-HIGH-PRESSURE", f"{self.crac}: Compressor 2 high discharge pressure (412 psi > 400 psi setpoint)",
                    "warning", self.crac, chain="Precursor: compressor high head pressure alarm on the CRAC", observer=obs)
            s.flags[(self.crac, "status")] = "degraded"
        t = self.pre + self.L * 0.35
        if self.at(ts, t, "trip"):
            self.ev(t, "FAC-CRAC-COMPRESSOR-TRIP", f"{self.crac}: Compressor 2 locked out on high head pressure; unit running on "
                    "compressor 1 only (cooling capacity ~55%)", "error", self.crac,
                    chain="ROOT CAUSE: compressor trips, CRAC loses ~45% capacity", observer=obs)
            s.flags[(self.crac, "status")] = "fault"
        t = self.pre + self.L * 0.6
        if self.at(ts, t, "sat"):
            sup = 18 + self.peak * ramp(t, self.pre, self.fail_at, "exp", 3)
            self.ev(t, "FAC-SUPPLY-TEMP-HIGH", f"{self.crac}: supply air temperature {sup:.1f} C above 24 C setpoint",
                    "warning", self.crac, chain="Supply air temperature alarm", observer=obs)
        if ts >= self.fail_at:
            downs = [h for h in s.down if s.topo.hosts[h]["row"] == self.row]
            if (downs or ts >= self.fail_at + mins(15)) and not hasattr(self, "t_first_down"):
                self.t_first_down = ts
            if hasattr(self, "t_first_down"):
                self.ticket_at(ts, self.t_first_down + mins(3), f"KubeNodeNotReady: {max(1, len(downs))} nodes NotReady in {self.cluster}",
                               "P1", where=sorted(downs)[0] if downs else None,
                               desc=f"Nodes unreachable: {', '.join(sorted(downs)[:6]) or 'pending'}. Elevated latency/5xx on "
                                    f"{', '.join(sorted(self.services))}.")
        self.affected = {"row": self.row, "crac": self.crac, "cluster": self.cluster,
                         "hosts_shutdown": sorted(h for h in s.down if s.down_reason.get(h) == "thermal"),
                         "services": sorted(self.services)}


class BadConfigChange(Scenario):
    name = "bad_config_change"
    title = "Admin config change lowers LB backend maxconn -> queueing grows with traffic -> 503s"
    default_lead = "75m"

    def setup(self):
        s, r = self.sim, self.rng
        cands = [v for v in s.services.values() if v["lb"] == "lb-1"] or list(s.services.values())
        self.svc = r.choice(cands)["name"]
        self.lb = s.services[self.svc]["lb"]
        self.services = {self.svc}
        self.admin = r.choice(list(ADMINS))
        self.chg = s.new_change_id()
        self.commit = s.sha()
        self.file = "haproxy/conf.d/backends.cfg"
        self.line = r.randint(60, 240)
        p = self.pre
        self.t_push = p + mins(22)
        self.t_reload = self.t_push + timedelta(seconds=75)
        docs = s.emit_change(self.chg, f"Tune {self.lb} backend limits for {self.svc} (reduce connection churn)", self.admin,
                             self.lb, created=p, approved=p + mins(9), implemented=self.t_reload + mins(3), risk="low")
        self.add_chain(docs["created"], "Change ticket opened for LB backend tuning")
        self.add_chain(docs["implemented"], "Change marked implemented (no validation noted)")
        ip = ADMINS[self.admin]
        self.ev(p + mins(15), "AUTH-SSH-SUCCESS", f"sshd: Accepted publickey for {self.admin} from {ip} port 51022 ssh2", "info",
                self.lb, chain="Admin logs into the load balancer", user={"name": self.admin}, source={"ip": ip},
                datacenter={"auth": {"method": "publickey"}})
        self.ev(p + mins(16), "AUTH-SUDO", f"sudo: {self.admin} : TTY=pts/0 ; USER=root ; COMMAND=/usr/bin/vim /etc/{self.file}",
                "info", self.lb, chain="Admin edits LB config as root", user={"name": self.admin},
                process={"command_line": f"/usr/bin/vim /etc/{self.file}"})
        diff = (f"@@ -{self.line},1 +{self.line},1 @@ backend {self.svc}\n"
                f"-    server-template {self.svc} 6 {self.svc}.prod.svc:8443 check maxconn 2000\n"
                f"+    server-template {self.svc} 6 {self.svc}.prod.svc:8443 check maxconn 200")
        self.ev(self.t_push, "CICD-CONFIG-PUSH", f"Config push to {self.lb}: {self.file} (commit {self.commit[:8]}) by {self.admin}",
                "info", self.lb, chain=f"ROOT CAUSE: maxconn lowered 2000 -> 200 in {self.file}:{self.line}",
                user={"name": self.admin}, file={"path": f"/etc/{self.file}"}, change={"id": self.chg},
                cicd={"pipeline": "lb-config-deploy", "repository": "infra/lb-config", "commit": self.commit,
                      "files_changed": [{"path": self.file, "line": self.line, "diff": diff}]})
        self.ev(self.t_reload, "LB-RELOAD", f"haproxy: reloaded, configuration /etc/{self.file} loaded (0 warnings)", "info",
                self.lb, chain="Load balancer reloads with the new config", observer={"name": self.lb, "type": "load-balancer"})
        self.root = {"summary": f"{self.admin} pushed commit {self.commit[:8]} under {self.chg}, changing {self.file} line "
                                f"{self.line}: backend '{self.svc}' maxconn dropped from 2000 to 200. As traffic grew, "
                                f"requests queued at the LB and then timed out with 503s.",
                     "category": "change.configuration", "entity": self.lb, "entity_type": "load_balancer",
                     "change_id": self.chg, "commit": self.commit, "file": self.file, "line": self.line, "user": self.admin}
        self.affected = {"service": self.svc, "load_balancer": self.lb}
        self.emergent = [("APP-ERROR", "Service request errors begin", self.in_set({self.svc}))]

    def signals(self):
        return [self.sig("app.service", "datacenter.app.p99_latency_ms", {"service.name": self.svc}, "p99 latency climbs after LB reload"),
                self.sig("app.service", "datacenter.app.error_rate", {"service.name": self.svc}, "5xx rate jumps at failure")]

    def apply(self, ts):
        s = self.sim
        if ts < self.t_reload:
            return
        f = ramp(ts, self.t_reload, self.fail_at, "exp", 3.5)
        s.mod(self.svc, "latency", 2200 * f if ts < self.fail_at else 2600)
        obs = {"name": self.lb, "type": "load-balancer"}
        if f > 0.2 and s.every((self.lb, "q"), ts, 300):
            depth = int(150 + 2500 * f)
            d = self.ev(ts, "LB-QUEUE-HIGH", f"backend {self.svc}: {depth} requests queued, all servers at maxconn (200)",
                        "warning", self.lb, observer=obs, service={"name": self.svc},
                        datacenter={"lb": {"backend": self.svc, "queue_depth": depth, "maxconn": 200}})
            if self.once("q1"):
                self.add_chain(d, "First LB backend queue warning")
        if ts >= self.fail_at:
            s.mod(self.svc, "err", 0.35 * ramp(ts, self.fail_at - mins(2), self.fail_at + mins(6)))
            s.flags[(self.svc, "errmsg")] = "503 Service Unavailable (no server available, queue timeout)"
            if s.every((self.lb, "ns"), ts, 60):
                d = self.ev(ts, "LB-NO-SERVER", f"backend {self.svc}: queue timeout, returning 503 to clients", "error", self.lb,
                            observer=obs, service={"name": self.svc}, datacenter={"lb": {"backend": self.svc, "maxconn": 200}})
                if self.once("ns1"):
                    self.add_chain(d, "FAILURE: LB starts returning 503s")
            self.ticket_at(ts, self.fail_at + mins(3), f"{self.svc}: HTTP 5xx error rate above 25%", "P1", where=self.lb,
                           service=self.svc, desc=f"SLO burn alert for {self.svc}. p99 latency > 2s, 5xx > 25% via {self.lb}.")
        elif f > 0.8:
            s.mod(self.svc, "err", 0.04 * f)


class DiskCascade(Scenario):
    name = "disk_cascade"
    title = "Failing disk ignored -> RAID degraded -> rebuild stress kills second disk -> array lost"
    default_lead = "4h"

    def setup(self):
        s, r = self.sim, self.rng
        self.svc = r.choice(list(s.services))
        self.host = r.choice(s.services[self.svc]["hosts"])
        self.services, self.hosts_scope = {self.svc}, {self.host}
        self.t_fail1 = self.pre + self.L * 0.7
        self.root = {"summary": f"/dev/sdb on {self.host} wore out (reallocated sectors growing, I/O errors) and failed. md0 "
                                f"went degraded and the rebuild load killed /dev/sdc, which already had pending sectors. "
                                f"The array failed, the filesystem went read-only, and {self.svc} pods on the host crashed.",
                     "category": "hardware.storage", "entity": self.host, "entity_type": "host",
                     "devices": ["/dev/sdb", "/dev/sdc"], "location": s.topo.loc[self.host]}
        self.affected = {"host": self.host, "service": self.svc, "array": "md0"}
        hp = self.in_set({self.host})
        self.emergent = [("SYS-IO-ERROR", "First kernel I/O error on the failing disk", hp)]

    def signals(self):
        return [self.sig("storage.smart", "datacenter.smart.reallocated_sectors", {"host.name": self.host, "datacenter.smart.device": "/dev/sdb"}, "Reallocated sectors grow exponentially"),
                self.sig("storage.smart", "datacenter.smart.pending_sectors", {"host.name": self.host, "datacenter.smart.device": "/dev/sdc"}, "Second disk accumulates pending sectors"),
                self.sig("storage.smart", "datacenter.smart.read_latency_ms", {"host.name": self.host}, "Read latency rises"),
                self.sig("system.host", "datacenter.host.iowait_pct", {"host.name": self.host}, "iowait jumps during rebuild")]

    def apply(self, ts):
        s, h = self.sim, self.host
        if ts < self.pre:
            return
        f = self.f(ts, end=self.t_fail1, shape="exp", k=5)
        s.mod(h, "realloc:sdb", 6 + 2400 * f)
        s.mod(h, "lat:sdb", 60 * f)
        s.mod(h, "pending:sdc", 40 * self.f(ts))
        if ts < self.t_fail1 and f > 0.05 and self.rng.random() < f * 0.7:
            sector = self.rng.randint(10 ** 8, 10 ** 9)
            s.emit(ts, "SYS-IO-ERROR", f"kernel: blk_update_request: I/O error, dev sdb, sector {sector} op 0x0:(READ)",
                   "error", h, jitter=s.tick_s, file={"device": "sdb"}, process={"name": "kernel"})
        if self.at(ts, self.t_fail1, "f1"):
            s.flags[(h, "sdb", "failed")] = True
            self.ev(self.t_fail1, "RAID-DISK-FAIL", "md/raid10:md0: Disk failure on sdb1, disabling device. "
                    "Operation continuing on 3 devices.", "critical", h, chain="First disk (sdb) fails; md0 degraded",
                    file={"device": "sdb"}, datacenter={"raid": {"array": "md0", "state": "degraded", "active_devices": 3}})
            self.ev(self.t_fail1 + mins(1), "RAID-REBUILD-START", "md: recovery of RAID array md0 started (hot spare sdd2)",
                    "warning", h, chain="Rebuild starts, adding heavy I/O load",
                    datacenter={"raid": {"array": "md0", "state": "recovering"}})
        if ts >= self.t_fail1:
            s.mod(h, "iowait", 0.22 if ts < self.fail_at else 0.6)
            s.mod(h, "lat:sdc", 25)
            if ts < self.fail_at and s.every((h, "rb"), ts, 600):
                pct = 63 * ramp(ts, self.t_fail1, self.fail_at)
                s.emit(ts, "RAID-REBUILD-PROGRESS", f"md0: recovery = {pct:.1f}% finish={int((1 - pct / 100) * 400)}min "
                       "speed=48210K/sec", "info", h, datacenter={"raid": {"array": "md0", "state": "recovering", "progress_pct": round(pct, 1)}})
            if ts < self.fail_at and self.rng.random() < 0.25:
                s.emit(ts, "SYS-IO-ERROR", "kernel: ata3.00: failed command: READ FPDMA QUEUED (dev sdc, UNC error)",
                       "error", h, jitter=s.tick_s, file={"device": "sdc"}, process={"name": "kernel"})
        if self.at(ts, self.fail_at, "f2"):
            s.flags[(h, "sdc", "failed")] = True
            self.ev(self.fail_at, "RAID-ARRAY-FAIL", "md/raid10:md0: Disk failure on sdc1 during recovery; md0: cannot continue, "
                    "array FAILED", "critical", h, chain="FAILURE: second disk (sdc) fails during rebuild; array lost",
                    file={"device": "sdc"}, datacenter={"raid": {"array": "md0", "state": "failed", "active_devices": 2}})
            self.ev(self.fail_at + timedelta(seconds=20), "SYS-FS-RO", "kernel: EXT4-fs error (device md0): ext4_journal_check_start: "
                    "Detected aborted journal; Remounting filesystem read-only", "critical", h,
                    chain="Filesystem remounted read-only", file={"path": "/var/lib/containerd", "device": "md0"})
        if ts >= self.fail_at:
            s.mod(h, "err", 0.9)
            s.flags[(self.svc, "errmsg")] = "upstream reset: read-only file system"
            if s.every((h, "cl"), ts, 300):
                pod = s.pod(self.svc, h, ts)
                d = s.k8s(ts, "K8S-CRASHLOOP", f"Back-off restarting failed container {self.svc} in pod {pod}", "error", h,
                          self.svc, reason="CrashLoopBackOff", pod=pod, jitter=30)
                if self.once("cl1"):
                    self.add_chain(d, "Pod on affected host enters CrashLoopBackOff")
            self.ticket_at(ts, self.fail_at + mins(4), f"{self.svc}: elevated 5xx; pod CrashLoopBackOff on {h}", "P2", where=h,
                           service=self.svc, desc=f"KubePodCrashLooping firing for {self.svc} on node {h}. Error rate above SLO.")


class MemoryLeak(Scenario):
    name = "memory_leak"
    title = "New release leaks heap -> GC thrash -> OOMKilled -> CrashLoopBackOff"
    default_lead = "3h"

    def setup(self):
        s, r = self.sim, self.rng
        self.svc = r.choice(list(s.services))
        self.services = {self.svc}
        self.hosts_scope = sorted(s.services[self.svc]["hosts"])
        self.old = s.version_at(self.svc, self.pre)
        self.new = s.bump(self.old)
        self.commit = s.sha()
        self.chg = s.new_change_id()
        self.factor = {h: r.uniform(0.9, 1.15) for h in self.hosts_scope}
        docs = s.emit_change(self.chg, f"Release {self.svc} {self.new} (standard pre-approved)", "svc-deploy", self.svc,
                             created=self.pre - mins(30), approved=self.pre - mins(30), implemented=self.pre + mins(4),
                             ctype="standard", approver="cab-auto")
        a, b = s.emit_deploy(self.pre, self.svc, self.new, "svc-deploy", self.commit)
        self.add_chain(b, f"ROOT CAUSE: {self.svc} {self.new} rolled out (commit {self.commit[:8]})")
        self.root = {"summary": f"Release {self.new} of {self.svc} (commit {self.commit[:8]}, {self.chg}) has a heap leak. "
                                f"Memory grew linearly on every replica until containers were OOMKilled and went into CrashLoopBackOff.",
                     "category": "software.release", "entity": self.svc, "entity_type": "service", "version": self.new,
                     "previous_version": self.old, "commit": self.commit, "change_id": self.chg}
        self.affected = {"service": self.svc, "hosts": sorted(self.hosts_scope)}
        sp = self.in_set({self.svc})
        self.emergent = [("APP-GC-WARN", "First GC-overhead warning", sp),
                         ("K8S-OOMKILLED", "First container OOMKilled", sp),
                         ("APP-ERROR", "Service request errors begin", sp)]

    def signals(self):
        return [self.sig("system.host", "datacenter.host.mem_used_pct", {"host.name": sorted(self.hosts_scope)}, "Memory grows linearly after deploy"),
                self.sig("app.service", "datacenter.app.gc_pause_p99_ms", {"service.name": self.svc}, "GC pauses grow quadratically"),
                self.sig("app.service", "service.version", {"service.name": self.svc}, "Version changes at precursor start")]

    def apply(self, ts):
        s = self.sim
        start = self.pre + mins(5)
        if ts < start:
            return
        f = ramp(ts, start, self.fail_at)
        for h in self.hosts_scope:
            base = s.topo.hosts[h]["base_mem"]
            s.mod(h, "mem", (97.5 - base) * min(1.0, f * self.factor[h]))
        s.mod(self.svc, "gc", 900 * f ** 2)
        s.mod(self.svc, "latency", 450 * f ** 2)
        if f > 0.4 and s.every((self.svc, "gc"), ts, 900):
            heap = 3.0 + 4.8 * f
            s.emit(ts, "APP-GC-WARN", f"{self.svc}: GC overhead {int(10 + 60 * f)}% of wall time over last 60s, "
                   f"heap {heap:.1f}G/8.0G after full GC", "warning", None, jitter=30,
                   labels={"cluster": s.services[self.svc]["cluster"]}, service={"name": self.svc, "version": self.new})
        if f > 0.85 and ts < self.fail_at:
            for h in self.hosts_scope:
                if self.rng.random() < 0.08:
                    self.oom(ts, h)
        if ts >= self.fail_at:
            s.mod(self.svc, "err", 0.3)
            s.setm(self.svc, "crashing", max(1, len(self.hosts_scope) // 2))
            s.flags[(self.svc, "errmsg")] = "503 upstream unavailable: no healthy endpoints"
            for h in self.hosts_scope:
                if s.every((h, "cl"), ts, 300):
                    pod = s.pod(self.svc, h, ts)
                    d = s.k8s(ts, "K8S-CRASHLOOP", f"Back-off restarting failed container {self.svc} in pod {pod}", "error", h,
                              self.svc, reason="CrashLoopBackOff", pod=pod, jitter=40)
                    if self.once("cl1"):
                        self.add_chain(d, "FAILURE: pods enter CrashLoopBackOff")
            self.ticket_at(ts, self.fail_at + mins(3), f"{self.svc}: pods CrashLoopBackOff, 5xx above SLO", "P1", service=self.svc,
                           desc=f"KubePodCrashLooping and error-rate alerts for {self.svc} in {s.services[self.svc]['cluster']}.")

    def oom(self, ts, h):
        s = self.sim
        pod = s.pod(self.svc, h, ts)
        s.emit(ts, "APP-OOM", f"{self.svc}: java.lang.OutOfMemoryError: Java heap space", "error", h, jitter=20,
               service={"name": self.svc, "version": self.new}, error={"type": "java.lang.OutOfMemoryError"})
        s.emit(ts + timedelta(seconds=25), "SYS-OOM-KILL", f"kernel: Memory cgroup out of memory: Killed process {self.rng.randint(1000, 60000)} "
               f"(java) total-vm:12.1G, anon-rss:8.0G, oom_score_adj:999", "error", h, process={"name": "java"})
        s.k8s(ts + timedelta(seconds=30), "K8S-OOMKILLED", f"Container {self.svc} in pod {pod} was OOMKilled (exit code 137)",
              "warning", h, self.svc, reason="OOMKilled", pod=pod)


class PDUOverload(Scenario):
    name = "pdu_overload"
    title = "Feed B down for firmware + new GPU servers on feed A -> breaker trips -> whole rack dark"
    default_lead = "150m"

    def setup(self):
        s, r = self.sim, self.rng
        self.rack = r.choice(list(s.topo.racks))
        R = s.topo.racks[self.rack]
        self.pa, self.pb = R["pdus"]
        self.rows = {R["row"]}
        self.services = {n for n, v in s.services.items() if any(h in R["hosts"] for h in v["hosts"])}
        self.tech = r.choice(TECHS)
        self.admin = r.choice(list(ADMINS))
        p, L = self.pre, self.L
        self.t_off = p + mins(10)
        self.new_hosts = []
        n0 = len(R["hosts"])
        existing = n0 * (180 + 300 * 0.45)
        per = max(700.0, (PDU_RATED_A * VOLTS * 1.08 - existing) / 4) - (180 + 300 * 0.35)
        self.gpu_extra = max(400.0, per)
        for i in range(4):
            t_on = p + mins(25 + 10 * i)
            h = s.topo.add_host(r, self.rack, n0 + i + 1, online_from=t_on, kind="gpu")
            h["base_cpu"] = 0.35
            self.new_hosts.append((h["name"], t_on))
        self.hosts_scope = set(R["hosts"])
        c1, c2 = s.new_change_id(), s.new_change_id()
        d1 = s.emit_change(c1, f"Firmware upgrade {self.pb} (feed B offline ~45 min, rack on single feed)", self.admin, self.pb,
                           created=p - mins(300), approved=p - mins(120), implemented=self.t_off, risk="medium")
        d2 = s.emit_change(c2, f"Install 4x GPU servers in rack {self.rack}", self.tech, self.rack,
                           created=p - mins(600), approved=p - mins(200), implemented=p + mins(20), risk="low")
        self.add_chain(d1["implemented"], "Change: PDU feed B firmware upgrade begins (rack on single feed)")
        self.add_chain(d2["implemented"], "Change: GPU server install in same rack, same window")
        self.ev(p + mins(3), "BADGE-GRANTED", f"Access granted: {self.tech} at hall-1-cage-2", "info", None,
                chain="Technician badges into the cage", user={"name": self.tech},
                datacenter={"access": {"door": "hall-1-cage-2", "direction": "in"}})
        obs = {"name": self.pb, "type": "pdu"}
        self.ev(self.t_off, "FAC-PDU-FEED-OFFLINE", f"{self.pb}: input power removed, unit entering maintenance mode "
                f"(firmware update)", "warning", self.pb, chain="Feed B offline; all load moves to feed A", observer=obs)
        self.ev(p + mins(55), "FAC-PDU-FW-STALLED", f"{self.pb}: firmware update stalled at 63% - unit in bootloader, "
                "outlets remain de-energized", "error", self.pb, chain="Firmware update stalls; feed B stays down", observer=obs)
        for i, (name, t_on) in enumerate(self.new_hosts):
            d = s.emit(t_on + timedelta(seconds=90), "SYS-BOOT", "kernel: Linux version 6.8.0-45-generic; systemd[1]: Reached target "
                       "Multi-User System", "info", name)
            s.k8s(t_on + mins(3), "K8S-NODE-READY", f"Node {name} status is now: NodeReady", "info", name, reason="NodeReady")
            if i == 0:
                self.add_chain(d, "New GPU servers power on (single-fed)")
        self.root = {"summary": f"While {self.pb} was offline for a firmware update ({c1}) that then stalled, four GPU servers "
                                f"were installed in rack {self.rack} ({c2}). All load landed on {self.pa}, which climbed past "
                                f"its 80% continuous rating and tripped the 30 A branch breaker, taking out the whole rack.",
                     "category": "facility.power", "entity": self.pa, "entity_type": "pdu", "change_ids": [c1, c2],
                     "location": s.topo.loc[self.pa]}
        self.affected = {"rack": self.rack, "pdus": [self.pa, self.pb], "services": sorted(self.services)}
        self.emergent = [("FAC-PDU-LOAD-WARN", "PDU feed A crosses 80% continuous rating", self.in_set({self.pa})),
                         ("FAC-PDU-LOAD-CRIT", "PDU feed A near breaker rating", self.in_set({self.pa})),
                         ("K8S-NODE-NOTREADY", "All nodes in the rack go NotReady", self.in_set(self.hosts_scope))]

    def signals(self):
        return [self.sig("facility.pdu", "datacenter.pdu.current_a", {"observer.name": self.pa}, "Feed A current steps up then ramps toward 30 A"),
                self.sig("facility.pdu", "datacenter.pdu.state", {"observer.name": self.pb}, "Feed B offline for the whole window")]

    def apply(self, ts):
        s = self.sim
        if ts >= self.t_off:
            s.flags[(self.pb, "offline")] = True
        for name, t_on in self.new_hosts:
            if ts >= t_on:
                s.mod(name, "watts", self.gpu_extra * ramp(ts, t_on + mins(5), self.fail_at, "log", 3))
        if self.at(ts, self.fail_at, "trip"):
            self.ev(self.fail_at, "FAC-PDU-BREAKER-TRIP", f"{self.pa}: branch circuit breaker CB2 tripped (overcurrent); "
                    f"all outlets de-energized", "critical", self.pa, chain="FAILURE: feed A breaker trips; rack loses power",
                    observer={"name": self.pa, "type": "pdu"})
            s.flags[(self.pa, "tripped")] = True
            for h in s.topo.racks[self.rack]["hosts"]:
                if s.is_online(s.topo.hosts[h], ts):
                    s.down.add(h)
                    s.down_reason[h] = "power"
        if ts >= self.fail_at:
            self.ticket_at(ts, self.fail_at + mins(3), f"Rack {self.rack}: all hosts unreachable (ping + BMC)", "P1",
                           where=self.pa, desc=f"Host-down alerts for every node in rack {self.rack}; BMCs also unreachable.")


class BadOptic(Scenario):
    name = "bad_optic"
    title = "Degrading optic on ToR uplink -> CRC errors -> link flaps/BGP resets -> err-disable -> congestion loss"
    default_lead = "3h"

    def setup(self):
        s, r = self.sim, self.rng
        svc = r.choice(list(s.services))
        host = r.choice(s.services[svc]["hosts"])
        self.rack = s.topo.hosts[host]["rack"]
        R = s.topo.racks[self.rack]
        self.tor, self.port, self.spine = R["tor"], "eth1/49", "spine-1"
        self.peer = f"10.255.{s.topo.rows.index(R['row'])}.{R['idx'] * 2}"
        self.hosts_scope = set(R["hosts"])
        self.services = {n for n, v in s.services.items() if any(h in R["hosts"] for h in v["hosts"])}
        self.root = {"summary": f"The QSFP28 optic in {self.tor} {self.port} (uplink to {self.spine}) degraded: rx power fell and "
                                f"CRC errors grew exponentially. The link started flapping, BGP to {self.spine} reset repeatedly, "
                                f"and the port was finally err-disabled. That left the rack on one congested uplink.",
                     "category": "network.hardware", "entity": self.tor, "entity_type": "switch", "interface": self.port,
                     "location": s.topo.loc[self.tor]}
        self.affected = {"rack": self.rack, "switch": self.tor, "services": sorted(self.services)}
        tp = self.in_set({self.tor})
        self.emergent = [("NET-XCVR-RX-LOW", "First transceiver rx-power-low warning", tp),
                         ("NET-LINK-DOWN", "First uplink flap", tp),
                         ("NET-BGP-DOWN", "First BGP session reset", tp),
                         ("APP-ERROR", "Service errors from packet loss", self.in_set(self.services))]

    def signals(self):
        return [self.sig("network.interface", "datacenter.iface.crc_errors", {"observer.name": self.tor, "datacenter.iface.interface": self.port}, "CRC errors grow exponentially"),
                self.sig("network.interface", "datacenter.iface.rx_power_dbm", {"observer.name": self.tor, "datacenter.iface.interface": self.port}, "Optic rx power declines linearly"),
                self.sig("network.interface", "datacenter.iface.out_discards", {"observer.name": self.tor, "datacenter.iface.interface": "eth1/50"}, "Surviving uplink congests after failure")]

    def apply(self, ts):
        s, r = self.sim, self.rng
        if ts < self.pre:
            return
        f = self.f(ts, shape="exp", k=4)
        lin = self.f(ts)
        obs = {"name": self.tor, "type": "switch", "ingress": {"interface": {"name": self.port}}}
        if ts < self.fail_at:
            s.mod(self.tor, f"crc:{self.port}", 2 + 6000 * f)
        s.mod(self.tor, f"rxpow:{self.port}", -11.0 * lin)
        rx = -2.4 - 11.0 * lin
        if rx < -7 and ts < self.fail_at and s.every((self.tor, "xcvr"), ts, 1200):
            s.emit(ts, "NET-XCVR-RX-LOW", f"%TRANSCEIVER-4-RX_POWER_LOW: {self.port} rx power {rx:.1f} dBm below warning "
                   "threshold -7.0 dBm", "warning", self.tor, jitter=30, observer=obs,
                   datacenter={"iface": {"interface": self.port, "rx_power_dbm": round(rx, 2)}})
        if 0.5 < lin and ts < self.fail_at and r.random() < 0.04 + 0.25 * f:
            t0 = ts + timedelta(seconds=r.uniform(0, s.tick_s))
            s.emit(t0, "NET-LINK-DOWN", f"%ETHPORT-5-IF_DOWN_LINK_FAILURE: Interface Ethernet{self.port[3:]} is down (Link failure)",
                   "error", self.tor, observer=obs)
            s.emit(t0 + timedelta(seconds=r.uniform(4, 30)), "NET-LINK-UP", f"%ETHPORT-5-IF_UP: Interface Ethernet{self.port[3:]} is up",
                   "info", self.tor, observer=obs)
            if r.random() < 0.6:
                s.emit(t0 + timedelta(seconds=3), "NET-BGP-DOWN", f"%BGP-5-ADJCHANGE: neighbor {self.peer} ({self.spine}) Down - "
                       "Interface flap", "error", self.tor, observer=obs, destination={"ip": self.peer})
                s.emit(t0 + timedelta(seconds=r.uniform(35, 90)), "NET-BGP-UP", f"%BGP-5-ADJCHANGE: neighbor {self.peer} ({self.spine}) Up",
                       "info", self.tor, observer=obs, destination={"ip": self.peer})
            for h in self.hosts_scope:
                s.mod(h, "pkt_loss", 0.01)
        if lin > 0.5 and ts < self.fail_at:
            for h in self.hosts_scope:
                s.mod(h, "pkt_loss", 0.004 * f)
        if self.at(ts, self.fail_at, "errdis"):
            self.ev(self.fail_at, "NET-ERRDISABLE", f"%ETHPORT-2-IF_DOWN_ERROR_DISABLED: Interface Ethernet{self.port[3:]} is down "
                    "(Error disabled. Reason: link-flap)", "critical", self.tor,
                    chain="FAILURE: uplink err-disabled; rack on single congested uplink", observer=obs)
            self.ev(self.fail_at + timedelta(seconds=2), "NET-BGP-DOWN", f"%BGP-5-ADJCHANGE: neighbor {self.peer} ({self.spine}) "
                    "Down - Interface down", "error", self.tor, observer=obs, destination={"ip": self.peer})
            s.flags[(self.tor, self.port, "down")] = True
        if ts >= self.fail_at:
            s.mod(self.tor, "drops:eth1/50", 40000)
            for h in self.hosts_scope:
                s.mod(h, "pkt_loss", 0.05)
            s.flags.update({(sv, "errmsg"): "upstream request timeout (connection reset)" for sv in self.services})
            self.ticket_at(ts, self.fail_at + mins(5), f"Intermittent timeouts on {', '.join(sorted(self.services)) or 'services'} "
                           f"in rack {self.rack}", "P2", where=self.tor,
                           desc=f"Elevated timeouts and retransmits from hosts in rack {self.rack}.")


class CertExpiry(Scenario):
    name = "cert_expiry"
    title = "DNS migration breaks ACME DNS-01 -> short-lived cert can't renew -> expires -> TLS failures"
    default_lead = "4h"

    def setup(self):
        s, r = self.sim, self.rng
        ext = [n for n, v in s.services.items() if v["external"]]
        self.svc = r.choice(ext)
        self.services = {self.svc}
        c = s.certs[self.svc]
        c["not_after"] = self.fail_at
        c["issuer"] = "dc-internal-acme-ca (24h certs)"
        self.domain, self.lb = c["domain"], c["lb"]
        self.admin = r.choice(list(ADMINS))
        self.chg = s.new_change_id()
        t = self.pre - mins(20)
        docs = s.emit_change(self.chg, "Migrate zone example.com from dns-provider-a to dns-provider-b", self.admin, "dns:example.com",
                             created=t - mins(240), approved=t - mins(60), implemented=t, closed=t + mins(40), risk="medium")
        self.add_chain(docs["implemented"], "ROOT CAUSE: DNS zone migrated; cert-manager's DNS API token not updated")
        self.root = {"summary": f"{self.chg} moved example.com to a new DNS provider, but cert-manager still had the old "
                                f"provider's API token. Every ACME DNS-01 renewal for {self.domain} failed with 403. The 24-hour "
                                f"certificate expired and TLS handshakes for {self.svc} began failing.",
                     "category": "change.configuration", "entity": self.domain, "entity_type": "certificate",
                     "change_id": self.chg, "user": self.admin}
        self.affected = {"service": self.svc, "domain": self.domain, "load_balancer": self.lb}
        self.emergent = [("K8S-CERT-RENEW-FAIL", "First failed certificate renewal", lambda w: True),
                         ("TLS-CERT-STATUS", "Certificate scanner reports expiry", self.in_set({self.svc}))]

    def signals(self):
        return [self.sig("tls.certs", "datacenter.tls.hours_remaining", {"service.name": self.svc}, "Hours remaining counts down to zero", dtype="logs"),
                self.sig("app.service", "datacenter.app.rps", {"service.name": self.svc}, "Request volume collapses at expiry")]

    def apply(self, ts):
        s = self.sim
        start = self.pre + mins(15)
        if start <= ts < self.fail_at and s.every((self.svc, "renew"), ts, 1800):
            h = s.services[self.svc]["hosts"][0]
            s.k8s(ts, "K8S-CERT-RENEW-FAIL", f"Certificate prod/{self.svc}-tls: error presenting DNS-01 challenge: failed to create "
                  f"TXT _acme-challenge.{self.domain}: 403 Forbidden (invalid API token for zone example.com)", "error", h,
                  self.svc, reason="Failed", jitter=60, tls={"server": {"x509": {"subject": {"common_name": [self.domain]}}}})
        if ts >= self.fail_at:
            s.setm(self.svc, "rps_mult", 0.12)
            s.mod(self.svc, "err", 0.02)
            if s.every((self.lb, "tls"), ts, 60):
                n = self.rng.randint(800, 4000)
                d = self.ev(ts, "LB-TLS-FAIL", f"{n} TLS handshakes failed for {self.domain} in last 60s: certificate has expired "
                            "(client alert 45)", "error", self.lb, observer={"name": self.lb, "type": "load-balancer"},
                            service={"name": self.svc}, tls={"server": {"x509": {"subject": {"common_name": [self.domain]}}}},
                            datacenter={"lb": {"tls_failures": n}})
                if self.once("t1"):
                    self.add_chain(d, "FAILURE: clients reject the expired certificate")
            self.ticket_at(ts, self.fail_at + mins(6), f"{self.svc}: request volume dropped ~88%; users report browser security warning",
                           "P1", where=self.lb, service=self.svc,
                           desc=f"Traffic anomaly alert for {self.svc}. Customer reports of 'Your connection is not private'.")


class SecurityBreach(Scenario):
    name = "security_breach"
    title = "SSH brute force -> compromised admin -> priv-esc + IAM role + firewall change -> data exfiltration"
    default_lead = "3h"

    def setup(self):
        s, r = self.sim, self.rng
        self.attacker = s.public_ip()
        self.user = "rlee"
        self.svc = "payments" if "payments" in s.services else r.choice(list(s.services))
        self.target = r.choice(s.services[self.svc]["hosts"])
        self.hosts_scope = {"bastion-1", self.target}
        p = self.pre
        t = p
        tried = ["root", "admin", "ubuntu", "oracle", "test", "rlee", "jdoe", "deploy"]
        end = p + mins(40)
        first = True
        while t < end:
            u = r.choice(tried)
            d = s.emit(t, "AUTH-SSH-FAIL", f"sshd: Failed password for {'invalid user ' if u not in ADMINS else ''}{u} from "
                       f"{self.attacker} port {r.randint(30000, 65000)} ssh2", "warning", "bastion-1", user={"name": u},
                       source={"ip": self.attacker}, datacenter={"auth": {"method": "password"}})
            if first:
                self.add_chain(d, "Brute-force SSH against bastion begins from an external IP")
                first = False
            t += timedelta(seconds=r.uniform(15, 45))
        t = p + mins(42)
        self.ev(t, "AUTH-SSH-SUCCESS", f"sshd: Accepted password for {self.user} from {self.attacker} port 52211 ssh2", "info",
                "bastion-1", chain="ROOT CAUSE: successful password login as rlee from the attacker IP (rlee normally uses "
                                   "publickey from VPN)", user={"name": self.user}, source={"ip": self.attacker},
                datacenter={"auth": {"method": "password"}})
        self.ev(p + mins(50), "AUTH-SSH-SUCCESS", f"sshd: Accepted password for {self.user} from 10.0.0.5 port 40112 ssh2", "info",
                self.target, chain="Lateral movement: bastion -> payments host", user={"name": self.user}, source={"ip": "10.0.0.5"},
                datacenter={"auth": {"method": "password"}})
        self.ev(p + mins(52), "AUTH-SUDO", f"sudo: {self.user} : TTY=pts/1 ; USER=root ; COMMAND=/bin/bash -i", "warning",
                self.target, chain="Privilege escalation to root shell", user={"name": self.user},
                process={"command_line": "/bin/bash -i"})
        self.ev(p + mins(65), "IAM-ROLE-CREATE", f"Role svc-backup-sync created by {self.user}", "info", None,
                chain="New IAM role created", user={"name": self.user}, source={"ip": self.attacker},
                datacenter={"iam": {"role": "svc-backup-sync"}})
        self.ev(p + mins(66), "IAM-POLICY-ATTACH", f"Policy '*:*' attached to role svc-backup-sync by {self.user}", "warning", None,
                chain="Wildcard admin policy attached to the role", user={"name": self.user},
                datacenter={"iam": {"role": "svc-backup-sync", "policy": "*:*"}})
        self.ev(p + mins(75), "FW-RULE-CHANGE", f"Rule 'tmp-backup-egress' added by {self.user}: ALLOW tcp "
                f"{s.topo.hosts[self.target]['ip']} -> {self.attacker}:443", "warning", "fw-edge-1",
                chain="Firewall egress rule opened to the attacker IP", user={"name": self.user},
                rule={"name": "tmp-backup-egress", "action": "allow"},
                source={"ip": s.topo.hosts[self.target]["ip"]}, destination={"ip": self.attacker, "port": 443})
        self.t_exfil = p + mins(80)
        self.root = {"summary": f"An attacker at {self.attacker} brute-forced SSH on bastion-1 and got in with {self.user}'s password. "
                                f"They moved to {self.target}, escalated to root, created IAM role svc-backup-sync with a "
                                f"wildcard policy, opened a firewall egress rule, and exfiltrated data over TLS.",
                     "category": "security.intrusion", "entity": self.target, "entity_type": "host",
                     "attacker_ip": self.attacker, "compromised_user": self.user}
        self.affected = {"host": self.target, "user": self.user, "service": self.svc}
        self.emergent = [("FW-ALLOW", "Outbound transfer to attacker IP observed", self.in_set({"fw-edge-1"}))]

    def signals(self):
        return [self.sig("system.host", "host.network.egress.bytes", {"host.name": self.target}, "Egress bytes surge to ~40x baseline")]

    def apply(self, ts):
        s = self.sim
        if ts < self.t_exfil:
            return
        f = ramp(ts, self.t_exfil, self.fail_at, "log", 3)
        s.mod(self.target, "egress_bps", 4.5e7 * f if ts < self.fail_at + mins(10) else 0)
        if ts < self.fail_at + mins(10) and s.every((self.target, "fw"), ts, 300):
            mb = int(45 * f * 300)
            s.emit(ts, "FW-ALLOW", f"ALLOW tcp {s.topo.hosts[self.target]['ip']} -> {self.attacker}:443 rule=tmp-backup-egress "
                   f"bytes={mb}MB", "info", "fw-edge-1", jitter=30, rule={"name": "tmp-backup-egress"},
                   source={"ip": s.topo.hosts[self.target]["ip"], "bytes": mb * 1024 * 1024},
                   destination={"ip": self.attacker, "port": 443})
        if self.at(ts, self.fail_at, "ids"):
            gb = round(45 * (self.fail_at - self.t_exfil).total_seconds() * 0.7 / 1024, 1)
            self.ev(self.fail_at, "IDS-EXFIL", f"Anomalous egress: {self.target} sent {gb} GB to {self.attacker} "
                    f"(never-seen destination, 40x baseline)", "critical", self.target,
                    chain="FAILURE: IDS detects data exfiltration", source={"ip": s.topo.hosts[self.target]["ip"]},
                    destination={"ip": self.attacker}, rule={"name": "egress-anomaly-v2"},
                    threat={"technique": {"id": ["T1048"], "name": ["Exfiltration Over Alternative Protocol"]}})
        if ts >= self.fail_at:
            self.ticket_at(ts, self.fail_at + mins(2), f"Security: possible data exfiltration from {self.target}", "P1",
                           where=self.target, desc="IDS egress-anomaly alert. Investigate host and outbound destination.")


class DiskFill(Scenario):
    name = "disk_fill"
    title = "Admin enables DEBUG logging -> /var fills on service nodes -> evictions + write failures"
    default_lead = "3h"

    def setup(self):
        s, r = self.sim, self.rng
        self.svc = r.choice(list(s.services))
        self.services = {self.svc}
        self.hosts_scope = sorted(s.services[self.svc]["hosts"])
        self.admin = r.choice(list(ADMINS))
        self.chg = s.new_change_id()
        self.commit = s.sha()
        self.file = f"{self.svc}/values-prod.yaml"
        self.line = r.randint(10, 80)
        self.factor = {h: r.uniform(0.92, 1.12) for h in self.hosts_scope}
        p = self.pre
        docs = s.emit_change(self.chg, f"Enable DEBUG logging on {self.svc} to investigate intermittent 499s", self.admin, self.svc,
                             created=p - mins(25), approved=p - mins(10), implemented=p + mins(4), risk="low")
        self.add_chain(docs["created"], "Change opened to raise log verbosity")
        self.ev(p, "CICD-CONFIG-PUSH", f"Config push {self.file} (commit {self.commit[:8]}) by {self.admin}", "info", None,
                chain=f"ROOT CAUSE: logLevel INFO -> DEBUG in {self.file}:{self.line}",
                labels={"cluster": s.services[self.svc]["cluster"]}, service={"name": self.svc}, user={"name": self.admin},
                change={"id": self.chg}, file={"path": self.file},
                cicd={"pipeline": "helm-values-sync", "repository": "platform/helm-values", "commit": self.commit,
                      "files_changed": [{"path": self.file, "line": self.line,
                                         "diff": f"@@ -{self.line},1 +{self.line},1 @@\n-  logLevel: INFO\n+  logLevel: DEBUG"}]})
        self.root = {"summary": f"{self.admin} set logLevel DEBUG for {self.svc} ({self.chg}, commit {self.commit[:8]}, "
                                f"{self.file}:{self.line}). Log volume grew roughly 40x and filled /var on every node running "
                                f"{self.svc}. Kubelet hit DiskPressure, evicted pods, and writes failed.",
                     "category": "change.configuration", "entity": self.svc, "entity_type": "service", "change_id": self.chg,
                     "commit": self.commit, "file": self.file, "line": self.line, "user": self.admin}
        self.affected = {"service": self.svc, "hosts": sorted(self.hosts_scope)}
        hp = self.in_set(self.hosts_scope)
        self.emergent = [("SYS-DISK-SPACE-WARN", "First /var space warning", hp),
                         ("SYS-DISK-SPACE-CRIT", "First /var critical", hp),
                         ("K8S-DISK-PRESSURE", "Kubelet reports DiskPressure", hp),
                         ("K8S-EVICTED", "Pods evicted", hp)]

    def signals(self):
        return [self.sig("system.host", "datacenter.host.var_used_pct", {"host.name": sorted(self.hosts_scope)}, "/var usage climbs linearly to 100%")]

    def apply(self, ts):
        s = self.sim
        start = self.pre + mins(5)
        if ts < start:
            return
        f = ramp(ts, start, self.fail_at)
        full = []
        for h in self.hosts_scope:
            base = s.topo.hosts[h]["base_var"]
            s.mod(h, "var", (100.5 - base) * min(1.0, f * self.factor[h]))
            if f * self.factor[h] >= 1.0:
                full.append(h)
        for h in full:
            if self.once(("evict", h)):
                pod = s.pod(self.svc, h, ts)
                s.k8s(ts, "K8S-EVICTED", f"The node was low on resource: ephemeral-storage. Pod {pod} evicted", "warning", h,
                      self.svc, reason="Evicted", pod=pod, jitter=30)
            if s.every((h, "full"), ts, 300):
                s.emit(ts, "APP-DISK-FULL", f"{self.svc}: failed to write /var/log/{self.svc}/app.log: No space left on device (ENOSPC)",
                       "error", h, jitter=30, service={"name": self.svc}, error={"code": "ENOSPC"})
        if full:
            s.flags[(self.svc, "errmsg")] = "500 internal error: ENOSPC writing request journal"
        if ts >= self.fail_at:
            self.ticket_at(ts, self.fail_at + mins(3), f"{self.svc}: 5xx above SLO; pods Evicted (DiskPressure)", "P2", service=self.svc,
                           desc=f"KubeNodeDiskPressure on nodes running {self.svc}; error rate alert.")


SCENARIOS = {c.name: c for c in (CoolingFailure, BadConfigChange, DiskCascade, MemoryLeak, PDUOverload,
                                 BadOptic, CertExpiry, SecurityBreach, DiskFill)}


# --------------------------------------------------------------------------------------
# red herrings
# --------------------------------------------------------------------------------------
class RedHerrings:
    """Plausible-but-unrelated anomalies placed near failures to make RCA non-trivial."""

    def __init__(self, sim: Sim):
        self.sim, self.rng = sim, sim.rng
        self.items = []
        self.spikes = []
        scs = sim.scenarios
        if not scs:
            return
        first_fail = min(sc.fail_at for sc in scs)
        busy_svcs = set().union(*(sc.services for sc in scs))
        busy_rows = set().union(*(sc.rows for sc in scs))
        r = self.rng

        free = [n for n in sim.services if n not in busy_svcs]
        if free:
            svc = r.choice(free)
            t = first_fail - mins(r.randint(10, 25))
            ver = sim.bump(sim.version_at(svc, t), minor=False)
            a, b = sim.emit_deploy(t, svc, ver, "svc-deploy", sim.sha())
            self.items.append({"time": t, "type": "unrelated_deploy", "event_id": b["event"]["id"],
                               "description": f"Routine deploy of {svc} {ver} shortly before the failure"})

        racks = [k for k, v in sim.topo.racks.items() if v["row"] not in busy_rows]
        if racks:
            rack = r.choice(racks)
            t = sim.start + (first_fail - sim.start) * r.uniform(0.3, 0.7)
            tech = r.choice(TECHS)
            d = sim.emit(t - mins(2), "BADGE-GRANTED", f"Access granted: {tech} at hall-1-cage-2", "info", None,
                         user={"name": tech}, datacenter={"access": {"door": "hall-1-cage-2", "direction": "in"}})
            sim.emit(t + mins(1), "FAC-DOOR-AJAR", f"Rack {rack}: rear door open > 60s", "warning", rack,
                     observer={"name": f"door-sensor-{rack.lower()}", "type": "sensor"})
            self.spikes.append((rack, t, t + mins(16)))
            self.items.append({"time": t, "type": "transient_temp_spike", "entity": rack,
                               "description": f"Rack {rack} inlet temp spikes ~7 C for 15 min while a tech has the rear door "
                                              f"open, then recovers"})

        t = first_fail - mins(r.randint(5, 40))
        ip = sim.public_ip()
        for i in range(25):
            u = r.choice(["root", "admin", "ubuntu", "pi", "postgres"])
            sim.emit(t + timedelta(seconds=i * r.uniform(2, 6)), "AUTH-SSH-FAIL",
                     f"sshd: Failed password for invalid user {u} from {ip} port {r.randint(30000, 65000)} ssh2", "warning",
                     "bastion-1", user={"name": u}, source={"ip": ip}, datacenter={"auth": {"method": "password"}})
        d = sim.emit(t + mins(3), "FW-AUTOBLOCK", f"fail2ban: banned {ip} for 3600s after 25 failures (jail sshd)", "info",
                     "fw-edge-1", source={"ip": ip}, rule={"name": "fail2ban-sshd"})
        self.items.append({"time": t, "type": "blocked_bruteforce", "entity": ip,
                           "description": "Short SSH brute-force burst against bastion, auto-blocked, no successful login"})

        t = first_fail - mins(r.randint(20, 60))
        admin = r.choice(list(ADMINS))
        chg = sim.new_change_id()
        sim.emit_change(chg, "Rotate API credentials for metrics exporter", admin, "prometheus-exporter",
                        created=t - mins(90), approved=t - mins(30), implemented=t, closed=t + mins(15))
        self.items.append({"time": t, "type": "unrelated_change", "entity": chg,
                           "description": f"{chg}: unrelated credential rotation by {admin} shortly before the failure"})

        host = r.choice(sim.base_hosts)
        t = first_fail - mins(r.randint(30, 90))
        for i in range(6):
            sim.edac(t + mins(i * 5 + r.uniform(0, 3)), host)
        self.items.append({"time": t, "type": "corrected_ecc_burst", "entity": host,
                           "description": f"Burst of corrected (harmless) ECC memory errors on {host}"})

    def apply(self, ts):
        for rack, a, b in self.spikes:
            if a <= ts <= b:
                x = (ts - a) / (b - a)
                for h in self.sim.topo.racks[rack]["hosts"]:
                    self.sim.mod(h, "inlet", 7 * math.sin(math.pi * x))


# --------------------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------------------
def index_of(doc):
    ds = doc["data_stream"]
    return f"{ds['type']}-{ds['dataset']}-{ds['namespace']}"


class Output:
    def __init__(self, sim, args):
        self.sim, self.args = sim, args
        self.fh = None
        if args.out:
            self.fh = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
        self.es = ES(args) if args.es_url else None
        self.sent = 0

    def dumps(self, obj):
        return json.dumps(obj, separators=(",", ":"),
                          default=lambda o: iso(self.sim.tmap(o)) if isinstance(o, datetime) else str(o))

    def write(self, docs):
        lines = []
        for d in docs:
            if self.args.format == "bulk":
                lines.append(json.dumps({"create": {"_index": index_of(d)}}))
            lines.append(self.dumps(d))
        if self.fh:
            self.fh.write("\n".join(lines) + "\n")
            self.fh.flush()
        if self.es:
            bulk = []
            for d in docs:
                bulk.append(json.dumps({"create": {"_index": index_of(d)}}))
                bulk.append(self.dumps(d))
            self.es.bulk(bulk)
        self.sent += len(docs)

    def close(self):
        if self.fh and self.fh is not sys.stdout:
            self.fh.close()


class ES:
    def __init__(self, args):
        self.url = args.es_url.rstrip("/") + "/_bulk"
        self.headers = {"Content-Type": "application/x-ndjson"}
        if args.es_api_key:
            self.headers["Authorization"] = f"ApiKey {args.es_api_key}"
        elif args.es_user:
            tok = base64.b64encode(f"{args.es_user}:{args.es_password or ''}".encode()).decode()
            self.headers["Authorization"] = f"Basic {tok}"
        self.ctx = ssl.create_default_context()
        if args.insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.errors = 0

    def bulk(self, lines, chunk=4000):
        for i in range(0, len(lines), chunk * 2):
            body = ("\n".join(lines[i:i + chunk * 2]) + "\n").encode()
            req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=120) as resp:
                    res = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                sys.exit(f"Elasticsearch bulk request failed: HTTP {e.code}: {e.read()[:500]!r}")
            except urllib.error.URLError as e:
                sys.exit(f"Cannot reach Elasticsearch at {self.url}: {e.reason}")
            if res.get("errors"):
                bad = [it["create"]["error"] for it in res["items"] if "error" in it.get("create", {})]
                if self.errors == 0 and bad:
                    print(f"  ! first bulk error: {json.dumps(bad[0])[:400]}", file=sys.stderr)
                self.errors += len(bad)


def stream_out(sim, out, speed):
    wall0 = datetime.now(UTC)
    sim.tmap = lambda dt: wall0 + (dt - sim.start) / speed
    buf = []
    for doc in sim.docs:
        wait = (sim.tmap(doc["@timestamp"]) - datetime.now(UTC)).total_seconds()
        if wait > 0:
            if buf:
                out.write(buf)
                buf = []
            time.sleep(wait)
        buf.append(doc)
        if len(buf) >= 2000:
            out.write(buf)
            buf = []
    if buf:
        out.write(buf)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Simulate data center metrics/logs with telegraphed failures for Elasticsearch.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--scenario", default="random", help="scenario name, comma list, 'random', 'all' or 'none'")
    ap.add_argument("--list-scenarios", action="store_true")
    ap.add_argument("--seed", type=int, default=None, help="random seed (printed if not given)")
    ap.add_argument("--start", help="ISO start time (default: now minus duration)")
    ap.add_argument("--duration", default="6h")
    ap.add_argument("--tick", default="60s", help="metric sampling interval")
    ap.add_argument("--fail-at", help="failure offset from start, e.g. 270m or 4.5h (single scenario only)")
    ap.add_argument("--lead", help="override precursor lead time, e.g. 2h")
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--racks-per-row", type=int, default=5)
    ap.add_argument("--hosts-per-rack", type=int, default=8)
    ap.add_argument("--dc", default="dal1")
    ap.add_argument("--namespace", default="dal1", help="data_stream.namespace (lowercase, no hyphens)")
    ap.add_argument("--no-red-herrings", action="store_true")
    ap.add_argument("--out", default="events.ndjson", help="output file, '-' for stdout, '' to disable")
    ap.add_argument("--format", choices=["bulk", "docs"], default="bulk", help="bulk = _bulk API NDJSON; docs = one doc per line")
    ap.add_argument("--answer-key", default="scenario.json")
    ap.add_argument("--catalog", default="event_catalog.json")
    ap.add_argument("--es-url", help="index directly, e.g. https://localhost:9200")
    ap.add_argument("--es-user")
    ap.add_argument("--es-password")
    ap.add_argument("--es-api-key")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (local dev clusters only)")
    ap.add_argument("--stream", action="store_true", help="emit in real time (timestamps compressed by --speed)")
    ap.add_argument("--speed", type=float, default=60.0, help="stream mode: simulated seconds per wall second")
    args = ap.parse_args()

    if args.list_scenarios:
        for n, c in SCENARIOS.items():
            print(f"{n:20s} lead={c.default_lead:5s} {c.title}")
        return
    if args.seed is None:
        args.seed = random.randrange(1, 10 ** 6)
    if "-" in args.namespace or args.namespace != args.namespace.lower():
        sys.exit("--namespace must be lowercase without hyphens (data stream naming rules)")

    sim = Sim(args)
    r = sim.rng
    if args.scenario == "all":
        names = list(SCENARIOS)
    elif args.scenario == "random":
        names = [r.choice(list(SCENARIOS))]
    elif args.scenario == "none":
        names = []
    else:
        names = [n.strip() for n in args.scenario.split(",") if n.strip()]
        bad = [n for n in names if n not in SCENARIOS]
        if bad:
            sys.exit(f"unknown scenario(s): {bad}. Use --list-scenarios.")

    span = sim.end - sim.start
    if args.fail_at and len(names) == 1:
        fails = [sim.start + parse_dur(args.fail_at)]
    elif len(names) == 1:
        fails = [sim.start + span * r.uniform(0.72, 0.85)]
    else:
        fracs = [0.45 + 0.43 * (i + 1) / len(names) for i in range(len(names))]
        r.shuffle(fracs)
        fails = [sim.start + span * f for f in fracs]
    for n, fa in zip(names, fails):
        cls = SCENARIOS[n]
        fa = fa.replace(second=0, microsecond=0)
        sim.scenarios.append(cls(sim, fa, parse_dur(args.lead or cls.default_lead)))
    if not args.no_red_herrings:
        sim.red = RedHerrings(sim)

    t0 = time.time()
    sim.run()
    gen_s = time.time() - t0

    out = Output(sim, args)
    if args.stream:
        stream_out(sim, out, args.speed)  # sets sim.tmap, so answer key below uses compressed times
    else:
        out.write(sim.docs)
    out.close()

    key = {"seed": args.seed, "dc": args.dc, "namespace": args.namespace,
           "window": {"start": sim.start, "end": sim.end, "tick_seconds": sim.tick_s,
                      "time_compression": args.speed if args.stream else 1},
           "scenarios": [sc.answer() for sc in sim.scenarios],
           "red_herrings": sorted(sim.red.items, key=lambda x: x["time"]) if sim.red else []}
    if args.answer_key:
        with open(args.answer_key, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(key, indent=2, default=lambda o: iso(sim.tmap(o)) if isinstance(o, datetime) else str(o)))

    if args.catalog:
        cat = {}
        for code, (dtype, ds, kind, c, et, action) in EVENTS.items():
            cat[code] = {"index": f"{dtype}-{ds}-{args.namespace}", "data_stream.type": dtype, "data_stream.dataset": ds,
                         "event.kind": kind, "event.category": c, "event.type": et, "event.action": action,
                         "count": sim.stats.get(code, 0)}
        with open(args.catalog, "w", encoding="utf-8") as fh:
            json.dump(cat, fh, indent=2)

    by_index = Counter()
    for code, n in sim.stats.items():
        dtype, ds = EVENTS[code][:2]
        by_index[f"{dtype}-{ds}-{args.namespace}"] += n
    p = lambda *a: print(*a, file=sys.stderr)
    p(f"dcsim seed={args.seed}  window {iso(sim.tmap(sim.start))} -> {iso(sim.tmap(sim.end))}  "
      f"{len(sim.docs):,} docs in {gen_s:.1f}s")
    for idx, n in sorted(by_index.items()):
        p(f"  {idx:42s} {n:>9,}")
    for sc in sim.scenarios:
        tk = sc.ticket["id"] if sc.ticket else "(no ticket - failure after window end?)"
        p(f"  scenario {sc.name:18s} precursors {iso(sim.tmap(sc.pre))}  failure {iso(sim.tmap(sc.fail_at))}  ticket {tk}")
    if sim.red:
        p(f"  red herrings: {len(sim.red.items)}")
    if out.es:
        p(f"  indexed into {args.es_url} ({out.es.errors} errors)")
    p(f"  answer key -> {args.answer_key}   catalog -> {args.catalog}   events -> {args.out or '(none)'}")


if __name__ == "__main__":
    main()
