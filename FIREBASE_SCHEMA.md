# Blockroom Firebase Schema & Frontend Integration Spec

Hand this file to the AI/developer building the Three.js Mini App so both
projects agree on paths, field ownership and the playback protocol.

* Backend (this bot) writes with **partial updates** (Firestore `merge`,
  RTDB `update`) and atomic create-if-missing operations.
* All timestamps are **ISO-8601 UTC strings** ending in `Z`
  (`2026-10-28T09:15:00Z`) - parse with `new Date(value)`.
* `telegramGroupId` is stored as a **string**; Telegram user ids are stored
  as **numbers** in fields, but as **string document keys** for members.
* Firestore is the reference backend; a Realtime Database adapter
  (`FIREBASE_BACKEND=rtdb`) writes the identical tree. Top-level names can be
  shifted with `FIREBASE_COLLECTION_PREFIX`; the bot and frontend MUST share
  the same resolved names.

---

## 1. Paths and fields

### `rooms/{roomId}`  (document, created by the bot)

| Field | Type | Written by | Meaning |
|---|---|---|---|
| `roomId` | string | bot | Same as the document id, `[a-z0-9_-]{6,32}` |
| `status` | `"active" \| "closed"` | bot | Closed rooms reject media; frontend should show "room closed" |
| `ownerTelegramId` | number | bot | Telegram user id of the owner (set at creation, verified on every privileged op such as /skip and /stop) |
| `ownerUsername` | string \| null | bot | Owner @username (may be null) |
| `ownerDisplayName` | string | bot | Owner first name |
| `telegramGroupId` | string | bot | Telegram group chat id, stringified |
| `telegramChatTitle` | string \| null | bot | Group title at creation |
| `style` | map | bot (at creation only) | `{theme, wallColor, floorColor, tvSize, ambientLight}` - defaults below. The bot never changes style after creation (no /style command) |
| `createdAt` | string | bot | ISO-8601 Z |
| `createdVia` | string | bot | Provenance, e.g. `telegram_command:/room` |
| `createdByWebhookUpdateId` | string \| null | bot | Traceability |
| `closedAt` | string \| null | bot | Set when closed |

Default style: `{"theme": "midnight", "wallColor": "#232344", "floorColor": "#151530", "tvSize": "medium", "ambientLight": true}`.
The bot writes these defaults once at `/room` time. There is no style-change
command any more; the frontend may style the room however it likes.

> The frontend MAY add its own sibling fields to room docs (e.g.
> `worldSeed`); the bot never overwrites keys it does not write.

### `rooms/{roomId}/members/{userId}`  (userId = string Telegram id)

| Field | Type | Meaning |
|---|---|---|
| `userId` | string | Telegram id as string (doc key) |
| `telegramId` | number | Telegram id |
| `displayName` | string | First name at join time |
| `username` | string \| null | @username |
| `role` | `"owner" \| "member"` | Owner is set by /room |
| `verifiedVia` | `"telegram_group_message" \| "telegram_mini_app_init_data"` | How identity was verified |
| `joinedAt`, `lastSeenAt` | string | ISO-8601 Z |

### `rooms/{roomId}/chat/{messageId}`

| Field | Type | Meaning |
|---|---|---|
| `messageId` | string | Doc key (Firestore auto id / RTDB push key) |
| `senderId` | string | Telegram id as string, or `"bot"` |
| `senderName` | string | Display name |
| `text` | string | <= 500 chars |
| `kind` | `"system" \| "media" \| "user"` | Bot events vs player chat |
| `roomId` | string | Denormalised |
| `createdAt` | string | ISO-8601 Z |

### `rooms/{roomId}/media/current`  (the shared TV state)

| Field | Type | Written by | Meaning |
|---|---|---|---|
| `roomId` | string | bot | Scope guard |
| `videoId` | string | bot | YouTube video id |
| `title` | string | bot | Video title |
| `channelTitle` | string | bot | Channel name |
| `thumbnailUrl` | string \| null | bot | Thumbnail (mqdefault) |
| `durationSeconds` | number \| null | bot | From videos.list when available |
| `embeddable` | boolean | bot | false -> show "video unavailable in embeds" |
| `mediaType` | `"audio" \| "video"` | bot | /play = audio, /vplay = video |
| `playbackState` | `"playing" \| "paused" \| "stopped" \| "buffering"` | bot (selection, /skip, /stop), members (position sync) | See sync protocol. `"stopped"` is written by the owner's `/skip` (empty queue) or `/stop`; the frontend MUST stop the player when it reads it |
| `positionSeconds` | number | bot (0 at selection), members (sync) | Playhead position |
| `startedAt` | string | bot | When the selection was made (UTC) |
| `updatedAt` | string | bot or members | Last write to this doc |
| `requestedBy` | map | bot | `{telegramId, displayName, username}` |
| `sourceQuery` | string \| null | bot | The /play or /vplay query |

