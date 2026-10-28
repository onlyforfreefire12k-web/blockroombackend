"""Telegram update handlers for the Blockroom bot.

User-facing commands: /room, /play, /vplay, /skip, /stop.  /start is handled
only because Telegram always delivers it on first contact and it is the one
place a private chat can be handed the Mini App open button - it is not a
feature command and is left out of the command menus.

Every handler receives a :class:`live.BlockroomState` plus the raw Telegram
objects.  Identity rules:

* Commands arrive via the secret-token-verified webhook, so ``message.from``
  is Telegram-authenticated; room ownership is always checked against the
  owner stored in Firebase, never against anything a client sends.
* Mini App identities are only trusted after ``initData`` signature
  verification (see live.py's /api/auth/telegram endpoint).

No handler ever echoes tokens, keys or initData back into chat.
"""

import logging
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from db import DatabaseError
from media import ACTIVE_PLAYBACK_STATES
from rooms import (
    RoomConfigError,
    RoomError,
    build_mini_app_link,
    is_room_owner,
)
from security import escape_html as escape
from telegram_api import (
    TelegramAPIError,
    build_inline_keyboard,
    callback_button,
    url_button,
    web_app_button,
)
from timeutil import utc_now_iso
from youtube_api import YouTubeConfigError, YouTubeError, YouTubeQuotaExceeded

log = logging.getLogger("blockroom.handlers")

MAX_QUERY_LENGTH = 100
MAX_BUTTON_TEXT = 64
PENDING_TTL_SECONDS = 600
PENDING_MAX_ENTRIES = 500
GROUP_TYPES = {"group", "supergroup"}


# ---------------------------------------------------------------------------
# Pending-selection store (inline buttons <-> search context)
# ---------------------------------------------------------------------------

@dataclass
class PendingEntry:
    kind: str                       # "select" | "pickroom"
    user_id: int                    # only this user may click the buttons
    chat_id: int
    token: str = ""
    media_type: Optional[str] = None
    query: Optional[str] = None
    results: List[object] = field(default_factory=list)
    room_ids: List[str] = field(default_factory=list)
    room_id: Optional[str] = None
    action: Optional[str] = None    # for pickroom: "skip" | "stop" | None
    message_id: Optional[int] = None
    created_at: float = field(default_factory=time.monotonic)


class PendingStore:
    """Short-lived in-memory store linking inline buttons to search context.

    Telegram caps ``callback_data`` at 64 bytes, so search results and the
    target room are kept here and referenced by an unguessable random token.
    This cache is per-process: run a single web service instance (the Render
    default for this bot) or back it with Redis when scaling horizontally.
    """

    def __init__(self, ttl_seconds: int = PENDING_TTL_SECONDS,
                 max_entries: int = PENDING_MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries = {}

    def add(self, entry: PendingEntry) -> str:
        with self._lock:
            self._evict_locked()
            token = pysecrets.token_hex(8)
            entry.token = token
            entry.created_at = time.monotonic()
            self._entries[token] = entry
            return token

    def get(self, token: str) -> Optional[PendingEntry]:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            if time.monotonic() - entry.created_at > self._ttl:
                self._entries.pop(token, None)
                return None
            return entry

    def pop(self, token: str) -> Optional[PendingEntry]:
        with self._lock:
            return self._entries.pop(token, None)

    def _evict_locked(self):
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items()
                   if now - entry.created_at > self._ttl]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) >= self._max:
            oldest = min(self._entries, key=lambda k: self._entries[k].created_at)
            self._entries.pop(oldest, None)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def send_reply(state, chat_id, text: str, reply_markup=None):
    if chat_id is None:
        return None
    try:
        return state.tg.send_message(chat_id, text, reply_markup=reply_markup)
    except TelegramAPIError as exc:
        log.warning("sendMessage failed: %s", exc)
        return None


def send_error(state, chat_id, text="Something went wrong on my side. Please try again."):
    try:
        send_reply(state, chat_id, escape(text))
    except Exception:  # never crash the webhook loop on error notices
        log.exception("Failed to deliver the error notice")


