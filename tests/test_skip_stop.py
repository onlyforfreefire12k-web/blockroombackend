"""/skip and /stop: queue advancement, owner authorization, room isolation.

Honesty checks are included: the bot's replies must never claim that media
"is playing" - a Firestore write only records the requested state.
"""

from conftest import (make_callback, make_message, make_update, post_update,
                      select_first_result)


def _queue(components, room):
    return components["db"].debug_media_queue(room)


def _pick_buttons(components):
    sent = components["tg"].sent_messages[-1]
    return [button["callback_data"]
            for row in sent["reply_markup"]["inline_keyboard"]
            for button in row if button["callback_data"].startswith("pick|")]


def _select_result(components, client, user_id, query, index=0):
    """Click result `index` (0-based) of the last /play presentation."""
    post_update(client, make_update(
        {"message": make_message(text="/play {}".format(query), user_id=user_id)}))
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    selects = [d for row in markup["inline_keyboard"] for d in
               [button["callback_data"] for button in row]
               if d.startswith("sel|")]
    post_update(client, make_update(
        {"callback_query": make_callback(selects[index], user_id=user_id)}))


def _rooms_by_owner(components):
    rooms = components["db"].debug_rooms()
    return {room["ownerTelegramId"]: rid for rid, room in rooms.items()}


def test_second_play_while_playing_is_queued_not_overwritten(components, client, room):
    select_first_result(components, client, user_id=222)  # -> current, playing

    # The fake YouTube returns the same list for any query; pick result #2
    # ("Midnight Drive (Live)", vid_bbb222) for the queued item.
    _select_result(components, client, 333, "midnight drive", index=1)

    # Current is unchanged; the new item is queued (first in line).
    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"
    assert media["playbackState"] == "playing"

    queue = _queue(components, room)
    assert len(queue) == 1
    assert queue[0]["videoId"] == "vid_bbb222"
    assert queue[0]["roomId"] == room

    # The confirmation edits the results message (honest phrasing, no
    # playback claims).
    edited = components["tg"].edited_messages[-1]["text"]
    assert "Queued for room" in edited
    assert "position 1" in edited
    assert "now playing" not in edited.lower()


def test_play_when_idle_sets_current_directly(components, client, room):
    # No current media: the first selection becomes the TV state right away.
    post_update(client, make_update(
        {"message": make_message(text="/play sunset driver", user_id=222)}))
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    select = [d for row in markup["inline_keyboard"] for d in
              [button["callback_data"] for button in row]
              if d.startswith("sel|")][0]
    post_update(client, make_update(
        {"callback_query": make_callback(select, user_id=222)}))

    assert _queue(components, room) == []
    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"
    assert "Saved to room" in components["tg"].edited_messages[-1]["text"]


def test_owner_skip_advances_the_queue(components, client, room):
    select_first_result(components, client, user_id=222)   # current: vid_aaa111
    _select_result(components, client, 333, "midnight drive", index=1)  # queued: vid_bbb222

    post_update(client, make_update(
        {"message": make_message(text="/skip", user_id=111)}))
    sent = components["tg"].sent_messages[-1]["text"]
    assert "Skipped" in sent
    assert "Midnight Drive" in sent
    # Honest phrasing - skip must not claim actual playback.
    assert "picks this up from Firebase" in sent
    assert "now playing" not in sent.lower()

    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_bbb222"
    assert media["playbackState"] == "playing"
    assert media["positionSeconds"] == 0
    assert media["requestedBy"]["telegramId"] == 333
    assert media["updatedAt"].endswith("Z")
    assert _queue(components, room) == []


def test_skip_with_empty_queue_marks_stopped(components, client, room):
    select_first_result(components, client, user_id=222)
    post_update(client, make_update(
        {"message": make_message(text="/skip", user_id=111)}))

    sent = components["tg"].sent_messages[-1]["text"]
    assert "marked stopped" in sent
    media = components["db"].get_media_current(room)
    assert media["playbackState"] == "stopped"
    assert media["positionSeconds"] == 0


