# Blockroom Telegram Bot

A Flask backend for Telegram that creates unique Blockroom game rooms, hands
out per-room Mini App join links, enforces room ownership, and writes
room-scoped YouTube media state (now-playing + queue) to Firebase for your
existing Three.js game frontend to consume.

**User-facing commands: `/room`, `/play`, `/vplay`, `/skip`, `/stop` - and
nothing else.** `/start` is handled only because Telegram always sends it on
first contact and it is the one place a private chat can be handed the Mini
App open button; it lists no commands and is not part of the command menus.

**What this bot is NOT:** it does not contain, render, or modify the game.
The Three.js Mini App is a separate project that reads the Firebase paths
documented in [FIREBASE_SCHEMA.md](FIREBASE_SCHEMA.md).

```
Telegram group                      Render (this service)                Firebase
┌─────────────────┐   webhook    ┌──────────────────────────┐  writes  ┌──────────────────┐
│ /room  /play    │ ───────────► │  live.py (Flask+gunicorn)│ ───────► │ rooms/{id}       │
│ /vplay /skip    │  secret hdr  │  handlers -> services    │  partial │   /members/{uid} │
│ /stop           │              │  Firestore | RTDB adapter│  updates │   /chat/{msgId}  │
└─────────────────┘              │  /health  /api/auth      │          │   /media/current │
       │ Mini App                └──────────────────────────┘          │   /media/queue   │
       ▼ initData                                                     └──────────────────┘
 Three.js frontend ◄── subscribes to rooms/{roomId}/media/current ──┘  (YouTube IFrame player)
```

## Commands

| Command | Who | What it does |
|---|---|---|
| `/room` | any group member (rate-limited) | Creates a NEW unique room every time, records the sender as owner, posts the Join Blockroom link |
| `/play <song>` | any group member | YouTube search -> pick a result. If the room TV is idle the selection becomes the room's `media/current`; if something is already selected it is appended to the room's `media/queue` |
| `/vplay <video>` | any group member | Same as `/play` plus a thumbnail preview album |
| `/skip` | **room owner only** | Advances that room's queue: promotes the next queued item to `media/current`, or marks the TV stopped when the queue is empty |
| `/stop` | **room owner only** | Marks only that room's `media/current` stopped and clears its queue |
| `/start` | (automatic) | Private-chat entry point that shows the Open Blockroom button only; silent when `MINI_APP_URL` is missing |

Removed commands (`/lyrics`, `/style`, `/close`, `/rooms`, `/help`, and any
others) are not handled at all - the bot silently ignores them.

Owner authorization for `/skip` and `/stop` always compares the sender's
Telegram id against the `ownerTelegramId` stored in Firebase at `/room`
time - never against anything a client sends. A room can only be controlled
from the group that created it.

**Honesty rule:** every confirmation says the state was *saved* to Firebase
("the in-game TV picks this up from Firebase"); the bot never claims media is
playing. Actual playback happens only in the frontend, which must implement
the media-state schema (see FIREBASE_SCHEMA.md and "Frontend work required"
below).

## Project layout

| File | Purpose |
|---|---|
| `live.py` | Flask app: webhook, `/health`, initData->custom-token endpoint |
| `handlers.py` | Command + callback handlers (`/room /play /vplay /skip /stop` + minimal `/start`) |
| `rooms.py` | Room creation, ownership checks, deep links |
| `media.py` | Room-scoped TV media state writes + per-room queue |
| `youtube_api.py` | YouTube Data API v3 search (metadata only) |
| `db.py` | `FirestoreAdapter`, `RealtimeDbAdapter`, `InMemoryAdapter` behind one interface |
| `telegram_api.py` | Telegram Bot API client |
| `security.py` | Webhook secret check, `initData` HMAC verification, rate limiter |
| `config.py` | Environment-driven configuration |
| `tests/` | Mocked pytest suite (`test_e2e.py` = opt-in real-credential checks) |
| `render.yaml`, `Procfile`, `.env.example`, `scripts/set_webhook.sh` | Deployment |

---

## 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # production
pip install -r requirements-dev.txt      # only for running tests
```

Python 3.11+ recommended (3.12 is pinned in render.yaml).

## 2. Configure Firebase

1. In the [Firebase console](https://console.firebase.google.com) open (or create)
   the project your game uses.
2. **Service account:** Project settings -> Service accounts -> Generate new
   private key -> download the JSON file. The bot needs only its content:
   * local dev: set `FIREBASE_SERVICE_ACCOUNT_PATH=./service-account.json`, or
     paste the whole JSON (one line) into `FIREBASE_SERVICE_ACCOUNT_JSON`
     (base64 also accepted); production on Render: put the JSON string in the
     `FIREBASE_SERVICE_ACCOUNT_JSON` environment variable. Never commit it.
3. **Database choice** - the bot ships with a clear adapter for both:
   * `FIREBASE_BACKEND=firestore` (default) - uses the paths in
     FIREBASE_SCHEMA.md. No composite indexes needed.
   * `FIREBASE_BACKEND=rtdb` + `FIREBASE_RTDB_URL=https://<project>-default-rtdb.firebaseio.com`
     - same logical paths in Realtime Database. Add
     `{"rules": {..., "rooms": {".indexOn": ["telegramGroupId"]}}}` so the
     group lookup stays cheap (see FIREBASE_SCHEMA.md).
