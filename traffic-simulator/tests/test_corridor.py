"""Caltrans highway queries read a bounded body and keep provider error text server-side."""
import json
from urllib.parse import urlparse
from urllib.request import Request

import pytest
from traffic_simulator import corridor


class Body:
    """Minimal urlopen response that records each requested read size."""

    def __init__(self, raw):
        self.raw, self.reads = raw, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        self.reads.append(size)
        return self.raw[:size]


def serve(monkeypatch, raw):
    """Answer the next Caltrans request with `raw` bytes and return the fake response."""
    body = Body(raw)
    monkeypatch.setattr(corridor, "urlopen", lambda *_args, **_kwargs: body)
    return body


def test_response_body_is_read_with_a_size_limit(monkeypatch):
    """The read stops at simulation.caltrans_response_max_mb (CALTRANS_RESPONSE_MAX_MB), in 1,000,000-byte MB."""
    body = serve(monkeypatch, b'{"objectIds": [1, 2]}')
    assert corridor.request({"f": "json"}) == {"objectIds": [1, 2]}
    assert body.reads == [20_000_001], "settings.toml default: 20 MB"
    monkeypatch.setenv("CALTRANS_RESPONSE_MAX_MB", "1")
    body = serve(monkeypatch, b'{"objectIds": [1, 2]}')
    corridor.request({"f": "json"})
    assert body.reads == [1_000_001]
    serve(monkeypatch, b'{"features": "' + b"x" * 1_000_000 + b'"}')
    with pytest.raises(RuntimeError, match="exceeded the size limit"):
        corridor.request({"f": "json"})
    serve(monkeypatch, b"<html>maintenance</html>")
    with pytest.raises(ValueError):
        corridor.request({"f": "json"})


def test_only_an_https_service_url_is_opened(monkeypatch):
    """A non-HTTPS service URL (for example file://) is refused before urlopen runs."""
    monkeypatch.setattr(corridor, "urlopen", lambda *_args, **_kwargs: pytest.fail("Opened a non-HTTPS URL"))
    for service in ("file:///etc/passwd", "http://caltrans-gis.dot.ca.gov/query", "ftp://example.invalid/query"):
        monkeypatch.setattr(corridor, "SERVICE", service)
        with pytest.raises(RuntimeError, match="must be an https:// URL"):
            corridor.request({"f": "json"})


def test_provider_error_text_stays_in_the_server_log(monkeypatch, caplog):
    error = {"code": 400, "message": "Invalid query near /arcgis/private/path", "details": ["raw provider detail"]}
    serve(monkeypatch, json.dumps({"error": error}).encode())
    with pytest.raises(RuntimeError) as raised:
        corridor.request({"f": "json"})
    assert str(raised.value) == "Caltrans highway service returned an error. Retry later."
    assert "Invalid query near /arcgis/private/path" in caplog.text
@pytest.mark.parametrize("target", [
    "https://evil.example/query", "http://caltrans-gis.dot.ca.gov/query",
    "https://caltrans-gis.dot.ca.gov:8443/query", "https://user@caltrans-gis.dot.ca.gov/query",
])
def test_caltrans_redirects_cannot_leave_the_provider_authority(target):
    with pytest.raises(RuntimeError):
        corridor.CaltransRedirects().redirect_request(Request(corridor.SERVICE), None, 302, "", {}, target)


def test_caltrans_may_redirect_to_a_canonical_path_on_the_same_https_authority():
    authority = urlparse(corridor.SERVICE).netloc
    target = f"https://{authority}/canonical/query"
    request = corridor.CaltransRedirects().redirect_request(Request(corridor.SERVICE), None, 302, "", {}, target)
    assert request.full_url == target
