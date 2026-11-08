# CLAUDE.md — dcsim (data center log simulator)

## What this project is

`dcsim.py` generates synthetic data center telemetry and logs for Elasticsearch. Each run seeds one or more **failure scenarios** whose cause is telegraphed by precursor signals (metric ramps, warnings, admin changes) before the failure happens and an **incident ticket** is raised.

It feeds a hackathon project: a tool that, given an incident ticket, automatically does the root-cause research an on-call engineer would do. The simulator must therefore produce logs where:
- the true cause is discoverable by walking backward from the ticket through shared identifiers,
- the ticket describes **symptoms only**, never the cause,
- background noise and **red herrings** make the search non-trivial,
- an **answer key** (`scenario.json`) records ground truth so the RCA tool can be scored.

Keep all four properties intact when changing anything.

## Running it

Pure Python 3.10+, standard library only. Do not add third-party dependencies (`requests`, `elasticsearch-py`, etc.) without being asked.

```bash
python3 dcsim.py --list-scenarios
python3 dcsim.py --scenario cooling_failure --seed 7                 # -> events.ndjson, scenario.json, event_catalog.json
python3 dcsim.py --scenario all --duration 12h --seed 7              # all nine; 12h gives them room
python3 dcsim.py --scenario none                                     # noise only (baseline)
python3 dcsim.py --scenario memory_leak --format docs --out -        # one doc per line to stdout
python3 dcsim.py --scenario disk_fill --es-url https://localhost:9200 --es-api-key "$KEY"
python3 dcsim.py --scenario bad_optic --stream --speed 60 --es-url ...   # live: timestamps compressed 60x
```

Key flags: `--seed`, `--start` (ISO, default now − duration), `--duration` (6h), `--tick` (60s), `--fail-at` (offset, single scenario), `--lead` (override precursor lead), `--rows/--racks-per-row/--hosts-per-rack` (4/5/8 → 160 hosts), `--namespace` (dal1), `--no-red-herrings`, `--format bulk|docs`, `--out` (`-` = stdout, `''` = none), `--answer-key`, `--catalog`, `--insecure`.

A default 6h run is ~180k docs and takes ~3s. Use a small topology for quick tests:
`--rows 2 --racks-per-row 2 --hosts-per-rack 3 --duration 3h`.

## Outputs

| File | Contents |
|---|---|
| `events.ndjson` | `_bulk` NDJSON: `{"create":{"_index":"<type>-<dataset>-<ns>"}}` + doc, sorted by `@timestamp` |
| `scenario.json` | Answer key: per scenario `root_cause`, `precursor_start`, `failure_time`, `ticket`, `affected`, `metric_signals`, `causal_chain` (with `event_id`s); plus `red_herrings` |
| `event_catalog.json` | Every event code → index, kind, category, type, action, count |

`scenario.json` is ground truth. **Never** let its contents leak into the event stream (no `root_cause` hints in messages, labels, or ticket text).

## Architecture (single file, top to bottom)

1. **`EVENTS` catalog**: `code -> (data_stream.type, dataset, event.kind, event.category, event.type, event.action)`. Single source of truth for how each event type is classified and routed.
2. **Helpers**: `ramp(ts, start, end, shape)` (linear/exp/log 0→1), `merge`, `parse_dur`, `iso`, `mins`.
3. **`Topology`**: rows (A, B, …) → racks (`C04`) → hosts (`cmp-c04-07`), one CRAC per row (`crac-c`), a ToR switch per rack (`tor-c04`, uplinks `eth1/49`→spine-1, `eth1/50`→spine-2), two PDUs per rack (`pdu-c04-a/b`), and appliances `lb-1`, `lb-2`, `bastion-1`, `fw-edge-1`, `ups-1`. Two rows per k8s cluster (`k8s-prod-1`, …). `topo.loc[entity]` maps every entity to its location labels.
4. **`Sim`**: world state and the tick loop.
   - `mods`: **per-tick** modifiers, reset every tick. Scenarios write them with `mod(ent, key, val)` (additive) or `setm` (absolute); emitters read them with `m(ent, key, default)`.
   - `flags`: **persistent** state (`(pdu, "offline")`, `(host, disk, "failed")`, `(svc, "errmsg")`, `(crac, "status")`, …).
   - `down` / `down_reason`: hosts that are off. Newly down hosts automatically emit `K8S-NODE-NOTREADY`, and their load shifts to the rest of the cluster.
   - `host_state`: per-tick computed host metrics, read by facility, network and service emitters.
   - Loop order per tick: reset mods → `scenario.apply(ts)` → red herrings → `emit_hosts` → `emit_facility` → `emit_network` → `emit_services` → `emit_storage` → `emit_certs` → `emit_noise`.
   - **Causality is emergent where possible**: e.g. inlet temp → CPU temp → throttle (≥92 °C) → thermal shutdown (≥100 °C); throttled/down/packet-loss/iowait hosts → service p99 and error rate; `/var` ≥ 99.5% → host errors. Prefer pushing a physical cause into `mods` and letting emitters derive symptoms over hand-scripting symptom logs.
