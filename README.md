# Triagent

An incident ticket arrives with symptoms only. Triagent's agent (NVIDIA Nemotron) investigates an Elasticsearch
cluster of data center logs and metrics, finds the root cause with cited evidence, and writes the problem, the cause
and the remediation back to the ticket and to a phone app.

The data comes from `sim/dcsim.py`, a deterministic simulator that plants failures with real precursors, red
herrings and symptom-only tickets, plus an answer key the scorer grades the agent against.

## Quickstart: a live incident to a ticket comment (about 10–15 minutes)

You need:
- **Docker** (Docker Desktop on macOS/Windows, give it 4 GB of memory or more; Docker Engine on Linux)
- **Python 3.10+**, `git`, `curl`. Python's venv module too (Ubuntu/WSL: `sudo apt install python3-venv`), or
  [uv](https://docs.astral.sh/uv/) instead, which `demo.sh` prefers when it is installed (faster installs)
- **An NVIDIA API key**: sign in at [build.nvidia.com](https://build.nvidia.com), open any Nemotron model, click
  *Get API Key*. It starts with `nvapi-`.
- Windows: run everything inside WSL2 (Ubuntu), not PowerShell.
- Linux and WSL2: Elasticsearch needs `sudo sysctl -w vm.max_map_count=262144` once per boot.

```bash
git clone https://github.com/smitheartc/triagent.git && cd triagent
cp server/.env.example server/.env
nano server/.env      # set NVIDIA_API_KEY=nvapi-... (one key is enough; the per-role keys fall back to it)
./demo.sh
```

`./demo.sh` (or `make demo`) does the rest:

| step | what happens | time on a fresh laptop |
|---|---|---|
| 1 | checks Python, Docker and the key; installs the server's packages into `.venv` | ~1 min |
| 2 | starts Elasticsearch in Docker (first pull ~700 MB) | 2–5 min |
| 3 | resets the `demo` namespace, starts the server (:8080) and the mock Jira (:8082) | seconds |
| 4 | streams a rack power failure at 60x: 5 simulated hours, the ticket lands ~4 min in | ~4 min |
| 5 | the mock Jira raises the webhook, the agent investigates, a reviewer model checks it, remediation is written | 2–4 min |
| 6 | prints the comment posted on the mock Jira issue and the job's cost and latency line | |

You're done when the terminal prints `==> comment on mock issue INC...` followed by the report. The services stay
up afterwards; `./demo.sh stop` stops them. Run another failure with `./demo.sh bad_optic` (list:
`python3 sim/dcsim.py --list-scenarios`).

While it runs:
- `curl -s localhost:8082/issues`: the mock Jira issues and their webhook state
- `curl -s localhost:8080/jobs`: investigations; `curl -s localhost:8080/jobs/<job_id>` is the full job: report,
  critic review, remediation, and the trace of every tool and model call. Its last `info` entry is the cost and
  latency line, for example `141,932 tokens (120,113 in, 21,819 out) over 18 model calls and 15 tool calls; 3m12s
  end to end, first report at 1m40s, 2m05s waiting on the model`
- `tail -f t/demo/server.log`: the same, as it happens

## More

Shortcuts in the `Makefile` (`sudo apt install make` if it is missing; `make help` lists them):

| command | what it does |
|---|---|
| `make score` | the accuracy table: every scenario through the pipeline against the answer key (`RUNS=1` for a quick pass, ~30 min at `PARALLEL=2`; loads seed 7 into namespace `dal1` the first time; `INGEST=1` reloads it, needed on a new or reset cluster). Writes `t/scores/<time>.md` |
| `make es` | Elasticsearch **and Kibana** (:5601) locally; from WSL without Docker integration, `make es DOCKER=docker.exe` |
| `make kibana` | two Kibana dashboards for judges: the whole simulated data center, and one incident start to finish. Needs the `dal1` data `make score` loads |
| `make test` | server unit tests, no network |
| `make server` / `make fake-server` | the real server on :8080 with auto-reload, or the scripted one for app work (no Elasticsearch or NVIDIA key) |
| `make remote HOST=<brev>` | Elasticsearch + Kibana on a Brev instance, tunnelled to localhost (see below); `make tunnel` / `make remote-status` later |
| `make tools-check` | every Elasticsearch tool against every ticket in the cluster, no model calls (run this first when the agent looks wrong) |

The phone app (Expo) is in `app/`; see `app/README.md`. Point it at `http://<this machine's LAN IP>:8080`.

How it fits together, the rules for changing it, and the build status are in `CLAUDE.md`. The simulator's rules are
in `sim/CLAUDE.md`.

## Elasticsearch on a remote machine (NVIDIA Brev)

The cluster can run on a Brev instance (or any SSH host with Docker) instead of this laptop. An SSH tunnel brings it
back to `localhost:9200` and `localhost:5601`, so nothing else changes: same `server/.env`, same `./demo.sh`, same
scorer. The remote ports are bound to 127.0.0.1 there, so the security-off cluster is never on the internet.

```bash
brev login && brev refresh          # once: Brev CLI, adds your instances to ~/.ssh/config (brev ls shows names)
docker compose -f escluster/docker-compose.yml stop    # free local 9200/5601 if a local cluster runs (data kept)
escluster/remote.sh up <instance>   # copy the compose file, start ES + Kibana there, open the tunnel (leave it open)
```

In a second terminal, load data through the tunnel the first time: `./demo.sh` for the demo namespace, and
`python3 server/scorer.py --ingest --runs 1` for the scored `dal1` data (then `make kibana`). Later sessions only need
`escluster/remote.sh tunnel <instance>`. `status` and `down` do what they say. Elasticsearch needs no GPU, so a CPU
instance is enough if Brev offers one; every tool call crosses the tunnel, so investigations run a little slower.

## Troubleshooting

- **`server/.env has no NVIDIA key`**: set `NVIDIA_API_KEY=nvapi-...` in `server/.env` (no quotes).
- **Elasticsearch never comes up**: `docker logs triagent-es`. Usually `vm.max_map_count` (Linux/WSL2, see above) or
  Docker has less than 4 GB of memory.
- **`something already answers on :8080`**: an earlier server is still running. `./demo.sh stop`, or stop it, or run
  `PORT=8090 ./demo.sh` (the mock Jira port is `MOCK_PORT`).
- **The job stays `running` for many minutes**: NVIDIA rate limits (HTTP 429). The client retries with back-off; check
  `t/demo/server.log`. Fewer parallel jobs (`TRIAGENT_MAX_PARALLEL_JOBS=1` in `server/.env`) helps.
- **`something already answers on localhost:9200`** from `remote.sh`: a local cluster is still running; stop it
  (`docker compose -f escluster/docker-compose.yml stop`).
- **Ticket never appears**: `t/demo/dcsim.log` (simulator) and `t/demo/jira_mock.log`.