4. **Matching your frontend's naming:** if the game already uses different
   top-level collection names, set `FIREBASE_COLLECTION_PREFIX` (for example
   `blockroom_` writes to `blockroom_rooms/{roomId}`), or simply align the
   frontend with the documented names. The two projects must share ONE set of
   records - the bot never maintains duplicate parallel rooms.
5. **De-duplication store:** the bot keeps `processed_updates/{updateId}`
   (Firestore) or `processed_updates/{updateId}` (RTDB) to make webhook
   retries safe. Optional: attach a Firestore TTL policy (e.g. 7 days) or a
   periodic cleanup; records are tiny.

## 3. Configure BotFather and the Mini App URL

1. `/newbot` in [@BotFather](https://t.me/BotFather) -> copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. **Register the Mini App** if not already done: `/newapp` (or
   Bot Settings -> Menu Button) and upload your game's build. BotFather gives
   you a link like `https://t.me/<yourbot>/<app_short_name>` - set that as
   `MINI_APP_URL`. Any `https://` URL that serves the Mini App also works;
   the bot then deep-links with `?tgWebAppStartParam=<roomId>` instead of
   `?startapp=<roomId>`. Both arrive in the frontend as
   `Telegram.WebApp.initDataUnsafe.start_param`.
3. **Group usage:** send `/setprivacy` -> Disable (or make the bot a group
   admin) so it can see `/room`, `/play`, `/vplay`, `/skip`, `/stop` in groups.
4. Register exactly these commands via `/setcommands` (do **not** list
   `/start` - Telegram handles it implicitly):
   ```
   room - Create a new Blockroom in this group
   play - Add a song to the room TV
   vplay - Add a video to the room TV
   skip - Skip to the next item (room owner)
   stop - Stop this room's TV (room owner)
   ```

## 4. Configure the YouTube API

1. In [Google Cloud Console](https://console.cloud.google.com) select the
   project (a new one is fine), enable **YouTube Data API v3**.
2. APIs & Services -> Credentials -> Create credentials -> API key.
3. Restrict the key to "YouTube Data API v3" and put it in `YOUTUBE_API_KEY`.
4. Quota: default 10,000 units/day. `search` costs 100 units, so roughly 90
   searches/day on the free quota. The details lookup (`videos.list`, 1 unit)
   adds duration/embeddability data. The bot returns friendly errors when the
   quota runs out and never downloads or redistributes media.

## 5. Deploy to Render

1. Push this folder to a Git repository.
2. Render dashboard -> New -> Blueprint -> select the repo (render.yaml picks
   up automatically), or create a Web Service manually with:
   * Build: `pip install -r requirements.txt`
   * Start: `gunicorn live:app --workers 2 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT`
   * Health check path: `/health`
3. Fill in the `sync: false` environment variables in the dashboard:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` (any long random string),
   `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_JSON`, `MINI_APP_URL`,
   `YOUTUBE_API_KEY`.
4. **Webhook registration** - pick one:
   * `AUTO_SET_WEBHOOK=true` (default in render.yaml): on boot the service
     calls `setWebhook` against `RENDER_EXTERNAL_URL + /webhook/telegram`
     with your secret; or
   * run `scripts/set_webhook.sh` locally with the three env vars set.
   Verify with `getWebhookInfo` (the script prints it).
5. The service binds to Render's assigned `$PORT`, uses webhook mode only,
   and has no polling process.

Local run without deploying: `python live.py` serves on `$PORT` (default
8080). For real webhooks locally you need an HTTPS endpoint (e.g. ngrok) plus
the env vars; for pure curl testing set `ALLOW_INSECURE_WEBHOOK=true`
(never in production).

## 6. Connecting the frontend to these Firebase paths

Give your frontend developer (or the AI building the Three.js app) the file
[FIREBASE_SCHEMA.md](FIREBASE_SCHEMA.md). It specifies every path, field
type, who writes what, the playback synchronisation algorithm, and ready-made
Firebase Security Rules. Summary of the integration flow:

1. Mini App boots, reads `start_param` -> `roomId`.
2. Mini App sends its `initData` to `POST /api/auth/telegram` ->
   receives a Firebase **custom token** -> `signInWithCustomToken`.
   The token's `telegramId` claim maps to `rooms/{roomId}/members/{telegramId}`,
   so rules can authorise reads. The backend only trusts signed initData -
   plain user ids from the browser are never trusted.
3. Subscribe to `rooms/{roomId}/media/current` and drive the in-game TV with
   the official YouTube IFrame API (algorithm in FIREBASE_SCHEMA.md).
4. Lock the database down with the provided Security Rules - **the bot's
   checks do not protect direct frontend writes; the rules do.**

### Honest security boundaries

Secured server-side by this bot: room creation, join-link generation, and the
owner-only TV controls `/skip` / `/stop` (verified against the stored owner),
room-scoped media selection writes, webhook authenticity, initData
verification, rate limits.

NOT secured by this bot (must be enforced by the Firebase Security Rules in
FIREBASE_SCHEMA.md): direct Firestore/RTDB writes from the browser, chat
messages created by players, playback-position updates made by clients. The
bot never claims to secure operations it does not check.

## 7. Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests -q
```

**94 tests pass** (plus 3 opt-in E2E tests that skip unless
`RUN_E2E_TESTS=true`). Everything external is mocked - no network, no real
Firebase/Telegram/YouTube - via an in-memory adapter and spy/fake clients.
Coverage maps to the command set:

* `/room` unique rooms, explicit owner assignment (message + Firebase), group
  id, style defaults, timestamps
* per-room unique join links (`startapp=<roomId>`)
* `/play` + `/vplay` search, thumbnails, embeddable filtering, selection;
  idle room -> selection becomes `media/current`, active room -> appended to
  `media/queue`
* `/skip` advances only that room's queue (FIFO promotion to
  `media/current`; stopped when empty); `/stop` marks only that room stopped
  and clears only its queue
