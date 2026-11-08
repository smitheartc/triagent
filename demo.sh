#!/usr/bin/env bash
# demo.sh: one incident live, end to end, on this machine.
#
#   ./demo.sh                  pdu_overload (rack loses power)
#   ./demo.sh bad_optic        any scenario from: python3 sim/dcsim.py --list-scenarios
#   ./demo.sh stop             stop the server, jira_mock and the simulator started by an earlier run
#
# What it does:
#   1. checks Python, Docker and server/.env (an NVIDIA key must be set); makes .venv on first run
#   2. starts Elasticsearch (escluster/docker-compose.yml) if nothing answers on $ES_URL
#   3. resets the "demo" namespace only (other namespaces such as dal1 are left alone) and applies the templates
#   4. starts the server (:8080) and jira_mock (:8082, watching only the demo namespace's tickets)
#   5. streams the scenario at 60x: 5 simulated hours, failure at 4h15m (255m), so the ticket lands about 4 minutes in
#   6. follows the ticket: webhook -> investigation -> comment on the mock issue, then prints the comment and the
#      job's cost and latency line. The services keep running for the app (EXPO_PUBLIC_API_URL=http://<this ip>:8080)
#
# Logs and the answer key go to t/demo/. Environment overrides: ES_URL, PORT, MOCK_PORT, DEMO_NAMESPACE, DEMO_SEED,
# DEMO_SPEED, DEMO_TIMEOUT (seconds to wait for the comment, default 1200).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
RUN="$ROOT/t/demo"
PY="$ROOT/.venv/bin/python"
ES_URL=${ES_URL:-http://localhost:9200}
PORT=${PORT:-8080}
MOCK_PORT=${MOCK_PORT:-8082}
NS=${DEMO_NAMESPACE:-demo}
SEED=${DEMO_SEED:-7}
SPEED=${DEMO_SPEED:-60}
TIMEOUT=${DEMO_TIMEOUT:-1200}
SCENARIO=${1:-pdu_overload}

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mdemo.sh: %s\033[0m\n' "$*" >&2; exit 1; }
up()   { curl -fsS -m 3 "$1" >/dev/null 2>&1; }

stop_all() {
    local name pid
    for name in dcsim jira_mock server; do
        [[ -f "$RUN/$name.pid" ]] || continue
        pid=$(cat "$RUN/$name.pid")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            echo "stopped $name (pid $pid)"
        fi
        rm -f "$RUN/$name.pid"
    done
}

if [[ "$SCENARIO" == "stop" ]]; then
    stop_all
    exit 0
fi

# ---------------------------------------------------------------------------- 1. prerequisites
say "checking prerequisites"
command -v python3 >/dev/null || die "python3 not found (3.10 or newer)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 is $(python3 -V 2>&1); need 3.10 or newer"
command -v curl >/dev/null || die "curl not found"
python3 sim/dcsim.py --list-scenarios | awk '{print $1}' | grep -qx "$SCENARIO" \
    || die "unknown scenario '$SCENARIO'; choose one of: $(python3 sim/dcsim.py --list-scenarios | awk '{print $1}' | xargs)"
[[ -f server/.env ]] || die "server/.env missing: cp server/.env.example server/.env and set NVIDIA_API_KEY"
grep -Eq '^NVIDIA_API_KEY(_INVESTIGATOR)?=[^[:space:]#]+' server/.env \
    || die "server/.env has no NVIDIA key: set NVIDIA_API_KEY=nvapi-... (from build.nvidia.com)"

if ! [[ -x "$PY" ]] || ! "$PY" -c 'import fastapi, uvicorn, openai, httpx, dotenv' 2>/dev/null; then
    say "installing server dependencies into .venv (first run only)"
    rm -rf .venv
    if command -v uv >/dev/null; then            # faster, and works where the system python has no venv module
        uv venv -q --python '>=3.10' .venv && uv pip install -q --python "$PY" -r server/requirements.txt \
            || die "uv could not build .venv"
    else
        python3 -m venv .venv || die "python3 -m venv failed: sudo apt install python3-venv (Debian/Ubuntu), or install uv"
        "$PY" -m pip install -q --upgrade pip
        "$PY" -m pip install -q -r server/requirements.txt
    fi
fi
mkdir -p "$RUN"

# ---------------------------------------------------------------------------- 2. Elasticsearch
if up "$ES_URL"; then
    say "Elasticsearch answers on $ES_URL"
else
    command -v docker >/dev/null || die "nothing answers on $ES_URL and docker is not installed"
    if [[ "$(uname -s)" == "Linux" ]] && (( $(sysctl -n vm.max_map_count 2>/dev/null || echo 262144) < 262144 )); then
        die "Elasticsearch needs vm.max_map_count >= 262144: sudo sysctl -w vm.max_map_count=262144"
    fi
    say "starting Elasticsearch in Docker (the first pull is about 700 MB)"
    docker compose -f escluster/docker-compose.yml up -d elasticsearch
    for _ in $(seq 1 90); do
        curl -fsS -m 3 "$ES_URL/_cluster/health" 2>/dev/null | grep -qE '"status":"(green|yellow)"' && break
        sleep 2
    done
    up "$ES_URL/_cluster/health" || die "Elasticsearch did not come up; see: docker logs triagent-es"
fi

# ---------------------------------------------------------------------------- 3. reset the demo namespace
stop_all >/dev/null
say "resetting namespace '$NS' and applying templates"
python3 escluster/es_setup.py --es-url "$ES_URL" --namespace "$NS" --reset --verify >"$RUN/es_setup.log" 2>&1 \
    || { tail -5 "$RUN/es_setup.log"; die "es_setup failed (full log: t/demo/es_setup.log)"; }

# ---------------------------------------------------------------------------- 4. server and jira_mock
up "http://localhost:$PORT/health" && die "something already answers on :$PORT; stop it or run with PORT=..."
up "http://localhost:$MOCK_PORT/health" && die "something already answers on :$MOCK_PORT; stop it or run with MOCK_PORT=..."

say "starting the server on :$PORT"
(cd server && ES_URL="$ES_URL" TICKET_BACKEND=mock JIRA_MOCK_URL="http://localhost:$MOCK_PORT" \
    nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" >"$RUN/server.log" 2>&1 & echo $! >"$RUN/server.pid")
for _ in $(seq 1 30); do up "http://localhost:$PORT/health" && break; sleep 1; done
up "http://localhost:$PORT/health" || { tail -20 "$RUN/server.log"; die "server did not start (t/demo/server.log)"; }

say "starting jira_mock on :$MOCK_PORT (tickets from namespace $NS)"
nohup "$PY" server/jira_mock.py --es-url "$ES_URL" --webhook "http://localhost:$PORT/webhook" --port "$MOCK_PORT" \
    --index "logs-incident.tickets-$NS" >"$RUN/jira_mock.log" 2>&1 & echo $! >"$RUN/jira_mock.pid"
for _ in $(seq 1 15); do up "http://localhost:$MOCK_PORT/health" && break; sleep 1; done
up "http://localhost:$MOCK_PORT/health" || { tail -20 "$RUN/jira_mock.log"; die "jira_mock did not start"; }

# ---------------------------------------------------------------------------- 5. stream the scenario
say "streaming $SCENARIO at ${SPEED}x (seed $SEED): the ticket should land in about $(( 15300 / ${SPEED%.*} / 60 )) minutes"
nohup python3 sim/dcsim.py --scenario "$SCENARIO" --seed "$SEED" --namespace "$NS" --duration 300m --fail-at 255m \
    --stream --speed "$SPEED" --es-url "$ES_URL" --out '' \
    --answer-key "$RUN/scenario.json" --catalog "$RUN/event_catalog.json" >"$RUN/dcsim.log" 2>&1 &
echo $! >"$RUN/dcsim.pid"

# ---------------------------------------------------------------------------- 6. follow the ticket
"$PY" - "$PORT" "$MOCK_PORT" "$TIMEOUT" "$RUN/dcsim.pid" <<'PY'
import json, os, sys, time, urllib.request

port, mock_port, timeout, dcsim_pid = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())
def alive(pidfile):
    try:
        os.kill(int(open(pidfile).read()), 0)
        return True
    except (OSError, ValueError):
        return False

t0, last, issue, job_id = time.time(), None, None, None
while time.time() - t0 < timeout:
    time.sleep(3)
    try:
        issues = get(f"http://localhost:{mock_port}/issues")
    except OSError:
        continue
    if not issues:
        if not alive(dcsim_pid):
            sys.exit("the simulator exited without a ticket; see t/demo/dcsim.log")
        msg = "simulator streaming, waiting for the ticket"
    else:
        issue = issues[0]
        wh = issue["webhook"]
        job_id = wh.get("job_id") or job_id
        status = ""
        if job_id:
            try:
                status = get(f"http://localhost:{port}/jobs/{job_id}")["status"]
            except OSError:
                pass
        msg = f"{issue['id']} [{issue['priority']}] {issue['title']} | webhook {wh['state']}" + \
              (f" | job {job_id} {status}" if job_id else "")
        if issue["comments"]:
            break
    if msg != last:
        print(f"  {time.strftime('%H:%M:%S')}  {msg}", flush=True)
        last = msg
else:
    sys.exit(f"no comment after {timeout:.0f}s; logs in t/demo/ (server.log, jira_mock.log, dcsim.log)")

full = get(f"http://localhost:{mock_port}/issue/{issue['id']}")
job = get(f"http://localhost:{port}/jobs/{job_id}")
cost = next((e["message"] for e in reversed(job["trace"]) if (e.get("message") or "").startswith("cost and latency")), "")
print(f"\n\033[1m==> comment on mock issue {issue['id']} after {time.time() - t0:.0f}s\033[0m\n")
print(full["comments"][-1]["body"])
print(f"\n\033[1m==> {cost}\033[0m")
print(f"\nJob JSON:     curl -s localhost:{port}/jobs/{job_id}")
print(f"Mock issue:   curl -s localhost:{mock_port}/issue/{issue['id']}")
PY
echo
echo "Still running: server :$PORT, jira_mock :$MOCK_PORT, simulator (finishes on its own). Stop all: ./demo.sh stop"
