"""Database adapters for the Blockroom bot.

Three interchangeable adapters implement one interface:

* :class:`FirestoreAdapter`   - default backend (``FIREBASE_BACKEND=firestore``)
* :class:`RealtimeDbAdapter`  - for frontends built on Realtime Database
                                (``FIREBASE_BACKEND=rtdb`` + ``FIREBASE_RTDB_URL``)
* :class:`InMemoryAdapter`    - used by the automated test-suite; also handy
                                for smoke-testing without credentials

Both production adapters write to exactly the paths documented in
FIREBASE_SCHEMA.md.  Top-level collection names can be shifted with
``FIREBASE_COLLECTION_PREFIX`` (e.g. ``blockroom_`` produces
``blockroom_rooms/{roomId}``) so the bot can point at the collection names the
game frontend already uses without code changes.

Design rules:
* Writes are partial updates (``merge`` / ``update``) - fields created by the
  frontend that the bot does not know about are never overwritten.
* Room creation and webhook de-duplication use atomic "create if missing"
  operations so concurrent requests can never produce duplicates.
"""

import base64
import copy
import json
import logging
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from timeutil import utc_now_iso

log = logging.getLogger("blockroom.db")

# Logical paths (relative to the optional prefix).  See FIREBASE_SCHEMA.md.
ROOMS_PATH = "rooms"
MEMBERS_PATH = "members"
CHAT_PATH = "chat"
MEDIA_PATH = "media"
MEDIA_CURRENT_ID = "current"
MEDIA_QUEUE_ID = "queue"
PROCESSED_PATH = "processed_updates"


class DatabaseError(Exception):
    """Generic, secret-free database failure."""


class RoomIdCollision(DatabaseError):
    """A room with this id already exists (retry with a fresh id)."""


class BaseAdapter(ABC):
    """Interface shared by Firestore, Realtime Database and the test adapter."""

    @abstractmethod
    def create_room(self, room_id: str, data: dict) -> None:
        """Atomically create rooms/{room_id}.  Raise RoomIdCollision if taken."""

    @abstractmethod
    def get_room(self, room_id: str) -> Optional[dict]:
        """Return the room dict (with roomId) or None."""

    @abstractmethod
    def update_room(self, room_id: str, partial: dict) -> None:
        """Merge partial fields into the room document."""

    @abstractmethod
    def set_room_member(self, room_id: str, user_id: str, data: dict) -> None:
        """Merge rooms/{room_id}/members/{user_id}."""

    @abstractmethod
    def get_room_member(self, room_id: str, user_id: str) -> Optional[dict]:
        """Return the member doc or None."""

    @abstractmethod
    def list_rooms_by_group(self, group_id) -> List[dict]:
        """All rooms belonging to a Telegram group (any status)."""

    @abstractmethod
    def set_media_current(self, room_id: str, data: dict) -> None:
        """Merge rooms/{room_id}/media/current."""

    @abstractmethod
    def get_media_current(self, room_id: str) -> Optional[dict]:
        """Return the current media record or None."""

    @abstractmethod
    def set_media_queue(self, room_id: str, data: dict) -> None:
        """Merge rooms/{room_id}/media/queue."""

    @abstractmethod
    def get_media_queue(self, room_id: str) -> Optional[dict]:
        """Return the queue document or None."""

    @abstractmethod
    def push_chat_message(self, room_id: str, data: dict) -> str:
        """Append a chat message; returns the generated message id."""

    @abstractmethod
    def is_update_processed(self, update_id: int) -> bool:
        """True when this Telegram update id was already handled."""

    @abstractmethod
    def mark_update_processed(self, update_id: int) -> bool:
        """Atomically claim an update id.  True = claimed, False = already taken."""

    @abstractmethod
    def health_check(self) -> bool:
        """Cheap connectivity probe for /health."""


# ---------------------------------------------------------------------------
# In-memory adapter (tests / smoke runs)
# ---------------------------------------------------------------------------

