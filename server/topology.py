"""Entity -> neighbors, derived from labels.* terms aggregations and the naming scheme. Never imports dcsim.

Naming scheme (lowercase device names; labels.row / labels.rack are uppercase):
  tor-<rack>         top-of-rack switch; uplinks report datacenter.iface.peer = spine-1 / spine-2
  pdu-<rack>-a|b     rack PDUs, feeds A and B
  crac-<row>         one CRAC per row
  spine-<n>          spine switches; known only as the peer of ToR uplinks
  lb-<n>             load balancers; network.lb logs (observer.name) and app metrics (datacenter.app.load_balancer)
  fw-<name>          edge firewall; its logs are logs-security.firewall-* (no device name on the docs)
  bastion-<n>        bastion host; its logs are the sshd logins in logs-security.auth-* without host.name
  ups-<n>            room UPS; metrics-facility.ups-* (no location labels; treated as feeding every PDU)

An entity is resolved by the first field that carries its exact name (host.name, service.name, observer.name,
labels.rack/row/cluster, user.name, then datacenter.*.unit, datacenter.app.load_balancer, datacenter.iface.peer,
change.configuration_item). Appliances whose docs carry no name are resolved through the prefix rules above.
"""
from __future__ import annotations

from typing import Any, Callable

Search = Callable[[str, dict[str, Any]], dict[str, Any]]

# (kind, field) in resolution order
_FIELDS = [
    ("host", "host.name"), ("service", "service.name"), ("device", "observer.name"), ("rack", "labels.rack"),
    ("row", "labels.row"), ("cluster", "labels.cluster"), ("user", "user.name"),
    ("device", "datacenter.crac.unit"), ("device", "datacenter.pdu.unit"), ("device", "datacenter.ups.unit"),
    ("load_balancer", "datacenter.app.load_balancer"), ("spine", "datacenter.iface.peer"),
    ("configuration_item", "change.configuration_item"),
]
_TOP = 30
_HOSTS_TOP = 64


def _appliance_docs(name: str, ns: str) -> tuple[str, list[dict]] | None:
    """(index, filters) for the docs that represent an appliance when the docs do not carry its name."""
    if name.startswith("fw-"):
        return f"logs-security.firewall-{ns}", []
    if name.startswith("bastion-"):
        return f"logs-security.auth-{ns}", [{"bool": {"must_not": {"exists": {"field": "host.name"}}}}]
    if name.startswith("lb-"):
        return f"logs-network.lb-{ns}", [{"term": {"observer.name": name}}]
    if name.startswith("ups-"):
        return f"metrics-facility.ups-{ns}", [{"term": {"observer.name": name}}]
    return None


class _Q:
    def __init__(self, search: Search, ns: str) -> None:
        self.search, self.ns = search, ns
        self.all = f"logs-*-{ns},metrics-*-{ns}"

    def terms(self, flt: list[dict], field: str, size: int = _TOP, index: str | None = None) -> list[str]:
        res = self.search(index or self.all, {"size": 0, "query": {"bool": {"filter": flt}},
                                              "aggs": {"x": {"terms": {"field": field, "size": size}}}})
        return [str(b["key"]) for b in res.get("aggregations", {}).get("x", {}).get("buckets", [])]

    def location(self, flt: list[dict], index: str | None = None) -> dict[str, list[str]]:
        return {lvl: self.terms(flt, f"labels.{lvl}", 5, index) for lvl in ("row", "rack", "cluster")}

    def count(self, flt: list[dict], index: str | None = None) -> int:
        res = self.search(index or self.all, {"size": 0, "query": {"bool": {"filter": flt}},
                                              "track_total_hits": True})
        return int(res.get("hits", {}).get("total", {}).get("value", 0))

    def existing_devices(self, names: list[str]) -> list[str]:
        if not names:
            return []
        present = set(self.terms([{"terms": {"observer.name": names}}], "observer.name", 64))
        return [d for d in names if d in present]


def _resolve(q: _Q, entity: str) -> tuple[str, str, str, list[str]] | None:
    """(kind, field, exact name, other kinds it is also seen as) or None."""
    variants = sorted({entity, entity.lower(), entity.upper()})
    res = q.search(q.all, {"size": 0, "aggs": {
        f"{i}": {"filter": {"terms": {f: variants}}, "aggs": {"v": {"terms": {"field": f, "size": 3}}}}
        for i, (_, f) in enumerate(_FIELDS)}})
    aggs = res.get("aggregations", {})
    hits = [(kind, f, aggs[str(i)]["v"]["buckets"][0]["key"]) for i, (kind, f) in enumerate(_FIELDS)
            if aggs.get(str(i), {}).get("doc_count", 0) > 0 and aggs[str(i)]["v"]["buckets"]]
    if not hits:
        return None
    kind, field, name = hits[0]
    return kind, field, str(name), sorted({k for k, _, _ in hits[1:] if k != kind})


def _rack_devices(racks: list[str], rows: list[str]) -> list[str]:
    return [f"tor-{r.lower()}" for r in racks] + [f"pdu-{r.lower()}-{f}" for r in racks for f in "ab"] + \
           [f"crac-{r.lower()}" for r in rows]


def _one(v: list[str]) -> str | list[str]:
    return v[0] if len(v) == 1 else v


