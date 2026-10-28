from datetime import datetime, timezone


def utc_now_iso() -> str:
    """ISO-8601 UTC with a trailing Z — the format the frontend parses."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
