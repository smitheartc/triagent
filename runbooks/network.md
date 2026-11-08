# Network: switch, link, optic or routing failure

Entity types: switch, interface, uplink, rack, host

## Symptoms this covers
Low transceiver receive power, CRC errors, interface flaps or err-disabled ports, BGP sessions dropping, intermittent timeouts for services in one rack.

## Immediate steps
- On switch `<switch>`, read the optic and errors: `show interface <interface> transceiver details` and `show interface <interface> counters errors`.
- Confirm the redundant uplink is healthy before touching the faulty one: `show interface status` and `show bgp ipv4 unicast summary` on switch `<switch>`.
- Take the faulty uplink out of service so traffic uses the healthy one: `configure terminal`, `interface <interface>`, `shutdown`.
- Replace the optic or clean the fiber on interface `<interface>`, then bring it back: `interface <interface>`, `no shutdown`.
- For an err-disabled interface `<interface>` on switch `<switch>`, clear it after the fix: `interface <interface>`, `shutdown`, `no shutdown`.

## How to verify
- `show interface <interface> transceiver details` shows receive power inside the optic's spec.
- `clear counters interface <interface>`, wait five minutes, and `show interface <interface> counters errors` shows no new CRC errors.
- `show bgp ipv4 unicast summary` shows the session Established and service p99 latency back to normal.

## How to prevent
- Alert on transceiver receive-power trends and CRC rate, not only on link down.
- Keep spare optics per switch model on site.

## Rollback risk
Shutting the last healthy uplink isolates rack `<rack>`. Check redundancy before every `shutdown`.

## Safe triage (cause not covered here)
- On switch `<switch>`, collect `show logging last 100` and `show interface status` before changing anything.
- Escalate to the network team with the interface, counters and timestamps.