class InMemoryAdapter(BaseAdapter):
    """Thread-safe in-memory implementation with production-like semantics."""

    def __init__(self):
        self._lock = threading.RLock()
        self._rooms: Dict[str, dict] = {}
        self._members: Dict[str, Dict[str, dict]] = {}
        self._media: Dict[str, dict] = {}
        self._media_queues: Dict[str, dict] = {}
        self._chat: Dict[str, List[dict]] = {}
        self._processed: Dict[str, str] = {}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _clone(value):
        return copy.deepcopy(value)

    def debug_room_ids(self):
        with self._lock:
            return sorted(self._rooms.keys())

    def debug_rooms(self):
        with self._lock:
            return {rid: self._clone(room) for rid, room in self._rooms.items()}

    # -- interface -----------------------------------------------------------
    def create_room(self, room_id, data):
        with self._lock:
            if room_id in self._rooms:
                raise RoomIdCollision(room_id)
            self._rooms[room_id] = self._clone(data)

    def get_room(self, room_id):
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                return None
            data = self._clone(room)
            data.setdefault("roomId", room_id)
            return data

    def update_room(self, room_id, partial):
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                raise DatabaseError("Room not found")
            room.update(self._clone(partial))

    def set_room_member(self, room_id, user_id, data):
        with self._lock:
            self._members.setdefault(room_id, {})[str(user_id)] = self._clone(data)

    def get_room_member(self, room_id, user_id):
        with self._lock:
            return self._clone(self._members.get(room_id, {}).get(str(user_id)))

    def list_rooms_by_group(self, group_id):
        with self._lock:
            wanted = str(group_id)
            out = []
            for rid, room in self._rooms.items():
                if str(room.get("telegramGroupId")) == wanted:
                    data = self._clone(room)
                    data.setdefault("roomId", rid)
                    out.append(data)
            return out

    def set_media_current(self, room_id, data):
        with self._lock:
            current = self._media.setdefault(room_id, {})
            current.update(self._clone(data))  # merge: preserves frontend fields

    def get_media_current(self, room_id):
        with self._lock:
            current = self._media.get(room_id)
            return self._clone(current) if current else None

    def set_media_queue(self, room_id, data):
        with self._lock:
            queue = self._media_queues.setdefault(room_id, {})
            queue.update(self._clone(data))  # merge: preserves frontend fields

    def get_media_queue(self, room_id):
        with self._lock:
            queue = self._media_queues.get(room_id)
            return self._clone(queue) if queue else None

    def debug_media_queue(self, room_id):
        """Test helper: the stored queue items for one room."""
        queue = self.get_media_queue(room_id)
        return (queue or {}).get("items") or []

    def push_chat_message(self, room_id, data):
        with self._lock:
            messages = self._chat.setdefault(room_id, [])
            message_id = "msg-{}".format(len(messages) + 1)
            doc = self._clone(data)
            doc["messageId"] = message_id
            messages.append(doc)
            return message_id

    def is_update_processed(self, update_id):
        with self._lock:
            return str(update_id) in self._processed

    def mark_update_processed(self, update_id):
        with self._lock:
            key = str(update_id)
            if key in self._processed:
                return False
            self._processed[key] = utc_now_iso()
            return True

    def health_check(self):
        return True


# ---------------------------------------------------------------------------
# Firestore adapter
# ---------------------------------------------------------------------------

