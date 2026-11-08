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
| push registration (at launch) | – | `POST /devices`, and Supabase `devices` when configured |

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

## Supabase: realtime Inbox, and reports away from the server

With `app/.env` set (copy `.env.example`; the values are in the Supabase dashboard under Project Settings → API, or
`SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` in `server/.env`):

```bash
EXPO_PUBLIC_SUPABASE_URL=https://<project>.supabase.co
EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
```

- **Inbox** reads the `jobs` table that `server/store.py` writes on every status change and subscribes to its
  changes (Supabase Realtime), so new incidents and status changes show up within about a second, even when the
  phone can't reach the server (cellular, another network). A line above the list says which source is live. If
  Supabase fails, it falls back to `GET /jobs` every 3 s.
- **Incident detail** asks the server first; if the server can't be reached, it shows the copy stored in Supabase
  with a notice. The live trace and the actions still need the server.
- The publishable key can only read `jobs` and insert into `devices` (row-level security, `python3 server/store.py
  --sql`); it can't read push tokens back or write jobs. Actions stay on the server's API.
- Without the two variables the app works as before, entirely from the server's API.
- Restart `npx expo start` after changing `.env`. For an EAS build, set the same two variables in the build's
  environment as well (EAS environment variables for the `preview` environment; see `npx eas-cli env --help`).

Code: `src/supabase.ts` (client, reads, subscription) and `src/useInbox.ts` (source selection and reloads).

## Push notifications

At launch the app gets its Expo push token and registers it two ways, each best effort: a row in Supabase's `devices`
table (when `app/.env` has the Supabase variables; works when the phone can't reach the server) and `POST /devices`
(works without Supabase). The server pushes to both sets, deduped, when a ticket's investigation finishes
(`server/notify.py`). Tapping a notification opens Incident detail for `data.job_id`.

Once per Supabase project, run the SQL from `python3 server/store.py --sql` in the SQL Editor (it is idempotent). It
lets the publishable key insert into `devices` and nothing else there.

Registration is skipped until these exist:

- an EAS project id in `app.json` (`extra.eas.projectId`, already set for the `triagent` org)
- on Android, a build with Firebase (Expo Go dropped remote push on Android in SDK 53); iOS Expo Go works

A development build talks plain `http://` to the LAN server, so Android needs cleartext traffic allowed
(`expo-build-properties` with `android.usesCleartextTraffic`); Expo Go already allows it.

## Android APK

One-time setup (nothing here is committed: `google-services.json` and the service-account key are gitignored):

1. Firebase console: add an Android app with package `com.smitheartc.triagent`, download `google-services.json`
   into `app/`.
2. Firebase → Project settings → Service accounts → generate a private key (JSON). Upload it for FCM V1 with
   `npx eas-cli credentials -p android` (Google Service Account → FCM V1), then delete the local copy.
3. EAS environment variables for `preview` (`app.config.ts` reads `GOOGLE_SERVICES_JSON`):

   ```bash
   npx eas-cli env:set --environment preview --name GOOGLE_SERVICES_JSON --type file --value ./google-services.json --visibility secret
   npx eas-cli env:set --environment preview --name EXPO_PUBLIC_SUPABASE_URL --value https://<project>.supabase.co --visibility plaintext
   npx eas-cli env:set --environment preview --name EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY --value sb_publishable_... --visibility plaintext
   npx eas-cli env:set --environment preview --name EXPO_PUBLIC_API_URL --value http://<laptop LAN IP>:8080 --visibility plaintext
   ```

Build and install: `npx eas-cli build -p android --profile preview`, then open the link it prints on the phone.
`EXPO_PUBLIC_*` values are baked in at build time, so a new server address means a new build.
