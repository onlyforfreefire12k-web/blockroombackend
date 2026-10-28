"""Security primitives: webhook secret validation, Telegram Mini App
``initData`` verification, HTML escaping and a thread-safe rate limiter.

Never log the contents of ``initData`` or any header value - this module only
returns booleans/parsed user fields and raises safe, secret-free errors.
"""

import hashlib
import hmac
import html
import json
import threading
import time
import urllib.parse
from collections import deque
from typing import Optional


# ---------------------------------------------------------------------------
# Webhook secret
# ---------------------------------------------------------------------------

def verify_webhook_secret_header(header_value: Optional[str], expected_secret: Optional[str]) -> bool:
    """Constant-time comparison of Telegram's secret-token header.

    Telegram sends the value configured in ``setWebhook`` back in the
    ``X-Telegram-Bot-Api-Secret-Token`` header on every update.
    """
    if not header_value or not expected_secret:
        return False
    return hmac.compare_digest(
        header_value.encode("utf-8"), expected_secret.encode("utf-8")
    )


# ---------------------------------------------------------------------------
# Telegram Mini App initData verification
# ---------------------------------------------------------------------------

class InitDataError(Exception):
    """Raised when Telegram Mini App initData fails verification."""


def verify_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict:
    """Verify signed Mini App ``initData`` (Telegram's documented HMAC scheme).

    Returns ``{"user": {...}, "auth_date": int, "start_param": str|None}`` on
    success; raises :class:`InitDataError` otherwise.  Error messages are safe
    to return to clients and never contain the token or the payload itself.

    Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not isinstance(init_data, str) or not bot_token:
        raise InitDataError("initData verification failed")

    # Raw (still URL-encoded) key=value pairs.  The data-check string MUST be
    # built from the encoded values exactly as Telegram transmitted them.
    fields = {}
    for pair in init_data.split("&"):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if not sep:
            raise InitDataError("initData is malformed")
        fields[key] = value

    received_hash = fields.pop("hash", None)
    fields.pop("signature", None)  # excluded from the hash per Telegram docs
    if not received_hash:
        raise InitDataError("initData is missing its hash field")

    data_check_string = "\n".join(
        "{}={}".format(key, fields[key]) for key in sorted(fields.keys())
    )
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        raise InitDataError("initData signature check failed")

    decoded = {key: urllib.parse.unquote(value) for key, value in fields.items()}

    try:
        auth_date = int(decoded.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise InitDataError("initData contains an invalid auth_date") from None
    if auth_date <= 0:
        raise InitDataError("initData is missing auth_date")

    now = int(time.time())
    if auth_date - now > 300:
        raise InitDataError("initData auth_date is in the future")
    if now - auth_date > max_age_seconds:
        raise InitDataError("initData is expired; reload the Mini App and try again")

    user_raw = decoded.get("user")
    if not user_raw:
        raise InitDataError("initData does not contain a Telegram user")
    try:
        user_obj = json.loads(user_raw)
    except ValueError:
        raise InitDataError("initData user field is malformed") from None
    if not isinstance(user_obj, dict) or "id" not in user_obj:
        raise InitDataError("initData user field is malformed")
    try:
        user_id = int(user_obj["id"])
    except (TypeError, ValueError):
        raise InitDataError("initData user id is invalid") from None

    return {
        "user": {
            "id": user_id,
            "display_name": user_obj.get("first_name") or "",
            "username": user_obj.get("username"),
            "photo_url": user_obj.get("photo_url"),
        },
        "auth_date": auth_date,
        "start_param": decoded.get("start_param"),
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def escape_html(value) -> str:
    """Escape user-controlled text before embedding it in HTML messages."""
    return html.escape(str(value if value is not None else ""), quote=True)


class RateLimiter:
    """In-process sliding-window rate limiter.

    Good enough for the single-service Render deployment this bot targets.
    For multi-instance deployments back this with Redis (see README,
    "Limitations").  Keys are namespaced by callers, e.g. ``room:<user_id>``.
    """

    def __init__(self, max_keys: int = 10000):
        self._hits = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        """Return True when ``key`` may perform the action, False otherwise."""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_keys:
                    self._evict_oldest_locked()
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def _evict_oldest_locked(self):
        oldest_key = None
        oldest_time = None
        for key, bucket in self._hits.items():
            last = bucket[-1] if bucket else 0
            if oldest_time is None or last < oldest_time:
                oldest_key, oldest_time = key, last
        if oldest_key is not None:
            self._hits.pop(oldest_key, None)
