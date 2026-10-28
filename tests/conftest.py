"""Shared fixtures: fake Telegram/YouTube clients + in-memory Firebase.

All tests here are fully mocked - no network, no real Firebase, no real
Telegram.  Real-credential end-to-end checks live in test_e2e.py and are
skipped unless RUN_E2E_TESTS=true.
"""

import hashlib
import hmac
import itertools
import json
import time
import urllib.parse

import pytest

import live
from config import Config
from db import InMemoryAdapter
from telegram_api import TelegramClient
from youtube_api import VideoResult, YouTubeClient

BOT_TOKEN = "123456:ABC-TEST-TOKEN"
WEBHOOK_SECRET = "test-webhook-secret-0123456789"
MINI_APP_URL = "https://t.me/TestRoomBot/blockroom"
YOUTUBE_KEY = "YT-TEST-KEY"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

_update_ids = itertools.count(5000)
_message_ids = itertools.count(1)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class SpyTelegram(TelegramClient):
    """Records every outbound call; never touches the network."""

    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []
        self.answered_callbacks = []
        self.media_groups = []
        self.webhook_calls = []
        self.chat_member_calls = []
        self.get_chat_member_status = "member"
        self._me = {"id": 42, "is_bot": True,
                    "first_name": "Blockroom Bot", "username": "TestRoomBot"}

    def send_message(self, chat_id, text, reply_markup=None, disable_preview=True):
        self.sent_messages.append({
            "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
        })
        return {"message_id": 1000 + len(self.sent_messages), "chat": {"id": chat_id}}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited_messages.append({
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "reply_markup": reply_markup,
        })
        return {"message_id": message_id}

    def send_media_group(self, chat_id, media_items):
        self.media_groups.append({"chat_id": chat_id, "media": media_items})
        return [{"message_id": 9000 + len(self.media_groups)}]

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answered_callbacks.append({
            "id": callback_query_id, "text": text, "show_alert": show_alert,
        })
        return True

    def get_chat_member(self, chat_id, user_id):
        self.chat_member_calls.append({"chat_id": chat_id, "user_id": user_id})
        if self.get_chat_member_status is None:
            return None
        return {"status": self.get_chat_member_status, "user": {"id": user_id}}

    def get_me(self, cached=True):
        return dict(self._me)

    def set_webhook(self, url, secret_token=None):
        self.webhook_calls.append({"url": url, "secret_token": secret_token})
        return True


def default_results():
    return [
        VideoResult(video_id="vid_aaa111", title="Sunset Driver",
                    channel_title="Neon Waves",
                    thumbnail_url="https://i.ytimg.com/vi/vid_aaa111/mqdefault.jpg",
                    duration_seconds=213, embeddable=True),
        VideoResult(video_id="vid_bbb222", title="Midnight Drive (Live)",
                    channel_title="Retro FM",
                    thumbnail_url="https://i.ytimg.com/vi/vid_bbb222/mqdefault.jpg",
                    duration_seconds=305, embeddable=True),
        VideoResult(video_id="vid_ccc333", title="Night Drive Mix",
                    channel_title="Chill Channel",
                    thumbnail_url="https://i.ytimg.com/vi/vid_ccc333/mqdefault.jpg",
                    duration_seconds=3600, embeddable=False),
    ]


class FakeYouTube(YouTubeClient):
    """Canned search results; mirrors the real client's embeddable contract."""

    def __init__(self, results=None, error=None):
        self.queries = []
        self.results = results if results is not None else default_results()
        self.error = error

    def search_videos(self, query, max_results=5, require_embeddable=False):
        self.queries.append({"query": query, "max_results": max_results,
                             "require_embeddable": require_embeddable})
        if self.error is not None:
            raise self.error
        out = list(self.results)
        if require_embeddable:
            out = [r for r in out if r.embeddable]
        return out[:max_results]


class FailingAdapter:
    """Wraps an adapter and fails selected methods with DatabaseError."""

    def __init__(self, inner, fail_methods):
        self._inner = inner
        self._fail = set(fail_methods)

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name in self._fail:
            def boom(*args, **kwargs):
                from db import DatabaseError
                raise DatabaseError("simulated database outage")
            return boom
        return attribute

    def debug_room_ids(self):
        return self._inner.debug_room_ids()


