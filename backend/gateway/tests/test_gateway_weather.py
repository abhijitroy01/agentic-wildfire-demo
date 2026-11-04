"""DTN batch receipts keep exact query identity and isolate unavailable or malformed points."""
import json
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def weather(load_handler, monkeypatch):
    module = load_handler("weather/lambda_weather_dtn.py")
    monkeypatch.setattr(module, "dtn_credentials", lambda: ("fixture", "fixture"))
    return module


def response(values):
    return {"features": [{"properties": {"2026-10-27T00:00:00Z": values}}]}


def test_four_point_batch_keeps_partial_and_failed_points_isolated(weather, monkeypatch, caplog):
    points = [{"latitude": number, "longitude": -120-number} for number in range(4)]
    replies = iter([response({"windSpeed": 0}), RuntimeError("private-provider-detail"),
                    response({"windSpeed": 9, "windDirection": 270}), response({"airTemp": 65})])

    def fetch(*_):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(weather, "make_dtn_request", fetch)
    result = weather.lambda_handler({"locations": points}, SimpleNamespace(aws_request_id="fixture-request"))
    assert [(item["latitude"], item["longitude"]) for item in result["locations"]] == [
        (point["latitude"], point["longitude"]) for point in points]
    assert [item["status"] for item in result["locations"]] == ["available", "unavailable", "available", "partial"]
    assert [item.get("windStatus") for item in result["locations"]] == ["calm", None, "measured", "missing"]
    assert result["locations"][0]["currentWeather"]["wind"]["speed"]["value"] == 0
    assert "direction" not in result["locations"][0]["currentWeather"]["wind"]
    assert "wind" not in result["locations"][3]["currentWeather"]
    assert result["locations"][1]["errorCode"] == "WEATHER_UNAVAILABLE"
    assert result["metadata"]["requestId"] == "fixture-request"
    assert result["metadata"]["locationCount"] == 4
    assert all(item["durationMs"] >= 0 for item in result["locations"])
    assert "private-provider-detail" not in caplog.text + json.dumps(result)
    assert "-120" not in caplog.text


@pytest.mark.parametrize("invalid", [
    [], True, "provider text", {"features": None}, {"features": [None]},
    {"features": [{"properties": None}]}, {"features": [{"properties": []}]},
    {"features": [{"properties": {"2026-10-27T00:00:00Z": None}}]},
    {"features": [{"properties": {"2026-10-27T00:00:00Z": []}}]},
    {"features": [{"properties": {"2026-10-27T00:00:00Z": {}}}]},
    {"features": [{"properties": {"2026-10-27": {"windSpeed": 0}}}]},
    {"features": [{"properties": {"2026-10-27T00:00:00": {"windSpeed": 0}}}]},
])
def test_malformed_provider_shapes_are_point_errors_not_batch_errors(weather, monkeypatch, invalid):
    replies = iter([invalid, response({"windSpeed": 0})])
    monkeypatch.setattr(weather, "make_dtn_request", lambda *_: next(replies))
    result = weather.lambda_handler({"locations": [{"latitude": 0, "longitude": 0},
                                                  {"latitude": 1, "longitude": 1}]}, None)
    assert "error" not in result
    assert result["locations"][0]["status"] == "unavailable"
    assert result["locations"][1]["windStatus"] == "calm"


def test_malformed_period_is_skipped_and_timestamps_sort_by_actual_instant(weather):
    raw = {"features": [{"properties": {
        "2026-10-27T00:00:00Z": None,
        "2026-10-26T00:00:00Z": {},
        "2026-10-27T02:00:00+03:00": {"windSpeed": 0},
        "2026-10-27T00:30:00Z": {"windSpeed": 12, "windDirection": 180},
    }}]}
    assert weather.map_dtn_response(0, 0, raw)["currentWeather"]["wind"]["speed"]["value"] == 0
    forecast = weather.map_dtn_response(0, 0, raw, True)["forecastData"]
    assert sum(len(day["periods"]) for day in forecast["days"]) == 2


@pytest.mark.parametrize("values, expected", [
    ({"windSpeed": 0}, "calm"), ({"windDirection": 270}, "missing"),
    ({"windSpeed": 5}, "direction_missing"), ({"windSpeed": 5, "windDirection": 0}, "measured"),
    ({"windSpeed": -1, "windDirection": 90}, "invalid"),
    ({"windSpeed": 5, "windDirection": 361}, "invalid"),
])
def test_measured_calm_never_substitutes_for_missing_wind(weather, values, expected):
    result = weather.map_dtn_response(0, 0, response(values))
    assert weather.point_status(result)["windStatus"] == expected


def test_caller_operation_uuid_and_lambda_request_id_correlate_without_query_coordinates(
        weather, monkeypatch, caplog):
    operation = "a9c4f286-1f27-49e9-8f55-f41bda8875e1"
    monkeypatch.setattr(weather, "make_dtn_request", lambda *_: response({"windSpeed": 0}))
    result = weather.lambda_handler({"latitude": 47.60395, "longitude": -122.3305,
                                     "operationId": operation}, SimpleNamespace(aws_request_id="fixture-request"))
    assert result["metadata"]["operationId"] == operation
    assert operation in caplog.text and "fixture-request" in caplog.text
    assert "47.60395" not in caplog.text and "-122.3305" not in caplog.text


@pytest.mark.parametrize("operation", ["not-a-uuid", "private text\nnew log line", {}, [], 0])
def test_invalid_correlation_is_refused_before_provider_and_never_logged(weather, monkeypatch, caplog, operation):
    def forbidden(*_):
        pytest.fail("Malformed correlation must not reach the provider")
    monkeypatch.setattr(weather, "make_dtn_request", forbidden)
    result = weather.lambda_handler({"latitude": 0, "longitude": 0, "operationId": operation}, None)
    assert result["error"]["code"] == "INVALID_WEATHER_QUERY"
    assert "private text" not in caplog.text + json.dumps(result)


