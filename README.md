# Blockroom bot backend (Flask + Firestore)

Five commands: **/room · /play · /vplay · /stop · /skip**
(plus Telegram onboarding `/start` and `/help`, which only send the welcome text)
No inline "choose a song" pickers. One shared room per Telegram group, one TV
state document, real-time for every connected game.

```
/room            -> creates (or reuses) the group's room + one "Join Blockroom" button
/play <song>     -> YouTube search "<song> lyrics" -> mediaType "audio"
/vplay <video>   -> YouTube search "<video> video song" -> mediaType "video"
/skip            -> owner only: restart current media
/stop            -> owner only: playbackState "stopped" (TV goes dark)
```

## Deploy on Render (free) — plain Python, no gunicorn

1. Push this `backend/` folder as its own repo (or set Root Directory = `backend`).
2. Render → New → Web Service → connect the repo.
   - Build: `pip install -r requirements.txt`
   - **Start: `python live.py`**
   - Health check: `/health`
   - Environment: **`BOT_MODE=polling`** (recommended on free — no webhook URL needed)
3. Environment (Render dashboard, never in git):

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | any long random string |
| `FIREBASE_PROJECT_ID` | `mini-ludo-28672` |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Firebase console → Project settings → Service accounts → Generate new private key (paste the whole JSON on one line) |
| `YOUTUBE_API_KEY` | Google Cloud console → APIs → YouTube Data API v3 |
| `MINI_APP_URL` | `https://t.me/XcorytBot/xcorytroom` (BotFather → /newapp or /editapp) |

4. That is it for delivery. In `BOT_MODE=polling` the bot deletes any old
   webhook and reads updates itself — `python live.py` is enough, no public
   URL, no `setWebhook` step.

   Prefer webhooks? Set `BOT_MODE=webhook`, add `RENDER_EXTERNAL_URL`
   (Render fills this automatically) + `TELEGRAM_WEBHOOK_SECRET`; the webhook is
   registered on startup. Check either mode at
   `https://<service>.onrender.com/telegram/status`.

### Troubleshooting: "only Flask started, the bot is silent"

| Symptom | Cause | Fix |
|---|---|---|
| `/health` shows `telegram: false` | `TELEGRAM_BOT_TOKEN` missing | add it in Render → Environment |
| `/health` `bot: null` | wrong/revoked token | paste a fresh token from @BotFather |
| `/health` `mode: webhook`, `webhook: "missing RENDER_EXTERNAL_URL…"` | no public URL configured | set `BOT_MODE=polling` (simplest) |
| `/telegram/status` `webhook.url` is `""` and `polling: false` | neither delivery mode active | `BOT_MODE=polling` |
| DM `/start` silent, group commands work | — | `/start` is now handled; redeploy |
| Group commands ignored (bot admin, privacy on) | BotFather privacy mode | @BotFather → `/setprivacy` → **Disable**, or make the bot a group admin |
| Old server still answering | webhook points at an old URL | polling deletes it automatically; or call `/telegram/set-webhook` |

5. Firebase console → Authentication → Sign-in method → **Anonymous** enable
   (the Mini App falls back to it), and deploy the game repo's
   `firestore.rules`.

## Firestore layout (matches the game)

```
rooms/{roomId}
  roomId, status, ownerTelegramId, telegramGroupId, capacity, style, createdAt
rooms/{roomId}/members/{telegramUserId}
  userId, telegramId, displayName, username, role, joinedAt, lastSeenAt
  (the game also writes x, z, yaw for shared avatars)
rooms/{roomId}/media/current
  roomId, videoId, title, channelTitle, thumbnailUrl, durationSeconds,
  embeddable, mediaType, playbackMethod, playbackState, positionSeconds,
  startedAt, updatedAt, requestedBy{telegramId,displayName,username}, sourceQuery
rooms/{roomId}/media/queue  { roomId, items[], updatedAt }
rooms/{roomId}/chat/{id}    { senderId, senderName, text, kind, roomId, createdAt }
```

Timestamps are ISO-8601 UTC strings (`...Z`). The frontend parses them and
uses `videoId` through the official YouTube IFrame player.

## Local run

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill values; BOT_MODE=polling
python live.py            # http://localhost:5000/health
```

Without Firebase credentials the bot runs on an in-memory adapter (great for
tests, useless for production).

## Shared avatars & names

On join the game writes the player's own member document (never anyone else's):
`name`, `displayName`, `joinedAt`, `lastSeen`, `x`, `z`, `yaw`. Every client
subscribes to the members subcollection, spawns a clone of the same rigged GLB
for each other member, smooths their position, and floats their Telegram first
name above the head. Max 8 remote avatars (mobile budget); stale after 90 s.

## Security notes

- The service-account JSON is server-side only. The browser receives only a
  short-lived Firebase **custom token** (`/api/auth/telegram`, uid `tg_<id>`).
- Webhook calls must carry `X-Telegram-Bot-Api-Secret-Token`.
- `/skip` and `/stop` verify `ownerTelegramId` on every call.
- Never commit `.env` or a service-account JSON. If either leaked, rotate it.
