"""/vplay: auto-selected playable video, no thumbnails album, honest errors.

/vplay must behave like /play (auto-select, no numbered menu, no thumbnail
album) and write the same TV playback contract to the room's media state.
"""

from conftest import make_message, make_update, post_update
from youtube_api import VideoResult


def _last_text(components):
    return components["tg"].sent_messages[-1]["text"]


def test_vplay_autoplays_without_thumbnails_or_menu(components, client, room):
    messages_before = len(components["tg"].sent_messages)
    response = post_update(client, make_update(
        {"message": make_message(text="/vplay sunset driver", user_id=222)}))
    assert response.status_code == 200

    # No thumbnail album - that was the old behaviour and it is gone.
    assert components["tg"].media_groups == []

    # One short confirmation message, no result list, no inline buttons.
    assert len(components["tg"].sent_messages) == messages_before + 1
    sent = _last_text(components)
    assert "▶️" in sent
    assert "Playing: Sunset Driver" in sent
    assert sent.strip().count("\n") == 0
    assert "Pick one" not in sent
    for message in components["tg"].sent_messages[messages_before:]:
        markup = message.get("reply_markup") or {}
        keyboard = markup.get("inline_keyboard")
        assert keyboard in (None, []), "media commands must never send buttons"

    # Non-embeddable "Night Drive Mix" must never be the pick.
    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"
    assert media["mediaType"] == "video"


def test_vplay_saves_full_video_metadata_for_the_tv(components, client, room):
    post_update(client, make_update(
        {"message": make_message(text="/vplay midnight drive", user_id=222,
                                 first_name="Bob", username="bob")}))

    media = components["db"].get_media_current(room)
    assert media["mediaType"] == "video"
    assert media["videoId"] == "vid_aaa111"      # first embeddable match
    assert media["title"] == "Sunset Driver"
    assert media["channelTitle"] == "Neon Waves"
    assert media["thumbnailUrl"] == "https://i.ytimg.com/vi/vid_aaa111/mqdefault.jpg"
    assert media["durationSeconds"] == 213
    assert media["embeddable"] is True
    assert media["playbackMethod"] == "youtube-iframe"
    assert media["playbackState"] == "playing"
    assert media["requestedBy"]["displayName"] == "Bob"
    assert media["requestedBy"]["telegramId"] == 222
    assert media["roomId"] == room


def test_vplay_unplayable_result_is_reported_honestly(components, client, room):
    components["yt"].results = [
        VideoResult(video_id="vid_bad001", title="Blocked Video",
                    channel_title="NoEmbed", embeddable=False),
    ]
    post_update(client, make_update(
        {"message": make_message(text="/vplay blocked video", user_id=222)}))

    sent = _last_text(components)
    assert "No playable result" in sent
    assert "Playing" not in sent                 # no false claim
    assert components["db"].get_media_current(room) is None


def test_vplay_no_results_is_reported_honestly(components, client, room):
    components["yt"].results = []
    post_update(client, make_update(
        {"message": make_message(text="/vplay nothing", user_id=222)}))
    sent = _last_text(components)
    assert "No results" in sent
    assert "Playing" not in sent
    assert components["db"].get_media_current(room) is None


def test_vplay_any_member_can_request(components, client, room):
    """Media requests are for every room member, not only the owner (111)."""
    post_update(client, make_update(
        {"message": make_message(text="/vplay sunset driver", user_id=444)}))
    assert "Playing: Sunset Driver" in _last_text(components)
    media = components["db"].get_media_current(room)
    assert media["videoId"] == "vid_aaa111"
    assert media["requestedBy"]["telegramId"] == 444
