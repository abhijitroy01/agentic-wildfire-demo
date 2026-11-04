"""Actual spawned-process checks for cancellation, safe failures and bounded JSON."""
import multiprocessing
import os
import time
from pathlib import Path

import httpx
import pytest
from traffic_simulator.errors import IntegrationError
from traffic_simulator.transport_work import TransportError, run_transport

from backend import fire_risk_transport

MODULE = "backend.tests.test_transport_work"
transport_failure = fire_risk_transport.transport_failure


def echo(payload):
    return {"payload": payload, "pid": os.getpid(), "method": multiprocessing.get_start_method()}


def blocking(payload):
    Path(payload["marker"]).write_text(str(os.getpid()))
    while True:
        time.sleep(0.1)


def untrusted_failure(_payload):
    raise RuntimeError("token=fixture-secret https://provider.invalid/?credential=fixture-secret")


def domain_failure(_payload):
    raise IntegrationError("WEATHER_UNAVAILABLE", "Measured weather is unavailable.", 503, retryable=True)


def landscape_failure(_payload):
    from fire_risk_modeler.landscape import LandscapeError
    raise LandscapeError("LANDSCAPE_NODATA", "The selected landscape contains missing values.")


def model(payload):
    request = httpx.Request("POST", "https://private.invalid/?secret=fixture-secret")
    response = httpx.Response(429, headers={"Retry-After": payload["delay"]}, request=request)
    raise httpx.HTTPStatusError("fixture-secret response", request=request, response=response)


def busy(payload):
    return model(payload)


def unexpectedly_exit(_payload):
    os._exit(23)


def oversized_result(_payload):
    return "x" * 2048


def test_actual_child_uses_spawn_and_returns_bounded_json():
    result = run_transport(MODULE, "echo", {"label": "fixture", "coordinates": [47.6, -122.3]}, 4096,
                           lambda: None)
    assert result["method"] == "spawn" and result["pid"] != os.getpid()
    assert result["payload"] == {"label": "fixture", "coordinates": [47.6, -122.3]}


def test_explicit_cancellation_terminates_and_joins_its_blocking_child(tmp_path):
    marker = tmp_path / "child.pid"

    def cancel():
        if marker.exists():
            raise InterruptedError("Fixture cancellation")

    with pytest.raises(InterruptedError, match="Fixture cancellation"):
        run_transport(MODULE, "blocking", {"marker": str(marker)}, 4096, cancel)
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not any(child.pid == pid for child in multiprocessing.active_children())


@pytest.mark.parametrize("operation,code,message", [
    ("domain_failure", "WEATHER_UNAVAILABLE", "Measured weather is unavailable."),
    ("landscape_failure", "LANDSCAPE_NODATA", "The selected landscape contains missing values."),
    ("untrusted_failure", "TRANSPORT_FAILED", "Blocking transport operation failed."),
])
def test_safe_domain_failures_preserve_meaning_without_upstream_exception_text(operation, code, message):
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, operation, {}, 4096, lambda: None)
    assert raised.value.code == code and raised.value.message == message
    assert "fixture-secret" not in str(raised.value)
    assert "https://" not in str(raised.value)
    if operation == "untrusted_failure":
        assert raised.value.reason == "RuntimeError"


def test_model_busy_preserves_explicit_integer_retry_after_without_provider_text():
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "model", {"delay": "7"}, 4096, lambda: None)
    assert raised.value.code == "MODEL_BUSY" and raised.value.status == 429
    assert raised.value.retryable is True and raised.value.retry_after == 7
    assert "fixture-secret" not in str(raised.value)


@pytest.mark.parametrize("delay", ["", "HTTP date", "-1", "0", "99999999999999999"])
def test_invalid_busy_retry_header_is_a_safe_terminal_transport_failure(delay):
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "model", {"delay": delay}, 4096, lambda: None)
    assert raised.value.code == "TRANSPORT_FAILED"
    assert raised.value.retry_after is None


def test_provider_throttle_is_not_relabelled_as_private_model_capacity():
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "busy", {"delay": "7"}, 4096, lambda: None)
    assert raised.value.code == "TRANSPORT_FAILED" and raised.value.retry_after is None


def test_oversized_result_and_input_cannot_cross_the_ipc_envelope():
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "oversized_result", {}, 1024, lambda: None)
    assert raised.value.code == "TRANSPORT_OUTPUT_LIMIT"
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "echo", {"data": "x" * 2048}, 1024, lambda: None)
    assert raised.value.code == "TRANSPORT_INPUT_LIMIT"


def test_child_exit_without_envelope_is_detected_and_reaped():
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "unexpectedly_exit", {}, 4096, lambda: None)
    assert raised.value.code == "TRANSPORT_EXITED"


def test_no_deadline_is_explicit_json_null():
    assert fire_risk_transport.deadline(None) == float("inf")
    assert fire_risk_transport.deadline(123.0) == 123.0
    with pytest.raises(TransportError) as raised:
        run_transport(MODULE, "echo", {"deadline": float("inf")}, 4096, lambda: None)
    assert raised.value.code == "TRANSPORT_INPUT_INVALID"
