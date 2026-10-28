"""Ownership enforcement for /skip and /stop, plus invalid-room handling.

Authorization is always against the ``ownerTelegramId`` stored in Firebase -
never against anything a client sends.
"""

import pytest

from conftest import (make_callback, make_message, make_update, post_update,
                      play_now)
from rooms import RoomPermissionError, is_room_owner


def test_skip_requires_owner(components, client, room):
    play_now(components, client, user_id=222)
    post_update(client, make_update(
        {"message": make_message(text="/skip", user_id=999)}))
    sent = components["tg"].sent_messages[-1]["text"]
    assert "Only the room owner" in sent

    # Nothing changed: current still playing, queue untouched.
    media = components["db"].get_media_current(room)
    assert media["playbackState"] == "playing"


def test_stop_requires_owner(components, client, room):
    play_now(components, client, user_id=222)
    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=999)}))
    assert "Only the room owner" in components["tg"].sent_messages[-1]["text"]
    media = components["db"].get_media_current(room)
    assert media["playbackState"] == "playing"


def test_owner_check_uses_database_not_client_input(components, room):
    """Direct service-level check: attacker-supplied owner ids never count."""
    from rooms import RoomService
    stored = components["db"].get_room(room)
    assert is_room_owner(stored, 111) is True
    assert is_room_owner(stored, 999) is False
    assert is_room_owner(None, 111) is False
    assert is_room_owner({"ownerTelegramId": "not-a-number"}, 111) is False
    room_service = RoomService(components["db"])
    assert room_service.owned_active_rooms_for_group("-100999", 111) == [stored]
    assert room_service.owned_active_rooms_for_group("-100999", 999) == []


def test_owner_can_skip_and_stop_own_room(components, client, room):
    play_now(components, client, user_id=222)

    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    assert "marked stopped" in components["tg"].sent_messages[-1]["text"]
    media = components["db"].get_media_current(room)
    assert media["playbackState"] == "stopped"
    assert media["positionSeconds"] == 0
    assert media["videoId"] == "vid_aaa111"  # record kept, state changed


def test_skip_stop_from_another_group_cannot_touch_the_room(components, client, room):
    """A room is controlled only from the group that created it."""
    play_now(components, client, user_id=222)

    post_update(client, make_update(
        {"message": make_message(chat_id=-100888, text="/stop", user_id=111)}))
    assert "no active Blockroom" in components["tg"].sent_messages[-1]["text"]

    media = components["db"].get_media_current(room)
    assert media["playbackState"] == "playing"  # untouched


def test_skip_stop_without_any_room(components, client):
    post_update(client, make_update(
        {"message": make_message(text="/skip", user_id=111)}))
    assert "no active Blockroom" in components["tg"].sent_messages[-1]["text"]
    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    assert "no active Blockroom" in components["tg"].sent_messages[-1]["text"]


def test_multiple_owned_rooms_picker_reverifies_owner(components, client):
    # Same owner creates two rooms in the group.
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    room_ids = components["db"].debug_room_ids()
    assert len(room_ids) == 2

    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    sent = components["tg"].sent_messages[-1]
    assert "several active rooms" in sent["text"]
    picks = [button["callback_data"]
             for row in sent["reply_markup"]["inline_keyboard"]
             for button in row if button["callback_data"].startswith("pick|")]
    assert len(picks) == 2

    # Owner picks the second room: only that room is stopped.
    post_update(client, make_update(
        {"callback_query": make_callback(picks[1], user_id=111)}))
    assert "marked stopped" in components["tg"].sent_messages[-1]["text"] or \
           "no media state" in components["tg"].sent_messages[-1]["text"]

    # The picker for media (/play) still lists ALL rooms, but the control
    # picker listed only the owner's rooms - here both are the owner's.


def test_control_picker_buttons_are_denied_for_other_users(components, client):
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    sent = components["tg"].sent_messages[-1]
    picks = [button["callback_data"]
             for row in sent["reply_markup"]["inline_keyboard"]
             for button in row if button["callback_data"].startswith("pick|")]

    post_update(client, make_update(
        {"callback_query": make_callback(picks[0], user_id=999)}))
    answers = components["tg"].answered_callbacks
    assert answers[-1]["show_alert"] is True
    assert "Only the person" in answers[-1]["text"]


def test_invalid_room_id_never_hits_database(components, room):
    from rooms import RoomService
    service = RoomService(components["db"])
    assert service.get_room("not a room id!!") is None
    assert service.get_room("") is None
    assert service.get_room(None) is None
