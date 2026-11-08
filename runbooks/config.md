# Config: configuration change or push

Entity types: load balancer, service, host, configuration file, change, commit

## Symptoms this covers
Errors, queueing, rejected connections, or resource exhaustion (for example a disk filling) that start after a configuration push, a load balancer reload, or a Helm values change.

## Immediate steps
- Identify the change: change `<change_id>`, commit `<commit>`, file `<file>` line `<line>`; read the diff with `git show <commit>`.
- Revert the change through the pipeline: `git revert <commit>` and push, so the config pipeline redeploys the previous version.
- On load balancer `<lb>`, if the pipeline is slow, restore the previous file, validate and reload: `haproxy -c -f /etc/haproxy/haproxy.cfg` then `systemctl reload haproxy`.
- For a service configured through Helm, roll the release back: `helm -n prod history <service>` then `helm -n prod rollback <service> <revision>`.
- If the change filled a host's disk with logs, free space on host `<host>`: `journalctl --vacuum-size=1G` and `logrotate -f /etc/logrotate.conf`, after the revert.

## How to verify
- `git log -1 -- <file>` shows the revert deployed, and `haproxy -c -f /etc/haproxy/haproxy.cfg` (or `helm -n prod get values <service>`) shows the old values.
- The symptom metric (5xx rate, queue depth, disk used) returns to baseline.
- Change `<change_id>` is updated with the revert.

## How to prevent
- Validate configuration in CI and require peer review for production config.
- Changes must name a validation check before they are marked implemented.

## Rollback risk
Reverting the commit also reverts anything else in it. A reload of load balancer `<lb>` briefly drops idle connections.

## Safe triage (cause not covered here)
- Diff the running configuration against the last known good version before changing anything, then escalate to the change owner.
