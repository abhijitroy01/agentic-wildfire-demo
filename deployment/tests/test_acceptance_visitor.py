"""The acceptance checks' shared visitor client: same-origin headers, one error style with the v1 code, receipts."""
import json
import os
import subprocess  # nosec B404 - runs only this interpreter on the repository's own modules, never a shell
import sys
from pathlib import Path

import httpx
import pytest

from deployment.acceptance import visitor
from deployment.acceptance.visitor import AcceptanceError, checked, error_code, visitor_client, write_report

REPOSITORY = Path(__file__).resolve().parents[2]
STAGING_BLOCK = json.dumps({"error": {"code": "NETWORK_NOT_ALLOWED", "message": "staging only"}}).encode()


def answer_with(monkeypatch, reply):
    """Make every visitor client answer through reply(request) instead of the network."""
    client = httpx.Client
    monkeypatch.setattr(visitor.httpx, "Client",
                        lambda **options: client(transport=httpx.MockTransport(reply), **options))


def test_visitor_client_sends_a_same_origin_browsers_headers_to_the_v1_api(monkeypatch):
    sent = []

    def reply(request):
        sent.append(request)
        return httpx.Response(200, json={"ready": True})
    answer_with(monkeypatch, reply)
    with visitor_client("https://workspace.example/") as client:
        assert checked(client.post("demo/session")).json() == {"ready": True}
        assert client.timeout == httpx.Timeout(visitor.TIMEOUT_SECONDS)
    assert str(sent[0].url) == "https://workspace.example/simulation-api/v1/demo/session"
    assert sent[0].headers["origin"] == "https://workspace.example"
    assert sent[0].headers["sec-fetch-site"] == "same-origin"
    assert "authorization" not in sent[0].headers


def test_checked_names_the_request_status_and_v1_code_but_never_the_query(monkeypatch):
    # A presigned-URL credential shape, built at runtime.
    signature = "X-Amz-Signature=" + "f" * 64
    answer_with(monkeypatch, lambda request: httpx.Response(503, json={"error": {"code": "SIMULATOR_BUSY"}}))
    with visitor_client("https://workspace.example") as client, pytest.raises(AcceptanceError) as failed:
        checked(client.get("research/jobs?" + signature))
    assert str(failed.value) == "GET /simulation-api/v1/research/jobs: HTTP 503 SIMULATOR_BUSY"
    assert isinstance(failed.value, RuntimeError), "The deployer reports RuntimeError messages as they are"


def test_checked_reads_a_streamed_error_before_naming_its_code(monkeypatch):
    # An iterator body stays unread until read() is called, as a real streamed response does.
    answer_with(monkeypatch, lambda request: httpx.Response(403, content=iter([STAGING_BLOCK])))
    expected = r"^POST /simulation-api/v1/investigation/turns: HTTP 403 NETWORK_NOT_ALLOWED$"
    with (visitor_client("https://workspace.example") as client,
          client.stream("POST", "investigation/turns", json={}) as response,
          pytest.raises(AcceptanceError, match=expected)):
        checked(response)


def test_checked_returns_success_and_refuses_errors_and_redirects_without_an_envelope(monkeypatch):
    replies = iter([httpx.Response(200, json={"ok": True}), httpx.Response(502, text="<html>Bad gateway</html>"),
                    httpx.Response(302, headers={"location": "https://elsewhere.example/"})])
    answer_with(monkeypatch, lambda request: next(replies))
    with visitor_client("https://workspace.example") as client:
        response = client.get("workspace-config")
        assert checked(response) is response
        with pytest.raises(AcceptanceError, match=r": HTTP 502 \(no v1 error code\)$"):
            checked(client.get("workspace-config"))
        with pytest.raises(AcceptanceError, match=": HTTP 302 "):
            checked(client.get("workspace-config"))


def test_error_code_reads_only_the_v1_error_envelope():
    assert error_code(STAGING_BLOCK) == "NETWORK_NOT_ALLOWED"
    for body in (b"<html>403</html>", b"[]", b'{"error": "text"}', b'{"detail": {"code": "X"}}', b""):
        assert error_code(body) == ""


def test_receipts_are_indented_json_in_a_created_folder(tmp_path):
    path = tmp_path / "release" / "acceptance-response.json"
    write_report(path, {"status": "passed", "checks": ["owned stream"]})
    assert path.read_text() == json.dumps({"status": "passed", "checks": ["owned stream"]}, indent=2) + "\n"


@pytest.mark.parametrize("module", ["smoke_simulation_api", "accept_firetruck", "rehearse_expo",
                                    "check_guidance_model"])
def test_acceptance_scripts_start_as_modules_from_the_repository_root(module):
    """The deployer and the docs run each script with -m from the repository root and nothing on PYTHONPATH."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    # This interpreter with a fixed module name and --help; no shell and no network.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(  # nosec B603 - fixed argument list, no shell
        [sys.executable, "-m", f"deployment.acceptance.{module}", "--help"], cwd=REPOSITORY, env=environment,
        capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.startswith("usage:")
