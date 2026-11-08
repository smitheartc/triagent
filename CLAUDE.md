# CLAUDE.md — Triagent

## What this project is

Triagent is a hackathon project (NVIDIA challenge track). When an incident ticket arrives with symptoms only, an agent investigates an Elasticsearch cluster of data center logs and metrics, finds the root cause, and hands the on-call engineer the problem, the cause with evidence, and remediation steps. Results go back to the ticket and to a phone app.

The data comes from `dcsim.py`, a deterministic simulator that seeds failure scenarios with telegraphed precursors, red herrings, symptom-only tickets, and an answer key (`scenario.json`). That answer key is what lets us score the agent instead of demoing it once and hoping.

Four things must stay true across every change:
1. The ticket never carries the cause. The agent has to find it.
2. Every claim in a report cites `event.id` values that exist in the cluster.
3. The triage agent and the checker run on Nemotron models (NVIDIA challenge rule, see below).
4. Accuracy is measured, not assumed. The scorer runs against all scenarios before anything is called working.

## NVIDIA challenge rule: Nemotron only for the reasoning path

The investigator and the critic MUST be Nemotron-family models served through NVIDIA's hosted API. We have credits for this; using any other provider for these two roles disqualifies the entry.

- Base URL: `https://integrate.api.nvidia.com/v1` (OpenAI-compatible chat completions). Keys from build.nvidia.com, one per model as the organizers issue them: `NVIDIA_API_KEY_INVESTIGATOR`, `NVIDIA_API_KEY_CRITIC`, `NVIDIA_API_KEY_SMALL`, each falling back to `NVIDIA_API_KEY`.
- Model ids (hosted catalog names; confirm the exact string with one smoke call before trusting it in code):
  - `nvidia/nemotron-3.5-super-vl-120b-a12b` for the investigator (tool use, long context). The older `nvidia/nemotron-3-super-120b-a12b` id is not served to our account.
  - `nvidia/nemotron-3.5-lightning-30b-a3b` for the critic, remediation writer, and the push-notification summarizer (cheap, fast). `nvidia/nemotron-3-nano-30b-a3b` is retired (410).
  - `nvidia/nemotron-3-ultra-550b-a55b` only if Super fails the scorer and we have credit to spare
- Reasoning is on by default. Toggle per call with `extra_body={"chat_template_kwargs": {"enable_thinking": True|False}}`. Investigator: on. Critic: on. Summarizer: off.
- NVIDIA recommends `temperature=1.0, top_p=0.95`. For the scorer, pin `seed` where the API accepts it and run each scenario 3 times.
- All model calls go through `server/llm.py`. Nothing else imports the OpenAI client. That file reads `NVIDIA_BASE_URL`, `NVIDIA_API_KEY_<ROLE>` / `NVIDIA_API_KEY`, `TRIAGENT_MODEL_INVESTIGATOR`, `TRIAGENT_MODEL_CRITIC`, `TRIAGENT_MODEL_SMALL` from the environment. Swapping a model is an env change, never a code change.
- Tool calling: send tools in the OpenAI `tools=[...]` format. If a model returns the tool call as text instead of a structured `tool_calls` entry, `llm.py` parses the first JSON object out of the text and treats it as the call. Keep that fallback; do not remove it when it looks unused.
- Log every request and response (model, tokens, latency, tool name) to the investigation trace. Credits are finite and judges ask for cost per investigation.

Claude (the coding assistant) is fine for writing code and docs. It is not allowed in the runtime triage path.

## Repo layout

