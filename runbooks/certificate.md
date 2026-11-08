# Certificate: issuance, renewal or expiry

Entity types: certificate, issuer, load balancer, service

## Symptoms this covers
TLS handshake failures, browsers rejecting a certificate, certificate renewal failures, certificate scans reporting few days remaining or expired.

## Immediate steps
- Read the certificate state: `kubectl -n <namespace> describe certificate <certificate>` and `kubectl -n <namespace> get certificaterequest,order,challenge`.
- Find why renewal fails (issuer credentials, DNS or HTTP challenge): `kubectl -n cert-manager logs deploy/cert-manager --since=1h | grep <certificate>`.
- Fix the issuer: update the DNS provider API token or issuer config that the challenge needs, in the secret the issuer references (`kubectl -n cert-manager get issuer,clusterissuer -o yaml`).
- Force a renewal of certificate `<certificate>`: `cmctl renew <certificate> -n <namespace>`.
- If renewal cannot succeed quickly, install a manually issued certificate on load balancer `<lb>` and run `systemctl reload haproxy`.

## How to verify
- `echo | openssl s_client -connect <hostname>:443 -servername <hostname> 2>/dev/null | openssl x509 -noout -dates` shows a notAfter in the future.
- `kubectl -n <namespace> get certificate <certificate>` shows READY True, and TLS handshake errors on load balancer `<lb>` stop.

## How to prevent
- Alert on renewal failures and on fewer than 14 days remaining, not on expiry.
- Any change to DNS providers or credentials must update the certificate issuers in the same change.

## Rollback risk
A manually installed certificate does not renew; replace it with an automated one before it expires. A wrong chain breaks older clients.

## Safe triage (cause not covered here)
- Collect `kubectl -n <namespace> describe certificate <certificate>` and the cert-manager logs, then escalate to the platform team.
