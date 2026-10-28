"""/vplay: video search, thumbnails, embeddability filtering."""

from conftest import make_callback, make_message, make_update, post_update


def _keyboard_data(markup):
    return [button["callback_data"] for row in markup["inline_keyboard"]
            for button in row]


def test_vplay_shows_thumbnails_and_filters_non_embeddable(components, client, room):
    response = post_update(client, make_update(
        {"message": make_message(text="/vplay sunset driver", user_id=222)}))
    assert response.status_code == 200

    # Thumbnail album sent for video results (title + channel in captions).
    assert components["tg"].media_groups, "expected a thumbnail media group"
    captions = [item["caption"] for item in components["tg"].media_groups[0]["media"]]
    assert any("Sunset Driver" in caption for caption in captions)
    assert any("Neon Waves" in caption for caption in captions)

    # Non-embeddable result filtered out.
    sent = components["tg"].sent_messages[-1]
    assert "Night Drive Mix" not in sent["text"]
    assert len([d for d in _keyboard_data(sent["reply_markup"])
                if d.startswith("sel|")]) == 2


def test_vplay_selection_saves_video_metadata(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/vplay midnight drive", user_id=222)}))
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    select = [d for row in markup["inline_keyboard"] for d in
              [button["callback_data"] for button in row]
              if d.startswith("sel|")][0]

    post_update(client, make_update(
        {"callback_query": make_callback(select, user_id=222)}))

    media = components["db"].get_media_current(room)
    assert media["mediaType"] == "video"
    assert media["videoId"] == "vid_aaa111"
    assert media["title"] == "Sunset Driver"
    assert media["channelTitle"] == "Neon Waves"
    assert media["thumbnailUrl"] == "https://i.ytimg.com/vi/vid_aaa111/mqdefault.jpg"
    assert media["durationSeconds"] == 213
    assert media["requestedBy"]["displayName"] == "Bob"


def test_vplay_confirmation_does_not_claim_playback(components, client, room):
    """The bot must not report playback success just because an id was saved."""
    post_update(client, make_update(
        {"message": make_message(text="/vplay sunset driver", user_id=222)}))
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    select = [d for row in markup["inline_keyboard"] for d in
              [button["callback_data"] for button in row]
              if d.startswith("sel|")][0]
    post_update(client, make_update(
        {"callback_query": make_callback(select, user_id=222)}))

    for answer in components["tg"].answered_callbacks:
        assert "playing" not in (answer["text"] or "").lower()
    for edited in components["tg"].edited_messages:
        text = edited["text"].lower()
        assert "now playing" not in text
        # Honest phrasing: the frontend picks the state up from Firebase.
        assert "picks this up" in text