def test_aggregate_budget_retains_completed_points_and_does_not_start_unattempted_points(weather, monkeypatch):
    clock, calls, waits = [0.0], [], []
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("DTN_OPERATION_TIMEOUT_SECONDS", "6")
    monkeypatch.setenv("DTN_REQUEST_TIMEOUT_SECONDS", "6")

    def fetch(_endpoint, params):
        calls.append(params["lat"])
        waits.append(weather.request_timeout())
        if len(calls) == 1:
            clock[0] += 4
            return response({"windSpeed": 0})
        clock[0] += 2
        raise weather.WeatherBudgetExpired()

    monkeypatch.setattr(weather, "make_dtn_request", fetch)
    result = weather.lambda_handler({"locations": [{"latitude": value, "longitude": 0} for value in range(3)]}, None)
    assert calls == [0, 1] and waits == [6, 2]
    assert result["locations"][0]["windStatus"] == "calm"
    assert [point["errorCode"] for point in result["locations"][1:]] == [
        "WEATHER_BUDGET_EXHAUSTED", "WEATHER_BUDGET_EXHAUSTED"]
    assert all(point["status"] == "unavailable" for point in result["locations"][1:])
    assert weather.OPERATION_DEADLINE.get() is None, "An invocation cannot leak its deadline to the next"


def test_lambda_remaining_time_reserves_response_publication_before_provider_calls(weather, monkeypatch):
    monkeypatch.setattr(weather.time, "monotonic", lambda: 10)
    waits = []
    monkeypatch.setattr(weather, "make_dtn_request",
                        lambda *_: waits.append(weather.request_timeout()) or response({"windSpeed": 0}))
    result = weather.lambda_handler({"latitude": 0, "longitude": 0}, SimpleNamespace(
        aws_request_id="fixture", get_remaining_time_in_millis=lambda: 4000))
    assert waits == [1] and result["locations"][0]["windStatus"] == "calm"
    result = weather.lambda_handler({"latitude": 0, "longitude": 0}, SimpleNamespace(
        get_remaining_time_in_millis=lambda: 2000))
    assert waits == [1], "No provider call may begin after the response reserve"
    assert result["locations"][0]["errorCode"] == "WEATHER_BUDGET_EXHAUSTED"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_read_refusal_is_not_retried(weather, monkeypatch, status):
    requests = []
    real_client = httpx.Client

    def handle(request):
        requests.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(weather.token_manager, "get_token", lambda: "fixture")
    monkeypatch.setattr(weather.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    assert weather.make_dtn_request("/v2/conditions", {}) is None
    assert len(requests) == 1


def test_transient_read_retry_uses_one_remaining_budget_and_preserves_measured_zero(weather, monkeypatch):
    clock, requests, timeouts = [0.0], [], []
    real_client = httpx.Client
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(weather.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    monkeypatch.setattr(weather.token_manager, "get_token", lambda: "fixture")

    def handle(request):
        requests.append(request)
        return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json=response({"windSpeed": 0}))

    def client(**kwargs):
        timeouts.append(kwargs["timeout"])
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(weather.httpx, "Client", client)
    deadline = weather.OPERATION_DEADLINE.set(6)
    limits = weather.REQUEST_LIMITS.set((15.0, 2))
    try:
        result = weather.make_dtn_request("/v2/conditions", {})
    finally:
        weather.OPERATION_DEADLINE.reset(deadline)
        weather.REQUEST_LIMITS.reset(limits)
    assert len(requests) == 2 and timeouts == [6, 5] and clock[0] == 1
    assert weather.map_dtn_response(0, 0, result)["currentWeather"]["wind"]["speed"]["value"] == 0


def test_retry_after_cannot_consume_the_response_budget(weather, monkeypatch):
    requests, sleeps = [], []
    real_client = httpx.Client
    monkeypatch.setattr(weather.time, "monotonic", lambda: 0)
    monkeypatch.setattr(weather.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(weather.token_manager, "get_token", lambda: "fixture")

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "999999"})

    monkeypatch.setattr(weather.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    deadline = weather.OPERATION_DEADLINE.set(2)
    try:
        with pytest.raises(weather.WeatherBudgetExpired):
            weather.make_dtn_request("/v2/conditions", {})
    finally:
        weather.OPERATION_DEADLINE.reset(deadline)
    assert len(requests) == 1 and not sleeps


@pytest.mark.parametrize("setting, supplied", [
    ("DTN_OPERATION_TIMEOUT_SECONDS", None),
    ("DTN_REQUEST_TIMEOUT_SECONDS", "not-a-number"),
    ("DTN_OPERATION_TIMEOUT_SECONDS", "10"),
    ("DTN_MAX_ATTEMPTS", "4"),
    ("DTN_SERIALIZATION_RESERVE_SECONDS", "nan"),
])
def test_missing_or_invalid_declared_limits_fail_before_credentials_or_provider(
        weather, monkeypatch, setting, supplied):
    if supplied is None:
        monkeypatch.delenv(setting)
    else:
        monkeypatch.setenv(setting, supplied)

    def forbidden(*_):
        pytest.fail("Undeclared operation limits cannot begin external work")

    monkeypatch.setattr(weather, "dtn_credentials", forbidden)
    monkeypatch.setattr(weather, "make_dtn_request", forbidden)
    result = weather.lambda_handler({"latitude": 0, "longitude": 0}, None)
    assert result["error"]["code"] == "WEATHER_NOT_CONFIGURED"
    assert weather.OPERATION_DEADLINE.get() is None and weather.REQUEST_LIMITS.get() is None
