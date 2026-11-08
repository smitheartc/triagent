# Memory: memory exhaustion from software

Entity types: service, pod, host

## Symptoms this covers
Steadily growing memory, GC overhead warnings, OutOfMemoryError, containers OOMKilled (exit 137), CrashLoopBackOff, 503s from too few healthy pods.

## Immediate steps
- Confirm which pods of service `<service>` are affected: `kubectl -n prod get pods -l app=<service>` and `kubectl -n prod top pods -l app=<service>`.
- Confirm the OOM kills on each pod of service `<service>`: `kubectl -n prod describe pod <pod>` (Last State: OOMKilled, exit code 137).
- If memory began growing after a release, roll service `<service>` back: `kubectl -n prod rollout undo deployment/<service>`.
- To buy time while you decide, restart the pods: `kubectl -n prod rollout restart deployment/<service>`.
- Raise the memory limit of service `<service>` only if usage is flat and simply above the limit: `kubectl -n prod set resources deployment/<service> --limits=memory=<limit>`.

## How to verify
- `kubectl -n prod top pods -l app=<service>` shows memory flat over 30 minutes.
- `kubectl -n prod get events --field-selector reason=OOMKilling` shows no new events and restart counts stop rising.
- Error rate and p99 latency for service `<service>` return to baseline.

## How to prevent
- Add a memory soak test to CI for service `<service>` and alert on memory slope per pod.
- Take a heap profile from one leaking pod before restarting it next time.

## Rollback risk
Rolling back drops any fixes in the new version and can break if it ran a schema migration. Restarts only reset the clock on a leak.

## Safe triage (cause not covered here)
- Capture `kubectl -n prod describe pod <pod>` and a heap dump from one pod, then escalate to the owning team.
