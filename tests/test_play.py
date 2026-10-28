"""/play: auto-selection (no menus), room targeting, honest errors.

The bot must never show numbered result buttons: it picks the first
relevance-ranked PLAYABLE (embeddable) result itself and confirms briefly.
"""

import re

import pytest

from conftest import (WEBHOOK_SECRET, make_callback, make_message, make_update,
                      post_update, SECRET_HEADER)
from youtube_api import YouTubeQuotaExceeded


def _last_text(components):
    return components["tg"].sent_messages[-1]["text"]


def _pick_button_for(components, room_id):
    """The pick| callback_data whose button text names `room_id`."""
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    for row in markup["inline_keyboard"]:
        for button in row:
            if button.get("text") == "Room {}".format(room_id):
                return button["callback_data"]
    raise AssertionError("no pick button for room {}".format(room_id))


def _all_callback_data(components):
    out = []
    for message in components["tg"].sent_messages:
        markup = message.get("reply_markup") or {}
        for row in markup.get("inline_keyboard", []):
            for button in row:
                if "callback_data" in button:
                    out.append(button["callback_data"])
    return out


def test_play_requires_a_query(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/play", user_id=222)}))
    assert "Usage" in _last_text(components)
    assert components["yt"].queries == []


def test_play_without_room_sends_guidance(components, client):
    post_update(client, make_update(
        {"message": make_message(text="/play test song", user_id=222)}))
    assert "/room" in _last_text(components)
    assert components["yt"].queries == []


def test_play_rejected_in_private_chat(components, client):
    post_update(client, make_update(
        {"message": make_message(chat_id=42, chat_type="private",
                                 text="/play test", user_id=222)}))
    assert "group" in _last_text(components).lower()
    assert components["yt"].queries == []


def test_play_autoplays_best_result_without_any_menu(components, client, room):
    messages_before = len(components["tg"].sent_messages)
    response = post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    assert response.status_code == 200

    # One search, embeddability verified against video details.
    assert len(components["yt"].queries) == 1
    assert components["yt"].queries[0]["query"] == "sunset driver"

    # Short confirmation only - exactly one message, no buttons anywhere.
    assert len(components["tg"].sent_messages) == messages_before + 1
    assert "▶️" in _last_text(components)
    assert "Playing: Sunset Driver" in _last_text(components)
    assert _last_text(components).strip().count("\n") == 0  # single line
    assert _all_callback_data(components) == []             # no sel|/pick|/cxl|

    # The room's TV state was written with the full playback contract.
    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"
    assert media["roomId"] == room
    assert media["title"] == "Sunset Driver"
    assert media["mediaType"] == "audio"
    assert media["playbackState"] == "playing"
    assert media["positionSeconds"] == 0
    assert media["requestedBy"]["telegramId"] == 222
    assert media["embeddable"] is True
    assert media["playbackMethod"] == "youtube-iframe"  # what the TV must use
    assert media["startedAt"].endswith("Z")
    assert media["updatedAt"].endswith("Z")


def test_play_skips_non_embeddable_and_plays_best_playable(components, client, room):
    # "Night Drive Mix" (vid_ccc333) is not embeddable; the top-ranked result
    # above it in a custom order must be skipped in favour of a playable one.
    from youtube_api import VideoResult
    components["yt"].results = [
        VideoResult(video_id="vid_bad001", title="Blocked Track",
                    channel_title="NoEmbed", embeddable=False),
        VideoResult(video_id="vid_aaa111", title="Sunset Driver",
                    channel_title="Neon Waves", embeddable=True),
    ]
    post_update(client, make_update(
        {"message": make_message(text="/play blocked track", user_id=222)}))

    assert "Playing: Sunset Driver" in _last_text(components)
    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"


def test_play_with_only_unplayable_results_is_honest(components, client, room):
    from youtube_api import VideoResult
    components["yt"].results = [
        VideoResult(video_id="vid_bad001", title="Blocked Track",
                    channel_title="NoEmbed", embeddable=False),
    ]
    post_update(client, make_update(
        {"message": make_message(text="/play blocked track", user_id=222)}))

    sent = _last_text(components)
    assert "No playable result" in sent          # honest: nothing playable
    assert "Playing" not in sent                 # never a false confirmation
    assert components["db"].get_media_current(room) is None


def test_play_no_results_message(components, client, room):
    components["yt"].results = []
    post_update(client, make_update(
        {"message": make_message(text="/play obscure track", user_id=222)}))
    sent = _last_text(components)
    assert "No results" in sent
    assert "Playing" not in sent
    assert components["db"].get_media_current(room) is None


def test_play_while_playing_queues_without_menu(components, client, room):
    from conftest import play_now
    play_now(components, client, user_id=222)             # -> current, playing

    post_update(client, make_update(
        {"message": make_message(text="/play midnight drive", user_id=333)}))

    sent = _last_text(components)
    # The fake YouTube returns the same ranked list for any query, so the
    # auto-selected item for "midnight drive" is also the top playable match.
    assert "Queued: Sunset Driver (position 1)" in sent
    assert "Playing:" not in sent  # honest: the new item is NOT playing yet

    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"              # current untouched
    queue = components["db"].debug_media_queue(room)
    assert len(queue) == 1 and queue[0]["videoId"] == "vid_aaa111"
    assert queue[0]["playbackMethod"] == "youtube-iframe"


def test_room_media_isolation_between_rooms(components, client):
    # Two rooms in the same group -> /play must ask which room to target.
    # (Room targeting is the ONLY remaining inline button for /play.)
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    room_ids = components["db"].debug_room_ids()
    assert len(room_ids) == 2

    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    sent = components["tg"].sent_messages[-1]
    assert "Multiple active rooms" in sent["text"]
    data = _all_callback_data(components)
    assert len([d for d in data if d.startswith("pick|")]) == 2
    assert not [d for d in data if d.startswith("sel|")]  # never a result menu

    # Target the first room explicitly via its labelled pick button.
    target_room, other_room = room_ids[0], room_ids[1]
    post_update(client, make_update(
        {"callback_query": make_callback(_pick_button_for(components, target_room),
                                         user_id=222)}))

    media = components["db"].get_media_current(target_room)
    other = components["db"].get_media_current(other_room)
    assert media is not None and media["videoId"] == "vid_aaa111"
    assert other is None  # the other room's TV was never touched

    # The confirmation names the track, never the selection mechanics.
    assert "Playing: Sunset Driver" in _last_text(components)


def test_pick_buttons_denied_for_other_users(components, client):
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    room_ids = components["db"].debug_room_ids()

    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    pick = _pick_button_for(components, room_ids[0])

    post_update(client, make_update(
        {"callback_query": make_callback(pick, user_id=999)}))
    answers = components["tg"].answered_callbacks
    assert answers[-1]["show_alert"] is True
    assert "Only the person" in answers[-1]["text"]
    assert components["db"].get_media_current(room_ids[0]) is None
    assert components["db"].get_media_current(room_ids[1]) is None


def test_room_deleted_between_picker_and_click_is_handled(components, client):
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    room_ids = components["db"].debug_room_ids()

    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    pick = _pick_button_for(components, room_ids[1])
    components["db"]._rooms.pop(room_ids[1])  # room vanishes before the click

    response = post_update(client, make_update(
        {"callback_query": make_callback(pick, user_id=222)}))
    assert response.status_code == 200
    assert "no longer exists" in components["tg"].answered_callbacks[-1]["text"]


def test_result_selection_buttons_are_gone_entirely(components, client, room):
    """Regression guard: no sel|/cxl| button may ever be produced again."""
    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    assert _all_callback_data(components) == []
    post_update(client, make_update(
        {"message": make_message(text="/vplay sunset driver", user_id=222)}))
    assert _all_callback_data(components) == []


def test_stale_or_malformed_callbacks_answered_safely(components, client, room):
    for payload in ("sel|deadbeefdeadbeef|0", "pick|deadbeefdeadbeef|0",
                    "cxl|deadbeefdeadbeef|0", "nonsense"):
        response = post_update(client, make_update(
            {"callback_query": make_callback(payload, user_id=222)}))
        assert response.status_code == 200
    assert components["db"].get_media_current(room) is None


def test_youtube_quota_error_is_friendly(components, client, room):
    components["yt"].error = YouTubeQuotaExceeded("quota exhausted")
    response = post_update(client, make_update(
        {"message": make_message(text="/play anything", user_id=222)}))
    assert response.status_code == 200  # handled, no 500 to Telegram
    assert "quota" in _last_text(components).lower()
    assert "Playing" not in _last_text(components)
    assert components["db"].get_media_current(room) is None


def test_youtube_misconfiguration_message(components, app_factory, client, room):
    application = app_factory(services={"youtube": None})
    post_update(application.test_client(), make_update(
        {"message": make_message(text="/play anything", user_id=222)}))
    assert "YOUTUBE_API_KEY" in _last_text(components)


def test_search_rate_limit(components, app_factory, client, room):
    application = app_factory(cfg_overrides={"rate_limit_searches_per_minute": 1})
    c = application.test_client()
    post_update(c, make_update({"message": make_message(text="/play one", user_id=222)}))
    post_update(c, make_update({"message": make_message(text="/play two", user_id=222)}))
    assert "too fast" in _last_text(components)


def test_media_write_failure_is_honest(components, app_factory, client, room):
    from conftest import FailingAdapter
    failing = FailingAdapter(components["db"], fail_methods={"set_media_current"})
    application = app_factory(services={"db": failing})
    response = post_update(application.test_client(), make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    assert response.status_code == 200
    sent = components["tg"].sent_messages[-1]["text"]
    assert "database" in sent.lower()
    assert "Playing" not in sent  # no false confirmation on failure
