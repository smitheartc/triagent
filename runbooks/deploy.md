# Deploy: bad release or rollout

Entity types: service, deployment, version, commit

## Symptoms this covers
Errors, latency or crashes that start right after a rollout of a new version; pods on the new image failing while the previous version was healthy.

## Immediate steps
- Confirm the rollout and its revision for service `<service>`: `kubectl -n prod rollout history deployment/<service>`.
- Stop further rollouts of service `<service>`: `kubectl -n prod rollout pause deployment/<service>`, and freeze change `<change_id>` in the change console.
- Roll service `<service>` back to the previous version `<previous_version>`: `kubectl -n prod rollout undo deployment/<service>`, or run the `<service>-release` pipeline with `<previous_version>`.
- In the change console, attach this report to change `<change_id>` and notify the author of commit `<commit>` (`git log -1 --format='%an <%ae>' <commit>`).

## How to verify
- `kubectl -n prod rollout status deployment/<service>` completes and `kubectl -n prod get pods -l app=<service> -o jsonpath='{.items[*].spec.containers[*].image}'` shows `<previous_version>`.
- Error rate and p99 latency for service `<service>` return to baseline.

## How to prevent
- Canary new versions and roll back automatically on SLO burn.
- Require a validation step in change `<change_id>`'s template before marking it implemented.

## Rollback risk
Rolling back breaks if the new version ran a non-reversible schema or data migration. Check migrations in commit `<commit>` first.

## Safe triage (cause not covered here)
- Compare `kubectl -n prod rollout history deployment/<service>` with the incident start before acting, then escalate to the owning team.
