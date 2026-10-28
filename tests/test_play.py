"""/play: search presentation, room targeting, selection, errors."""

import re

import pytest

from conftest import (WEBHOOK_SECRET, make_callback, make_message, make_update,
                      post_update, SECRET_HEADER)
from youtube_api import YouTubeQuotaExceeded


def _room_of_last_message(components):
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    return markup["inline_keyboard"]


def _keyboard_data(markup):
    return [button["callback_data"] for row in markup for button in row]


def test_play_requires_a_query(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/play", user_id=222)}))
    assert "Usage" in components["tg"].sent_messages[-1]["text"]
    assert components["yt"].queries == []


def test_play_without_room_sends_guidance(components, client):
    post_update(client, make_update(
        {"message": make_message(text="/play test song", user_id=222)}))
    sent = components["tg"].sent_messages[-1]["text"]
    assert "/room" in sent
    assert components["yt"].queries == []


def test_play_rejected_in_private_chat(components, client):
    post_update(client, make_update(
        {"message": make_message(chat_id=42, chat_type="private",
                                 text="/play test", user_id=222)}))
    assert "group" in components["tg"].sent_messages[-1]["text"].lower()
    assert components["yt"].queries == []


def test_play_shows_results_with_selection_buttons(components, client, room):
    response = post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    assert response.status_code == 200

    # Non-embeddable "Night Drive Mix" is filtered out server-side.
    assert len(components["yt"].queries) == 1
    assert components["yt"].queries[0]["require_embeddable"] is True

    sent = components["tg"].sent_messages[-1]
    assert "Sunset Driver" in sent["text"]
    assert "Neon Waves" in sent["text"]
    assert "Night Drive Mix" not in sent["text"]

    data = _keyboard_data(_room_of_last_message(components))
    assert len([d for d in data if d.startswith("sel|")]) == 2
    assert any(d.startswith("cxl|") for d in data)


def test_play_selection_updates_only_that_room(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    data = _keyboard_data(_room_of_last_message(components))
    token = data[0].split("|")[1]

    response = post_update(client, make_update(
        {"callback_query": make_callback(data[0], user_id=222)}))
    assert response.status_code == 200

    # Telegram callback acknowledged with an honest confirmation.
    answers = components["tg"].answered_callbacks
    assert answers and "Saved to room" in answers[-1]["text"]

    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"
    assert media["roomId"] == room
    assert media["mediaType"] == "audio"
    assert media["playbackState"] == "playing"
    assert media["positionSeconds"] == 0
    assert media["requestedBy"]["telegramId"] == 222
    assert media["startedAt"].endswith("Z")
    assert media["updatedAt"].endswith("Z")
    assert media["embeddable"] is True

    # The results message was edited to confirm the selection.
    assert components["tg"].edited_messages
    assert "Saved to room" in components["tg"].edited_messages[-1]["text"]


def test_room_media_isolation_between_rooms(components, client):
    # Two rooms in the same group -> /play must ask which room to target.
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    room_ids = components["db"].debug_room_ids()
    assert len(room_ids) == 2

    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    pick_data = _keyboard_data(_room_of_last_message(components))
    picks = [d for d in pick_data if d.startswith("pick|")]
    assert len(picks) == 2

    # Pick the first room; the results message names the targeted room.
    post_update(client, make_update(
        {"callback_query": make_callback(picks[0], user_id=222)}))
    results = components["tg"].sent_messages[-1]["text"]
    target_room = re.search(r"room <code>([a-z0-9_-]+)</code>", results).group(1)
    other_room = next(rid for rid in room_ids if rid != target_room)

    sel_data = _keyboard_data(_room_of_last_message(components))
    first_sel = [d for d in sel_data if d.startswith("sel|")][0]

    post_update(client, make_update(
        {"callback_query": make_callback(first_sel, user_id=222)}))

    media = components["db"].get_media_current(target_room)
    other = components["db"].get_media_current(other_room)
    assert media is not None and media["videoId"] == "vid_aaa111"
    assert other is None  # the other room's TV was never touched


def test_selection_by_a_different_user_is_denied(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    data = _keyboard_data(_room_of_last_message(components))

    post_update(client, make_update(
        {"callback_query": make_callback(data[0], user_id=999)}))

    answers = components["tg"].answered_callbacks
    assert answers[-1]["show_alert"] is True
    assert "Only the person" in answers[-1]["text"]
    assert components["db"].get_media_current(room) is None


def test_expired_or_unknown_token_handled(components, client, room):
    response = post_update(client, make_update(
        {"callback_query": make_callback("sel|deadbeefdeadbeef|0", user_id=222)}))
    assert response.status_code == 200
    assert "expired" in components["tg"].answered_callbacks[-1]["text"].lower()
    assert components["db"].get_media_current(room) is None


def test_selection_for_deleted_room_handled(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    data = _keyboard_data(_room_of_last_message(components))
    token = data[0].split("|")[1]

    # Room disappears between search and click.
    components["db"]._rooms.pop(room)

    response = post_update(client, make_update(
        {"callback_query": make_callback("sel|{}|0".format(token), user_id=222)}))
    assert response.status_code == 200
    assert "no longer exists" in components["tg"].answered_callbacks[-1]["text"]


def test_no_results_message(components, client, room):
    components["yt"].results = []
    post_update(client, make_update(
        {"message": make_message(text="/play obscure track", user_id=222)}))
    assert "No playable results" in components["tg"].sent_messages[-1]["text"]


def test_youtube_quota_error_is_friendly(components, client, room):
    components["yt"].error = YouTubeQuotaExceeded("quota exhausted")
    response = post_update(client, make_update(
        {"message": make_message(text="/play anything", user_id=222)}))
    assert response.status_code == 200  # handled, no 500 to Telegram
    assert "quota" in components["tg"].sent_messages[-1]["text"].lower()


def test_youtube_misconfiguration_message(components, app_factory, client, room):
    application = app_factory(services={"youtube": None})
    post_update(application.test_client(), make_update(
        {"message": make_message(text="/play anything", user_id=222)}))
    sent = components["tg"].sent_messages[-1]["text"]
    assert "YOUTUBE_API_KEY" in sent


def test_search_rate_limit(components, app_factory, client, room):
    application = app_factory(cfg_overrides={"rate_limit_searches_per_minute": 1})
    c = application.test_client()
    post_update(c, make_update({"message": make_message(text="/play one", user_id=222)}))
    post_update(c, make_update({"message": make_message(text="/play two", user_id=222)}))
    assert "too fast" in components["tg"].sent_messages[-1]["text"]


def test_play_multiple_rooms_picker_mentions_room_owners(components, client):
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    post_update(client, make_update(
        {"message": make_message(text="/play anything", user_id=222)}))
    sent = components["tg"].sent_messages[-1]
    assert "Multiple active rooms" in sent["text"]
    assert sent["reply_markup"] is not None
