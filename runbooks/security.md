# Security: intrusion or misuse

Entity types: host, user account, source IP, firewall rule, firewall, IAM

## Symptoms this covers
IDS exfiltration alerts, unexpected logins or sudo, new IAM roles or policies, unauthorized firewall rule changes, unusual egress from a host.

## Immediate steps
- Preserve evidence first: in the virtualization or storage console, snapshot host `<host>`'s disks, and capture memory with `avml /tmp/<host>.lime` before any reboot.
- Isolate host `<host>`: `kubectl cordon <host>` and `kubectl drain <host> --ignore-daemonsets --delete-emptydir-data`, then block its traffic on the switch or firewall.
- On firewall `fw-edge-1`, block source IP `<source_ip>` (`deny ip host <source_ip> any`) and remove the unauthorized rule `<rule>`.
- Lock the compromised user account `<user>`: `usermod -L <user>`, remove its keys from `~<user>/.ssh/authorized_keys`, and rotate its credentials.
- In the IAM console, export and then delete the roles and policies the attacker created for user account `<user>`.

## How to verify
- Firewall logs show no traffic to or from `<source_ip>`, and IDS alerts for host `<host>` stop.
- `last <user>` and the auth logs show no new logins for the account.
- The IAM audit shows no unexpected roles or policies remain.

## How to prevent
- Require MFA on the bastion, enforce egress allowlists, and require approval for firewall rule changes.
- Alert on new IAM roles and on rules allowing egress to new external IPs.

## Rollback risk
Isolating host `<host>` takes its workloads down. Rebooting or reimaging before the snapshot destroys evidence.

## Safe triage (cause not covered here)
- Preserve logs and disk state, restrict access to the affected host, and escalate to the security team before cleaning anything up.
