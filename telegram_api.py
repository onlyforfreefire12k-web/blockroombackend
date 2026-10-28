"""Thin Telegram Bot API client (sendMessage + inline buttons only)."""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class Telegram:
    def __init__(self, token: str, timeout: int = 15):
        self.token = token
        self.timeout = timeout

    @staticmethod
    def _safe_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Loggable form: never includes the bot token or message text bodies."""
        safe: Dict[str, Any] = {}
        for key in ("chat_id", "parse_mode", "disable_web_page_preview"):
            if key in payload:
                safe[key] = payload[key]
        markup = payload.get("reply_markup")
        if isinstance(markup, dict):
            safe["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {
                            k: (v if k != "url" else _mask_url(v))
                            for k, v in button.items()
                        }
                        for button in row
                    ]
                    for row in markup.get("inline_keyboard", [])
                ]
            }
        if "text" in payload:
            safe["text_len"] = len(str(payload.get("text") or ""))
        return safe

    def call(self, method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.token:
            log.warning("Telegram token missing; skipping %s", method)
            return None
        try:
            res = requests.post(API.format(token=self.token, method=method), json=payload, timeout=self.timeout)
            try:
                body = res.json()
            except ValueError:
                log.warning(
                    "Telegram %s returned non-JSON (HTTP %s)", method, getattr(res, "status_code", "?")
                )
                return None
            if not body.get("ok"):
                log.warning(
                    "Telegram %s rejected: error_code=%s description=%r payload=%s",
                    method,
                    body.get("error_code"),
                    body.get("description"),
                    self._safe_payload(payload),
                )
                return None
            return body.get("result")
        except requests.RequestException as exc:
            log.warning("Telegram %s network error: %s", method, exc)
            return None

    def send(
        self,
        chat_id: int,
        text: str,
        keyboard: Optional[List[List[Dict[str, Any]]]] = None,
    ) -> bool:
        """Returns True when Telegram accepted the message."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", payload) is not None

    def send_raw(self, chat_id: int, text: str, keyboard: Optional[List[List[Dict[str, Any]]]] = None):
        """sendMessage that returns Telegram's raw body (ok/error_code/description)."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if not self.token:
            return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
        try:
            res = requests.post(API.format(token=self.token, method="sendMessage"), json=payload, timeout=self.timeout)
            body = res.json()
            if not body.get("ok"):
                log.warning(
                    "sendMessage rejected: error_code=%s description=%r payload=%s",
                    body.get("error_code"),
                    body.get("description"),
                    self._safe_payload(payload),
                )
            return body
        except requests.RequestException as exc:
            return {"ok": False, "description": f"network error: {exc}"}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def set_webhook(self, url: str, secret: str) -> Optional[Dict[str, Any]]:
        return self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )

    def delete_webhook(self) -> Optional[Dict[str, Any]]:
        return self.call("deleteWebhook", {"drop_pending_updates": True})

    def webhook_info(self) -> Optional[Dict[str, Any]]:
        return self.call("getWebhookInfo", {})

    def me(self) -> Optional[Dict[str, Any]]:
        return self.call("getMe", {})

    def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
        if not self.token:
            return []
        try:
            res = requests.post(
                API.format(token=self.token, method="getUpdates"),
                json={
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=timeout + 10,
            )
            body = res.json()
            if not body.get("ok"):
                log.warning("getUpdates failed: %s", body.get("description"))
                return []
            result = body.get("result")
            return result if isinstance(result, list) else []
        except requests.RequestException as exc:
            log.warning("getUpdates network error: %s", exc)
            return []


BUTTON_TEXT_MAX = 64
STARTAPP_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TME_LINK_RE = re.compile(r"^https://t\.me/[A-Za-z0-9_]+/[A-Za-z0-9_]+$")


def _mask_url(url: str) -> str:
    """keep host/path for debugging, drop query values (may carry tokens)"""
    try:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "<unparsable url>"


def is_https(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme == "https" and bool(parts.netloc)
    except Exception:
        return False


def web_app_button(text: str, url: str) -> Dict[str, Any]:
    """InlineKeyboardButton of type web_app.

    Telegram only allows this in PRIVATE chats between a user and the bot, and
    the URL must be the Mini App's https URL — never a t.me link. Using it in a
    group returns ``BUTTON_TYPE_INVALID``.
    """
    return {"text": text, "web_app": {"url": url}}


def url_button(text: str, url: str) -> Dict[str, Any]:
    """InlineKeyboardButton of type url — valid in every chat type."""
    return {"text": text, "url": url}


def is_webapp_url(mini_app_url: str) -> bool:
    """True only for a real Mini App https URL (not a t.me deep link)."""
    base = (mini_app_url or "").strip()
    if not is_https(base):
        return False
    host = urllib.parse.urlsplit(base).netloc.lower()
    return host not in ("t.me", "telegram.me", "www.t.me")


def build_join_link(mini_app_url: str, room_id: str) -> str:
    """Build the room-specific join URL and validate the startapp payload.

    t.me Mini App      -> https://t.me/<bot>/<app>?startapp=<roomId>
    game https URL     -> https://game/?tgWebAppStartParam=<roomId>&room=<roomId>
    """
    base = (mini_app_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("MINI_APP_URL is not configured")
    if not is_https(base):
        raise ValueError("MINI_APP_URL must be an https URL")
    if not STARTAPP_RE.match(room_id or ""):
        raise ValueError("room id is not a valid startapp payload")

    parts = urllib.parse.urlsplit(base)
    if parts.netloc.lower() == "t.me":
        # must be t.me/<bot>/<app> for a Mini App start parameter
        if not TME_LINK_RE.match(f"https://{parts.netloc}{parts.path}"):
            raise ValueError("MINI_APP_URL must look like https://t.me/<bot>/<app>")
        query = urllib.parse.urlencode({"startapp": room_id})
    else:
        query = urllib.parse.urlencode({"tgWebAppStartParam": room_id, "room": room_id})

    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    if len(url) > 1900:
        raise ValueError("join URL is too long")
    return url


def join_keyboard(
    link: str, *, chat_type: str = "group", mini_app_url: str = ""
) -> Tuple[List[List[Dict[str, Any]]], str]:
    """The one and only Join Blockroom button.

    Returns (inline_keyboard, button_type). Only one field is ever set:
      * private chat + webapp URL -> ``web_app`` (Mini App button)
      * everything else          -> ``url``      (valid in groups supergroups)
    Returns an empty keyboard when the link is not a valid https URL.
    """
    if not is_https(link):
        return [], "none"

    use_webapp = chat_type == "private" and is_webapp_url(mini_app_url or link)
    button = (
        web_app_button("Join Blockroom", link)
        if use_webapp
        else url_button("Join Blockroom", link)
    )

    # hard guarantees before the payload reaches Telegram
    text = button["text"]
    assert isinstance(text, str) and 1 <= len(text) <= BUTTON_TEXT_MAX
    assert len([k for k in button if k != "text"]) == 1, "exactly one button type"
    keyboard = [[button]]
    return keyboard, ("web_app" if use_webapp else "url")