5. **`Scenario` base + 9 subclasses** (registered in `SCENARIOS`).
6. **`RedHerrings`**: unrelated deploy, transient rack temp spike (with door-open alarm), blocked SSH brute force, unrelated change, harmless ECC burst. Placed near the first failure and kept away from scenario-affected services and rows.
7. **Output**: `Output`, `ES` (urllib `_bulk`, `create` op), `stream_out` (remaps timestamps via `sim.tmap`).
8. **`main()`**: CLI, scenario selection and fail-time placement, writes outputs and a summary to stderr.

## Document contract (do not break)

Every document has:
- `@timestamp`
- `data_stream.{type,dataset,namespace}`; the target index is exactly `<type>-<dataset>-<namespace>`
- `event.{id,code,action,kind,category[],type[],dataset,module,severity}`
- `log.level` (`info|warning|error|critical`)
- `labels.{dc,room}`, plus `row/rack/cluster` when the entity has a location
- `host.{name,hostname,ip}` when the entity is a host

Conventions:
- ECS field names where they exist (`host.*`, `user.name`, `source.ip`, `destination.*`, `service.{name,version}`, `observer.*`, `orchestrator.*`, `tls.*`, `file.*`, `error.*`, `rule.name`, `process.*`, `threat.*`).
- Measurements and domain-specific fields go under the custom `datacenter.<area>.*` namespace (`datacenter.bmc.inlet_temp_c`, `datacenter.pdu.current_a`, …). Include units in the field name (`_c`, `_a`, `_ms`, `_pct`, `_dbm`, `_kw`).
- Non-ECS objects already in use: `change.*`, `cicd.*`, `ticket.*`, `kubernetes.*`. Reuse them; don't invent parallel ones.
- Dataset names: lowercase, dot-separated, **no hyphens** (data stream naming rules). Namespace: lowercase, no hyphens.
- Public IPs come only from RFC 5737 test ranges (`sim.public_ip()`). Domains use `example.com`.
- Pod names come from `sim.pod(svc, host, ts)` and change when the service version changes.

## Determinism rules

The same `--seed` must produce byte-identical output, whatever `PYTHONHASHSEED` is. After any change, verify:
```bash
for i in 1 2; do PYTHONHASHSEED=$i python3 dcsim.py --scenario all --seed 11 --start 2026-11-07T08:00:00Z \
  --out /tmp/d$i.ndjson --answer-key /tmp/d$i.json --catalog /tmp/c.json 2>/dev/null; done; cmp /tmp/d1.ndjson /tmp/d2.ndjson && echo deterministic
```
- All randomness goes through `sim.rng`; event IDs come from the separate `sim.idrng`. Never use the `random` module directly, `uuid4()`, `time.time()` or `datetime.now()` inside generation.
- **Never iterate a `set` (or a dict built from one) while drawing random numbers.** Use `sorted(...)`. This bug has already happened once (`hosts_scope`).
- Only use `--start` in tests. The default start depends on wall-clock time.

## How to add a new event type

1. Add a code to `EVENTS` with a **unique `event.action`**, and pick the dataset (an existing one, or a new `area.thing` dataset).
2. Emit it only through `sim.emit(ts, code, message, level, where, jitter=..., **fields)` (or `sim.k8s(...)` for Kubernetes events, `sim.emit_change(...)` for change tickets, `sim.emit_deploy(...)` for deploys).
3. The catalog file updates itself; check the code shows a non-zero count in a run that should produce it.

## How to add a new scenario

Subclass `Scenario` and add it to `SCENARIOS`:

```python
class MyScenario(Scenario):
    name = "my_scenario"            # snake_case, used on the CLI
    title = "cause -> precursor -> ... -> failure"
    default_lead = "3h"             # precursor window before failure

    def setup(self):                # runs once, before the loop
        # pick targets with self.rng; fill self.services / self.rows / self.hosts_scope
        #   (red herrings avoid these, and they must be sorted lists or sets you never iterate with rng)
        # emit fixed-time discrete events (changes, logins, deploys) here via self.ev(...)
        # set self.root = {summary, category, entity, entity_type, + ids such as change_id/commit/file/line/user}
        # set self.affected = {...}
        # set self.emergent = [(code, description, predicate)]: first matching log after self.pre joins the chain
        ...

    def signals(self):              # metric series that telegraph the failure (answer key only)
        return [self.sig("system.host", "datacenter.host.mem_used_pct", {"host.name": [...]}, "why it matters")]

    def apply(self, ts):            # runs every tick
        # ramps: self.f(ts, shape="exp") is 0 at self.pre and 1 at self.fail_at
        # push causes into self.sim.mods / flags; let emitters produce symptoms
        # one-shot timed events: if self.at(ts, when, "key"): self.ev(when, CODE, msg, level, where, chain="what this step means")
        # rate-limited repeats: if self.sim.every((entity, "key"), ts, seconds): ...
        # after failure: self.ticket_at(ts, self.fail_at + mins(3), "<symptom-only title>", "P1|P2", where=..., service=..., desc=...)
        ...
```

