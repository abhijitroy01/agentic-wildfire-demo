import json
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from traffic_simulator.persistence import budget_day, commit, digest, record

from backend import runtime_client
from backend.tests.builders import client, owner_of

TOOLS = "/api/internal/v1/simulations/"
INVESTIGATION = {"question": "Show current weather.", "thread_id": "local-thread", "context_envelope": {}}
STORAGE_DETAIL = "fixture storage detail that stays out of the log"


def spent_agent_requests(store):
    """Return how many requests today's shared daily agent budget has counted (0 before the first)."""
    return (store.get(f"budget:agent_requests:{budget_day()[0]}") or {}).get("count", 0)


def test_a_runtime_run_error_reaches_the_browser_as_one_sanitized_frame(demo, monkeypatch, signing_secret,
                                                                         runtime_binding):
    """A Runtime RUN_ERROR ends an investigation with exactly one RUN_ERROR frame, which carries none of its text."""
    detail = "provider-" + "detail-from-the-runtime"

    class Stream:
        """A Runtime response body that starts the run, then reports a failure."""
        data = (b'data: {"type":"RUN_STARTED"}\n\n'
                + ("data: " + json.dumps({"type": "RUN_ERROR", "message": detail}) + "\n\n").encode())

        def set_socket_timeout(self, seconds):
            pass

        def read(self, size):
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

        def close(self):
            pass

    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    monkeypatch.setattr(runtime_client.boto3, "client", lambda *_, **__: SimpleNamespace(
        invoke_agent_runtime=lambda **kwargs: {"response": Stream(), "runtimeSessionId": kwargs["runtimeSessionId"]}))
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        response = browser.post("/api/v1/investigation/turns", json=INVESTIGATION)
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [frame["type"] for frame in frames] == ["RUN_STARTED", "CUSTOM", "RUN_ERROR"]
    assert frames[-1] == {"type": "RUN_ERROR", "message": "Investigation could not complete. Please retry."}
    assert detail not in response.text


def test_an_unsuccessful_run_finished_ends_an_investigation_with_one_error_and_no_receipt(demo, monkeypatch,
                                                                                          signing_secret):
    """An investigation whose Runtime run finishes unsuccessfully sends one RUN_ERROR and keeps no receipt.

    The snapshot before it is relayed, no RUN_FINISHED reaches the browser, and the investigation lease is released.
    """
    snapshot = {"type": "STATE_SNAPSHOT", "snapshot": {"content": {
        "location": {"name": "Fixture point", "latitude": 39.75, "longitude": -121.6},
        "mapData": {"summary": {"totalActiveFires": 0}, "locations": []}}}}
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda *_, **__: iter([
        snapshot, {"type": "RUN_FINISHED", "outcome": {"type": "interrupt"}}]))
    app, store = demo
    with client(app) as browser:
        browser.post("/api/v1/demo/session")
        owner = owner_of(browser)
        response = browser.post("/api/v1/investigation/turns", json=INVESTIGATION)
    session_id = uuid5(NAMESPACE_URL, digest(["investigation", owner, INVESTIGATION["thread_id"]]))
    assert store.get(f"investigation_observations:{session_id}") is None, "An unsuccessful run kept a receipt"
    assert store.get("investigation_lease:" + digest(owner))["lease_until"] == 0
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames == [snapshot, {"type": "RUN_ERROR", "message": "Investigation could not complete. Please retry."}]


def test_a_concurrent_investigation_that_loses_its_lease_commit_spends_no_budget(demo, monkeypatch, signing_secret):
    """Two starts that both read no lease: the one whose lease commit loses gets 409 and spends nothing."""
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda *_, **__: pytest.fail("The losing start ran"))
    app, store = demo
    with client(app) as browser:
        browser.post("/api/v1/demo/session")
        owner = owner_of(browser)
        key = "investigation_lease:" + digest(owner)
        # The other start committed its lease after this request read none.
        commit(store, [(record("investigation_lease", digest(owner), owner, attempt_id="other-start",
                               lease_until=time.time() + 200), None)])
        read, stale = store.get, []

        def before_the_other_commit(identifier):
            """Answer this request's first lease read as it was before the other start committed."""
            if identifier == key and not stale:
                stale.append(identifier)
                return None
            return read(identifier)

        monkeypatch.setattr(store, "get", before_the_other_commit)
        racing = browser.post("/api/v1/investigation/turns", json=INVESTIGATION)
        assert racing.status_code == 409 and racing.json()["error"]["code"] == "REVISION_CONFLICT"
    assert stale and spent_agent_requests(store) == 0


@pytest.mark.parametrize("arn", ["configured-server-runtime", "arn:aws:bedrock-agentcore::123456789012:runtime/x"])
def test_a_malformed_runtime_arn_leaves_investigations_not_configured(demo, monkeypatch, signing_secret, arn):
    """A Runtime ARN the client cannot use is refused at the route with 503, before the lease or the budget."""
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", arn)
    app, store = demo
    with client(app) as browser:
        browser.post("/api/v1/demo/session")
        refused = browser.post("/api/v1/investigation/turns", json=INVESTIGATION)
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"
        assert browser.get("/api/v1/workspace-config").json()["agent_configured"] is False
        assert store.get("investigation_lease:" + digest(owner_of(browser))) is None
    assert spent_agent_requests(store) == 0


def test_a_refused_budget_releases_the_lease_so_the_same_request_is_refused_again(demo, monkeypatch, signing_secret):
    """Past the daily budget a start gets 429 and holds no lease: asking again is refused by the budget, not 409."""
    monkeypatch.setenv("AGENT_REQUESTS_PER_DAY", "0")
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda *_, **__: pytest.fail("A refused start ran"))
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        for _ in range(2):
            refused = browser.post("/api/v1/investigation/turns", json=INVESTIGATION)
            assert refused.status_code == 429 and refused.json()["error"]["code"] == "DAILY_LIMIT_REACHED"
