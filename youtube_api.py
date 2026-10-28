"""YouTube Data API v3 search — only embeddable results are returned."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

log = logging.getLogger(__name__)
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


@dataclass
class Video:
    video_id: str
    title: str
    channel_title: str
    thumbnail_url: Optional[str]
    duration_seconds: Optional[int]
    embeddable: bool


def _iso8601_to_seconds(value: str) -> Optional[int]:
    if not value or not value.startswith("PT"):
        return None
    total, num = 0, ""
    for ch in value[2:]:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            total += int(num or 0) * 3600
            num = ""
        elif ch == "M":
            total += int(num or 0) * 60
            num = ""
        elif ch == "S":
            total += int(num or 0)
            num = ""
    return total


def search(api_key: str, query: str, want_video: bool, limit: int = 12) -> List[Video]:
    """Search and keep only playable, embeddable results."""
    if not api_key or not query.strip():
        return []
    params = {
        "key": api_key,
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": str(limit),
        "safeSearch": "none",
        "videoEmbeddable": "true",
        "videoSyndicated": "true",
    }
    if not want_video:
        # audio request: bias to lyric/audio uploads so the TV shows lyrics
        params["videoCategoryId"] = "10"
    try:
        res = requests.get(SEARCH_URL, params=params, timeout=12)
        data = res.json()
    except requests.RequestException as exc:
        log.warning("YouTube search failed: %s", exc)
        return []
    items = data.get("items") or []
    ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
    if not ids:
        return []

    details = {}
    try:
        res = requests.get(
            VIDEOS_URL,
            params={"key": api_key, "part": "contentDetails,status,snippet", "id": ",".join(ids)},
            timeout=12,
        )
        for item in (res.json().get("items") or []):
            details[item["id"]] = item
    except requests.RequestException as exc:
        log.warning("YouTube details failed: %s", exc)

    out: List[Video] = []
    for vid in ids:
        d = details.get(vid)
        if not d:
            continue
        status = d.get("status") or {}
        if status.get("embeddable") is False:
            continue
        if status.get("privacyStatus") not in (None, "public"):
            continue
        snippet = d.get("snippet") or {}
        out.append(
            Video(
                video_id=vid,
                title=snippet.get("title") or "Untitled",
                channel_title=snippet.get("channelTitle") or "",
                thumbnail_url=(snippet.get("thumbnails", {}).get("mqdefault") or {}).get("url"),
                duration_seconds=_iso8601_to_seconds((d.get("contentDetails") or {}).get("duration", "")),
                embeddable=True,
            )
        )
    return out