def parse_command(text: str):
    """Return (command, addressed_to, args) or None for non-commands."""
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    first, _, rest = text.partition(" ")
    command = first[1:]
    addressed_to = None
    if "@" in command:
        command, addressed_to = command.split("@", 1)
    if not command:
        return None
    return command.lower(), addressed_to, rest.strip()


def bot_identity(state):
    """Cached (bot_username, bot_display_name); safe on API failures."""
    if state.bot_username is not None:
        return state.bot_username, state.bot_display_name
    with state.bot_identity_lock:
        if state.bot_username is not None:
            return state.bot_username, state.bot_display_name
        try:
            me = state.tg.get_me() if state.tg else None
            if me:
                state.bot_username = me.get("username") or ""
                state.bot_display_name = me.get("first_name") or "Blockroom Bot"
            else:
                state.bot_username = ""
        except TelegramAPIError:
            state.bot_username = ""
        return state.bot_username, state.bot_display_name


def push_room_chat(state, room_id: str, sender_id, sender_name: str,
                   text: str, kind: str = "system"):
    """Best-effort echo of bot events into rooms/{roomId}/chat."""
    if state.db is None:
        return
    try:
        state.db.push_chat_message(room_id, {
            "senderId": str(sender_id),
            "senderName": sender_name,
            "text": (text or "")[:500],
            "kind": kind,
            "roomId": room_id,
            "createdAt": utc_now_iso(),
        })
    except DatabaseError:
        log.warning("Could not write room chat message for room %s", room_id)


def _active_rooms(state, chat):
    """Return (rooms, error_occurred).  Sends the user-facing error itself."""
    if state.rooms is None:
        send_reply(state, chat.get("id"),
                   "The bot's database is not configured yet. "
                   "Ask the admin to check the Firebase settings.")
        return None, True
    try:
        return state.rooms.active_rooms_for_group(chat.get("id")), False
    except DatabaseError:
        log.exception("Room lookup failed for group %s", chat.get("id"))
        send_reply(state, chat.get("id"),
                   "The database is unreachable right now. Please try again soon.")
        return None, True


def _number_keyboard(token: str, count: int) -> dict:
    rows, row = [], []
    for index in range(count):
        row.append(callback_button(str(index + 1), "sel|{}|{}".format(token, index)))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([callback_button("Cancel", "cxl|{}|0".format(token))])
    return build_inline_keyboard(rows)


def _pick_keyboard(token: str, room_ids: List[str]) -> dict:
    rows = [
        [callback_button("Room {}".format(rid)[:MAX_BUTTON_TEXT],
                         "pick|{}|{}".format(token, index))]
        for index, rid in enumerate(room_ids)
    ]
    rows.append([callback_button("Cancel", "cxl|{}|0".format(token))])
    return build_inline_keyboard(rows)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_update(state, update: dict) -> str:
    """Route one Telegram update.  Never raises; always returns an outcome."""
    try:
        callback = update.get("callback_query")
        if callback:
            handle_callback_query(state, callback)
            return "callback"
        message = update.get("message")
        if message:
            handle_message(state, update, message)
            return "message"
        return "ignored"
    except Exception:
        log.exception("Unhandled failure while processing update %s",
                      update.get("update_id"))
        chat = None
        if isinstance(update.get("message"), dict):
            chat = update["message"].get("chat")
        elif isinstance(update.get("callback_query"), dict):
            chat = (update["callback_query"].get("message") or {}).get("chat")
        if isinstance(chat, dict) and chat.get("id") is not None:
            send_error(state, chat.get("id"))
        return "error"


