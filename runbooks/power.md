# Power: PDU, UPS, breaker or feed failure

Entity types: PDU, rack, feed, UPS, host

## Symptoms this covers
PDU load warnings, a breaker trip, a feed offline (maintenance or firmware), a whole rack going dark, BMCs unreachable, NodeNotReady for every host in one rack.

## Immediate steps
- In the DCIM console, open PDU `<pdu>` and its partner feed for rack `<rack>`; record the current per feed and which feed is offline.
- If a feed is offline for maintenance or a firmware update, stop or roll back that work in the DCIM console and bring the feed back online first.
- Before resetting a tripped breaker, shed load so one feed stays under 80% of rating: power off non-critical hosts with `ipmitool -I lanplus -H <host>-bmc chassis power off`.
- Reset the breaker on PDU `<pdu>` from the PDU web console (or on site), then power hosts on one at a time: `ipmitool -I lanplus -H <host>-bmc chassis power on`.
- Keep rack `<rack>` hosts cordoned until both feeds are healthy: `kubectl cordon <host>`.
- In the DCIM console, check UPS `ups-1` load and battery state.

## How to verify
- DCIM shows both feeds of rack `<rack>` online and each under 80% of rated current.
- `ipmitool -I lanplus -H <host>-bmc chassis status` reports power on for every host in the rack.
- `kubectl get nodes -o wide | grep <rack>` shows the nodes Ready, then `kubectl uncordon <host>`.

## How to prevent
- Change approval must check that the remaining feed can carry the rack before one feed goes down.
- Run a capacity check before installing new servers; alert at 80% per feed.
- Schedule PDU firmware updates one feed at a time with a rollback plan.

## Rollback risk
Re-energizing a breaker with the same load trips it again. Powering every host on at once causes inrush and a second trip.

## Safe triage (cause not covered here)
- In the DCIM console, map which racks share the affected feed and UPS before acting.
- Escalate to facilities with the alarm ids and the rack list.
