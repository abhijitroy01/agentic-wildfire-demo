"""Required checks in the existing release path; no separate deployment environment."""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from botocore.config import Config

from deployment import ROOT
from deployment.acceptance.smoke_simulation_api import execution_status, investigation_turn
from deployment.acceptance.visitor import checked, visitor_client, write_report

FIRE_QUESTION = "Is there a fire going near sacramento ca"
# Fixed (D38): an hour, longer than the scripts' own waits (a 30-minute run wait, a 7.5-minute investigation wait, a
# 3-minute radio wait and 65-second requests), so only a stall reaches it.
ACCEPTANCE_TIMEOUT_SECONDS = 60 * 60


def candidate_check(session, arn, endpoint, version, build, release_dir):
    """Invoke the inactive endpoint once; require actual build identity, fire-only tools and native statuses."""
    thread = str(uuid4())
    payload = {"threadId": thread, "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
               "messages": [{"id": "question", "role": "user", "content": FIRE_QUESTION}],
               "forwardedProps": {"prompt": FIRE_QUESTION}}
    client = session.client("bedrock-agentcore", config=Config(
        read_timeout=190, connect_timeout=10, retries={"max_attempts": 0}))
    response = client.invoke_agent_runtime(agentRuntimeArn=arn, qualifier=endpoint, runtimeSessionId=thread,
        contentType="application/json", accept="text/event-stream", payload=json.dumps(payload).encode())
    stream = response["response"]
    calls, statuses, execution, snapshot, finished = {}, [], None, None, False
    report = {"endpoint": endpoint, "version": version, "expected_build": build, "status": "running"}
    deadline, size = time.monotonic()+210, 0
    try:
        # EventEncoder emits one JSON data line per event. Stop at the terminal event; never wait for EOF.
        for raw in stream.iter_lines(chunk_size=1024):
            size += len(raw)
            if size > 10*1024*1024 or time.monotonic() >= deadline:
                raise RuntimeError("Candidate invocation exceeded the acceptance bound")
            if not raw.startswith(b"data: "):
                continue
            event = json.loads(raw[6:])
            if event["type"] == "RUN_ERROR":
                raise RuntimeError("Candidate Runtime failed")
            if event["type"] == "TOOL_CALL_START":
                calls[event["toolCallId"]] = event["toolCallName"]
            elif event["type"] == "TOOL_CALL_RESULT":
                statuses.append(execution_status(event, calls))
            elif event["type"] == "CUSTOM" and event.get("name") == "runtime_execution":
                execution = event["value"]
            elif event["type"] == "STATE_SNAPSHOT":
                snapshot = event["snapshot"]
            elif event["type"] == "RUN_FINISHED":
                finished = True
                break
        content = (snapshot or {}).get("content", {})
        if (not finished or not content.get("mapData") or (execution or {}).get("build_id") != build
                or "detection_agent_tool" not in calls.values() or "public_response_agent_tool" in calls.values()
                or content.get("knowledgeBaseCitations") or content.get("forecastData")
                or any(loc.get("currentWeather") for loc in content.get("mapData", {}).get("locations", []))
                or not statuses or "success" not in statuses):
            raise RuntimeError("Candidate did not prove the expected build and fire-only completion")
        report["status"] = "passed"
        print("Candidate Runtime: expected build and fire-only execution passed", flush=True)
        return report
    finally:
        stream.close()
        if report["status"] != "passed":
            report["status"] = "failed"
        report.update(tools=list(calls.values()), execution=execution, snapshot=snapshot,
                      execution_statuses=statuses)
        (release_dir / "acceptance-runtime-candidate.json").write_text(json.dumps(report, indent=2))


class CutoverAcceptance:
    """Keep an owned visitor conversation in memory across promotion; cookies never enter release artifacts."""
    def __init__(self, url, release_dir):
        self.client = visitor_client(url)
        self.thread = str(uuid4())
        self.path = release_dir / "acceptance-cutover.json"
        self.report = {"status": "running", "turns": []}

    def save(self):
        write_report(self.path, self.report)

    def seed(self, binding):
        checked(self.client.post("demo/session"))
        receipt = investigation_turn(self.client, self.thread, FIRE_QUESTION, name="before-promotion",
                                     wants_detection=True, wants_guidance=False)
        actual = receipt["identities"].get("runtime_binding", {})
        if any(actual.get(key) != value for key, value in binding.items()):
            raise RuntimeError("Pre-promotion conversation did not use the prior API binding")
        self.report["turns"].append(receipt)
        self.save()

    def finish(self, binding, build):
        before = self.report["turns"][0]
        original = before["identities"]["runtime_binding"]
        reference = before["snapshot"]["content"].get("location")
        for name, question, detection, guidance in (
            ("weather-after-promotion", "What is the current weather there?", True, False),
            ("guidance-after-promotion", "Give me response guidance.", False, True),
            ("fire-after-guidance", FIRE_QUESTION, True, False),
        ):
            receipt = investigation_turn(self.client, self.thread, question, name=name,
                wants_detection=detection, wants_guidance=guidance, expected_build=build)
            self.report["turns"].append(receipt)
            self.save()
            actual = receipt["identities"]["runtime_binding"]
            execution = receipt["identities"]["runtime_execution"]
            if (any(actual.get(key) != value for key, value in binding.items())
                    or actual["conversation_id"] != original["conversation_id"]
                    or actual["execution_session"] == original["execution_session"]
                    or execution.get("memory_session_id") != original["conversation_id"]
                    or execution.get("restored_messages", 0) < 2):
                raise RuntimeError("Promotion did not change execution while preserving native Memory")
            content = receipt["snapshot"]["content"]
            if name == "weather-after-promotion":
                location = content.get("location")
                weather = [loc for loc in (content.get("mapData") or {}).get("locations", [])
                           if loc.get("currentWeather")]
                if (not reference or not location or not weather
                        or any(loc.get("fire") for loc in content["mapData"]["locations"])
                        or any(abs(reference[key]-value[key]) > 0.000001
                               for value in [location, *weather] for key in ("latitude", "longitude"))):
                    raise RuntimeError("The promoted conversation lost its remembered weather location")
            if name == "fire-after-guidance" and (
                    content.get("forecastData") or any(loc.get("currentWeather")
                    for loc in (content.get("mapData") or {}).get("locations", []))):
                raise RuntimeError("Fire-only request fetched weather after guidance")
        self.report["status"] = "passed"
        self.save()
        print("Existing conversation: new execution build, retained Memory and scoped follow-ups passed", flush=True)
        return {"status": "passed", "receipt": self.path.name}

    def close(self):
        self.client.close()


