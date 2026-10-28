"""/start behaviour: kept only as the private-chat entry point to the Mini App.

/start is not a feature command - it lists no commands and stays silent when
MINI_APP_URL is not configured.
"""

from conftest import (MINI_APP_URL, WEBHOOK_SECRET, make_message, make_update,
                      post_update, SECRET_HEADER)


def test_start_sends_only_the_mini_app_button(components, client):
    response = post_update(client, make_update(
        {"message": make_message(chat_id=111, chat_type="private", text="/start")}))
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    messages = components["tg"].sent_messages
    assert len(messages) == 1
    sent = messages[0]
    assert sent["chat_id"] == 111
    assert "Open Blockroom" in sent["text"] or sent["reply_markup"]
    # No command list: /start must not advertise feature commands.
    for removed in ("/room", "/play", "/vplay", "/skip", "/stop",
                    "/lyrics", "/style", "/close", "/rooms", "/help"):
        assert removed not in sent["text"]
    keyboard = sent["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["web_app"]["url"] == MINI_APP_URL  # from env, not hardcoded


def test_start_without_mini_app_url_is_silent(components, app_factory):
    application = app_factory(cfg_overrides={"mini_app_url": None})
    response = post_update(application.test_client(), make_update(
        {"message": make_message(chat_id=222, chat_type="private", text="/start")}))
    assert response.status_code == 200
    assert components["tg"].sent_messages == []


def test_start_works_in_groups(components, client):
    response = post_update(client, make_update(
        {"message": make_message(text="/start")}))
    assert response.status_code == 200
    assert len(components["tg"].sent_messages) == 1


def test_unknown_and_removed_commands_are_ignored(components, client):
    for text in ("/frobnicate", "/lyrics song", "/style theme sunset",
                 "/close", "/rooms", "/help", "/pause", "/resume"):
        post_update(client, make_update({"message": make_message(text=text)}))
    assert components["tg"].sent_messages == []
    assert components["db"].debug_room_ids() == []


def test_command_addressed_to_another_bot_is_ignored(components, client):
    post_update(client, make_update(
        {"message": make_message(text="/room@SomeOtherBot")}))
    assert components["tg"].sent_messages == []


def test_non_command_text_is_ignored(components, client):
    post_update(client, make_update(
        {"message": make_message(text="hello everyone")}))
    assert components["tg"].sent_messages == []
