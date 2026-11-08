#!/usr/bin/env bash
# escluster/remote.sh: Elasticsearch + Kibana on a remote machine (an NVIDIA Brev instance, or any SSH host with
# Docker), reached from this machine through an SSH tunnel. Every tool keeps using http://localhost:9200 and
# http://localhost:5601, so server/.env, demo.sh, the scorer and Kibana need no changes.
#
#   escluster/remote.sh up <ssh-host>       copy docker-compose.yml there, start ES + Kibana, then open the tunnel
#   escluster/remote.sh tunnel <ssh-host>   only open the tunnel (the cluster is already running there)
#   escluster/remote.sh status <ssh-host>   container state and cluster health on the remote side
#   escluster/remote.sh down <ssh-host>     stop the remote containers (data stays in the remote volume)
#
# <ssh-host> is anything `ssh <ssh-host>` reaches. For Brev: `brev login`, create the instance, then `brev refresh`
# adds it to ~/.ssh/config under its name (check with `brev ls`).
#
# The remote side binds 9200 and 5601 to 127.0.0.1 only: security is off in docker-compose.yml, so the ports must
# never be reachable from the internet. The tunnel is the only way in.
#
# The tunnel runs in the foreground; leave its terminal open (Ctrl-C closes it). Local ports 9200 and 5601 must be
# free, so stop a local cluster first (docker compose -f escluster/docker-compose.yml stop; its data stays).
# The remote cluster starts empty: load it through the tunnel with ./demo.sh and
# `python3 server/scorer.py --ingest --runs 1` (seed 7 into namespace dal1), then `make kibana`.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CMD=${1:-}
HOST=${2:-}
REMOTE_DIR=triagent-es
PORTS=(9200 5601)

die() { printf '\033[31mremote.sh: %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
[[ -n "$CMD" && -n "$HOST" ]] || { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

remote() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }

check_ssh() {
    remote true 2>/dev/null || die "cannot ssh to '$HOST' without a prompt. For Brev: brev login && brev refresh, then try: ssh $HOST"
}

start_remote() {
    check_ssh
    remote 'command -v docker >/dev/null && docker compose version >/dev/null' \
        || die "docker with the compose plugin is not available on $HOST"
    say "copying docker-compose.yml to $HOST:~/$REMOTE_DIR (ports bound to 127.0.0.1 there)"
    remote "mkdir -p ~/$REMOTE_DIR"
    sed -e 's/"9200:9200"/"127.0.0.1:9200:9200"/' -e 's/"5601:5601"/"127.0.0.1:5601:5601"/' \
        "$ROOT/escluster/docker-compose.yml" | remote "cat > ~/$REMOTE_DIR/docker-compose.yml"
    remote "grep -q '127.0.0.1:9200:9200' ~/$REMOTE_DIR/docker-compose.yml" \
        || die "could not bind the remote ports to 127.0.0.1; not starting an open cluster"
    say "vm.max_map_count on $HOST (Elasticsearch needs 262144)"
    remote '[ "$(sysctl -n vm.max_map_count)" -ge 262144 ] || {
              echo vm.max_map_count=262144 | sudo -n tee /etc/sysctl.d/99-elasticsearch.conf >/dev/null &&
              sudo -n sysctl -q -w vm.max_map_count=262144; }' \
        || die "set it on $HOST by hand: sudo sysctl -w vm.max_map_count=262144"
    say "starting Elasticsearch and Kibana on $HOST (the first pull is about 1.5 GB)"
    remote "cd ~/$REMOTE_DIR && docker compose up -d"
    say "waiting for the cluster"
    for _ in $(seq 1 90); do
        remote "curl -fsS -m 3 localhost:9200/_cluster/health" 2>/dev/null | grep -qE '"status":"(green|yellow)"' && return 0
        sleep 2
    done
    die "Elasticsearch on $HOST did not come up: ssh $HOST 'docker logs triagent-es | tail -50'"
}

open_tunnel() {
    local p
    for p in "${PORTS[@]}"; do
        if curl -s -m 2 -o /dev/null "http://localhost:$p"; then
            die "something already answers on localhost:$p (a local cluster?). Stop it first:
    docker compose -f escluster/docker-compose.yml stop"
        fi
    done
    check_ssh
    say "tunnel open: localhost:9200 -> $HOST Elasticsearch, localhost:5601 -> $HOST Kibana. Leave this running; Ctrl-C closes it."
    exec ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -L 9200:localhost:9200 -L 5601:localhost:5601 "$HOST"
}

case "$CMD" in
    up)     start_remote; open_tunnel ;;
    tunnel) open_tunnel ;;
    status) check_ssh
            remote "cd ~/$REMOTE_DIR 2>/dev/null && docker compose ps; curl -s -m 3 localhost:9200/_cluster/health; echo" ;;
    down)   check_ssh; remote "cd ~/$REMOTE_DIR && docker compose stop" ;;
    *)      die "unknown command '$CMD' (up | tunnel | status | down)" ;;
esac