Scenario requirements:
- **Telegraphed**: at least one metric ramp **and** at least one log warning before `fail_at`. Precursors should start subtle (exp ramps) and get louder.
- **Root cause is a concrete event in the stream**: an alarm, change, commit, login or deploy, with IDs (change ID, commit SHA, `file:line`, user, device, interface). Mark it in the chain with `chain="ROOT CAUSE: ..."`. Mark the failure moment with `chain="FAILURE: ..."`.
- **Traceable**: the path from ticket → symptom → cause must be walkable through shared fields (`labels.rack/row/cluster`, `host.name`, `service.name`, `observer.name`, `user.name`, `source.ip`, `change.id`).
- **Ticket is symptom-only**, raised a few minutes after `fail_at`, exactly once (`ticket_at` handles the once-only part; `when` must be a fixed time, not `ts + x`).
- Keep the realistic tone: messages should look like the real system's log format (kernel, sshd, haproxy, NX-OS, kubelet, BMC SEL, BMS alarms).

## Verification after changes

Run all of these before calling a change done:
1. `python3 dcsim.py --list-scenarios`
2. Each scenario on its own, checking it prints a ticket ID (not `(no ticket ...)`):
   ```bash
   for s in $(python3 dcsim.py --list-scenarios | awk '{print $1}'); do
     python3 dcsim.py --scenario $s --seed 11 --start 2026-11-07T08:00:00Z --out /tmp/$s.ndjson \
       --answer-key /tmp/$s.json --catalog /tmp/cat.json 2>&1 | grep "scenario "; done
   ```
3. Read the causal chain in each answer key and confirm it is in a sensible order: root cause → precursors → failure → ticket.
4. Bulk-format check: lines alternate action/doc; `_index == <type>-<dataset>-<namespace>`; index names match `^(logs|metrics)-[a-z0-9._]+-[a-z0-9_.]+$`; timestamps non-decreasing; every doc has the required fields above; no two codes share an `event.action`.
5. The determinism check above.
6. `--scenario none` still runs (noise only, empty `scenarios` list).

There is no Elasticsearch in the default dev environment. If ES is needed, ask the user for a URL and credentials, or point them to a local single-node cluster (Docker). Never disable TLS verification by default; `--insecure` stays opt-in.

## Planned next work: Elasticsearch cluster and data stream setup

The user wants help building the ES side so each data stream is separated and correctly mapped. The intended direction:
- **Index templates per data stream family** (`metrics-bmc.sensors-*`, `logs-security.auth-*`, …) or per area (`metrics-facility.*-*`), with `data_stream: {}`, priority > 200 so they win over the built-in `logs`/`metrics` templates, and composed from component templates (shared ECS/labels mappings + per-dataset mappings).
- **Explicit mappings** instead of dynamic ones: `labels.*`, `event.*`, `host.name`, `service.name`, `observer.name`, `user.name` as `keyword`; IPs as `ip`; `datacenter.*` numerics as `float`/`scaled_float`/`long`; `data_stream.*` as `constant_keyword`; `message` as `match_only_text`. Consider `index.mode: time_series` (TSDS) for metrics with `host.name`/`observer.name`/`service.name` as dimensions.
- **ILM**: short retention for high-volume metrics (bmc.sensors, system.host, network.interface, storage.smart), longer retention for security, change and incident logs.
- Possibly **ingest pipelines** for enrichment, and Kibana data views per area (facility, compute, network, security, change, incidents) for the RCA tool and demo.
- Deliver these as scripts or JSON files that apply via the REST API (e.g. `es_setup.py` using urllib, or curl-able JSON), idempotently, with the same auth flags as `dcsim.py`.
- Use `event_catalog.json` as the list of data streams to create templates for. If you add a dataset to `EVENTS`, add its template too.

## Things not to do

- Don't put root-cause information in tickets or in any field the RCA tool will read as a "hint".
- Don't rename existing datasets, codes or fields casually. The RCA tool and future ES mappings depend on them. If a rename is needed, change it everywhere and say so.
- Don't make the default run dramatically larger or slower. Keep big topologies opt-in via flags.
- Don't use real company names, real public IPs, or real people's names in generated data.
- Don't commit generated output (`events.ndjson`, `scenario.json`, `event_catalog.json`) unless asked. Use `/tmp` or a `t/` scratch dir for test runs.
