"""/room: unique rooms, ownership, join links, dedupe, rate limiting."""

from conftest import (MINI_APP_URL, WEBHOOK_SECRET, make_message, make_update,
                      post_update, SECRET_HEADER)


def _send_room(client, user_id=111, chat_id=-100999, text="/room"):
    return post_update(client, make_update(
        {"message": make_message(chat_id=chat_id, user_id=user_id, text=text)}))


def _join_links(components):
    links = []
    for message in components["tg"].sent_messages:
        markup = message.get("reply_markup") or {}
        for row in markup.get("inline_keyboard", []):
            for button in row:
                if button.get("url"):
                    links.append(button["url"])
    return links


def test_room_creates_unique_room_with_owner(components, client):
    response = _send_room(client)
    assert response.status_code == 200

    room_ids = components["db"].debug_room_ids()
    assert len(room_ids) == 1
    stored = components["db"].get_room(room_ids[0])
    assert stored["ownerTelegramId"] == 111          # command sender is owner
    assert stored["telegramGroupId"] == "-100999"    # group id saved
    assert stored["roomId"] == room_ids[0]           # internal id kept in Firebase
    assert stored["status"] == "active"
    assert stored["style"]["theme"] == "midnight"    # style defaults saved
    assert stored["createdAt"].endswith("Z")
    assert stored["createdVia"] == "telegram_command:/room"

    owner = components["db"].get_room_member(room_ids[0], 111)
    assert owner is not None and owner["role"] == "owner"

    # Compact user-facing message: heading + creator name only.
    sent = components["tg"].sent_messages[-1]
    assert "Join Blockroom" in sent["reply_markup"]["inline_keyboard"][0][0]["text"]
    assert "Blockroom Created" in sent["text"]
    assert "Created by: Alice" in sent["text"]
    # Short means short: no room id, no group details, no instructions.
    assert room_ids[0] not in sent["text"]
    assert "Test Group" not in sent["text"]
    assert "/skip" not in sent["text"] and "/stop" not in sent["text"]
    assert "Room ID" not in sent["text"] and "Owner:" not in sent["text"]
    lines = [line for line in sent["text"].splitlines() if line.strip()]
    assert len(lines) == 2  # heading + creator line

    # The join link is still room-specific (startapp=<roomId>).
    link = sent["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert "startapp={}".format(room_ids[0]) in link


def test_every_room_command_creates_a_separate_room(components, client):
    _send_room(client, user_id=111)
    _send_room(client, user_id=222)

    rooms = components["db"].debug_rooms()
    assert len(rooms) == 2
    by_owner = {room["ownerTelegramId"]: room for room in rooms.values()}
    first, second = by_owner[111], by_owner[222]
    assert first["roomId"] != second["roomId"]
    # Independent rooms: same default style, never shared state.
    assert first["style"] == second["style"]


def test_join_links_are_unique_and_room_specific(components, client):
    _send_room(client, user_id=111)
    _send_room(client, user_id=222)

    room_ids = components["db"].debug_room_ids()
    links = _join_links(components)
    assert len(links) == 2
    assert links[0] != links[1]
    assert links[0].startswith(MINI_APP_URL)
    for link, room_id in zip(links, sorted(room_ids)):
        matching = [l for l in links if "startapp={}".format(room_id) in l]
        assert len(matching) == 1  # each room id appears in exactly one link


def test_duplicate_webhook_delivery_creates_one_room(components, client):
    update = make_update({"message": make_message(text="/room")})
    first = post_update(client, update)
    second = post_update(client, update)  # Telegram retry, same update_id

    assert first.get_json()["ok"] is True
    assert second.get_json()["duplicate"] is True
    assert len(components["db"].debug_room_ids()) == 1


def test_room_rejected_in_private_chat(components, client):
    response = post_update(client, make_update(
        {"message": make_message(chat_id=555, chat_type="private", text="/room")}))
    assert response.status_code == 200
    assert components["db"].debug_room_ids() == []
    assert "group" in components["tg"].sent_messages[-1]["text"].lower()


def test_room_rate_limit(components, app_factory):
    application = app_factory(cfg_overrides={"rate_limit_rooms_per_hour": 2})
    c = application.test_client()
    _send_room(c, user_id=111)
    _send_room(c, user_id=111)
    _send_room(c, user_id=111)  # third attempt blocked

    assert len(components["db"].debug_room_ids()) == 2
    assert "too many rooms" in components["tg"].sent_messages[-1]["text"]


def test_room_fails_closed_without_mini_app_url(components, app_factory):
    application = app_factory(cfg_overrides={"mini_app_url": None})
    response = _send_room(application.test_client(), user_id=111)
    assert response.status_code == 200
    assert components["db"].debug_room_ids() == []  # no orphan rooms
    assert "MINI_APP_URL" in components["tg"].sent_messages[-1]["text"]


def test_room_creation_reports_database_outage(components, app_factory):
    from conftest import FailingAdapter
    failing = FailingAdapter(components["db"], fail_methods={"create_room"})
    application = app_factory(services={"db": failing})
    response = _send_room(application.test_client(), user_id=111)
    assert response.status_code == 200  # handled error, Telegram not retried
    assert "database" in components["tg"].sent_messages[-1]["text"].lower()


def test_atomic_dedupe_claim_is_one_shot(components):
    db = components["db"]
    assert db.mark_update_processed(424242) is True
    assert db.mark_update_processed(424242) is False
    assert db.is_update_processed(424242) is True


def test_channel_posts_are_ignored(components, client):
    update = make_update({"channel_post": {
        "message_id": 1, "chat": {"id": -100123, "type": "channel"},
        "text": "/room", "date": 1}})
    response = post_update(client, update)
    assert response.status_code == 200
    assert components["db"].debug_room_ids() == []
    assert components["tg"].sent_messages == []