def handle_message(state, update: dict, message: dict):
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    text = message.get("text") or ""
    if not text.startswith("/"):
        return
    parsed = parse_command(text)
    if parsed is None:
        return
    command, addressed_to, args = parsed

    if addressed_to:
        username, _ = bot_identity(state)
        if username and addressed_to.lower() != username.lower():
            return  # the command was meant for a different bot

    user_id = user.get("id")
    if user_id is None:
        return  # no authenticated sender (e.g. anonymous service message)

    if command == "start":
        cmd_start(state, chat, user)
    elif command == "room":
        cmd_room(state, update, chat, user)
    elif command == "play":
        cmd_play(state, chat, user, args, media_type="audio")
    elif command == "vplay":
        cmd_play(state, chat, user, args, media_type="video")
    elif command == "skip":
        cmd_skip(state, chat, user)
    elif command == "stop":
        cmd_stop(state, chat, user)
    # Unknown commands (including /lyrics /style /close /rooms /help, which
    # were deliberately removed) are ignored silently.


# ---------------------------------------------------------------------------
# /start - kept only as the private-chat entry point to the Mini App
# ---------------------------------------------------------------------------

def cmd_start(state, chat, user):
    """Telegram always delivers /start on first contact.

    It exists purely to hand the user the Mini App open button; it lists no
    commands and advertises no features.  When MINI_APP_URL is missing there
    is nothing useful to offer, so the bot stays silent instead of turning
    /start into a feature command.
    """
    if not state.cfg.mini_app_configured:
        return
    keyboard = []
    if state.cfg.mini_app_url.startswith("https://"):
        keyboard.append([web_app_button("Open Blockroom", state.cfg.mini_app_url)])
    else:
        # http:// URLs (local dev) cannot be web_app buttons; use a plain link.
        keyboard.append([url_button("Open Blockroom", state.cfg.mini_app_url)])
    send_reply(state, chat.get("id"),
               "Tap below to open Blockroom.",
               build_inline_keyboard(keyboard))


# ---------------------------------------------------------------------------
# /room
# ---------------------------------------------------------------------------

def cmd_room(state, update, chat, user):
    chat_id = chat.get("id")
    if chat.get("type") not in GROUP_TYPES:
        send_reply(state, chat_id,
                   "Rooms are created inside Telegram groups. Add me to a group "
                   "and send /room there.")
        return
    if state.rooms is None:
        send_reply(state, chat_id,
                   "The bot's database is not configured yet. "
                   "Ask the admin to check the Firebase settings.")
        return
    if not state.cfg.mini_app_configured:
        send_reply(state, chat_id,
                   "MINI_APP_URL is not configured on the server, so I cannot "
                   "generate a working join link. Ask the admin to configure it, "
                   "then run /room again.")
        return
    if not state.limiter.allow("room:{}".format(user["id"]),
                               state.cfg.rate_limit_rooms_per_hour, 3600):
        send_reply(state, chat_id,
                   "You have created too many rooms recently. Please try again later.")
        return

    # Best-effort verification that the requester is still a group member.
    # Failure to look this up (bot lacks rights) is non-fatal: the update
    # itself already came through the authenticated webhook from this group.
    member = state.tg.get_chat_member(chat_id, user["id"]) if state.tg else None
    if member and member.get("status") in ("left", "kicked"):
        send_reply(state, chat_id, "You are not a member of this group.")
        return

    try:
        room = state.rooms.create_room(owner=user, chat=chat,
                                       update_id=update.get("update_id"))
    except DatabaseError:
        log.exception("Room creation failed in group %s", chat_id)
        send_reply(state, chat_id,
                   "Could not create the room because the database is unavailable. "
                   "Please try again later.")
        return
    except RoomError as exc:
        send_reply(state, chat_id, escape(str(exc)))
        return

    state.rooms.ensure_member(room["roomId"], user, role="owner",
                              verified_via="telegram_group_message")

    try:
        link = build_mini_app_link(state.cfg.mini_app_url, room["roomId"])
    except RoomConfigError as exc:
        send_reply(state, chat_id, escape(str(exc)))
        return

    group_title = escape(chat.get("title") or "this group")
    owner_name = escape(user.get("first_name") or "unknown")
    text = (
        "<b>Blockroom created</b>\n"
        "Room ID: <code>{}</code>\n"
        "Owner: {}\n"
        "Group: {}\n\n"
        "Tap <b>Join Blockroom</b> to enter this room in the game. "
        "Every /room command creates a fresh, separate room.\n"
        "As the owner, {} can send /skip and /stop here to control this "
        "room's TV."
    ).format(escape(room["roomId"]), owner_name, group_title, owner_name)
    send_reply(state, chat_id, text,
               build_inline_keyboard([[url_button("Join Blockroom", link)]]))

    _, display_name = bot_identity(state)
    push_room_chat(state, room["roomId"], "bot", display_name,
                   "Room {} created by {}".format(room["roomId"],
                                                  user.get("first_name") or "member"),
                   kind="system")


