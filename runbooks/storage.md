# Storage: disk or RAID failure

Entity types: host, disk, RAID array

## Symptoms this covers
RAID member failed, degraded or failed array, block I/O errors, filesystem remounted read-only, rising iowait, pods on one host crash-looping.

## Immediate steps
- On host `<host>`, read the array state: `cat /proc/mdstat` and `mdadm --detail /dev/<array>`.
- Identify the failed disk by serial before touching hardware: `smartctl -i /dev/<disk>` and `smartctl -a /dev/<disk>`.
- Move workloads off the host: `kubectl cordon <host>` then `kubectl drain <host> --ignore-daemonsets --delete-emptydir-data`.
- If the array is degraded but alive, remove and replace the failed member: `mdadm --manage /dev/<array> --remove /dev/<disk>`, swap the disk, then `mdadm --manage /dev/<array> --add /dev/<disk>`.
- If the array has failed, keep host `<host>` out of service (`kubectl taint node <host> storage=failed:NoExecute`) and restore its data from backup onto a healthy host.

## How to verify
- `cat /proc/mdstat` shows the rebuild progressing and then `[UU]`.
- `dmesg -T | grep -i 'i/o error'` shows no new errors and the filesystem is mounted read-write (`findmnt /`).
- `kubectl get node <host>` is Ready before `kubectl uncordon <host>`.

## How to prevent
- Replace a disk on its first failure; do not run degraded arrays. Keep a hot spare.
- Alert on SMART reallocated and pending sectors before a disk fails.

## Rollback risk
Pulling the wrong disk from a degraded array destroys it. Match the serial from `smartctl -i` to the bay first.

## Safe triage (cause not covered here)
- On host `<host>`, capture `dmesg -T | tail -200` and `smartctl -a` for every disk, then escalate to the hardware team.