# ---------------------------------------------------------------------------
# Telegram update builders
# ---------------------------------------------------------------------------

def make_update(payload: dict) -> dict:
    return {"update_id": next(_update_ids), **payload}


def make_message(chat_id=-100999, chat_type="supergroup", user_id=111,
                 username="alice", first_name="Alice", text="/room"):
    return {
        "message_id": next(_message_ids),
        "chat": {"id": chat_id, "type": chat_type,
                 "title": "Test Group" if chat_type.startswith("group") else first_name},
        "from": {"id": user_id, "is_bot": False,
                 "first_name": first_name, "username": username},
        "text": text,
        "date": int(time.time()),
    }


def make_callback(callback_data, user_id=222, first_name="Bob", chat_id=-100999):
    return {
        "id": "cq-{}".format(next(_message_ids)),
        "from": {"id": user_id, "is_bot": False,
                 "first_name": first_name, "username": "user{}".format(user_id)},
        "data": callback_data,
        "message": {"message_id": 777, "chat": {"id": chat_id, "type": "supergroup"}},
    }


def post_update(client, update, secret=WEBHOOK_SECRET):
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    return client.post("/webhook/telegram", json=update, headers=headers)


def make_init_data(bot_token=BOT_TOKEN, user=None, auth_date=None,
                   tamper_hash=False):
    """Build correctly signed initData (same HMAC scheme as Telegram)."""
    user = user or {"id": 777000, "first_name": "Mini",
                    "username": "miniuser",
                    "photo_url": "https://t.me/i/userpic/320/abc.jpg"}
    params = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAG",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    encoded = {key: urllib.parse.quote(value, safe="")
               for key, value in params.items()}
    data_check_string = "\n".join(
        "{}={}".format(key, encoded[key]) for key in sorted(encoded))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"),
                          hashlib.sha256).digest()
    signature = hmac.new(secret_key, data_check_string.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    encoded["hash"] = signature
    init_data = "&".join("{}={}".format(key, encoded[key])
                         for key in sorted(encoded))
    if tamper_hash:
        init_data = init_data.replace(signature, "0" * len(signature))
    return init_data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return Config(
        telegram_bot_token=BOT_TOKEN,
        telegram_webhook_secret=WEBHOOK_SECRET,
        firebase_project_id="test-project",
        mini_app_url=MINI_APP_URL,
        youtube_api_key=YOUTUBE_KEY,
        testing=True,
    )


@pytest.fixture
def components(cfg):
    db = InMemoryAdapter()
    tg = SpyTelegram()
    yt = FakeYouTube()
    minter_calls = []

    def minter(uid, claims):
        minter_calls.append({"uid": uid, "claims": claims})
        return "test-token-for-{}".format(uid)

    return {"cfg": cfg, "db": db, "tg": tg, "yt": yt,
            "minter": minter, "minter_calls": minter_calls}


@pytest.fixture
def app_factory(components):
    def _make(cfg_overrides=None, services=None):
        import dataclasses
        cfg = dataclasses.replace(components["cfg"], **(cfg_overrides or {}))
        services = services or {}
        application = live.create_app(
            cfg=cfg,
            db_adapter=services.get("db", components["db"]),
            telegram=services.get("telegram", components["tg"]),
            youtube=services.get("youtube", components["yt"]),
            token_minter=components["minter"],
        )
        application.config["TESTING"] = True
        return application
    return _make


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def room(components, client):
    """One active room in the default group, owned by user 111."""
    client.post("/webhook/telegram", json=make_update(
        {"message": make_message(text="/room", user_id=111)}),
        headers={SECRET_HEADER: WEBHOOK_SECRET})
    room_ids = components["db"].debug_room_ids()
    assert len(room_ids) == 1
    return room_ids[0]


def select_first_result(components, client, user_id=222, query="sunset driver"):
    """Run /play and click result 1; returns the room's current media or None."""
    post_update(client, make_update(
        {"message": make_message(text="/play {}".format(query), user_id=user_id)}))
    markup = components["tg"].sent_messages[-1]["reply_markup"]
    select = [d for row in markup["inline_keyboard"] for d in
              [button["callback_data"] for button in row]
              if d.startswith("sel|")][0]
    post_update(client, make_update(
        {"callback_query": make_callback(select, user_id=user_id)}))
