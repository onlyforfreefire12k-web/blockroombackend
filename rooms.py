"""Room lifecycle: creation, lookup and ownership.

Ownership rules enforced here:
* The owner recorded at creation time (from the authenticated Telegram update)
  is the single source of truth - never a client-supplied value.
* Every privileged operation (room-scoped media control such as /skip and
  /stop) re-reads the room and compares the stored ``ownerTelegramId``
  against the acting user's Telegram id, so one room's controls can never
  bleed into another room.
"""

import logging
import re
import secrets
from typing import Optional
from urllib.parse import quote, urlsplit

from db import BaseAdapter, DatabaseError, RoomIdCollision
from timeutil import utc_now_iso

log = logging.getLogger("blockroom.rooms")

# Written once at creation so the frontend always has baseline room styling.
# (The bot does not change styles after creation - no /style command exists.)
DEFAULT_ROOM_STYLE = {
    "theme": "midnight",
    "wallColor": "#232344",
    "floorColor": "#151530",
    "tvSize": "medium",
    "ambientLight": True,
}

# Unpredictable, unambiguous room ids (~50 bits of entropy).
ROOM_ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ROOM_ID_LENGTH = 10
ROOM_ID_RE = re.compile(r"^[a-z0-9_-]{6,32}$")


class RoomError(Exception):
    pass


class RoomConfigError(RoomError):
    pass


class RoomNotFound(RoomError):
    pass


class RoomPermissionError(RoomError):
    pass


def generate_room_id() -> str:
    return "".join(secrets.choice(ROOM_ID_ALPHABET) for _ in range(ROOM_ID_LENGTH))


def validate_room_id(room_id) -> bool:
    return isinstance(room_id, str) and bool(ROOM_ID_RE.match(room_id))


def is_room_owner(room: dict, telegram_id) -> bool:
    """Compare against the owner stored in Firebase - never client input."""
    if room is None:
        return False
    stored_owner = room.get("ownerTelegramId")
    try:
        return stored_owner is not None and int(stored_owner) == int(telegram_id)
    except (TypeError, ValueError):
        return False


def build_mini_app_link(mini_app_url: str, room_id: str) -> str:
    """Build a deep link that opens the Mini App with the room id attached.

    Two MINI_APP_URL shapes are supported (documented in README section 3):

    * ``https://t.me/<bot>/<app_short_name>``  -> ``?startapp=<roomId>``
      (Telegram's Mini App start-parameter mechanism)
    * any other ``https://`` Mini App URL      -> ``?tgWebAppStartParam=<roomId>``

    The frontend reads either value from ``Telegram.WebApp.initDataUnsafe.start_param``.
    """
    url = (mini_app_url or "").strip()
    if not url:
        raise RoomConfigError("MINI_APP_URL is not configured on the server")
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise RoomConfigError("MINI_APP_URL must be an https:// URL")
    path_parts = [part for part in parsed.path.split("/") if part]
    separator = "&" if parsed.query else "?"
    quoted = quote(room_id, safe="")
    if parsed.netloc.lower() == "t.me" and len(path_parts) >= 2:
        return "{}{}startapp={}".format(url, separator, quoted)
    return "{}{}tgWebAppStartParam={}".format(url, separator, quoted)


class RoomService:
    def __init__(self, adapter: BaseAdapter):
        self._adapter = adapter

    # -- creation ------------------------------------------------------------

    def create_room(self, owner: dict, chat: dict, update_id=None) -> dict:
        """Create a brand-new room; every invocation yields a separate room.

        A cryptographically random id is allocated with an atomic
        create-if-missing write, so concurrent /room commands can never
        overwrite each other or collide (on the negligible chance of a
        collision the id is simply regenerated, up to 5 attempts).
        """
        now = utc_now_iso()
        for _ in range(5):
            room_id = generate_room_id()
            payload = {
                "roomId": room_id,
                "status": "active",
                "ownerTelegramId": int(owner["id"]),
                "ownerUsername": owner.get("username"),
                "ownerDisplayName": owner.get("first_name") or "",
                "telegramGroupId": str(chat["id"]),
                "telegramChatTitle": chat.get("title"),
                "style": dict(DEFAULT_ROOM_STYLE),
                "createdAt": now,
                "createdVia": "telegram_command:/room",
                "createdByWebhookUpdateId": str(update_id) if update_id is not None else None,
            }
            try:
                self._adapter.create_room(room_id, payload)
            except RoomIdCollision:
                continue
            payload["roomId"] = room_id
            return payload
        raise RoomError("Could not allocate a unique room id; please retry.")

    # -- lookups ---------------------------------------------------------------

    def get_room(self, room_id) -> Optional[dict]:
        if not validate_room_id(room_id):
            return None
        return self._adapter.get_room(room_id)

    def require_room(self, room_id) -> dict:
        room = self.get_room(room_id)
        if not room:
            raise RoomNotFound("Room {} was not found.".format(room_id))
        return room

    def active_rooms_for_group(self, group_id) -> list:
        rooms = self._adapter.list_rooms_by_group(group_id)
        return [room for room in rooms if room.get("status") == "active"]

    # -- ownership ---------------------------------------------------------------

    def owned_active_rooms_for_group(self, group_id, owner_telegram_id) -> list:
        """Active rooms in a group whose stored owner is ``owner_telegram_id``."""
        return [room for room in self.active_rooms_for_group(group_id)
                if is_room_owner(room, owner_telegram_id)]

    # -- membership ------------------------------------------------------------

    def ensure_member(self, room_id: str, user: dict, role: str = "member",
                      verified_via: str = "telegram") -> None:
        """Best-effort membership write; failures never block room flows."""
        now = utc_now_iso()
        doc = {
            "userId": str(user["id"]),
            "telegramId": int(user["id"]),
            "displayName": user.get("first_name") or "",
            "username": user.get("username"),
            "role": role,
            "verifiedVia": verified_via,
            "joinedAt": now,
            "lastSeenAt": now,
        }
        try:
            self._adapter.set_room_member(room_id, str(user["id"]), doc)
        except DatabaseError as exc:
            log.warning("Could not persist member for room %s: %s", room_id, exc)
