# Unknown: root cause not determined

Entity types: service, host, cluster

## Symptoms this covers
Any incident where the investigation did not find a concrete cause.

## Immediate steps
- Stabilize the affected service `<service>` without guessing: check `kubectl -n prod get pods -l app=<service>` and `kubectl -n prod get events --sort-by=.lastTimestamp | tail -50`.
- Check recent changes to service `<service>` in the change console and with `kubectl -n prod rollout history deployment/<service>` for anything in the last 6 hours.
- On host `<host>`, capture state before restarting anything: `journalctl --since '-1h' > /tmp/<host>-journal.txt` and `dmesg -T | tail -200`.
- In the paging console, escalate service `<service>` to its owning team with the ticket, this report and its open questions.

## How to verify
- The ticket's symptom metric returns to baseline, and the owning team has confirmed a cause.

## How to prevent
- Add the missing signal that would have identified the cause to monitoring.

## Rollback risk
Restarting or rolling back blindly can destroy evidence or make things worse. Capture state first.

## Safe triage (cause not covered here)
- Capture state, avoid restarts, and escalate to the owning team.
