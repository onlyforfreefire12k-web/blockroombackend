"""Thin Telegram Bot API client.

Only the methods this bot needs are implemented.  All calls go over HTTPS via
``requests`` with strict timeouts.  Error messages raised by this module never
contain the bot token or full request URLs (the URL embeds the token).
"""

import json
import logging
import threading
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("blockroom.telegram")


class TelegramAPIError(Exception):
    """Raised when a Telegram Bot API call fails."""


def build_inline_keyboard(rows: List[List[dict]]) -> dict:
    return {"inline_keyboard": rows}


def url_button(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def callback_button(text: str, data: str) -> dict:
    return {"text": text[:64], "callback_data": data[:64]}


def web_app_button(text: str, url: str) -> dict:
    return {"text": text, "web_app": {"url": url}}


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        session: Optional[requests.Session] = None,
        api_base: str = "https://api.telegram.org",
        timeout=(3.05, 10),
    ):
        self._token = bot_token
        self._session = session or requests.Session()
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._me: Optional[dict] = None
        self._me_lock = threading.Lock()

    # -- low level ----------------------------------------------------------

    def _call(self, method: str, payload: Optional[dict] = None) -> Any:
        url = "{}/bot{}/{}".format(self._api_base, self._token, method)
        try:
            response = self._session.post(url, json=payload or {}, timeout=self._timeout)
        except requests.RequestException:
            # from None: never let the chained traceback leak the token URL
            raise TelegramAPIError("Telegram API call failed ({})".format(method)) from None
        if response.status_code in (401, 403):
            raise TelegramAPIError(
                "Telegram rejected the request for {} (auth error; check the bot token)".format(method)
            )
        try:
            data = response.json()
        except ValueError:
            raise TelegramAPIError(
                "Telegram returned a non-JSON response for {}".format(method)
            ) from None
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description", "unknown error") if isinstance(data, dict) else "unknown error"
            raise TelegramAPIError(
                "Telegram API error for {}: {}".format(method, description)
            )
        return data.get("result")

    # -- convenience wrappers ------------------------------------------------

    def get_me(self, cached: bool = True) -> dict:
        with self._me_lock:
            if cached and self._me is not None:
                return dict(self._me)
            result = self._call("getMe")
            self._me = result if isinstance(result, dict) else {}
            return dict(self._me)

    def send_message(self, chat_id, text: str, reply_markup: Optional[dict] = None,
                     disable_preview: bool = True) -> dict:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": disable_preview},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def edit_message_text(self, chat_id, message_id: int, text: str,
                          reply_markup: Optional[dict] = None) -> dict:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("editMessageText", payload)

    def send_media_group(self, chat_id, media_items: List[dict]) -> list:
        # Telegram requires the "media" field to be a JSON-serialized string.
        payload = {"chat_id": chat_id, "media": json.dumps(media_items[:10])}
        result = self._call("sendMediaGroup", payload)
        return result if isinstance(result, list) else []

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None,
                              show_alert: bool = False) -> Optional[bool]:
        """Acknowledge a callback.  Failures are swallowed (non-critical)."""
        payload = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]
        try:
            self._call("answerCallbackQuery", payload)
            return True
        except TelegramAPIError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)
            return None

    def get_chat_member(self, chat_id, user_id) -> Optional[dict]:
        """Return the member dict, or None when the lookup fails (best effort)."""
        try:
            result = self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
            return result if isinstance(result, dict) else None
        except TelegramAPIError as exc:
            log.info("getChatMember unavailable: %s", exc)
            return None

    def set_webhook(self, url: str, secret_token: Optional[str] = None) -> bool:
        payload = {
            "url": url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        self._call("setWebhook", payload)
        return True
