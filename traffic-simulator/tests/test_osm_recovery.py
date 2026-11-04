"""Only bounded, classified public-source recovery replays an unchanged OSM operation."""
import httpx
import pytest
from traffic_simulator import osm_extracts, transport_work
from traffic_simulator.errors import IntegrationError


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (404, False), (412, False),
                                                (429, True), (500, True), (502, True), (503, True), (504, True)])
def test_http_status_recovery_is_classified_and_provider_text_never_escapes(caplog, status, retryable):
    response = httpx.Response(status, request=httpx.Request("GET", osm_extracts.INDEX_URL),
                              headers={"Retry-After": "9999"}, text="Private provider detail")
    with pytest.raises(IntegrationError) as failure, osm_extracts.client(lambda: None):
        response.raise_for_status()
    assert failure.value.retryable is retryable
    assert "Private" not in failure.value.message
    assert f"status={status}" in caplog.text and "Private provider detail" not in caplog.text
    assert (failure.value.retry_after or 0) <= osm_extracts.settings.osm_source_retry_max_seconds()


def test_transient_recovery_is_bounded_and_reuses_exact_pinned_source(monkeypatch):
    calls, delays, payload = [], [], {"source": {"url": "https://download.geofabrik.de/us-261025.osm.pbf"}}

    def transport(module, operation, selected, maximum, check, **kwargs):
        calls.append((module, operation, selected, maximum))
        raise IntegrationError("OSM_SOURCE_UNAVAILABLE", "Source temporarily unavailable.", 503, retryable=True)
    monkeypatch.setattr(transport_work, "run_transport", transport)
    monkeypatch.setattr(osm_extracts, "pause", lambda seconds, check: delays.append(seconds) or check())
    with pytest.raises(IntegrationError):
        osm_extracts.transport("download", payload, lambda: None)
    assert len(calls) == 3 and len(delays) == 2
    assert all(call == calls[0] and call[2] is payload for call in calls)


@pytest.mark.parametrize("error", [osm_extracts.invalid(), IntegrationError("OSM_SOURCE_UNAVAILABLE", "404", 502),
                                  IntegrationError("OSM_SOURCE_CAPACITY", "Too large", 503)])
def test_invalid_coverage_or_capacity_does_not_trigger_source_replay(monkeypatch, error):
    calls = []

    def transport(*_, **__):
        calls.append(1)
        raise error
    monkeypatch.setattr(transport_work, "run_transport", transport)
    with pytest.raises(IntegrationError):
        osm_extracts.transport("index", {}, lambda: None)
    assert len(calls) == 1


def test_source_lease_loss_during_backoff_prevents_a_second_owned_process(monkeypatch):
    calls, checks = [], []

    def check():
        checks.append(1)
        if len(checks) > 1:
            raise InterruptedError("lost source lease")

    def transport(*_, **__):
        calls.append(1)
        raise IntegrationError("OSM_SOURCE_UNAVAILABLE", "Transient", 503, retryable=True)
    monkeypatch.setattr(transport_work, "run_transport", transport)
    monkeypatch.setattr(osm_extracts, "pause", lambda _, check: check())
    with pytest.raises(InterruptedError):
        osm_extracts.transport("resolve", {}, check)
    assert len(calls) == 1
