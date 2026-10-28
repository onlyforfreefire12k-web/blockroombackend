"""Real end-to-end checks - REQUIRE real credentials and are skipped by default.

Run with:  RUN_E2E_TESTS=true TELEGRAM_BOT_TOKEN=... YOUTUBE_API_KEY=... pytest tests/test_e2e.py

These hit the real Telegram/YouTube APIs (read-only, no messages are sent to
real chats).  They are separate from the mocked suite so CI stays hermetic.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E_TESTS") != "true",
    reason="Real-credential E2E tests are disabled; "
           "set RUN_E2E_TESTS=true plus real env vars to run them.",
)


def test_real_youtube_search():
    from youtube_api import YouTubeClient
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        pytest.fail("YOUTUBE_API_KEY must be set for E2E tests")
    results = YouTubeClient(key).search_videos("lofi beats", max_results=3)
    assert results, "expected at least one search result"
    assert all(result.video_id for result in results)


def test_real_telegram_get_me():
    from telegram_api import TelegramClient
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        pytest.fail("TELEGRAM_BOT_TOKEN must be set for E2E tests")
    me = TelegramClient(token).get_me(cached=False)
    assert me.get("is_bot") is True
    assert me.get("username")


def test_real_webhook_registration():
    """Confirms the deployed Render URL + secret are accepted by Telegram.

    Requires RENDER_EXTERNAL_URL and TELEGRAM_WEBHOOK_SECRET to be set and the
    service to be deployed.  Safe to re-run: setWebhook is idempotent.
    """
    from telegram_api import TelegramClient
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    url = os.environ.get("RENDER_EXTERNAL_URL")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not (token and url and secret):
        pytest.fail("TELEGRAM_BOT_TOKEN, RENDER_EXTERNAL_URL and "
                    "TELEGRAM_WEBHOOK_SECRET must be set for this E2E test")
    client = TelegramClient(token)
    assert client.set_webhook(url.rstrip("/") + "/webhook/telegram",
                              secret_token=secret) is True