* owner authorization for `/skip` + `/stop` (non-owners rejected, picker
  re-verifies ownership, controls only work from the owning group)
* room isolation: media, queue and controls never leak between rooms or groups
* removed commands (`/lyrics`, `/style`, `/close`, `/rooms`, `/help`, others)
  are ignored; `/start` shows only the Mini App button
* invalid room handling (deleted rooms, bad ids, wrong group)
* invalid webhook secrets, missing configuration, simulated Firebase outages
* duplicate webhook delivery (single room, atomic dedupe claim)
* initData signature/expiry/tamper + custom-token claim mapping
* honest-phrasing checks: no bot message claims media is playing

Real-credential checks (search, getMe, webhook registration) live in
`tests/test_e2e.py`: `RUN_E2E_TESTS=true pytest tests/test_e2e.py`.

## 8. What still needs real credentials / frontend work

* **Not deployed or run against live services** - everything here was verified
  with the mocked suite; run the E2E tests after deploying with real keys.
* The Mini App frontend must consume `start_param`, call `/api/auth/telegram`,
  sign in with the custom token, subscribe to the media path, and render the
  YouTube IFrame player (spec in FIREBASE_SCHEMA.md).
* **Frontend work required for /skip and /stop to be visible in-game** (the
  bot only writes Firebase state):
  1. Treat `playbackState: "stopped"` explicitly - stop/park the IFrame player
     (the previous sync snippet only handled "paused" vs everything else, so a
     stopped state would have kept playing).
  2. Handle a `media/current` snapshot with no `videoId` (or a missing
     snapshot) by rendering the idle TV.
  3. Optional: display `rooms/{roomId}/media/queue` as an up-next list.
  See the updated protocol in FIREBASE_SCHEMA.md section 3.
* Firebase Security Rules from FIREBASE_SCHEMA.md must be pasted into your
  project (Admin SDK bypasses rules; clients do not).
* YouTube quota depends on your key.
* Rate limits and the pending-selection cache are per-process (single Render
  instance). For multiple instances, back `RateLimiter`/`PendingStore` and the
  per-room queue lock with Redis.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | BotFather token |
| `TELEGRAM_WEBHOOK_SECRET` | yes (prod) | Secret-token header validation |
| `FIREBASE_PROJECT_ID` | yes | Firebase project |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | yes (or path) | Admin credentials (JSON or base64) |
| `FIREBASE_SERVICE_ACCOUNT_PATH` | local alt | File path alternative |
| `FIREBASE_BACKEND` | no | `firestore` (default) or `rtdb` |
| `FIREBASE_RTDB_URL` | if rtdb | Realtime Database URL |
| `FIREBASE_COLLECTION_PREFIX` | no | e.g. `blockroom_` |
| `MINI_APP_URL` | yes | Mini App link (t.me or https) |
| `YOUTUBE_API_KEY` | for /play /vplay | YouTube Data API v3 |
| `RENDER_EXTERNAL_URL` | auto | Provided by Render |
| `AUTO_SET_WEBHOOK` | no | `true` registers the webhook at boot |
| `ALLOW_INSECURE_WEBHOOK` | no | Local-dev only; never in production |
| `MAX_INIT_DATA_AGE_SECONDS`, `RATE_LIMIT_*`, `LOG_LEVEL` | no | Tuning |

## Troubleshooting

* **Bot ignores /room in groups** -> privacy mode still on (see section 3).
* **Bot ignores /lyrics, /style, /close, /rooms, /help** -> intentional; only
  `/room`, `/play`, `/vplay`, `/skip`, `/stop` exist (plus the implicit
  `/start` Mini App button).
* **403 from /health is fine; webhook 403s** -> the header does not match
  `TELEGRAM_WEBHOOK_SECRET`; re-run the webhook registration.
* **"Firebase credentials are not configured" at boot** -> health stays green
  but commands reply with guidance; fix the service-account variable.
* **"MINI_APP_URL is not configured"** -> /room refuses to create rooms rather
  than produce dead links; set the variable and retry.
* **YouTube quota errors** -> friendly message shown; waits for quota reset.