class FirestoreAdapter(BaseAdapter):
    def __init__(self, project_id=None, service_account_json=None,
                 service_account_path=None, collection_prefix=""):
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore  # noqa: F401
        except ImportError as exc:
            raise DatabaseError(
                "firebase-admin is not installed; run pip install -r requirements.txt"
            ) from None

        cred = self._build_credentials(service_account_json, service_account_path)
        options = {}
        if project_id:
            options["projectId"] = project_id
        try:
            self._app = firebase_admin.initialize_app(cred, options)
        except ValueError:
            self._app = firebase_admin.get_app()  # already initialised
        self._db = firestore.client(self._app)
        self._rooms_collection = "{}{}".format(collection_prefix or "", ROOMS_PATH)
        try:
            from google.cloud.firestore_v1 import FieldFilter  # type: ignore
        except ImportError:  # older google-cloud-firestore
            FieldFilter = None  # type: ignore
        self._field_filter_cls = FieldFilter

    @staticmethod
    def _build_credentials(service_account_json, service_account_path):
        from firebase_admin import credentials

        if service_account_json:
            raw = service_account_json
            if not raw.lstrip().startswith("{"):
                try:
                    raw = base64.b64decode(raw).decode("utf-8")
                except Exception:
                    raise DatabaseError(
                        "FIREBASE_SERVICE_ACCOUNT_JSON is neither JSON nor base64"
                    ) from None
            try:
                info = json.loads(raw)
            except ValueError:
                raise DatabaseError(
                    "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON"
                ) from None
            try:
                return credentials.Certificate(info)
            except Exception:
                raise DatabaseError(
                    "Firebase service account payload is invalid"
                ) from None
        try:
            return credentials.Certificate(service_account_path)
        except Exception:
            raise DatabaseError(
                "Could not load the Firebase service account file"
            ) from None

    @staticmethod
    def _is_already_exists(exc) -> bool:
        return (
            exc.__class__.__name__ == "AlreadyExists"
            or getattr(exc, "code", None) == 6
            or "ALREADY_EXISTS" in str(exc).upper()
        )

    def _rooms(self):
        return self._db.collection(self._rooms_collection)

    # -- interface -----------------------------------------------------------
    def create_room(self, room_id, data):
        try:
            self._rooms().document(room_id).create(data)
        except Exception as exc:
            if self._is_already_exists(exc):
                raise RoomIdCollision(room_id) from None
            raise DatabaseError("Firestore room creation failed") from None

    def get_room(self, room_id):
        try:
            snap = self._rooms().document(room_id).get()
        except Exception:
            raise DatabaseError("Firestore read failed") from None
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data.setdefault("roomId", snap.id)
        return data

    def update_room(self, room_id, partial):
        try:
            self._rooms().document(room_id).set(partial, merge=True)
        except Exception:
            raise DatabaseError("Firestore update failed") from None

    def set_room_member(self, room_id, user_id, data):
        try:
            self._rooms().document(room_id).collection(MEMBERS_PATH) \
                .document(str(user_id)).set(data, merge=True)
        except Exception:
            raise DatabaseError("Firestore member write failed") from None

    def get_room_member(self, room_id, user_id):
        try:
            snap = self._rooms().document(room_id).collection(MEMBERS_PATH) \
                .document(str(user_id)).get()
        except Exception:
            raise DatabaseError("Firestore member read failed") from None
        return snap.to_dict() if snap.exists else None

    def list_rooms_by_group(self, group_id):
        query = self._rooms().where(
            filter=self._field_filter_cls("telegramGroupId", "==", str(group_id))
        ) if self._field_filter_cls is not None else self._rooms().where(
            "telegramGroupId", "==", str(group_id)
        )
        try:
            out = []
            for snap in query.stream():
                data = snap.to_dict() or {}
                data.setdefault("roomId", snap.id)
                out.append(data)
            return out
        except Exception:
            raise DatabaseError("Firestore query failed") from None

    def set_media_current(self, room_id, data):
        try:
            self._rooms().document(room_id).collection(MEDIA_PATH) \
                .document(MEDIA_CURRENT_ID).set(data, merge=True)
        except Exception:
            raise DatabaseError("Firestore media write failed") from None

    def get_media_current(self, room_id):
        try:
            snap = self._rooms().document(room_id).collection(MEDIA_PATH) \
                .document(MEDIA_CURRENT_ID).get()
        except Exception:
            raise DatabaseError("Firestore media read failed") from None
        return snap.to_dict() if snap.exists else None

    def set_media_queue(self, room_id, data):
        try:
            self._rooms().document(room_id).collection(MEDIA_PATH) \
                .document(MEDIA_QUEUE_ID).set(data, merge=True)
        except Exception:
            raise DatabaseError("Firestore media queue write failed") from None

    def get_media_queue(self, room_id):
        try:
            snap = self._rooms().document(room_id).collection(MEDIA_PATH) \
                .document(MEDIA_QUEUE_ID).get()
        except Exception:
            raise DatabaseError("Firestore media queue read failed") from None
        return snap.to_dict() if snap.exists else None

    def push_chat_message(self, room_id, data):
        try:
            doc_ref = self._rooms().document(room_id).collection(CHAT_PATH).document()
            doc = dict(data)
            doc["messageId"] = doc_ref.id
            doc_ref.set(doc)
            return doc_ref.id
        except Exception:
            raise DatabaseError("Firestore chat write failed") from None

    def is_update_processed(self, update_id):
        try:
            snap = self._db.collection(PROCESSED_PATH).document(str(update_id)).get()
        except Exception:
            raise DatabaseError("Firestore dedupe read failed") from None
        return snap.exists

    def mark_update_processed(self, update_id):
        try:
            self._db.collection(PROCESSED_PATH).document(str(update_id)).create(
                {"processedAt": utc_now_iso()}
            )
            return True
        except Exception as exc:
            if self._is_already_exists(exc):
                return False
            raise DatabaseError("Firestore dedupe write failed") from None

    def health_check(self):
        try:
            self._rooms().limit(1).get()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Realtime Database adapter
