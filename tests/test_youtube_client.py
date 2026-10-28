"""Real YouTubeClient parsing/error-mapping with a fake HTTP session."""

import pytest
import requests

from youtube_api import (YouTubeClient, YouTubeConfigError, YouTubeError,
                         YouTubeQuotaExceeded)

API_KEY = "UNITTEST-KEY"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        if isinstance(self._responses[0], Exception):
            raise self._responses.pop(0)
        return self._responses.pop(0)


SEARCH_PAYLOAD = {
    "items": [
        {"id": {"videoId": "abc123"},
         "snippet": {"title": "Test Song <b>&</b>", "channelTitle": "Chan & Co",
                     "thumbnails": {"medium": {"url": "https://thumb/1.jpg"}}}},
        {"id": {"videoId": "def456"},
         "snippet": {"title": "Second Song", "channelTitle": "Chan2",
                     "thumbnails": {}}},
    ]
}

VIDEOS_PAYLOAD = {
    "items": [
        {"id": "abc123", "contentDetails": {"duration": "PT3M33S"},
         "status": {"embeddable": True, "privacyStatus": "public"}},
        {"id": "def456", "contentDetails": {"duration": "PT1H2M"},
         "status": {"embeddable": False, "privacyStatus": "public"}},
    ]
}


def test_search_parses_results_and_durations():
    session = FakeSession([FakeResponse(200, SEARCH_PAYLOAD),
                           FakeResponse(200, VIDEOS_PAYLOAD)])
    client = YouTubeClient(API_KEY, session=session)
    results = client.search_videos("test", max_results=5)

    assert len(results) == 2
    first = results[0]
    assert first.video_id == "abc123"
    assert first.title == "Test Song <b>&</b>"
    assert first.channel_title == "Chan & Co"
    assert first.thumbnail_url == "https://thumb/1.jpg"
    assert first.duration_seconds == 213
    assert first.embeddable is True
    assert results[1].duration_seconds == 3720      # PT1H2M
    assert results[1].embeddable is False

    assert session.calls[0]["params"]["key"] == API_KEY
    assert session.calls[1]["params"]["id"] == "abc123,def456"


def test_embeddable_param_forwarded():
    session = FakeSession([FakeResponse(200, {"items": []})])
    client = YouTubeClient(API_KEY, session=session)
    client.search_videos("test", require_embeddable=True)
    assert session.calls[0]["params"]["videoEmbeddable"] == "true"


def test_quota_exceeded_maps_to_dedicated_error():
    quota = {"error": {"message": "quota exceeded",
                       "errors": [{"reason": "quotaExceeded"}]}}
    session = FakeSession([FakeResponse(403, quota)])
    client = YouTubeClient(API_KEY, session=session)
    with pytest.raises(YouTubeQuotaExceeded):
        client.search_videos("test")


def test_invalid_key_maps_to_config_error():
    bad_key = {"error": {"message": "API key not valid. Please pass a valid API key.",
                         "errors": [{"reason": "badRequest"}]}}
    session = FakeSession([FakeResponse(400, bad_key)])
    client = YouTubeClient(API_KEY, session=session)
    with pytest.raises(YouTubeConfigError):
        client.search_videos("test")


@pytest.mark.parametrize("status,payload", [
    (403, {"error": {"message": "forbidden", "errors": []}}),
    (500, {}),
])
def test_other_http_errors_are_generic(status, payload):
    session = FakeSession([FakeResponse(status, payload)])
    client = YouTubeClient(API_KEY, session=session)
    with pytest.raises(YouTubeError) as excinfo:
        client.search_videos("test")
    assert API_KEY not in str(excinfo.value)


def test_network_errors_do_not_leak_the_key():
    session = FakeSession([requests.ConnectionError("boom https://googleapis.com/...KEY")])
    client = YouTubeClient(API_KEY, session=session)
    with pytest.raises(YouTubeError) as excinfo:
        client.search_videos("test")
    assert API_KEY not in str(excinfo.value)


def test_details_failure_degrades_gracefully():
    session = FakeSession([FakeResponse(200, SEARCH_PAYLOAD),
                           FakeResponse(500, {})])
    client = YouTubeClient(API_KEY, session=session)
    results = client.search_videos("test")
    assert len(results) == 2
    assert results[0].duration_seconds is None  # search still usable
