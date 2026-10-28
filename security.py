"""Webhook secret check + Telegram Mini App initData verification."""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Dict, Optional


class InitDataError(Exception):
    pass


# Every command the bot exposes. Nothing else is handled.
COMMANDS = {"room", "play", "vplay", "stop", "skip"}


def parse_command(text: str) -> tuple[str, str]:
    """/play@MyBot kesariya -> ("play", "kesariya")"""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return "", ""
    head, _, rest = raw.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    return (name if name in COMMANDS else ""), rest.strip()


def verify_webhook_secret(header_value: Optional[str], expected: Optional[str]) -> bool:
    if not header_value or not expected:
        return False
    return hmac.compare_digest(header_value.encode(), expected.encode())


def verify_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> Dict:
    """Telegram's documented HMAC scheme for Mini App initData."""
    if not init_data or not bot_token:
        raise InitDataError("missing initData")
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception as exc:  # pragma: no cover
        raise InitDataError("malformed initData") from exc

    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise InitDataError("missing hash")

    data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("bad signature")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if max_age_seconds and (time.time() - auth_date) > max_age_seconds:
        raise InitDataError("initData expired")

    import json

    try:
        user = json.loads(pairs.get("user") or "{}")
    except ValueError as exc:
        raise InitDataError("bad user payload") from exc
    if not user.get("id"):
        raise InitDataError("missing user")

    return {"user": user, "auth_date": auth_date, "start_param": pairs.get("start_param")}
