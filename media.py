"""Shared TV media state — the exact fields the game frontend reads."""

from __future__ import annotations

from typing import Optional

from timeutil import utc_now_iso
from youtube_api import Video, search

PLAYBACK_METHOD = "youtube-iframe"
ACTIVE_STATES = {"playing", "buffering"}


def record_from_video(video: Video, media_type: str, requester: dict, query: str, room_id: str) -> dict:
    now = utc_now_iso()
    return {
        "roomId": room_id,
        "videoId": video.video_id,
        "title": video.title,
        "channelTitle": video.channel_title,
        "thumbnailUrl": video.thumbnail_url,
        "durationSeconds": video.duration_seconds,
        "embeddable": bool(video.embeddable),
        "mediaType": media_type,          # "audio" (/play) | "video" (/vplay)
        "playbackMethod": PLAYBACK_METHOD,
        "playbackState": "playing",
        "positionSeconds": 0,
        "startedAt": now,
        "updatedAt": now,
        "requestedBy": {
            "telegramId": int(requester.get("id", 0) or 0),
            "displayName": requester.get("first_name") or "Player",
            "username": requester.get("username"),
        },
        "sourceQuery": query,
    }


def pick(db, room_id: str, *, query: str, want_video: bool, requester: dict, api_key: str):
    """Search YouTube and write media/current. Returns (record, error)."""
    # /play -> lyrics version first, then plain audio; /vplay -> music video
    queries = [f"{query} lyrics"] if not want_video else [f"{query} video song"]
    if not want_video:
        queries.append(f"{query} audio")

    chosen: Optional[Video] = None
    for q in queries:
        results = search(api_key, q, want_video=want_video)
        if results:
            chosen = results[0]
            break
    if chosen is None:
        return None, "No playable result found. Try a different song name."

    media_type = "video" if want_video else "audio"
    record = record_from_video(chosen, media_type, requester, query, room_id)
    db.set_media(room_id, record)

    # queue is a single document; keep it short and bot-owned
    items = db.get_queue(room_id)
    db.set_queue(room_id, items[-49:] if items else [], utc_now_iso())
    return record, None


def skip(db, room_id: str):
    """Owner /skip: play media when current is active, otherwise restart it."""
    current = db.get_media(room_id) or {}
    if not current.get("videoId"):
        return None
    now = utc_now_iso()
    patch = {
        "playbackState": "playing",
        "positionSeconds": 0,
        "startedAt": now,
        "updatedAt": now,
    }
    db.set_media(room_id, patch)
    return {**current, **patch}


def stop(db, room_id: str):
    db.set_media(
        room_id,
        {
            "playbackState": "stopped",
            "positionSeconds": 0,
            "updatedAt": utc_now_iso(),
        },
    )
    db.set_queue(room_id, [], utc_now_iso())