# ---------------------------------------------------------------------------
# /play and /vplay
# ---------------------------------------------------------------------------

def cmd_play(state, chat, user, args, media_type: str):
    chat_id = chat.get("id")
    query = (args or "").strip()
    usage = "Usage: /play <song name>" if media_type == "audio" else "Usage: /vplay <video name>"
    if not query:
        send_reply(state, chat_id, escape(usage))
        return
    if len(query) > MAX_QUERY_LENGTH:
        send_reply(state, chat_id,
                   "That query is too long ({} characters max).".format(MAX_QUERY_LENGTH))
        return
    if chat.get("type") not in GROUP_TYPES:
        send_reply(state, chat_id,
                   "Media plays inside group rooms. Add me to a group, create a "
                   "room with /room, then use {} there.".format(
                       "/play" if media_type == "audio" else "/vplay"))
        return
    if state.rooms is None:
        send_reply(state, chat_id,
                   "The bot's database is not configured yet. "
                   "Ask the admin to check the Firebase settings.")
        return
    if state.yt is None:
        send_reply(state, chat_id,
                   "YouTube search is not configured on the server "
                   "(missing YOUTUBE_API_KEY). Ask the bot admin to enable it.")
        return
    if not state.limiter.allow("search:{}".format(user["id"]),
                               state.cfg.rate_limit_searches_per_minute, 60):
        send_reply(state, chat_id,
                   "You are searching too fast. Please wait a moment and try again.")
        return

    rooms, err = _active_rooms(state, chat)
    if err:
        return
    if not rooms:
        send_reply(state, chat_id,
                   "There is no active Blockroom in this group yet. Send /room first.")
        return
    if len(rooms) == 1:
        _present_results(state, chat_id, user, rooms[0], query, media_type)
        return

    # Several rooms live here: make the sender pick the target room first,
    # so media can never land in the wrong room.
    entry = PendingEntry(kind="pickroom", user_id=user["id"], chat_id=chat_id,
                         query=query, media_type=media_type,
                         room_ids=[room["roomId"] for room in rooms])
    token = state.pending.add(entry)
    send_reply(state, chat_id,
               "Multiple active rooms here. Which room should receive the media?",
               _pick_keyboard(token, entry.room_ids))