```
triagent/
  CLAUDE.md              this file
  escluster/
    docker-compose.yml   single-node Elasticsearch 8.17 + Kibana for every teammate
    es_setup.py          ILM + component/index templates + Kibana data views, idempotent, stdlib only
  sim/
    dcsim.py             simulator (rules in sim/CLAUDE.md, read it before touching the simulator)
    CLAUDE.md            simulator rules; authoritative for everything under sim/
  runbooks/              one markdown file per cause category (see Remediation)
  server/                FastAPI orchestrator (Python 3.11)
    contracts.py         every shape that crosses a section boundary (Ticket, Report, Job, TraceEntry, HTTP API)
    main.py              webhook in, SSE out, job queue, ticket patch, push; create_app() builds the app
    fake_server.py       main.py's routes with scripted investigations; no ES or NVIDIA (build the app on this)
    fakes.py             FakeTools / fake_chat for backend sections; scripted jobs for fake_server
    tracelog.py          the investigation trace (seq, phase, totals)
    llm.py               the only module that talks to Nemotron
    tools.py             ES tool implementations and their JSON schemas
    topology.py          entity -> neighbors, built from labels.* aggregations (no dcsim import)
    investigator.py      tool-use loop, max 20 calls
    critic.py            no-tools review of a report against its cited docs
    remediate.py         runbook-grounded remediation
    scorer.py            report vs scenario.json (stdlib only)
    tools_check.py       no-LLM harness: every tool against every ticket in the cluster
    remediate_check.py   every category's remediation names the entity type and a command or console action
    jira_mock.py         watches ES for ticket docs, raises webhooks, accepts comment patches (stdlib only)
    templates/           ticket_comment.md / ticket_comment_failed.md, filled by main.patch_ticket()
    store.py             Supabase upsert of each job on every status change, devices table, reload at startup
    notify.py            Expo push when a webhook job finishes (prompts/summarize.md on the small model)
    prompts/             one .md per prompt, loaded at startup, versioned in git
  app/                   Expo (React Native + web) client
  t/                     scratch output, gitignored
```

## Running it

```bash
# cluster
docker compose up -d                                  # ES :9200, Kibana :5601, security off, local only
python3 sim/es_setup.py --es-url http://localhost:9200 --kibana-url http://localhost:5601 --verify
python3 sim/dcsim.py --scenario all --seed 7 --duration 12h --es-url http://localhost:9200 \
    --answer-key t/scenario.json --catalog t/event_catalog.json --out ''

# server
cd server && pip install -r requirements.txt
cp .env.example .env                                  # fill NVIDIA_API_KEY_*, ES_URL, SUPABASE_*
uvicorn main:app --reload --port 8080
python3 jira_mock.py --es-url http://localhost:9200 --webhook http://localhost:8080/webhook
curl -s localhost:8082/issues                          # mock issues; GET /issue/<INC id> shows the comment

# one investigation by hand
curl -XPOST localhost:8080/investigate -d @t/ticket.json

# score
python3 server/scorer.py --answer-key t/scenario.json --runs 3

# live demo (failure unfolds in real time, 60x)
python3 sim/dcsim.py --scenario bad_optic --stream --speed 60 --es-url http://localhost:9200

# app
cd app && npx expo start
```

Reset between runs: `python3 sim/es_setup.py --es-url http://localhost:9200 --reset` then re-ingest. On WSL2 set `vm.max_map_count=262144` first.

## Pipeline

```
ticket doc in ES  ->  jira_mock (or real Jira/SNOW webhook)  ->  POST /webhook
  -> orchestrator enqueues job, streams steps over SSE
  -> investigator (Nemotron Super + ES tools, <= 20 calls)  ->  report JSON
  -> critic (Nemotron Nano, no tools, re-fetches cited docs)  ->  pass | fail + corrections (one retry)
  -> remediate (Nemotron Nano + runbooks/<category>.md)
  -> store report (Supabase), patch ticket comment, push notification
scorer: report vs scenario.json -> accuracy table
```

## Contracts (do not break)

`server/contracts.py` is the single source of every shape that crosses a section boundary: the ticket, the report,
the Job JSON (`GET /jobs/{id}` and `t/jobs/<id>.json`), trace entries, critic and remediation output, the HTTP API
and SSE framing, the Supabase jobs table. Code against it; never redefine a shape locally. Changing it is its own
commit titled `contracts: ...`, and `server/tests/test_contracts.py` must pass (it checks that the real pipeline and
the fake server both produce exactly these shapes). The summaries below are the parts every prompt and tool depends on.

