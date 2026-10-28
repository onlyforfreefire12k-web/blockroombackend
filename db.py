"""Cloud Firestore access (Firebase Admin SDK).

Only Cloud Firestore is used. The document layout matches
``FIREBASE_SCHEMA.md`` and the game frontend:

    rooms/{roomId}
    rooms/{roomId}/members/{telegramUserId}
    rooms/{roomId}/media/current
    rooms/{roomId}/media/queue
    rooms/{roomId}/chat/{messageId}

The bot writes media and membership; the game only ever reads media.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import firebase_admin
from firebase_admin import credentials, firestore

log = logging.getLogger(__name__)


class DbError(RuntimeError):
    pass


def _init_app(project_id: str, sa_json: Optional[str], sa_path: Optional[str]):
    if firebase_admin._apps:  # already initialised (gunicorn preload)
        return firebase_admin.get_app()
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
    elif sa_path:
        cred = credentials.Certificate(sa_path)
    else:
        cred = credentials.ApplicationDefault()
    return firebase_admin.initialize_app(cred, {"projectId": project_id})


class FirestoreDb:
    def __init__(self, project_id: str, sa_json: Optional[str], sa_path: Optional[str], prefix: str = ""):
        _init_app(project_id, sa_json, sa_path)
        self.client = firestore.client()
        self.prefix = prefix.strip("/")

    # -- paths ---------------------------------------------------------
    def _rooms(self):
        return self.client.collection(f"{self.prefix}/{ 'rooms' }".lstrip("/") if self.prefix else "rooms")

    def room_ref(self, room_id: str):
        return self._rooms().document(room_id)

    def member_ref(self, room_id: str, user_id: str):
        return self.room_ref(room_id).collection("members").document(str(user_id))

    def media_ref(self, room_id: str):
        return self.room_ref(room_id).collection("media").document("current")

    def queue_ref(self, room_id: str):
        return self.room_ref(room_id).collection("media").document("queue")

    def chat_ref(self, room_id: str):
        return self.room_ref(room_id).collection("chat")

    # -- rooms ---------------------------------------------------------
    def create_room(self, room_id: str, data: Dict[str, Any]) -> bool:
        ref = self.room_ref(room_id)
        if ref.get().exists:
            return False
        ref.set(data)
        return True

    def list_rooms_by_group(self, group_id: str) -> List[str]:
        query = self._rooms().where("telegramGroupId", "==", str(group_id))
        return [doc.id for doc in query.stream()]

    def get_room(self, room_id: str) -> Optional[Dict[str, Any]]:
        snap = self.room_ref(room_id).get()
        if not snap.exists:
            return None
        out = snap.to_dict() or {}
        out.setdefault("roomId", room_id)
        return out

    def update_room(self, room_id: str, patch: Dict[str, Any]) -> None:
        self.room_ref(room_id).set(patch, merge=True)

    # -- members -------------------------------------------------------
    def upsert_member(self, room_id: str, user_id: str, data: Dict[str, Any]) -> None:
        self.member_ref(room_id, user_id).set(data, merge=True)

    def get_member(self, room_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        snap = self.member_ref(room_id, user_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def member_count(self, room_id: str) -> int:
        return sum(1 for _ in self.room_ref(room_id).collection("members").stream())

    # -- media ---------------------------------------------------------
    def set_media(self, room_id: str, data: Dict[str, Any]) -> None:
        self.media_ref(room_id).set(data, merge=True)

    def get_media(self, room_id: str) -> Optional[Dict[str, Any]]:
        snap = self.media_ref(room_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def get_queue(self, room_id: str) -> List[Dict[str, Any]]:
        snap = self.queue_ref(room_id).get()
        if not snap.exists:
            return []
        return list((snap.to_dict() or {}).get("items") or [])

    def set_queue(self, room_id: str, items: List[Dict[str, Any]], now_iso: str) -> None:
        self.queue_ref(room_id).set({"roomId": room_id, "items": items[:50], "updatedAt": now_iso})

    # -- chat ----------------------------------------------------------
    def push_chat(self, room_id: str, data: Dict[str, Any]) -> None:
        self.chat_ref(room_id).add(data)


class MemoryDb:
    """In-memory adapter used by the tests and local dev."""

    def __init__(self, *_a, **_kw):
        self.rooms: Dict[str, Dict[str, Any]] = {}
        self.members: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.media: Dict[str, Dict[str, Any]] = {}
        self.queues: Dict[str, List[Dict[str, Any]]] = {}
        self.chats: Dict[str, List[Dict[str, Any]]] = {}

    def create_room(self, room_id, data):
        if room_id in self.rooms:
            return False
        self.rooms[room_id] = dict(data)
        return True

    def list_rooms_by_group(self, group_id):
        return [rid for rid, data in self.rooms.items() if str(data.get("telegramGroupId")) == str(group_id)]

    def get_room(self, room_id):
        room = self.rooms.get(room_id)
        if room is None:
            return None
        out = dict(room)
        out.setdefault("roomId", room_id)
        return out

    def update_room(self, room_id, patch):
        self.rooms.setdefault(room_id, {}).update(patch)

    def upsert_member(self, room_id, user_id, data):
        self.members.setdefault(room_id, {}).setdefault(str(user_id), {}).update(data)

    def get_member(self, room_id, user_id):
        return self.members.get(room_id, {}).get(str(user_id))

    def member_count(self, room_id):
        return len(self.members.get(room_id, {}))

    def set_media(self, room_id, data):
        self.media.setdefault(room_id, {}).update(data)

    def get_media(self, room_id):
        return self.media.get(room_id)

    def get_queue(self, room_id):
        return list(self.queues.get(room_id, []))

    def set_queue(self, room_id, items, _now_iso):
        self.queues[room_id] = list(items[:50])

    def push_chat(self, room_id, data):
        self.chats.setdefault(room_id, []).append(dict(data))


def make_db(config):
    if config.firebase_ready:
        return FirestoreDb(
            config.firebase_project_id or "",
            config.firebase_service_account_json,
            config.firebase_service_account_path,
            config.firebase_collection_prefix,
        )
    log.warning("Firebase Admin credentials missing — using the in-memory adapter")
    return MemoryDb()
