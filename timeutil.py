"""Shared timestamp helper.

All timestamps persisted to Firebase are ISO-8601 UTC strings with second
precision (for example ``2026-10-28T09:15:00Z``).  Strings are used instead of
Firestore-native Timestamps so that both the Firestore adapter and the
Realtime Database adapter produce byte-identical records, and so the game
frontend can parse them with ``new Date(value)`` without any conversion layer.
"""

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
