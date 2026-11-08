# Cooling: CRAC unit or airflow failure

Entity types: CRAC unit, row, rack, host

## Symptoms this covers
CRAC compressor trip or high head pressure, supply air temperature rising, host inlet temperature warnings, CPU thermal throttling, thermal shutdowns and NodeNotReady across one row.

## Immediate steps
- In the BMS console, open the alarms for CRAC unit `<crac>` and acknowledge them; read the compressor status and supply air temperature.
- In the BMS console, lower the supply setpoint and raise fan speed on the neighbouring CRAC units to carry row `<row>`.
- In the facilities work-order console, open an urgent on-site inspection for CRAC unit `<crac>`; do not reset a compressor lockout remotely more than once.
- On the hottest hosts, read inlet temperature from the BMC: `ipmitool -I lanplus -H <host>-bmc sdr type Temperature`.
- Move workloads off throttling hosts in row `<row>`: `kubectl cordon <host>` then `kubectl drain <host> --ignore-daemonsets --delete-emptydir-data`.
- If inlet stays above 32 °C, power off non-critical hosts in rack `<rack>`: `ipmitool -I lanplus -H <host>-bmc chassis power soft`.

## How to verify
- BMS trend for CRAC unit `<crac>` shows supply air back under 20 °C and the compressor running.
- `ipmitool -I lanplus -H <host>-bmc sdr type Temperature` shows inlet under 27 °C on the row's hosts.
- `kubectl get nodes -l topology.kubernetes.io/zone=<row>` shows every node Ready before you uncordon with `kubectl uncordon <host>`.

## How to prevent
- Keep N+1 CRAC capacity per row and test failover quarterly.
- Alert on supply air temperature trend and head pressure, not only on hard trips.
- Track filter and condenser maintenance in the facilities work-order system.

## Rollback risk
Repeated compressor resets on a faulted unit can destroy the compressor. Uncordoning hosts before inlet temperature is stable brings the throttling back.

## Safe triage (cause not covered here)
- In the BMS console, check every CRAC unit in row `<row>` and the room temperature map before touching hosts.
- Escalate to facilities with the alarm ids and the affected row.
