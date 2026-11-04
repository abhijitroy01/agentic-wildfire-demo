"""Wildfire Gateway adapter: the feed is fetched over HTTPS only, and a capped result says it is truncated."""
import email.message
import io
import json
import urllib.request
import urllib.response
from types import SimpleNamespace

import pytest

QUERY = {"geometry": "-118.5,34.1", "distance": 20}


@pytest.fixture
def wildfire(load_handler):
    """Load the wildfire adapter Lambda."""
    return load_handler("wildfire/handler.py")


@pytest.fixture
def arcgis(wildfire, monkeypatch):
    """Answer the feed request with a canned ArcGIS body and record every URL opened."""
    reply, seen = SimpleNamespace(body=b'{"features": []}'), []

    def urlopen(request, timeout):
        seen.append(request.full_url)
        return io.BytesIO(reply.body)

    monkeypatch.setattr(wildfire.OPENER, "open", urlopen)
    return SimpleNamespace(reply=reply, seen=seen)


def test_capped_results_say_they_are_truncated(wildfire, arcgis):
    arcgis.reply.body = json.dumps({"features": [{"attributes": {}}], "exceededTransferLimit": True}).encode()
    result = wildfire.handler(QUERY, None)
    assert result["truncated"] is True
    assert result["features"] == [{"attributes": {}}]
    assert "resultRecordCount=100" in arcgis.seen[0]
    arcgis.reply.body = b'{"features": []}'
    assert wildfire.handler(QUERY, None) == {"features": []}


@pytest.mark.parametrize("url", ["http://services9.arcgis.com/query", "file:///etc/hosts", "ftp://example.com/q"])
def test_feed_urls_other_than_https_are_never_opened(wildfire, arcgis, monkeypatch, url):
    monkeypatch.setattr(wildfire, "URL", url)
    assert wildfire.handler(QUERY, None)["error"]["code"] == "WILDFIRE_UNAVAILABLE"
    assert arcgis.seen == []


@pytest.mark.parametrize("extra, accepted", [(0, True), (1, False)])
def test_a_body_over_the_limit_is_refused_not_cut(wildfire, arcgis, extra, accepted):
    padding = wildfire.BODY_LIMIT + extra - len(b'{"features": [], "pad": ""}')
    arcgis.reply.body = b'{"features": [], "pad": "' + b"x" * padding + b'"}'
    result = wildfire.handler(QUERY, None)
    assert (result.get("features") == []) is accepted
    assert accepted or result["error"]["code"] == "WILDFIRE_UNAVAILABLE"


def test_a_redirect_is_refused_and_its_target_never_requested(wildfire, monkeypatch):
    """A 3xx answer makes the tool unavailable; the opener never follows it to another host."""
    seen = []

    def https_open(handler, request):
        seen.append(request.full_url)
        headers, redirect = email.message.Message(), request.full_url.startswith(wildfire.URL)
        if redirect:
            headers["Location"] = "https://internal.example/feed"
        body, code = (b"", 302) if redirect else (b'{"features": []}', 200)
        response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, code)
        response.msg = "Found" if redirect else "OK"
        return response

    # Only the network transport is faked: the tool's own opener handles the 302.
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    assert wildfire.handler(QUERY, None)["error"]["code"] == "WILDFIRE_UNAVAILABLE"
    assert len(seen) == 1 and seen[0].startswith(wildfire.URL + "?")


class Trickle:
    """A feed answer that arrives one byte per socket read."""

    def __init__(self, body):
        self.body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read1(self, size):
        return self.body.read(min(size, 1))


@pytest.mark.parametrize("seconds_per_read", [0, 5])
def test_the_whole_read_has_a_deadline(wildfire, monkeypatch, seconds_per_read):
    """A trickled body is read in full while it is quick, and refused once the read deadline passes."""
    now = [0.0]

    def monotonic():
        now[0] += seconds_per_read
        return now[0]

    monkeypatch.setattr(wildfire, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(wildfire.OPENER, "open", lambda request, timeout: Trickle(b'{"features": []}'))
    result = wildfire.handler(QUERY, None)
    if seconds_per_read:
        # 16 bytes at 5 s each would take 80 s; the read stops once 30 s have passed.
        assert result["error"]["code"] == "WILDFIRE_UNAVAILABLE"
    else:
        assert result == {"features": []}
