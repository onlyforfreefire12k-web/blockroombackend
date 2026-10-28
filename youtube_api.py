"""YouTube Data API v3 search client.

The bot only fetches *metadata* (video id, title, channel, thumbnail,
duration, embeddability) - it never downloads or redistributes audio/video.
Playback is the frontend's job via the official YouTube IFrame embed.

Error messages are secret-free: the API key is never included in any
exception string, log line, or HTTP response.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("blockroom.youtube")

SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$")


class YouTubeError(Exception):
    pass


class YouTubeQuotaExceeded(YouTubeError):
    pass


class YouTubeConfigError(YouTubeError):
    pass


@dataclass
class VideoResult:
    video_id: str
    title: str
    channel_title: str
    thumbnail_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    embeddable: bool = True


def _parse_duration(value) -> Optional[int]:
    if not value or not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    total = 0.0
    total += int(days or 0) * 86400
    total += int(hours or 0) * 3600
    total += int(minutes or 0) * 60
    total += float(seconds or 0)
    return int(total)


class YouTubeClient:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None,
                 timeout=(3.05, 10)):
        self._api_key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout

    # -- public ---------------------------------------------------------------

    def search_videos(self, query: str, max_results: int = 5,
                      require_embeddable: bool = False) -> List[VideoResult]:
        """Search YouTube and enrich with duration/embeddability metadata."""
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "key": self._api_key,
        }
        if require_embeddable:
            params["videoEmbeddable"] = "true"
        data = self._get(SEARCH_ENDPOINT, params, context="search")
        items = data.get("items") or []

        results: List[VideoResult] = []
        ids: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            ids.append(video_id)
            results.append(VideoResult(
                video_id=video_id,
                title=snippet.get("title") or "Untitled",
                channel_title=snippet.get("channelTitle") or "Unknown channel",
                thumbnail_url=self._best_thumbnail(snippet.get("thumbnails")),
            ))

        durations, embeddable = self._fetch_video_details(ids)
        for result in results:
            if result.video_id in durations:
                result.duration_seconds = durations[result.video_id]
            if result.video_id in embeddable:
                result.embeddable = embeddable[result.video_id]
        return results

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _best_thumbnail(thumbnails) -> Optional[str]:
        if not isinstance(thumbnails, dict):
            return None
        for key in ("medium", "high", "default"):
            entry = thumbnails.get(key) or {}
            url = entry.get("url")
            if url:
                return url
        return None

    def _fetch_video_details(self, ids: List[str]) -> Tuple[Dict[str, int], Dict[str, bool]]:
        """Optional enrichment; failures degrade gracefully (no durations)."""
        if not ids:
            return {}, {}
        params = {
            "part": "contentDetails,status",
            "id": ",".join(ids[:50]),
            "key": self._api_key,
        }
        try:
            data = self._get(VIDEOS_ENDPOINT, params, context="video details")
        except YouTubeError as exc:
            log.warning("Video details lookup skipped: %s", exc)
            return {}, {}
        durations: Dict[str, int] = {}
        embeddable: Dict[str, bool] = {}
        for item in data.get("items") or []:
            video_id = item.get("id")
            if not video_id:
                continue
            content = item.get("contentDetails") or {}
            duration = _parse_duration(content.get("duration"))
            if duration is not None:
                durations[video_id] = duration
            status = item.get("status") or {}
            embeddable[video_id] = bool(status.get("embeddable", True)) and (
                status.get("privacyStatus", "public") != "private"
            )
        return durations, embeddable

    def _get(self, url: str, params: dict, context: str) -> dict:
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException:
            # from None: keep the API key (embedded in params/url) out of tracebacks
            raise YouTubeError("YouTube {} request failed".format(context)) from None

        status = response.status_code
        if status in (401, 403):
            reason = self._error_reason(response)
            if reason and "quota" in reason.lower():
                raise YouTubeQuotaExceeded(
                    "YouTube API quota is exhausted; try again later"
                ) from None
            raise YouTubeConfigError(
                "YouTube API rejected the request (check YOUTUBE_API_KEY)"
            ) from None
        if status == 400:
            reason = self._error_reason(response) or ""
            if "api key" in reason.lower() or "keyinvalid" in reason.lower():
                raise YouTubeConfigError(
                    "YOUTUBE_API_KEY is not valid"
                ) from None
            raise YouTubeError(
                "YouTube {} request was rejected".format(context)
            ) from None
        if status != 200:
            raise YouTubeError(
                "YouTube {} failed with status {}".format(context, status)
            ) from None
        try:
            return response.json()
        except ValueError:
            raise YouTubeError("YouTube returned a non-JSON response") from None

    @staticmethod
    def _error_reason(response) -> Optional[str]:
        try:
            payload = response.json()
        except ValueError:
            return None
        error = payload.get("error") or {}
        reasons = []
        for entry in error.get("errors") or []:
            if isinstance(entry, dict) and entry.get("reason"):
                reasons.append(str(entry["reason"]))
        if error.get("message"):
            reasons.append(str(error["message"]))
        return " ".join(reasons) if reasons else None
