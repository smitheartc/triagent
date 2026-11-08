# Triagent app

Expo client (iOS, Android, web from one codebase) for the on-call engineer: the incident inbox, the agent's report,
its investigation streaming live, actions on the incident, and the scorer's accuracy table. It talks only to the
server's HTTP API and SSE (see `server/contracts.py`); it never talks to Elasticsearch or Nemotron.

## Run it

```bash
# 1. the API: fake_server.py serves the real routes with scripted investigations (no ES, no NVIDIA keys)
cd server && pip install -r requirements.txt
TRIAGENT_FAKE_SPEED=5 uvicorn fake_server:app --host 0.0.0.0 --port 8080   # --host so a phone can reach it

# 2. the app
cd app && npm install
npx expo start          # press w for web, or scan the QR code with Expo Go on a phone on the same Wi-Fi
```

The app calls port 8080 on the machine that serves the JS bundle (your laptop, for Expo Go and the browser). For a
server anywhere else, set `EXPO_PUBLIC_API_URL=http://host:port` before `npx expo start`. A phone needs the laptop's
firewall to allow inbound 8080. The same commands work against the real pipeline (`uvicorn main:app`).

## Screens and endpoints

| screen | route | endpoint |
|---|---|---|
| Inbox (polls every 3 s) | `/` | `GET /jobs` |
| Incident detail (polls until done/failed) | `/jobs/[id]` | `GET /jobs/{id}` |
| Live investigation | `/jobs/[id]/live` | `GET /jobs/{id}/events` (SSE, read with `expo/fetch` streaming) |
| Actions: approve, escalate, ask | `/jobs/[id]/actions` | `POST /jobs/{id}/actions`; ask: `POST /jobs/{id}/ask`, answer polled from `GET /jobs/{id}` |
| Scoreboard | `/scores` | `GET /scores/latest` |
| push registration (at launch) | – | `POST /devices` |

Routes live in `src/app/` (Expo Router). `src/api.ts` is the HTTP client, `src/sse.ts` the trace stream (replay,
tail, reconnect, dedupe by `seq`), `src/push.ts` the push setup.

## Contracts

`src/contracts.ts` is generated from the server's pydantic models; never edit it by hand. After a `contracts: ...`
commit on the server, regenerate (needs the server's Python deps):

```bash
npm run contracts                          # PYTHON=../server/.venv/bin/python npm run contracts to pick one
```

## Checks

```bash
npm run typecheck && npm run lint
```

## Push notifications

The app registers its Expo push token with `POST /devices` at launch, and tapping a notification opens Incident
detail for `data.job_id`. Registration is best effort and is skipped until these exist:

- an EAS project id in `app.json` (`npx eas-cli@latest init` adds `extra.eas.projectId`)
- a development build on Android (Expo Go dropped remote push on Android in SDK 53); iOS Expo Go works
- `server/notify.py`, which sends the pushes (not built yet)

A development build talks plain `http://` to the LAN server, so Android needs cleartext traffic allowed
(`expo-build-properties` with `android.usesCleartextTraffic`); Expo Go already allows it.

## Not done yet

- Supabase: the spec reads jobs from Supabase (realtime Inbox) once `server/store.py` writes them; until then the
  Inbox polls `GET /jobs`.
