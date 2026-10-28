"""Room-scoped TV media state and per-room queue.

The bot only ever writes inside ``rooms/{roomId}/media/`` (merge/partial
updates) - never a global record, never another room's record:

* ``media/current``  - the "now selected" TV state the frontend subscribes to.
* ``media/queue``    - the ordered list of items waiting to be promoted by
  /skip.  Written and read only by the bot; the frontend may display it.

Actual playback happens in the game frontend: a Firestore write only records
the *requested* state, and the bot's messages never claim the video is
playing.  See FIREBASE_SCHEMA.md for the synchronisation protocol.
"""

import threading
from typing import Optional

from db import BaseAdapter
from timeutil import utc_now_iso
from youtube_api import VideoResult

VALID_MEDIA_TYPES = {"audio", "video"}
VALID_PLAYBACK_STATES = {"playing", "paused", "stopped", "buffering"}
ACTIVE_PLAYBACK_STATES = {"playing", "buffering"}
MAX_QUEUE_ITEMS = 50


class MediaError(Exception):
    pass


def queue_item_from_video(video: VideoResult, media_type: str, requester: dict,
                          source_query: Optional[str], room_id: str) -> dict:
    """Serialise one search result into the shared media-state field set."""
    now = utc_now_iso()
    return {
        "roomId": room_id,
        "videoId": video.video_id,
        "title": video.title,
        "channelTitle": video.channel_title,
        "thumbnailUrl": video.thumbnail_url,
        "durationSeconds": video.duration_seconds,
        "embeddable": bool(video.embeddable),
        "mediaType": media_type,
        "playbackState": "playing",
        "positionSeconds": 0,
        "startedAt": now,
        "updatedAt": now,
        "requestedBy": {
            "telegramId": int(requester.get("telegramId", 0) or 0),
            "displayName": requester.get("displayName") or "",
            "username": requester.get("username"),
        },
        "sourceQuery": source_query,
    }


class MediaService:
    def __init__(self, adapter: BaseAdapter):
        self._adapter = adapter
        # Serialises read-modify-write queue mutations per room within this
        # process (mirrors the per-process PendingStore / RateLimiter model).
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _room_lock(self, room_id: str) -> threading.Lock:
        with self._locks_guard:
            if room_id not in self._locks:
                self._locks[room_id] = threading.Lock()
            return self._locks[room_id]

    # -- current (now selected) ------------------------------------------------

    def get_current(self, room_id: str) -> Optional[dict]:
        return self._adapter.get_media_current(room_id)

    def set_from_video(self, room: dict, video, media_type: str,
                       requester: dict, source_query: Optional[str] = None) -> dict:
        """Write the shared TV state for exactly one room.

        ``video`` is a :class:`youtube_api.VideoResult`.  The write is a merge,
        so any extra fields the frontend keeps on the same document survive.
        Note this only *saves the selection* - actual playback happens in the
        game frontend, and the bot never claims otherwise.
        """
        room_id = room.get("roomId")
        if not room_id:
            raise MediaError("Room record has no roomId")
        if media_type not in VALID_MEDIA_TYPES:
            raise MediaError("Unsupported media type")
        record = queue_item_from_video(video, media_type, requester,
                                       source_query, room_id)
        self._adapter.set_media_current(room_id, record)
        return record

    def set_current_from_queue_item(self, room: dict, item: dict) -> dict:
        """Promote a queued item (already in the shared field set) to current."""
        room_id = (room or {}).get("roomId")
        if not room_id:
            raise MediaError("Room record has no roomId")
        record = dict(item)
        record["roomId"] = room_id
        now = utc_now_iso()
        record["playbackState"] = "playing"
        record["positionSeconds"] = 0
        record["startedAt"] = now
        record["updatedAt"] = now
        self._adapter.set_media_current(room_id, record)
        return record

    def mark_stopped(self, room_id: str) -> dict:
        """Mark the room's current media stopped (merge - keeps frontend keys)."""
        now = utc_now_iso()
        partial = {
            "playbackState": "stopped",
            "positionSeconds": 0,
            "updatedAt": now,
        }
        self._adapter.set_media_current(room_id, partial)
        return partial

    # -- queue (up next) ---------------------------------------------------------

    def get_queue(self, room_id: str) -> list:
        data = self._adapter.get_media_queue(room_id)
        items = (data or {}).get("items")
        return items if isinstance(items, list) else []

    def add_to_queue(self, room: dict, video, media_type: str, requester: dict,
                     source_query: Optional[str] = None) -> dict:
        """Append one item to the room's queue.  Returns (queue_doc, position).

        The write replaces the whole ``items`` list, so the per-room lock is
        what keeps concurrent /play commands from losing an entry.
        """
        room_id = room.get("roomId")
        if not room_id:
            raise MediaError("Room record has no roomId")
        if media_type not in VALID_MEDIA_TYPES:
            raise MediaError("Unsupported media type")
        item = queue_item_from_video(video, media_type, requester,
                                     source_query, room_id)
        item["queuedAt"] = item["startedAt"]
        with self._room_lock(room_id):
            items = self.get_queue(room_id)
            if len(items) >= MAX_QUEUE_ITEMS:
                raise MediaError("The queue for this room is full")
            items.append(item)
            doc = {
                "roomId": room_id,
                "items": items,
                "updatedAt": utc_now_iso(),
            }
            self._adapter.set_media_queue(room_id, doc)
        return doc, len(items)

    def pop_queue_head(self, room_id: str) -> Optional[dict]:
        """Remove and return the next queued item for this room (or None)."""
        with self._room_lock(room_id):
            items = self.get_queue(room_id)
            if not items:
                return None
            head = items.pop(0)
            doc = {
                "roomId": room_id,
                "items": items,
                "updatedAt": utc_now_iso(),
            }
            self._adapter.set_media_queue(room_id, doc)
            return head

    def clear_queue(self, room_id: str) -> None:
        with self._room_lock(room_id):
            self._adapter.set_media_queue(room_id, {
                "roomId": room_id,
                "items": [],
                "updatedAt": utc_now_iso(),
            })