**contracts.py is frozen by default. Do not edit it, not even to add a model or reword a docstring, without asking the
user first.** Other sections (the Expo app, store.py, notify.py) are built in parallel against it, often by another
agent at the same moment, and they generate types from its schema export (`APP_MODELS`). An edit there changes their
ground truth while they work. Work around it instead:
- A shape that only travels between two server modules and never reaches the app (for example jira_mock's comment
  body) lives in the module that serves it, defined once there and imported by the caller. Fold it into contracts.py
  later, as its own `contracts: ...` commit, once the user agrees.
- Never add to `APP_MODELS` a shape the app does not read.
- If a change in behavior makes a contracts docstring stale (for example dedupe rules), say so in your summary and
  leave the docstring alone. Don't "fix" it in passing.

**Webhook payload** (`POST /webhook`, what jira_mock and real Jira both send):
```json
{"ticket_id": "INC1234567", "title": "...", "description": "...", "priority": "P1",
 "created": "2026-11-07T14:03:00Z", "service": "checkout-api", "host": null,
 "labels": {"row": "C", "rack": "C04", "cluster": "k8s-prod-2"}, "namespace": "dal1",
 "source_event_id": "<event.id of the INC-CREATED doc>"}
```

**Report** (output of `submit_report`, stored verbatim):
```
problem: str
root_cause: {summary, category, entity, entity_type, identifiers{}, event_ids[]}
causal_chain: [{timestamp, event_id, what, why_it_matters}]
rejected_candidates: [{what, event_ids[], why_rejected}]
affected: {}
confidence: 0..1
open_questions: []
```
`category` is one of `cooling | power | network | storage | memory | deploy | config | certificate | security | unknown`. Runbooks are keyed by this list; adding a category means adding a runbook.

**Investigation trace**: every tool call, its arguments, a truncated result, and every `note_finding`, with timestamps and token counts. Stored next to the report. The app streams it live; the scorer reads tool counts from it.

**Tools** (names are part of the prompts; rename everywhere or not at all): `search_events`, `aggregate`, `metric_series`, `find_changes`, `expand_scope`, `get_doc`, `note_finding`, `submit_report`.
- Results are compact rows (ts, event.id, event.code, event.action, log.level, host.name, service.name, observer.name, user.name, labels, message trimmed to 200 chars). Never return raw `_source`.
- `search_events` caps `size` at 50. `aggregate` caps `top` at 30. `metric_series` downsamples to at most 60 points. These caps protect the context window; do not raise them to make one scenario pass.
- `find_changes` queries `logs-change.*`, `logs-cicd.*`, `logs-security.auth-*` (sudo and logins) and `logs-security.firewall-*` (rule changes) in one call.
- `expand_scope` derives neighbors from `labels.*` terms aggregations and the naming scheme (`crac-<row>`, `pdu-<rack>-a/b`, `tor-<rack>`), never from importing dcsim.

**ES fields the agent pivots on** (keyword-mapped by `es_setup.py`): `host.name`, `labels.row/rack/cluster`, `service.name`, `observer.name`, `user.name`, `source.ip`, `change.id`, `kubernetes.pod.name`, `event.code`, `event.id`. If dcsim adds a pivot field, add it to `es_setup.py` and to the investigator prompt.

## Prompts

Prompts live in `server/prompts/` as files, not inline strings. Current set: `investigator.md`, `critic.md`, `remediate.md`, `summarize.md`, `ask.md` (follow-up questions: `POST /jobs/{id}/ask` resumes the investigator with the stored report and a trace digest, `TRIAGENT_ASK_MAX_CALLS` default 8, ending with `answer_question`). Rules:
- The investigator prompt says: ticket is symptoms only; walk backward from ticket time; widen time (15m, 1h, 3h, 6h) then scope (host, rack, row, cluster, shared appliances); pivot on shared fields; aggregate before reading raw docs; a root cause is a change, deploy, commit, login, config edit, or hardware/environmental alarm, never a metric threshold; expect red herrings and record rejections; cite only retrieved `event.id`s; 20-call budget.
- The user message includes the ticket plus the compact event catalog (code, index, action for codes with count > 0). Keep it, it is how the model knows which indices exist.
- The critic gets the report and the full text of every cited doc, nothing else. It checks: ids exist, chain is time-ordered and ends before the ticket, root cause is a concrete event, adjacent chain steps share an identifier, rejections are listed, category matches evidence, confidence is consistent.
- Remediation uses only the runbook for the report's category. If the runbook does not cover it, the output says so and gives safe triage steps.
- Never put answer-key content, scenario names, or hints about which of the nine scenarios is running into any prompt. The scorer catches this only indirectly, so review prompt diffs for it.