def _present_results(state, chat_id, user, room, query, media_type):
    try:
        results = state.yt.search_videos(query, max_results=5, require_embeddable=True)
    except YouTubeQuotaExceeded:
        send_reply(state, chat_id,
                   "The YouTube API quota is exhausted right now. Please try again later.")
        return
    except YouTubeConfigError:
        send_reply(state, chat_id,
                   "YouTube search is misconfigured on the server. "
                   "Ask the admin to check YOUTUBE_API_KEY.")
        return
    except YouTubeError as exc:
        log.warning("YouTube search failed: %s", exc)
        send_reply(state, chat_id,
                   "YouTube search failed just now. Please try again in a moment.")
        return

    if not results:
        send_reply(state, chat_id,
                   'No playable results for "{}". Try different keywords.'.format(escape(query)))
        return

    # /vplay shows thumbnails via a photo album (when the API returned any).
    if media_type == "video":
        media_items = []
        for index, result in enumerate(results, 1):
            if not result.thumbnail_url:
                continue
            caption = "{}. {} - {}".format(index, result.title, result.channel_title)
            media_items.append({
                "type": "photo",
                "media": result.thumbnail_url,
                "caption": caption[:1000],
            })
        if media_items:
            try:
                state.tg.send_media_group(chat_id, media_items)
            except TelegramAPIError as exc:
                log.warning("sendMediaGroup failed (non-fatal): %s", exc)

    entry = PendingEntry(kind="select", user_id=user["id"], chat_id=chat_id,
                         query=query, media_type=media_type, results=results,
                         room_id=room["roomId"])
    token = state.pending.add(entry)

    lines = [
        '<b>Results for "{}"</b> - room <code>{}</code>'.format(escape(query), escape(room["roomId"])),
        "",
    ]
    for index, result in enumerate(results, 1):
        lines.append("{}. {} - {}".format(index, escape(result.title),
                                          escape(result.channel_title)))
    lines += ["", "Pick one:"]
    sent = send_reply(state, chat_id, "\n".join(lines), _number_keyboard(token, len(results)))
    if sent and sent.get("message_id") is not None:
        entry.message_id = sent["message_id"]


# ---------------------------------------------------------------------------
# Inline button callbacks
# ---------------------------------------------------------------------------

def handle_callback_query(state, callback):
    user = callback.get("from") or {}
    user_id = user.get("id")
    callback_id = callback.get("id")
    data = callback.get("data") or ""

    if not callback_id or user_id is None:
        return

    def answer(text, alert=False):
        state.tg.answer_callback_query(callback_id, text=text, show_alert=alert)

    if not state.limiter.allow("cb:{}".format(user_id),
                               state.cfg.rate_limit_callbacks_per_minute, 60):
        answer("You are doing that too often. Slow down.")
        return

    parts = data.split("|")
    if len(parts) != 3 or parts[0] not in ("sel", "pick", "cxl"):
        answer("Unknown action.")
        return
    kind, token, index_raw = parts
    try:
        index = int(index_raw)
    except ValueError:
        answer("Invalid selection.")
        return

    entry = state.pending.get(token)
    if entry is None:
        answer("That selection has expired. Run the command again.")
        return
    if entry.user_id != user_id:
        answer("Only the person who ran the command can choose.", alert=True)
        return

    if kind == "cxl":
        state.pending.pop(token)
        answer("Canceled.")
        _edit_entry_message(state, entry, "Search canceled.")
        return

    if kind == "pick":
        if entry.kind != "pickroom":
            answer("Unexpected action.")
            return
        if index < 0 or index >= len(entry.room_ids):
            answer("Invalid room choice.")
            return
        room = _require_active_room(state, entry.room_ids[index], entry.chat_id, answer)
        if room is None:
            return
        state.pending.pop(token)
        if entry.action in ("skip", "stop"):
            # Re-verify ownership at action time - the picker only listed the
            # sender's own rooms, but the stored owner is re-checked anyway.
            if not is_room_owner(room, user_id):
                answer("Only the room owner can do that.", alert=True)
                return
            answer("Working...")
            if entry.action == "skip":
                _perform_skip(state, entry.chat_id, room)
            else:
                _perform_stop(state, entry.chat_id, room)
            return
        answer("Loading results...")
        _present_results(state, entry.chat_id, user, room, entry.query, entry.media_type)
        return

    # kind == "sel"
    if entry.kind != "select":
        answer("Unexpected action.")
        return
    if index < 0 or index >= len(entry.results):
        answer("Invalid choice.")
        return
    room = _require_active_room(state, entry.room_id, entry.chat_id, answer)
    if room is None:
        return
    video = entry.results[index]

    requester = {
        "telegramId": user_id,
        "displayName": user.get("first_name") or "",
        "username": user.get("username"),
    }
    try:
        current = state.media.get_current(room["roomId"])
        room_is_active = bool(current) and \
            current.get("playbackState") in ACTIVE_PLAYBACK_STATES
        if room_is_active:
            # Something is already on the TV: line the request up instead of
            # replacing what is playing.  /skip promotes it later.
            _, position = state.media.add_to_queue(
                room, video, entry.media_type, requester,
                source_query=entry.query)
        else:
            state.media.set_from_video(room, video, entry.media_type, requester,
                                       source_query=entry.query)
    except DatabaseError:
        log.exception("Media write failed for room %s", room.get("roomId"))
        answer("Could not save the media state. Please try again.")
        send_error(state, entry.chat_id,
                   "The database rejected that selection. Please try again.")
        return
    except Exception:
        log.exception("Unexpected media failure for room %s", room.get("roomId"))
        answer("Could not save the media state. Please try again.")
        return

    state.pending.pop(token)
    answer("Saved to room {}.".format(room["roomId"]))
    if room_is_active:
        summary = ("<b>Queued for room</b> <code>{}</code> - position {}\n"
                   "{} - {}\n"
                   "Requested by {}\n"
                   "The room owner can send /skip to move to this item.").format(
                       escape(room["roomId"]), position,
                       escape(video.title), escape(video.channel_title),
                       escape(user.get("first_name") or "member"))
    else:
        summary = ("<b>Saved to room</b> <code>{}</code>\n"
                   "{} - {}\n"
                   "Type: {} - Requested by {}\n"
                   "The in-game TV picks this up from Firebase.").format(
                       escape(room["roomId"]),
                       escape(video.title), escape(video.channel_title),
                       escape(entry.media_type),
                       escape(user.get("first_name") or "member"))
    _edit_entry_message(state, entry, summary)
    _, display_name = bot_identity(state)
    push_room_chat(state, room["roomId"], "bot", display_name,
                   "{}: {} ({})".format(
                       "Queued" if room_is_active else "Now selected",
                       video.title, entry.media_type),
                   kind="media")


