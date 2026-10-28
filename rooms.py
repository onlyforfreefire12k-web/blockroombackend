"""Room creation, membership and one-room-per-telegram-chat bookkeeping."""

from __future__ import annotations

import re
import secrets
from typing import Optional

ROOM_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ROOM_ID_LENGTH = 10
ROOM_ID_RE = re.compile(r"^[a-z0-9_-]{6,32}$")


def generate_room_id() -> str:
    return "".join(secrets.choice(ROOM_ALPHABET) for _ in range(ROOM_ID_LENGTH))


def valid_room_id(room_id: str) -> bool:
    return bool(ROOM_ID_RE.match(room_id or ""))


def create_room(db, *, chat_id, chat_title, owner, theme=0, capacity=12, now_iso=None):
    """Create exactly one room for the group and return its id."""
    room_id = generate_room_id()
    payload = {
        "roomId": room_id,
        "status": "active",
        "ownerTelegramId": int(owner["id"]),
        "ownerUsername": owner.get("username"),
        "ownerDisplayName": owner.get("first_name") or "Owner",
        "telegramGroupId": str(chat_id),
        "telegramChatTitle": chat_title,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "createdVia": "telegram_command:/room",
        "capacity": capacity,
        "style": {"theme": theme, "scene": "blockroom"},
    }
    if not db.create_room(room_id, payload):
        raise RuntimeError("room id collision, retry")
    ensure_member(db, room_id, owner, role="owner", now_iso=now_iso)
    return room_id


def ensure_member(db, room_id: str, user: dict, role: str = "member", now_iso=None):
    db.upsert_member(
        room_id,
        str(user["id"]),
        {
            "userId": str(user["id"]),
            "telegramId": int(user["id"]),
            "displayName": user.get("first_name") or user.get("username") or "Player",
            "username": user.get("username"),
            "role": role,
            "lastSeenAt": now_iso,
            **({"joinedAt": now_iso} if now_iso else {}),
        },
    )


def active_room_for_chat(db, chat_id: int) -> Optional[dict]:
    """The room the bot created for this group (one active room per chat)."""
    for room_id in db.list_rooms_by_group(str(chat_id)) if hasattr(db, "list_rooms_by_group") else []:
        room = db.get_room(room_id)
        if room and room.get("status", "active") == "active":
            return room
    return None


def require_owned_room(db, room_id: str, user_id: int) -> Optional[dict]:
    room = db.get_room(room_id)
    if not room:
        return None
    if int(room.get("ownerTelegramId", 0) or 0) != int(user_id):
        return None
    return room


def room_for_group(db, group_id: str) -> Optional[dict]:
    """Look up the newest active room for a Telegram group id."""
    rooms = []
    if hasattr(db, "list_rooms_by_group"):
        rooms = db.list_rooms_by_group(group_id)
    else:  # MemoryDb fallback
        rooms = [r for r, v in getattr(db, "rooms", {}).items() if str(v.get("telegramGroupId")) == str(group_id)]
    active = [r for r in rooms if (db.get_room(r) or {}).get("status", "active") == "active"]
    if not active:
        return None
    return db.get_room(sorted(active)[-1])
