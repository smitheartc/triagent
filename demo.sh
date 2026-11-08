#!/usr/bin/env bash
# demo.sh: incidents live, end to end, on this machine.
#
#   ./demo.sh                          three incidents in one stream: pdu_overload,bad_optic,memory_leak
#                                      (a rack loses power, an uplink optic degrades, a release leaks memory)
#   ./demo.sh bad_optic                one incident: any scenario from python3 sim/dcsim.py --list-scenarios
#   ./demo.sh cooling_failure,disk_fill   any comma list (the server investigates up to 3 at once)
#   ./demo.sh stop                     stop the server, jira_mock and the simulator started by an earlier run
#
# What it does:
#   1. checks Python, Docker and server/.env (an NVIDIA key must be set); makes .venv on first run
#   2. starts Elasticsearch (escluster/docker-compose.yml) if nothing answers on $ES_URL
#   3. resets the "demo" namespace only (other namespaces such as dal1 are left alone) and applies the templates
#   4. starts the server (:8080) and jira_mock (:8082, watching only the demo namespace's tickets); the mock's
#      Jira-style board is http://localhost:8082/
#   5. streams the scenarios at 60x in one simulated data center: 5 simulated hours. One scenario fails at 4h15m
#      (ticket about 4 minutes in); with several, dcsim spreads the failures between ~45% and 88% of the window
#      (three: tickets about 3, 3.7 and 4.4 minutes in)
#   6. follows every ticket: webhook -> investigation -> comment on the mock issue, printing each comment and its job's
#      cost and latency line as it lands. Each finished ticket also sends a phone push (server/notify.py).
#      The services keep running for the app
#
# Logs and the answer key go to t/demo/. Environment overrides: ES_URL, PORT, MOCK_PORT, DEMO_NAMESPACE, DEMO_SEED,
# DEMO_SPEED, DEMO_TIMEOUT (seconds to wait for every ticket's comment, default 1200).
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
SCENARIO=${1:-pdu_overload,bad_optic,memory_leak}

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mdemo.sh: %s\033[0m\n' "$*" >&2; exit 1; }
up()   { curl -fsS -m 3 "$1" >/dev/null 2>&1; }

kill_tree() {   # the process and everything it started (uvicorn can sit one level below the recorded pid)
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$child"; done
    kill "$1" 2>/dev/null || true
}

stop_all() {
    local name pid
    for name in dcsim jira_mock server; do
        [[ -f "$RUN/$name.pid" ]] || continue
        pid=$(cat "$RUN/$name.pid")
        if kill -0 "$pid" 2>/dev/null; then
            kill_tree "$pid"
            echo "stopped $name (pid $pid)"
        fi
        rm -f "$RUN/$name.pid"
    done
    for _ in $(seq 1 20); do      # wait until the ports are free again
        up "http://localhost:$PORT/health" || up "http://localhost:$MOCK_PORT/health" || return 0
        sleep 0.5
    done
    echo "warning: something still answers on :$PORT or :$MOCK_PORT" >&2
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
KNOWN=$(python3 sim/dcsim.py --list-scenarios | awk '{print $1}')
IFS=, read -ra SCENARIOS <<<"$SCENARIO"
(( ${#SCENARIOS[@]} > 0 )) || die "no scenario given"
for sc in "${SCENARIOS[@]}"; do
    grep -qx "$sc" <<<"$KNOWN" || die "unknown scenario '$sc'; choose from: $(xargs <<<"$KNOWN")"
done
N=${#SCENARIOS[@]}
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
# exec: the subshell becomes uvicorn, so $! is the pid that holds the port
(cd server && ES_URL="$ES_URL" TICKET_BACKEND=mock JIRA_MOCK_URL="http://localhost:$MOCK_PORT" \
    exec nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" >"$RUN/server.log" 2>&1) &
echo $! >"$RUN/server.pid"
for _ in $(seq 1 30); do up "http://localhost:$PORT/health" && break; sleep 1; done
up "http://localhost:$PORT/health" || { tail -20 "$RUN/server.log"; die "server did not start (t/demo/server.log)"; }

say "starting jira_mock on :$MOCK_PORT (tickets from namespace $NS)"
nohup "$PY" server/jira_mock.py --es-url "$ES_URL" --webhook "http://localhost:$PORT/webhook" --port "$MOCK_PORT" --host 0.0.0.0 \
    --index "logs-incident.tickets-$NS" >"$RUN/jira_mock.log" 2>&1 & echo $! >"$RUN/jira_mock.pid"
for _ in $(seq 1 15); do up "http://localhost:$MOCK_PORT/health" && break; sleep 1; done
up "http://localhost:$MOCK_PORT/health" || { tail -20 "$RUN/jira_mock.log"; die "jira_mock did not start"; }
say "triage board: http://localhost:$MOCK_PORT/  (open it now to watch the ticket move)"

# ---------------------------------------------------------------------------- 5. stream the scenario
if (( N == 1 )); then
    FAIL_AT=(--fail-at 255m); FIRST=255                       # minutes into the 300m window
else
    FAIL_AT=(); FIRST=$(( 300 * (4500 + 4300 / N) / 10000 ))  # dcsim: the earliest failure is at 0.45 + 0.43/N of the window
fi
say "streaming $N incident(s) at ${SPEED}x (seed $SEED): $SCENARIO; the first ticket should land in about $(( (FIRST + 3) * 60 / ${SPEED%.*} / 60 )) minutes"
nohup python3 sim/dcsim.py --scenario "$SCENARIO" --seed "$SEED" --namespace "$NS" --duration 300m ${FAIL_AT[@]+"${FAIL_AT[@]}"} \
    --stream --speed "$SPEED" --es-url "$ES_URL" --out '' \
    --answer-key "$RUN/scenario.json" --catalog "$RUN/event_catalog.json" >"$RUN/dcsim.log" 2>&1 &
echo $! >"$RUN/dcsim.pid"

# ---------------------------------------------------------------------------- 6. follow the tickets
"$PY" - "$PORT" "$MOCK_PORT" "$TIMEOUT" "$RUN/dcsim.pid" "$N" <<'PY'
import json, os, sys, time, urllib.request

port, mock_port, timeout, dcsim_pid, want = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4], int(sys.argv[5])
def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())
def alive(pidfile):
    try:
        os.kill(int(open(pidfile).read()), 0)
        return True
    except (OSError, ValueError):
        return False

t0, last, done, jobs = time.time(), {}, {}, {}
while len(done) < want and time.time() - t0 < timeout:
    time.sleep(3)
    try:
        issues = sorted(get(f"http://localhost:{mock_port}/issues"), key=lambda i: i["created"])
    except OSError:
        continue
    if len(issues) < want and not alive(dcsim_pid):
        if not issues:
            sys.exit("the simulator exited without a ticket; see t/demo/dcsim.log")
        print(f"  the simulator finished with {len(issues)} of {want} tickets", flush=True)
        want = len(issues)
    if not issues and last.get(None) is None:
        print(f"  {time.strftime('%H:%M:%S')}  simulator streaming, waiting for the first ticket", flush=True)
        last[None] = True
    for issue in issues:
        iid, wh = issue["id"], issue["webhook"]
        if iid in done:
            continue
        job_id = jobs[iid] = wh.get("job_id") or jobs.get(iid)
        status = ""
        if job_id:
            try:
                status = get(f"http://localhost:{port}/jobs/{job_id}")["status"]
            except OSError:
                pass
        msg = f"{iid} [{issue['priority']}] {issue['title']} | webhook {wh['state']}" + \
              (f" | job {job_id} {status}" if job_id else "")
        if msg != last.get(iid):
            print(f"  {time.strftime('%H:%M:%S')}  {msg}", flush=True)
            last[iid] = msg
        if issue["comments"]:
            full = get(f"http://localhost:{mock_port}/issue/{iid}")
            job = get(f"http://localhost:{port}/jobs/{job_id}")
            cost = next((e["message"] for e in reversed(job["trace"])
                         if (e.get("message") or "").startswith("cost and latency")), "")
            print(f"\n\033[1m==> comment on mock issue {iid} ({len(done) + 1} of {want}) after {time.time() - t0:.0f}s\033[0m\n")
            print(full["comments"][-1]["body"])
            print(f"\n\033[1m==> {cost}\033[0m\n", flush=True)
            done[iid] = job_id
if len(done) < want:
    sys.exit(f"{len(done)} of {want} tickets commented after {timeout:.0f}s; logs in t/demo/ (server.log, jira_mock.log, dcsim.log)")

print(f"\033[1m==> all {want} tickets triaged in {time.time() - t0:.0f}s\033[0m")
for iid, job_id in done.items():
    print(f"  {iid}   job: curl -s localhost:{port}/jobs/{job_id}   board: http://localhost:{mock_port}/#{iid}")
PY
echo
echo "Still running: server :$PORT, jira_mock :$MOCK_PORT, simulator (finishes on its own). Stop all: ./demo.sh stop"
