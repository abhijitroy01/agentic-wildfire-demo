"""Owned blocking transport, bounded progress and canonical partial-file boundaries."""
import multiprocessing
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from traffic_simulator import osm_extracts, osm_transport, settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.transport_work import TransportError, run_transport


def test_actual_progress_messages_preserve_result_and_do_not_publish_byte_files():
    progress = []
    result = run_transport("traffic_simulator.testing", "progress_transport", {}, 4096, lambda: None,
                           progress=lambda phase, fraction: progress.append((phase, fraction)))
    assert result["pid"] != os.getpid() and result["bytes"] == 2048
    assert progress == [("Downloading us/washington: 1024 / 2048 bytes", .04)]


def test_progress_callback_lease_failure_terminates_and_reaps_the_owned_child(tmp_path):
    marker = tmp_path / "transport.pid"

    def lost(*_):
        raise InterruptedError("lost source lease")

    with pytest.raises(InterruptedError, match="lost source lease"):
        run_transport("traffic_simulator.testing", "progress_transport",
                      {"marker": str(marker), "block_after_progress": True}, 4096, lambda: None, progress=lost)
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not any(child.pid == pid for child in multiprocessing.active_children())


@pytest.mark.parametrize("payload", [{"phase": "x" * 257}, {"fraction": float("nan")}, {"fraction": 2}])
def test_invalid_child_progress_never_crosses_the_public_callback(payload):
    progress = []
    with pytest.raises(TransportError) as raised:
        run_transport("traffic_simulator.testing", "progress_transport", payload, 4096, lambda: None,
                      progress=lambda *args: progress.append(args))
    assert raised.value.code in ("TRANSPORT_PROGRESS_INVALID", "TRANSPORT_INPUT_INVALID")
    assert progress == []


def test_download_child_writes_only_an_owned_partial_and_retains_exact_request_conditions(monkeypatch):
    from test_osm_extracts import source

    selected = source()
    root = settings.data_dir("osm-extracts", "fixture-source")
    target = root / (uuid4().hex + ".part")
    calls = []
    real_client = httpx.Client

    def respond(request):
        calls.append((str(request.url), request.headers.get("If-Match")))
        return httpx.Response(200, content=b"immutable-pbf-fixture")

    monkeypatch.setattr(osm_extracts.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    result = osm_transport.download({"source": selected, "target": str(target),
                                     "scratch_root": str(settings.DATA)})
    assert result == {"bytes": selected["bytes"]} and target.read_bytes() == b"immutable-pbf-fixture"
    assert calls == [(selected["url"], selected["etag"])]
    for candidate in (root / "source.osm.pbf", Path(settings.DATA) / (uuid4().hex + ".part")):
        with pytest.raises(IntegrationError, match="owned partial target"):
            osm_transport.download({"source": selected, "target": str(candidate),
                                    "scratch_root": str(settings.DATA)})
        assert not candidate.exists()


def test_cancelled_parent_never_starts_a_download_or_leaves_a_partial(monkeypatch):
    from test_osm_extracts import source

    target = settings.data_dir("osm-extracts", "fixture-source") / (uuid4().hex + ".part")

    def stop():
        raise InterruptedError("lost source lease")

    monkeypatch.setattr(osm_extracts.httpx, "Client", lambda **_: pytest.fail("Cancelled operation opened HTTP"))
    with pytest.raises(InterruptedError, match="lost source lease"):
        osm_extracts.download(source(), target, stop, lambda *_: None)
    assert not target.exists()