def browser_environment(env, root=ROOT):
    """Return the browser harness environment; PLAYWRIGHT_MODULE defaults to the Playwright ui/package.json pins."""
    if not env.get("SIMULATION_FIXTURE"):
        raise ValueError("Set SIMULATION_FIXTURE for the required browser acceptance")
    if env.get("PLAYWRIGHT_MODULE"):
        return dict(env)
    module = root / "ui/node_modules/playwright"
    pinned = json.loads((root / "ui/package.json").read_text())["devDependencies"]["playwright"]
    installed = module / "package.json"
    if not installed.is_file() or json.loads(installed.read_text()).get("version") != pinned:
        raise ValueError(f"Install the pinned Playwright {pinned} with npm ci in ui/, or set PLAYWRIGHT_MODULE")
    return {**env, "PLAYWRIGHT_MODULE": str(module)}


def browser_check(command, env, release_dir):
    """Test captured production UI behavior locally with the maintained SUMO fixture harness."""
    env = browser_environment(env)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    output = release_dir / "browser"
    output.mkdir()
    with (output / "preview.log").open("w") as log:
        # Fixed checkout-local Vite executable, loopback only, no shell.
        preview = subprocess.Popen(  # nosec B603 - fixed argument list, no shell
            ["node", str(ROOT / "ui/node_modules/vite/bin/vite.js"), "preview", "--host", "127.0.0.1",
             "--port", str(port), "--strictPort"], cwd=ROOT / "ui", env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310 - fixed loopback HTTP
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                if preview.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Production UI preview failed; inspect browser/preview.log")
                time.sleep(0.25)
            command(["node", "ui/scripts/check_evacuation_workspace.cjs"], ROOT, {
                **env, "FABRIC_TEST_URL": url, "WFM_BROWSER_OUTPUT": str(output)})
        finally:
            preview.terminate()
            try:
                preview.wait(timeout=5)
            except subprocess.TimeoutExpired:
                preview.kill()
                preview.wait(timeout=5)


def live_checks(command, env, release_dir, url, *, camera=False):
    """Require real radio, investigation/SUMO, camera and response/report workflows before UI publication.

    camera is whether the installation configures the camera ([camera] device_id): the investigation run checks
    the camera routes against it inside its own demo session.
    """
    python = Path(sys.executable)
    expected = "configured" if camera else "not_configured"
    checks = {}
    for name, script, options in (
        ("investigation", "smoke_simulation_api.py",
         ["--submit", "--investigate", "--radio", "--vehicles", "200", "--radius-km", "2", "--camera", expected]),
        ("response", "accept_firetruck.py", []),
    ):
        receipt = release_dir / f"acceptance-{name}.json"
        # As modules from the repository root, so the scripts import the shared visitor client. The timeout ends the
        # whole process, so a host that stalls the status line, the headers or the chunk framing cannot hold it.
        try:
            command([python, "-m", "deployment.acceptance." + script.removesuffix(".py"), "--base-url", url,
                     "--output", receipt, *options], ROOT, env, timeout=ACCEPTANCE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Required {name} acceptance did not finish within {ACCEPTANCE_TIMEOUT_SECONDS // 60} minutes, so "
                f"the deployer stopped it. {receipt.name} shows the last step it completed. Check the workspace and "
                "its providers, then run deploy-all.sh --apply again.") from None
        report = json.loads(receipt.read_text())
        if report.get("status") != "passed":
            raise RuntimeError(f"Required {name} acceptance did not pass")
        checks[name] = {"status": "passed", "receipt": Path(receipt).name}
        if name == "investigation":
            if (report.get("camera") or {}).get("status") != "passed":
                raise RuntimeError("Required camera acceptance did not pass")
            checks["camera"] = {"status": "passed", "expected": expected, "receipt": Path(receipt).name}
    return checks