def _require_active_room(state, room_id, chat_id, answer):
    try:
        room = state.rooms.get_room(room_id)
    except DatabaseError:
        log.exception("Room lookup failed for %s", room_id)
        answer("Database error. Please try again.")
        return None
    if not room:
        answer("That room no longer exists.")
        return None
    if room.get("status") != "active":
        answer("That room is closed.")
        return None
    if str(room.get("telegramGroupId")) != str(chat_id):
        answer("That room belongs to a different group.")
        return None
    return room


def _edit_entry_message(state, entry, text):
    if entry.message_id is None or entry.chat_id is None:
        return
    try:
        state.tg.edit_message_text(entry.chat_id, entry.message_id, text,
                                   reply_markup={"inline_keyboard": []})
    except TelegramAPIError as exc:
        log.info("Could not edit message after selection: %s", exc)


# ---------------------------------------------------------------------------
# /skip and /stop - owner-only TV controls
# ---------------------------------------------------------------------------

def _resolve_control_room(state, chat, user, action: str):
    """Find the sender's own active room in this group for /skip or /stop.

    Returns the room dict, or None after sending the user-facing guidance.
    Authorization is always against the stored ``ownerTelegramId`` (never
    client input), and the action re-verifies ownership against a fresh room
    read before anything is written.
    """
    chat_id = chat.get("id")
    if chat.get("type") not in GROUP_TYPES:
        send_reply(state, chat_id,
                   "Room TV controls work inside the Telegram group that owns the room.")
        return None
    if not state.limiter.allow("ctl:{}".format(user["id"]),
                               state.cfg.rate_limit_controls_per_minute, 60):
        send_reply(state, chat_id,
                   "You are sending commands too fast. Please wait a moment and try again.")
        return None
    rooms, err = _active_rooms(state, chat)
    if err:
        return None
    if not rooms:
        send_reply(state, chat_id,
                   "There is no active Blockroom in this group yet. Send /room first.")
        return None
    owned = [room for room in rooms if is_room_owner(room, user["id"])]
    if not owned:
        send_reply(state, chat_id, escape("Only the room owner can do that."))
        return None
    if len(owned) == 1:
        return owned[0]

    # The sender owns several rooms here: let them pick, then re-verify
    # ownership again when the button is clicked (see handle_callback_query).
    entry = PendingEntry(kind="pickroom", user_id=user["id"], chat_id=chat_id,
                         action=action, room_ids=[r["roomId"] for r in owned])
    token = state.pending.add(entry)
    send_reply(state, chat_id,
               "You own several active rooms here. Which one should be affected?",
               _pick_keyboard(token, entry.room_ids))
    return None