**Field ownership:** only the bot selects media (`videoId`, `title`, ...) and
controls `/skip` / `/stop`. Members may update ONLY `playbackState`,
`positionSeconds`, `updatedAt` on this document (enforced by the rules
below). Everything else is backend/Admin-SDK-only. The frontend may keep
extra keys here (e.g. `frontendHint`); merges never wipe them.

### `rooms/{roomId}/media/queue`  (the up-next list, bot-only)

A single document next to `current`:

| Field | Type | Written by | Meaning |
|---|---|---|---|
| `roomId` | string | bot | Scope guard |
| `items` | array | bot | Ordered FIFO list of pending items, each with the same field set as `media/current` (including `videoId`, `title`, `mediaType`, `requestedBy`) plus `queuedAt` |
| `updatedAt` | string | bot | Last queue mutation |

Behaviour:

* `/play` or `/vplay` append here (with the position reported in chat) when
  the room already has an active `media/current`; otherwise the selection
  becomes `media/current` directly.
* `/skip` (room owner only) removes `items[0]` and writes it to
  `media/current` with fresh `startedAt`/`updatedAt` and
  `playbackState: "playing"`. An empty queue marks `media/current` stopped.
* `/stop` (room owner only) empties `items` and marks `media/current` stopped.
* Clients never write this document (Admin SDK only). Reading it to show an
  "up next" list is allowed and encouraged.
* Hard cap: 50 items per room.

A missing queue document simply means "queue is empty".

### `processed_updates/{updateId}`  (bot-internal, deny to clients)

`{processedAt: string}` - webhook de-duplication. Safe to TTL-delete after a
week. Clients get no read/write access.

---

## 2. Identity: initData -> Firebase Auth

The Mini App must never trust its own unverified browser identity for
database access. Use the backend exchange:

```
POST /api/auth/telegram
{"initData": "<raw Telegram.WebApp.initData>", "roomId": "<start_param>"}

200 -> {"ok": true, "firebaseUid": "tg_<id>", "token": "<Firebase custom token>",
        "user": {"id", "displayName", "username", "photoUrl"}}
```

Then in the frontend:

```js
import { getAuth, signInWithCustomToken } from "firebase/auth";
const res = await fetch(BOT_BASE_URL + "/api/auth/telegram", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({ initData: window.Telegram.WebApp.initData,
                         roomId: window.Telegram.WebApp.initDataUnsafe.start_param }),
});
const { token } = await res.json();
await signInWithCustomToken(getAuth(), token);
```

The custom token carries claims: `telegramId` (**string**, matches member doc
ids), `displayName`, `username`, `authProvider: "telegram"`, and `roomId`
(only if the backend verified membership). `initData` older than
`MAX_INIT_DATA_AGE_SECONDS` (default 24h) or with a bad HMAC is rejected with
401.

## 3. Playback synchronisation protocol (frontend)

```js
// one listener per room
onSnapshot(doc(db, "rooms", roomId, "media", "current"), (snap) => {
  const media = snap.data();
  // No snapshot, no videoId, or an explicit stop -> idle TV.
  if (!media || !media.videoId || media.playbackState === "stopped") {
    player.stopVideo();            // park the IFrame player
    return renderIdleTV(media);    // optionally show "stopped by owner"
  }
  if (media.embeddable === false) return renderUnavailable(media.title);

  const elapsed = media.playbackState === "playing"
    ? media.positionSeconds + (Date.now() - new Date(media.updatedAt).getTime()) / 1000
    : media.positionSeconds;

  if (player.getVideoData().video_id !== media.videoId) {
    player.loadVideoById(media.videoId, elapsed);      // official IFrame API
  } else if (Math.abs(player.getCurrentTime() - elapsed) > 3) {
    player.seekTo(elapsed, true);
  }
  if (media.playbackState === "paused") player.pauseVideo();
  else player.playVideo();
});
```