## Remediation runbooks

`runbooks/<category>.md`, one per category in the list above. Each has: symptoms this covers, immediate steps (imperative, name the entity type and the console or command), how to verify, how to prevent, rollback risk. They are written by hand, short, and they are the only source the remediation step may draw from. Nine categories, roughly an hour of work, and they are what makes the three steps credible.

## Scoring

`server/scorer.py` runs every scenario in `scenario.json` through the pipeline (`--runs N`, default 3) and prints a table:

| scenario | cause entity | category | chain overlap | red herring cited | confidence | tool calls | seconds | tokens |

- cause entity: report `root_cause.entity` equals answer key `root_cause.entity` (or `change_id`/`commit` match for change-driven scenarios)
- category: mapped from the answer key category prefix (`facility.cooling` -> cooling, `facility.power` -> power, `network.*` -> network, `hardware.storage` -> storage, `software.release` -> memory or deploy, `change.configuration` -> config or certificate by entity type, `security.*` -> security)
- chain overlap: Jaccard between report `causal_chain.event_id`s and answer key `causal_chain` ids that have an `event_id`
- red herring cited: any `red_herrings[].event_id` appearing in `root_cause.event_ids` is a hard fail for that run
- also run `--scenario none` tickets if any exist; the correct answer is `category: unknown` with low confidence

The accuracy table is a demo slide. Keep the scorer runnable from a clean checkout with one command. `scorer.py` is stdlib-only; it starts the server if none answers on `--server`, loads all scenarios at a fixed `--start` when the cluster is empty (`--ingest` deletes and reloads the dcsim data streams), runs `--parallel N` investigations at once, pins `?seed=` per run, and writes `t/scores/<timestamp>.md` and `.json`.

## Mock Jira

`jira_mock.py` polls `logs-incident.tickets-*` for new `INC-CREATED` docs, stores them, POSTs the webhook payload above, and exposes `POST /issue/{id}/comment` that the orchestrator calls with the report. Real Jira/SNOW integration is a config toggle (`TICKET_BACKEND=mock|none|jira|snow`, only mock and none are built) with the same payload shape. The demo runs on mock. Do not make the mock smarter than needed.

- Tickets already in ES when the mock starts are processed too (`--skip-existing` to ignore them); webhooks retry until the server answers, so start order does not matter. Issues live in memory.
- dcsim reuses ticket ids for the same seed. A new ticket doc for a known id (re-ingest, second `--stream` run) reopens the issue, and the server dedupes on `ticket_id` + `created`, so it gets a fresh investigation. An exact replay (same `--start`) is a duplicate: restart the server to re-run it.
- Only jobs that arrived on `/webhook` comment; `/investigate` (curl, scorer) never does. A failed patch is a trace `error` entry, never a failed job. The comment text is `server/templates/ticket_comment*.md`.

## Mobile app

Expo, TypeScript, one codebase for iOS, Android and web. Supabase for auth, realtime, and report storage; Expo push for notifications.

