"""Telegram command handlers.

Exactly five commands: /room /play /vplay /stop /skip.
No inline "choose a song" pickers — a command acts immediately and the shared
Firestore media document drives every connected TV in real time.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import media
import rooms
from telegram_api import Telegram, build_join_link, escape, url_button, web_app_button
from timeutil import utc_now_iso

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, cfg, db, tg: Telegram):
        self.cfg = cfg
        self.db = db
        self.tg = tg

    # ------------------------------------------------------------------
    def handle(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            self.tg.answer_callback((update["callback_query"] or {}).get("id", ""))
            return
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if not text.startswith("/") or not chat or not user:
            return

        head, _, arg = text.partition(" ")
        command = head[1:].split("@", 1)[0].lower()
        arg = arg.strip()

        if command in ("start", "help"):
            self.cmd_start(chat, user)
        elif command == "room":
            self.cmd_room(chat, user)
        elif command == "play":
            self.cmd_media(chat, user, arg, want_video=False)
        elif command == "vplay":
            self.cmd_media(chat, user, arg, want_video=True)
        elif command in ("stop", "skip"):
            self.cmd_owner(chat, user, command)
        else:
            # any other /command: short hint instead of silence
            self.tg.send(chat.get("id"), self.welcome_text(chat, user, unknown=True))

    # ------------------------------------------------------------------
    def welcome_text(self, chat: dict, user: dict, unknown: bool = False) -> str:
        name = escape(user.get("first_name") or user.get("username") or "Player")
        head = "🤖 <b>Blockroom bot</b>" if not unknown else "Unknown command."
        in_group = (chat.get("type") or "private") != "private"
        where = (
            "Send <b>/room</b> in this group to create the room, then everyone taps "
            "<b>Join Blockroom</b>."
            if in_group
            else "Send <b>/room</b> to create your room, then tap <b>Join Blockroom</b>."
        )
        return (
            f"{head}\n\nHi <b>{name}</b>! I run the 3D Blockroom TV.\n{where}\n\n"
            "<b>Commands</b>\n"
            "/room — create / open the room\n"
            "/play <i>song name</i> — song with lyrics on the TV\n"
            "/vplay <i>video name</i> — video on the TV\n"
            "/skip — owner only\n"
            "/stop — owner only\n\n"
            f"<i>{'Add me to a group' if not in_group else 'Everyone in this group shares the same room and TV.'}</i>"
        )

    def cmd_start(self, chat: dict, user: dict) -> None:
        self.tg.send(chat.get("id"), self.welcome_text(chat, user))

    # ------------------------------------------------------------------
    def _room_for_chat(self, chat_id: int) -> Dict[str, Any] | None:
        return rooms.room_for_group(self.db, str(chat_id))

    def cmd_room(self, chat: Dict[str, Any], user: Dict[str, Any]) -> None:
        chat_id = chat["id"]
        existing = self._room_for_chat(chat_id)
        if existing:
            room = existing  # joining reuses the room — never create a second one
        else:
            role = "owner"
            _ = role
            room_id = rooms.create_room(
                self.db,
                chat_id=chat_id,
                chat_title=chat.get("title"),
                owner=user,
                capacity=self.cfg.room_capacity,
                now_iso=utc_now_iso(),
            )
            room = self.db.get_room(room_id) or {"roomId": room_id}
        rooms.ensure_member(self.db, room["roomId"], user, role="owner", now_iso=utc_now_iso())

        try:
            link = build_join_link(self.cfg.mini_app_url or "", room["roomId"])
        except ValueError:
            self.tg.send(chat_id, "Room created, but MINI_APP_URL is not configured on the server.")
            return

        keyboard = [[web_app_button("Join Blockroom", link)]] if self.cfg.mini_app_ready else [[url_button("Join Blockroom", link)]]
        created_by = escape(user.get("first_name") or user.get("username") or "Player")
        self.tg.send(
            chat_id,
            f"🎬 <b>Blockroom is live</b>\nCreated by <b>{created_by}</b>\n\n"
            f"Tap <b>Join Blockroom</b> to enter the same 3D room with everyone else.\n"
            f"<i>/play song</i> · <i>/vplay video</i> · owner: <i>/skip</i> <i>/stop</i>",
            keyboard,
        )

    # ------------------------------------------------------------------
    def cmd_media(self, chat: Dict[str, Any], user: Dict[str, Any], query: str, want_video: bool) -> None:
        chat_id = chat["id"]
        room = self._room_for_chat(chat_id)
        if room is None:
            self.tg.send(chat_id, "No room yet — send <b>/room</b> first.")
            return
        if not query:
            self.tg.send(chat_id, "Usage: <code>/{} song name</code>".format("vplay" if want_video else "play"))
            return
        if not self.cfg.youtube_ready:
            self.tg.send(chat_id, "YouTube search is not configured on the server.")
            return

        rooms.ensure_member(self.db, room["roomId"], user, role="member", now_iso=utc_now_iso())
        record, error = media.pick(
            self.db,
            room["roomId"],
            query=query,
            want_video=want_video,
            requester=user,
            api_key=self.cfg.youtube_api_key or "",
        )
        if error or not record:
            self.tg.send(chat_id, escape(error or "Nothing playable found."))
            return

        kind = "Video" if want_video else "Song (lyrics)"
        self.tg.send(
            chat_id,
            f"▶️ <b>{kind}</b> · {escape(record['title'])}\n"
            f"<i>{escape(record.get('channelTitle') or '')}</i>\nPlaying on the room TV now.",
        )

    # ------------------------------------------------------------------
    def cmd_owner(self, chat: Dict[str, Any], user: Dict[str, Any], command: str) -> None:
        chat_id = chat["id"]
        room = self._room_for_chat(chat_id)
        if room is None:
            self.tg.send(chat_id, "No room yet — send <b>/room</b> first.")
            return
        if not rooms.require_owned_room(self.db, room["roomId"], int(user["id"])):
            self.tg.send(chat_id, "Only the room owner can use that command.")
            return
        if command == "stop":
            media.stop(self.db, room["roomId"])
            self.tg.send(chat_id, "⏹ Stopped on every TV.")
        else:
            record = media.skip(self.db, room["roomId"])
            if record:
                self.tg.send(chat_id, f"⏭ Restarted · {escape(record.get('title') or '')}")
            else:
                self.tg.send(chat_id, "Nothing is playing.")