def neighbors(search: Search, ns: str, entity: str) -> dict[str, Any]:
    q = _Q(search, ns)
    resolved = _resolve(q, entity)
    appliance = _appliance_docs(entity.lower(), ns)
    if resolved is None and appliance is None:
        return {"entity": entity, "found": False, "note": "no docs carry this name (host, service, device, rack, "
                "row, cluster, user, unit, load balancer, interface peer, change CI) and it matches no appliance "
                "naming rule (fw-, bastion-, lb-, ups-)"}

    if resolved is None:
        kind, field, name, also = "appliance", "", entity.lower(), []
    else:
        kind, field, name, also = resolved
    out: dict[str, Any] = {"entity": name, "type": kind}
    if field:
        out["matched_field"] = field
    if also:
        out["also_seen_as"] = also
    flt = [{"term": {field: name}}] if field else []

    # Appliances first: their own docs are the most specific evidence of where they are and who touches them.
    if appliance is not None and kind in ("appliance", "device", "configuration_item", "load_balancer"):
        index, aflt = appliance
        if q.count(aflt, index):
            out["type"] = "appliance"
            out["evidence_index"] = index
            loc = q.location(aflt, index)
            out.update({k: _one(v) for k, v in loc.items() if v})
            if name.startswith("fw-"):
                out["rules"] = q.terms(aflt, "rule.name", 10, index)
                out["rule_changers"] = q.terms(aflt + [{"term": {"event.code": "FW-RULE-CHANGE"}}], "user.name",
                                               10, index)
            elif name.startswith("bastion-"):
                out["users_logged_in"] = q.terms(aflt, "user.name", 20, index)
                out["source_ips"] = q.terms(aflt, "source.ip", 10, index)
            elif name.startswith("ups-"):
                out["pdus_fed"] = q.terms([{"prefix": {"observer.name": "pdu-"}}], "observer.name", 64)
            if loc["rack"]:
                out["devices"] = q.existing_devices(_rack_devices(loc["rack"], loc["row"]))

    if kind == "host":
        loc = q.location(flt)
        out.update({k: _one(v) for k, v in loc.items() if v})
        out["rack_peers"] = [h for h in q.terms([{"terms": {"labels.rack": loc["rack"]}}], "host.name", _HOSTS_TOP)
                             if h != name] if loc["rack"] else []
        out["services_on_host"] = q.terms(flt, "service.name")
        out["devices"] = q.existing_devices(_rack_devices(loc["rack"], loc["row"]))
    elif kind == "rack":
        rows = q.terms(flt, "labels.row", 5)
        out.update({"row": _one(rows), "cluster": _one(q.terms(flt, "labels.cluster", 5))})
        out["hosts"] = q.terms(flt, "host.name", _HOSTS_TOP)
        out["devices"] = q.existing_devices(_rack_devices([name], rows))
    elif kind == "row":
        out["cluster"] = q.terms(flt, "labels.cluster", 5)
        out["racks"] = q.terms(flt, "labels.rack")
        out["devices"] = q.existing_devices([f"crac-{name.lower()}"])
    elif kind == "cluster":
        out["rows"] = q.terms(flt, "labels.row")
        out["racks"] = q.terms(flt, "labels.rack")
        out["services"] = q.terms(flt, "service.name")
    elif kind == "service":
        out["cluster"] = q.terms(flt, "labels.cluster", 5)
        out["hosts"] = q.terms(flt, "host.name", _HOSTS_TOP)
        out["racks"] = q.terms(flt, "labels.rack")
        out["load_balancer"] = q.terms(flt, "datacenter.app.load_balancer", 5)
    elif kind == "user":
        out["hosts_touched"] = q.terms(flt, "host.name")
        out["source_ips"] = q.terms(flt, "source.ip", 10)
        out["changes"] = q.terms(flt, "change.id", 10)
    elif kind == "spine":
        down = flt
        out["downstream_switches"] = q.terms(down, "observer.name", 64)
        out["racks"] = q.terms(down, "labels.rack")
        out["clusters"] = q.terms(down, "labels.cluster", 5)
    elif kind in ("device", "load_balancer", "configuration_item"):
        if out.get("type") != "appliance":
            loc = q.location(flt)
            out.update({k: _one(v) for k, v in loc.items() if v})
            if name.startswith("crac-"):
                out["racks_cooled"] = q.terms([{"terms": {"labels.row": loc["row"]}}], "labels.rack")
            elif loc["rack"]:
                out["hosts_in_rack"] = q.terms([{"terms": {"labels.rack": loc["rack"]}}], "host.name", _HOSTS_TOP)
            if name.startswith("tor-"):
                out["uplink_peers"] = q.terms(flt, "datacenter.iface.peer", 5)
            out["devices"] = [d for d in q.existing_devices(_rack_devices(loc["rack"], [])) if d != name]
        if name.startswith("lb-"):
            lbf = [{"term": {"datacenter.app.load_balancer": name}}]
            out["services_behind"] = q.terms(lbf, "service.name")
            out["clusters"] = q.terms(lbf, "labels.cluster", 5)
        if kind == "configuration_item":
            out["changes"] = q.terms(flt, "change.id", 10)
    return {k: v for k, v in out.items() if v not in (None, [], "")}