def test_skip_without_any_media_state(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/skip", user_id=111)}))
    assert "Nothing is queued" in components["tg"].sent_messages[-1]["text"]
    assert components["db"].get_media_current(room) is None


def test_stop_clears_current_and_queue_for_that_room_only(components, client):
    # Two rooms owned by different people, each with its own media state.
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    owners = _rooms_by_owner(components)
    room_a, room_b = owners[111], owners[333]

    components["db"].set_media_current(room_a, {"videoId": "x1", "playbackState": "playing",
                                                "positionSeconds": 42, "roomId": room_a})
    components["db"].set_media_current(room_b, {"videoId": "x2", "playbackState": "playing",
                                                "positionSeconds": 7, "roomId": room_b})
    components["db"].set_media_queue(room_a, {"roomId": room_a, "items": [{"videoId": "q1"}],
                                              "updatedAt": "2026-01-01T00:00:00Z"})

    # Owner 111 has exactly one room here -> /stop acts on it directly.
    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    assert "marked stopped" in components["tg"].sent_messages[-1]["text"]

    media_a = components["db"].get_media_current(room_a)
    media_b = components["db"].get_media_current(room_b)
    assert media_a["playbackState"] == "stopped"
    assert media_a["positionSeconds"] == 0
    assert _queue(components, room_a) == []
    # The other room is untouched.
    assert media_b["playbackState"] == "playing"
    assert media_b["positionSeconds"] == 7
    assert media_b["videoId"] == "x2"


def test_skip_isolated_between_rooms(components, client):
    post_update(client, make_update({"message": make_message(text="/room", user_id=111)}))
    post_update(client, make_update({"message": make_message(text="/room", user_id=333)}))
    owners = _rooms_by_owner(components)
    room_a, room_b = owners[111], owners[333]

    components["db"].set_media_current(room_a, {"videoId": "a-now", "playbackState": "playing",
                                                "roomId": room_a})
    components["db"].set_media_queue(room_a, {"roomId": room_a, "items": [
        {"videoId": "a-next", "title": "A Next", "channelTitle": "C",
         "requestedBy": {"telegramId": 1, "displayName": "Q"}},
    ], "updatedAt": "2026-01-01T00:00:00Z"})
    components["db"].set_media_current(room_b, {"videoId": "b-now", "playbackState": "playing",
                                                "roomId": room_b})

    post_update(client, make_update(
        {"message": make_message(text="/skip", user_id=111)}))

    assert components["db"].get_media_current(room_a)["videoId"] == "a-next"
    assert components["db"].get_media_current(room_b)["videoId"] == "b-now"
    assert _queue(components, room_a) == []
    assert _queue(components, room_b) == []


def test_stop_without_media_state_reports_honestly(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    sent = components["tg"].sent_messages[-1]["text"]
    assert "no media state" in sent


def test_control_rate_limit(components, app_factory, room):
    application = app_factory(cfg_overrides={"rate_limit_controls_per_minute": 1})
    c = application.test_client()
    post_update(c, make_update({"message": make_message(text="/stop", user_id=111)}))
    post_update(c, make_update({"message": make_message(text="/skip", user_id=111)}))
    assert "too fast" in components["tg"].sent_messages[-1]["text"]


def test_skip_stop_database_outage_is_handled(components, app_factory, client, room):
    from conftest import FailingAdapter
    select_first_result(components, client, user_id=222)

    failing = FailingAdapter(components["db"], fail_methods={"get_media_current"})
    application = app_factory(services={"db": failing})
    response = post_update(application.test_client(), make_update(
        {"message": make_message(text="/stop", user_id=111)}))
    assert response.status_code == 200  # handled, no 500 to Telegram
    assert "database" in components["tg"].sent_messages[-1]["text"].lower()