Build it against `server/fake_server.py`, which serves the real API with scripted investigations and needs no ES or
NVIDIA keys: `cd server && pip install -r requirements.txt && uvicorn fake_server:app --host 0.0.0.0 --port 8080`
(`TRIAGENT_FAKE_SPEED=5` plays faster). Generate TypeScript types from `python3 server/contracts.py --schema
app/src/contracts.schema.json` (for example with `npx json-schema-to-typescript`). Endpoints per screen: Inbox
`GET /jobs`; Incident detail `GET /jobs/{id}`; Live investigation `GET /jobs/{id}/events` (SSE); Actions
`POST /jobs/{id}/actions`; Scoreboard `GET /scores/latest`; push registration `POST /devices`. Until `store.py`
writes Supabase, read jobs from the API; the Supabase `jobs` table has the `JobSummary` columns plus the full Job
JSON (`python3 server/contracts.py --sql`). Screens, in build order: Inbox, Incident detail (problem, cause, evidence timeline with event ids, remediation, confidence), Live investigation (the trace streaming), Actions (approve remediation, escalate, ask a follow-up that re-enters the investigator with the existing findings as context), Scoreboard. Shared demo token for auth; no real login flow. The app talks only to the server's HTTP API and SSE (and to Supabase once `store.py` lands); it never talks to Elasticsearch or Nemotron directly.

## Dependencies

- `sim/`: Python 3.10+, standard library only. No exceptions (see `sim/CLAUDE.md`).
- `server/`: `fastapi`, `uvicorn`, `openai` (client only, pointed at NVIDIA), `httpx`, `python-dotenv`, `supabase`. Add nothing else without saying why in the PR.
- `app/`: Expo SDK, `@supabase/supabase-js`, `expo-notifications`. No native modules.

## Verification before calling anything done

1. `python3 sim/es_setup.py --es-url ... --verify` passes (every index name resolves to a dcsim template).
2. `python3 server/scorer.py --runs 1` completes for all nine scenarios with no exceptions, and no run cites a red herring as the cause.
3. One full path by hand: ingest, webhook from jira_mock, report stored, ticket comment present, app shows it.
4. A tool-schema smoke test: call each tool directly with a known ticket (no LLM) and check it returns rows. Do this first when something breaks; most "the agent is wrong" bugs are tool bugs.
5. For simulator changes, the checks in `sim/CLAUDE.md` (determinism, per-scenario tickets, bulk format).
6. Prompt changes: rerun the scorer before and after and paste both tables in the PR.

## Things not to do

- Don't call Anthropic, OpenAI, or any non-Nemotron model from `server/` at runtime. `llm.py` refuses a model id that doesn't start with `nvidia/nemotron` unless `TRIAGENT_ALLOW_ANY_MODEL=1` is set, and that flag is for local experiments only.
- Don't dump raw logs into a prompt. A 6h run is ~180k docs; the agent queries, it does not read.
- Don't let scenario names, answer keys, or `scenario.json` reach the server at runtime. Only the scorer reads it, after the fact.
- Don't raise tool result caps to make one scenario pass. Fix the prompt or the tool's ranking instead.
- Don't rename datasets, event codes, tool names, or report fields casually. Prompts, mappings, scorer and app all depend on them. If a rename is needed, change it everywhere in one commit and say so.
- Don't expose port 9200 with security off beyond the LAN. For the demo, teammates point at one machine or an Elastic Cloud deployment with an API key.
- Don't demo the app on the real pipeline before the scorer table is agreed. Building it in parallel against `fake_server.py` is fine. The agent is the project; the app shows it.
- Don't commit generated output (`events.ndjson`, `scenario.json`, `event_catalog.json`, traces, `.env`). Use `t/`.

## Build order and status

1. ES cluster and mappings (`docker-compose.yml`, `es_setup.py`): done
2. `tools.py` + `topology.py` with a no-LLM test harness (`tools_check.py`): done
3. `llm.py` with the Nemotron smoke test (tool calls come back structured; text fallback kept): done
4. `investigator.py` loop: done
5. `scorer.py`, run all nine: done (`--runs 3`: 20/27 before the critic)
6. `critic.py`, measure the change in the table: done (18/27 after; the retry costs more than it fixes)
7. `runbooks/` and `remediate.py`: done (`remediate_check.py` passes for all ten categories)
8. `jira_mock.py` and ticket patching: done (a fresh ingest ends with the report as a comment on the mock issue)
9. `app/` in the screen order above: built against `fake_server.py`, all five screens plus push registration (`app/README.md`); Supabase reads wait on `store.py`
10. Real Jira/SNOW toggle, only if time remains

Contracts (`server/contracts.py`), `fakes.py` and `fake_server.py` exist so steps 7 to 9 can be built in parallel.