* **`playbackState === "stopped"` must actually stop the player.** The bot
  writes it on the owner's `/skip` (empty queue) or `/stop`; if your old
  snippet only branched on "paused", a stopped room would keep playing.
* Compare `updatedAt` before applying to ignore stale writes.
* `/skip` promotions arrive as a normal `media/current` update (new `videoId`,
  fresh `startedAt`, `positionSeconds: 0`) - no extra wiring needed beyond the
  generic listener above.
* Fire `positionSeconds` updates from members at most every ~5s while playing
  (one "host"/oldest member writes to avoid write storms), with
  `updatedAt: new Date().toISOString().replace(/\.\d{3}Z$/, "Z")`.
* Optional: subscribe to `rooms/{roomId}/media/queue` to show the up-next
  list. Read-only for clients.
* Non-embeddable videos: the bot already filters most out at search time; if
  one slips through, show an "unavailable" card - do not attempt playback.
* Read `start_param` for the room id; if the room doc is missing or
  `status === "closed"`, show a "room no longer available" screen.

## 4. Firebase Security Rules (paste into your project)

### Firestore

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    function isTgUser() {
      return request.auth != null && request.auth.token.telegramId != null;
    }
    function isRoomMember(roomId) {
      return isTgUser() &&
        exists(/databases/$(database)/documents/rooms/$(roomId)/members/$(request.auth.token.telegramId));
    }
    match /rooms/{roomId} {
      allow get, list: if isRoomMember(roomId);
      allow write: if false;                       // room docs: backend only
      match /members/{userId} {
        allow get, list: if isTgUser();
        allow write: if false;                     // backend only
      }
      match /chat/{msgId} {
        allow read: if isRoomMember(roomId);
        allow create: if isRoomMember(roomId)
          && request.resource.data.senderId == request.auth.token.telegramId
          && request.resource.data.text is string
          && request.resource.data.text.size() > 0
          && request.resource.data.text.size() <= 500;
        allow update, delete: if false;
      }
      match /media/{docId} {
        allow read: if isRoomMember(roomId);
        allow create, delete: if false;            // only the bot selects media
        allow update: if isRoomMember(roomId)
          && request.resource.data.diff(resource.data).affectedKeys()
               .hasOnly(['playbackState', 'positionSeconds', 'updatedAt']);
      }
    }
    match /processed_updates/{id} { allow read, write: if false; }
  }
}
```

### Realtime Database (equivalent, if `FIREBASE_BACKEND=rtdb`)

```json
{
  "rules": {
    "processed_updates": { ".read": false, ".write": false },
    "rooms": {
      ".indexOn": ["telegramGroupId"],
      "$roomId": {
        ".read": "auth != null && auth.token.telegramId != null && root.child('rooms').child($roomId).child('members').child(auth.token.telegramId).exists()",
        ".write": false,
        "members": { ".write": false },
        "chat": { "$msgId": {
            ".write": "auth != null && newData.child('senderId').val() === auth.token.telegramId"
        }},
        "media": {
            "current": { ".write": false,
                "playbackState":   { ".write": "auth != null" },
                "positionSeconds": { ".write": "auth != null" },
                "updatedAt":       { ".write": "auth != null" }
            },
            "queue": { ".write": false }
        }
      }
    }
  }
}
```

(Read-side member check per room as in Firestore; adjust to taste. The Admin
SDK bypasses all rules, so the bot is unaffected.)

## 5. Notes for both sides

* Room ids are unpredictable (10 chars, ~50 bits, unambiguous alphabet) and
  are generated server-side only.
* Every `/room` creates a NEW room with the sender as owner (also stated in
  the chat message); old links keep working until the room is closed. Deleted
  or closed rooms must be handled gracefully by the frontend.
* One group can hold several active rooms; the bot asks users which room to
  target before media selection, and `/skip` / `/stop` are scoped to the
  sender's own room(s) in the group that created them, so `media/*` changes
  are always room-scoped.
* The bot never downloads or re-encodes YouTube content; the frontend must
  use the official YouTube IFrame embed.
* Firestore group-scoped queries by the bot use equality on
  `telegramGroupId` only - no composite indexes required. RTDB needs the
  `.indexOn` shown above.