# ---------------------------------------------------------------------------

class RealtimeDbAdapter(BaseAdapter):
    def __init__(self, database_url, service_account_json=None,
                 service_account_path=None, project_id=None, collection_prefix=""):
        try:
            import firebase_admin
            from firebase_admin import credentials, db as rtdb  # noqa: F401
        except ImportError as exc:
            raise DatabaseError(
                "firebase-admin is not installed; run pip install -r requirements.txt"
            ) from None

        cred = FirestoreAdapter._build_credentials(service_account_json, service_account_path)
        options = {"databaseURL": database_url}
        if project_id:
            options["projectId"] = project_id
        try:
            self._app = firebase_admin.initialize_app(cred, options)
        except ValueError:
            self._app = firebase_admin.get_app()
        prefix = (collection_prefix or "").strip("/")
        root_path = "/" + prefix if prefix else "/"
        self._root = rtdb.reference(root_path, self._app)
        self._rtdb = rtdb
        self._collision_cls = type("_RtdbCollision", (Exception,), {})
        self._txn_abort_cls = getattr(rtdb, "TransactionAbortedError", None)

    def _transaction_claim(self, ref, payload):
        """Atomically create ref with payload; raise _RtdbCollision if taken."""

        def transform(current):
            if current not in (None, {}):
                raise self._collision_cls()
            return payload

        try:
            ref.transaction(transform)
        except Exception as exc:
            if isinstance(exc, self._collision_cls) or (
                self._txn_abort_cls is not None and isinstance(exc, self._txn_abort_cls)
            ):
                raise RoomIdCollision("key already exists") from None
            raise DatabaseError("Realtime Database transaction failed") from None

    # -- interface -----------------------------------------------------------
    def create_room(self, room_id, data):
        self._transaction_claim(self._root.child("{}/{}".format(ROOMS_PATH, room_id)), data)

    def get_room(self, room_id):
        try:
            payload = self._root.child("{}/{}".format(ROOMS_PATH, room_id)).get()
        except Exception:
            raise DatabaseError("Realtime Database read failed") from None
        if payload in (None, {}):
            return None
        data = dict(payload)
        data.setdefault("roomId", room_id)
        return data

    def update_room(self, room_id, partial):
        try:
            self._root.child("{}/{}".format(ROOMS_PATH, room_id)).update(partial)
        except Exception:
            raise DatabaseError("Realtime Database update failed") from None

    def set_room_member(self, room_id, user_id, data):
        try:
            self._root.child("{}/{}/{}/{}".format(
                ROOMS_PATH, room_id, MEMBERS_PATH, str(user_id))).update(data)
        except Exception:
            raise DatabaseError("Realtime Database member write failed") from None

    def get_room_member(self, room_id, user_id):
        try:
            data = self._root.child("{}/{}/{}/{}".format(
                ROOMS_PATH, room_id, MEMBERS_PATH, str(user_id))).get()
        except Exception:
            raise DatabaseError("Realtime Database member read failed") from None
        return data if data else None

    def list_rooms_by_group(self, group_id):
        try:
            data = self._root.child(ROOMS_PATH).order_by_child("telegramGroupId") \
                .equal_to(str(group_id)).get() or {}
        except Exception:
            raise DatabaseError("Realtime Database query failed") from None
        out = []
        for rid, payload in data.items():
            if isinstance(payload, dict):
                room = dict(payload)
                room.setdefault("roomId", rid)
                out.append(room)
        return out

    def set_media_current(self, room_id, data):
        try:
            self._root.child("{}/{}/{}/{}".format(
                ROOMS_PATH, room_id, MEDIA_PATH, MEDIA_CURRENT_ID)).update(data)
        except Exception:
            raise DatabaseError("Realtime Database media write failed") from None

    def get_media_current(self, room_id):
        try:
            data = self._root.child("{}/{}/{}/{}".format(
                ROOMS_PATH, room_id, MEDIA_PATH, MEDIA_CURRENT_ID)).get()
        except Exception:
            raise DatabaseError("Realtime Database media read failed") from None
        return data if data else None

    def set_media_queue(self, room_id, data):
        try:
            self._root.child("{}/{}/{}/{}".format(
                ROOMS_PATH, room_id, MEDIA_PATH, MEDIA_QUEUE_ID)).update(data)
        except Exception:
            raise DatabaseError("Realtime Database media queue write failed") from None

    def get_media_queue(self, room_id):
        try:
            data = self._root.child("{}/{}/{}/{}".format(
                ROOMS_PATH, room_id, MEDIA_PATH, MEDIA_QUEUE_ID)).get()
        except Exception:
            raise DatabaseError("Realtime Database media queue read failed") from None
        return data if data else None

    def push_chat_message(self, room_id, data):
        try:
            ref = self._root.child("{}/{}/{}".format(ROOMS_PATH, room_id, CHAT_PATH))
            key = ref.push().key
            if not key:
                raise DatabaseError("Realtime Database push failed") from None
            doc = dict(data)
            doc["messageId"] = key
            ref.child(key).set(doc)
            return key
        except DatabaseError:
            raise
        except Exception:
            raise DatabaseError("Realtime Database chat write failed") from None

    def is_update_processed(self, update_id):
        try:
            return bool(self._root.child("{}/{}".format(
                PROCESSED_PATH, str(update_id))).get())
        except Exception:
            raise DatabaseError("Realtime Database dedupe read failed") from None

    def mark_update_processed(self, update_id):
        try:
            self._transaction_claim(
                self._root.child("{}/{}".format(PROCESSED_PATH, str(update_id))),
                {"processedAt": utc_now_iso()},
            )
            return True
        except RoomIdCollision:
            return False
        except DatabaseError:
            raise

    def health_check(self):
        try:
            self._root.child(ROOMS_PATH).limit_to_first(1).get()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_adapter(cfg) -> BaseAdapter:
    """Build the adapter selected by cfg.firebase_backend."""
    if not cfg.firebase_credentials_available:
        raise DatabaseError(
            "Firebase credentials are not configured; set FIREBASE_SERVICE_ACCOUNT_JSON "
            "or FIREBASE_SERVICE_ACCOUNT_PATH"
        )
    backend = (cfg.firebase_backend or "firestore").lower()
    common = {
        "service_account_json": cfg.firebase_service_account_json,
        "service_account_path": cfg.firebase_service_account_path,
        "project_id": cfg.firebase_project_id,
        "collection_prefix": cfg.firebase_collection_prefix,
    }
    if backend == "firestore":
        return FirestoreAdapter(**common)
    if backend == "rtdb":
        if not cfg.firebase_rtdb_url:
            raise DatabaseError(
                "FIREBASE_RTDB_URL is required when FIREBASE_BACKEND=rtdb"
            )
        return RealtimeDbAdapter(database_url=cfg.firebase_rtdb_url, **common)
    raise DatabaseError(
        "Unsupported FIREBASE_BACKEND '{}'; use 'firestore' or 'rtdb'".format(backend)
    )
