# Blockroom bot backend (Flask + Firestore)

Five commands only: **/room · /play · /vplay · /stop · /skip**
No inline "choose a song" pickers. One shared room per Telegram group, one TV
state document, real-time for every connected game.

```
/room            -> creates (or reuses) the group's room + one "Join Blockroom" button
/play <song>     -> YouTube search "<song> lyrics" -> mediaType "audio"
/vplay <video>   -> YouTube search "<video> video song" -> mediaType "video"
/skip            -> owner only: restart current media
/stop            -> owner only: playbackState "stopped" (TV goes dark)
```

## Deploy on Render (free)

1. Push this `backend/` folder as its own repo (or set Root Directory = `backend`).
2. Render → New → Web Service → connect the repo.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn live:app --workers 2 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT`
   - Health check: `/health`
3. Environment (Render dashboard, never in git):

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | any long random string |
| `FIREBASE_PROJECT_ID` | `mini-ludo-28672` |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Firebase console → Project settings → Service accounts → Generate new private key (paste the whole JSON on one line) |
| `YOUTUBE_API_KEY` | Google Cloud console → APIs → YouTube Data API v3 |
| `MINI_APP_URL` | `https://t.me/XcorytBot/xcorytroom` (BotFather → /newapp or /editapp) |

4. Set the webhook once (Render shell or your machine):

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<your-service>.onrender.com/webhook/telegram" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

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
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill values
export $(grep -v '^#' .env | xargs)
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
