# Wiring the TV into your existing game (integration guide)

Your game code was **not** part of the bot ZIP, so this folder ships a
self-contained reference TV player instead of editing your game directly.
`tv-player.js` implements the full playback half of the media flow using the
exact schema the bot writes - drop it in, or read it and port the parts you
need. Either way, the contract is the one documented in
[`FIREBASE_SCHEMA.md`](../FIREBASE_SCHEMA.md).

```
Telegram /play|/vplay ──► bot searches YouTube (embeddable only)
                      ──► writes rooms/{roomId}/media/current
tv-player.js onSnapshot ──► YouTube IFrame player actually plays it
```

## 1. What was broken (and what this fixes)

| Symptom | Root cause | Fix |
|---|---|---|
| Bot showed thumbnails/results but TV never played | The bot only wrote Firestore; no code on the game side subscribed to `rooms/{roomId}/media/current` and drove a real player | `tv-player.js` subscribes with `onSnapshot` and plays via the YouTube IFrame API |
| "Playback" claimed although nothing played | Confirmation was tied to the database write | Bot now only confirms after selecting an **embeddable** source; the TV shows honest loading/error states |
| Raw YouTube URL assumed playable | Browsers cannot play YouTube URLs as files | The IFrame Player API is the only legitimate route; the bot stamps `playbackMethod: "youtube-iframe"` and the TV enforces it |
| Autoplay blocked silently | Browsers require a user gesture for sound | TV starts muted (allowed everywhere) and shows a **"Tap to enable sound"** control |
| One player's tab switch could pause everyone | Any frontend writing pause/stop shares it room-wide | The TV is **display-only**: it never writes to Firestore, has no visibilitychange handlers, and pause/stop exist only as owner `/skip` `/stop` in Telegram |

## 2. Integration steps (three lines of glue)

```html
<!-- inside your game page (the Mini App) -->
<div id="blockroom-tv" style="width:320px;height:180px;border-radius:12px;overflow:hidden"></div>

<script type="module">
  import { createBlockroomTV } from "./frontend-reference/tv-player.js";

  const params = new URLSearchParams(Telegram.WebApp.initDataUnsafe.start_param
    ? "" : window.location.search);
  const roomId = Telegram.WebApp.initDataUnsafe.start_param || params.get("room");

  window.blockroomTV = await createBlockroomTV({
    container: document.getElementById("blockroom-tv"),
    roomId,                                   // one TV = one room
    telegramInitData: Telegram.WebApp.initData,
    botApiBase: "https://YOUR-BOT.onrender.com", // your Render service
    firebaseConfig: window.firebaseConfig,    // your web app config (public)
    // firestore: existingFirestoreInstance,  // optional, reuse your own
  });
</script>
```

Notes:

* If your game already initializes Firebase, pass your Firestore instance and
  the module will reuse it (no second app is created).
* If your game already signs in with a custom token via the bot's
  `/api/auth/telegram`, you can skip the module's auth step by passing
  `firestore` only - identity is already established.
* `mediaType` (`audio` vs `video`) is a styling concern for your game: both
  play through the same IFrame player. Many games show the video frame for
  `/vplay` and hide/shrink it for `/play`; keep the iframe in the DOM either
  way so playback keeps working.

## 3. Room isolation

The TV subscribes to exactly `rooms/<this roomId>/media/current`. Two rooms
run two independent TV instances with two independent documents; nothing is
shared, so room A's `/play` can never affect room B. The `roomId` in the
document is a scope guard the bot writes on every record.

## 4. Firestore Security Rules (read path)

The TV only needs to **read** the room's media docs. Allow authenticated
members to read and keep all writes server-side:

```
match /rooms/{roomId} {
  allow read: if request.auth != null;
  match /media/{docId} {
    allow read: if request.auth != null;
    allow write: if false;   // bot (Admin SDK) bypasses rules; browsers never write
  }
}
```

Full rules (including the bot's write path via the Admin SDK, which bypasses
these rules) are in `FIREBASE_SCHEMA.md`.

## 5. Verification checklist (requires real credentials - run these yourself)

Automated tests cannot prove browser playback; do this once with your real
Firebase project, bot token and YouTube key:

1. `/room` in a Telegram group → short "Blockroom Created" message with a
   working Join Blockroom button → open it.
2. `/play <song>` → expect one short `▶️ Playing: …` message, no numbered
   menu; the TV shows "Loading…" then plays. Tap the TV once for sound.
3. `/vplay <video>` → same flow, no thumbnail album; video is visible on the TV.
4. Second device/tab joins the same room → both TVs play the same video
   roughly in sync (both seek to `startedAt` offset).
5. `/play` again while playing → `➕ Queued:` message; owner `/skip` → TV
   switches to the queued item.
6. Owner `/stop` → TV shows the idle message. Non-owner `/skip`/`/stop` →
   refused by the bot.
7. Open two different rooms in two groups → play different media in each →
   verify neither TV changes.
8. Paste a known embedding-disabled video query (e.g. an official music
   video that blocks embedding) → the TV must show "Cannot play: the uploader
   does not allow…" instead of a black frame, and the bot must have said
   "No playable result" if that was the only match.