def cmd_skip(state, chat, user):
    room = _resolve_control_room(state, chat, user, "skip")
    if room is not None:
        _perform_skip(state, chat.get("id"), room)


def cmd_stop(state, chat, user):
    room = _resolve_control_room(state, chat, user, "stop")
    if room is not None:
        _perform_stop(state, chat.get("id"), room)


def _perform_skip(state, chat_id, room):
    """Advance this room's queue: promote the next item, or mark the TV stopped.

    Writes stay inside rooms/{roomId}/media/ and the replies never claim
    actual playback - the frontend applies the state when it syncs.
    """
    room_id = room["roomId"]
    try:
        next_item = state.media.pop_queue_head(room_id)
        had_media = False
        if next_item:
            state.media.set_current_from_queue_item(room, next_item)
        else:
            current = state.media.get_current(room_id)
            had_media = bool(current and current.get("videoId"))
            if had_media:
                state.media.mark_stopped(room_id)
    except DatabaseError:
        log.exception("Skip failed for room %s", room_id)
        send_reply(state, chat_id,
                   "The database is unreachable right now. Please try again soon.")
        return

    _, display_name = bot_identity(state)
    if next_item:
        requested = ((next_item.get("requestedBy") or {}).get("displayName")) or "member"
        send_reply(
            state, chat_id,
            "<b>Skipped.</b> Next for room <code>{}</code>:\n"
            "{} - {}\n"
            "Requested by {}\n"
            "The in-game TV picks this up from Firebase.".format(
                escape(room_id), escape(next_item.get("title") or "unknown"),
                escape(next_item.get("channelTitle") or ""),
                escape(requested)))
        push_room_chat(state, room_id, "bot", display_name,
                       "Owner skipped to: {}".format(
                           next_item.get("title") or "unknown"),
                       kind="media")
    elif had_media:
        send_reply(
            state, chat_id,
            "Nothing else is queued for room <code>{}</code>. The media state "
            "is marked stopped - the in-game TV ends playback when it reads "
            "the state.".format(escape(room_id)))
        push_room_chat(state, room_id, "bot", display_name,
                       "Owner skipped; queue empty, TV marked stopped",
                       kind="media")
    else:
        send_reply(
            state, chat_id,
            "Nothing is queued for room <code>{}</code> and there is no media "
            "state to skip.".format(escape(room_id)))


def _perform_stop(state, chat_id, room):
    """Stop exactly this room: mark its media/current stopped, clear its queue.

    Other rooms in this or any other group are never touched.
    """
    room_id = room["roomId"]
    try:
        current = state.media.get_current(room_id)
        had_media = bool(current and current.get("videoId"))
        if had_media:
            state.media.mark_stopped(room_id)
        state.media.clear_queue(room_id)
    except DatabaseError:
        log.exception("Stop failed for room %s", room_id)
        send_reply(state, chat_id,
                   "The database is unreachable right now. Please try again soon.")
        return

    if had_media:
        send_reply(
            state, chat_id,
            "Room <code>{}</code> media state is marked stopped and its queue "
            "was cleared. The in-game TV ends playback when it reads the "
            "state.".format(escape(room_id)))
    else:
        send_reply(
            state, chat_id,
            "Room <code>{}</code> has no media state in Firebase; its queue was "
            "cleared.".format(escape(room_id)))
    push_room_chat(state, room_id, "bot", bot_identity(state)[1],
                   "Owner stopped the room TV", kind="media")
