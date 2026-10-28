"""Adapter-level behaviour: merge semantics, collisions, dedupe, media queue."""

import pytest

from db import InMemoryAdapter, RoomIdCollision, DatabaseError
from media import MediaService
from rooms import DEFAULT_ROOM_STYLE, RoomService
from youtube_api import VideoResult


def _room_payload(room_id):
    return {"roomId": room_id, "status": "active", "telegramGroupId": "-1",
            "style": dict(DEFAULT_ROOM_STYLE)}


def test_create_room_collision_detected():
    adapter = InMemoryAdapter()
    adapter.create_room("roomxyz12", _room_payload("roomxyz12"))
    with pytest.raises(RoomIdCollision):
        adapter.create_room("roomxyz12", _room_payload("roomxyz12"))


def test_media_merge_preserves_frontend_fields():
    adapter = InMemoryAdapter()
    adapter.create_room("roomxyz12", _room_payload("roomxyz12"))
    # The frontend wrote its own field on the same document first:
    adapter.set_media_current("roomxyz12", {"frontendPlaybackHint": "keep-me"})
    # Now the bot saves a selection:
    service = MediaService(adapter)
    video = VideoResult(video_id="v1", title="T", channel_title="C")
    service.set_from_video({"roomId": "roomxyz12"}, video, "audio",
                           {"telegramId": 1, "displayName": "A"})
    stored = adapter.get_media_current("roomxyz12")
    assert stored["frontendPlaybackHint"] == "keep-me"  # not overwritten
    assert stored["videoId"] == "v1"


def test_media_rejects_unknown_type():
    service = MediaService(InMemoryAdapter())
    video = VideoResult(video_id="v1", title="T", channel_title="C")
    with pytest.raises(Exception):
        service.set_from_video({"roomId": "roomxyz12"}, video, "hologram", {})


def test_list_rooms_by_group_and_status_filtering():
    adapter = InMemoryAdapter()
    adapter.create_room("roomaaaa11", _room_payload("roomaaaa11"))
    adapter.create_room("roombbbb22", _room_payload("roombbbb22"))
    adapter.update_room("roombbbb22", {"status": "closed"})
    service = RoomService(adapter)
    active = service.active_rooms_for_group("-1")
    assert [room["roomId"] for room in active] == ["roomaaaa11"]


def test_generated_room_ids_match_validation_rule():
    from rooms import generate_room_id, validate_room_id
    for _ in range(50):
        room_id = generate_room_id()
        assert validate_room_id(room_id)


# ---------------------------------------------------------------------------
# Media queue adapter semantics
# ---------------------------------------------------------------------------

def test_media_queue_round_trip_and_merge():
    adapter = InMemoryAdapter()
    adapter.create_room("roomxyz12", _room_payload("roomxyz12"))
    adapter.set_media_queue("roomxyz12", {"roomId": "roomxyz12",
                                          "items": [{"videoId": "q1"}],
                                          "updatedAt": "2026-01-01T00:00:00Z"})
    stored = adapter.get_media_queue("roomxyz12")
    assert [item["videoId"] for item in stored["items"]] == ["q1"]

    # Replacing the list replaces the whole queue (merge keeps other keys).
    adapter.set_media_queue("roomxyz12", {"items": [{"videoId": "q2"},
                                                     {"videoId": "q3"}]})
    stored = adapter.get_media_queue("roomxyz12")
    assert [item["videoId"] for item in stored["items"]] == ["q2", "q3"]
    assert stored["roomId"] == "roomxyz12"


def test_media_queue_is_room_scoped():
    adapter = InMemoryAdapter()
    adapter.create_room("roomaaaa11", _room_payload("roomaaaa11"))
    adapter.create_room("roombbbb22", _room_payload("roombbbb22"))
    adapter.set_media_queue("roomaaaa11", {"items": [{"videoId": "q1"}]})
    assert adapter.get_media_queue("roombbbb22") is None
    assert adapter.debug_media_queue("roombbbb22") == []


def test_media_queue_missing_room_returns_none():
    adapter = InMemoryAdapter()
    assert adapter.get_media_queue("nothere1") is None


def test_service_queue_pop_and_clear_are_fifo():
    adapter = InMemoryAdapter()
    adapter.create_room("roomxyz12", _room_payload("roomxyz12"))
    service = MediaService(adapter)
    video1 = VideoResult(video_id="v1", title="T1", channel_title="C")
    video2 = VideoResult(video_id="v2", title="T2", channel_title="C")
    service.add_to_queue({"roomId": "roomxyz12"}, video1, "audio",
                         {"telegramId": 1, "displayName": "A"})
    service.add_to_queue({"roomId": "roomxyz12"}, video2, "audio",
                         {"telegramId": 2, "displayName": "B"})
    head = service.pop_queue_head("roomxyz12")
    assert head["videoId"] == "v1"
    assert service.pop_queue_head("roomxyz12")["videoId"] == "v2"
    assert service.pop_queue_head("roomxyz12") is None

    service.add_to_queue({"roomId": "roomxyz12"}, video1, "audio",
                         {"telegramId": 1, "displayName": "A"})
    service.clear_queue("roomxyz12")
    assert service.pop_queue_head("roomxyz12") is None


def test_mark_stopped_merges_and_keeps_frontend_fields():
    adapter = InMemoryAdapter()
    adapter.create_room("roomxyz12", _room_payload("roomxyz12"))
    adapter.set_media_current("roomxyz12", {"videoId": "v1",
                                             "playbackState": "playing",
                                             "frontendPlaybackHint": "keep-me"})
    service = MediaService(adapter)
    service.mark_stopped("roomxyz12")
    stored = adapter.get_media_current("roomxyz12")
    assert stored["playbackState"] == "stopped"
    assert stored["positionSeconds"] == 0
    assert stored["frontendPlaybackHint"] == "keep-me"  # not overwritten
    assert stored["videoId"] == "v1"
