"""Thin Telegram Bot API client (sendMessage + inline buttons only)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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

    def call(self, method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.token:
            log.warning("Telegram token missing; skipping %s", method)
            return None
        try:
            res = requests.post(API.format(token=self.token, method=method), json=payload, timeout=self.timeout)
            body = res.json()
            if not body.get("ok"):
                log.warning("Telegram %s failed: %s", method, body.get("description"))
                return None
            return body.get("result")
        except requests.RequestException as exc:
            log.warning("Telegram %s network error: %s", method, exc)
            return None

    def send(self, chat_id: int, text: str, keyboard: Optional[List[List[Dict[str, Any]]]] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def set_webhook(self, url: str, secret: str) -> None:
        self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )


def web_app_button(text: str, url: str) -> Dict[str, Any]:
    return {"text": text, "web_app": {"url": url}}


def url_button(text: str, url: str) -> Dict[str, Any]:
    return {"text": text, "url": url}


def build_join_link(mini_app_url: str, room_id: str) -> str:
    """t.me mini app -> ?startapp= ; plain https -> ?tgWebAppStartParam="""
    base = (mini_app_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("MINI_APP_URL is not configured")
    if "t.me/" in base:
        return f"{base}?startapp={room_id}"
    return f"{base}?tgWebAppStartParam={room_id}&room={room_id}"
