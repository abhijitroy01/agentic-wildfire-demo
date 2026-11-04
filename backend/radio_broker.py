"""Fixed-upstream radio relay. Provider credentials never cross the browser boundary."""
import asyncio
import json
import logging
import os
import time
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK

from backend import leases, runtime_client
from backend.api_contracts import Contract
from backend.dependencies import store_for

# Relay failures are logged by exception type only: upstream error messages can carry request details.
logger = logging.getLogger(__name__)
# How long one radio investigation's relay may last before the broker ends it; the lease also frees the visitor's
# slot when a stream never closes cleanly. A protocol bound, not an operational setting (D38).
LEASE_SECONDS = 1800
# The events the radio service sends on /ws/monitor/start (investigation.run_monitor and POCMonitor); others drop.
RELAYED_EVENTS = frozenset({"connected", "simulation_audio", "log", "step_start", "step_complete", "detection",
                            "complete", "error"})
# Failures reach the browser as the broker's own text, in the event shape the UI reads; upstream detail stays here.
INVESTIGATION_FAILED = {"type": "error", "data": {"message": "Radio investigation could not complete."}}
CONNECTION_FAILED = {"type": "error", "data": {"message": "Radio connection unavailable. Please retry."}}


class RadioRequest(Contract):
    playback: bool = False


def create_radio_router(owner_dependency):
    """Build the radio stream route that relays a visitor's request to the private radio service."""
    router = APIRouter()

    @router.post("/api/v1/radio/stream")
    async def radio(body: RadioRequest, request: Request, user: str = Depends(owner_dependency)):
        private = os.getenv("RADIO_SERVICE_URL", "").rstrip("/")
        base = private.replace("http://", "ws://").replace("https://", "wss://")
        key = os.getenv("BROADCASTIFY_PRIVATE_KEY", "")
        parsed = urlsplit(base)
        private_host = bool(parsed.hostname and parsed.hostname.endswith(".internal"))
        if ((parsed.scheme != "wss" and not (private_host and parsed.scheme == "ws")) or
                not parsed.hostname or parsed.path or parsed.query or
                parsed.fragment or parsed.username or not key):
            raise IntegrationError("RADIO_NOT_CONFIGURED", "Configure the server radio connection.", 503)
        store, lease_id = store_for(request), digest(user)
        # The radio service streams the recording to Amazon Transcribe and calls Bedrock on its transcripts, so each
        # investigation is one request from the shared daily agent budget, spent once the visitor's radio lease is
        # held and before any upstream work starts.
        lease = leases.acquire(store, "radio_lease", lease_id, user, LEASE_SECONDS,
                               IntegrationError("RADIO_IN_PROGRESS", "A radio investigation is already running.", 409),
                               spend=runtime_client.spend_agent_request)
        attempt = lease["attempt_id"]
        url = base + "/ws/monitor/start?" + urlencode({"playback": str(body.playback).lower()})

        async def stream():
            terminal = False
            try:
                async with connect(url, max_size=256*1024, open_timeout=15, close_timeout=5,
                                   additional_headers={"Authorization": "Bearer "+key}) as upstream:
                    while time.time() < lease["lease_until"]:
                        if await request.is_disconnected():
                            return
                        try:
                            raw = await asyncio.wait_for(upstream.recv(), timeout=2)
                        except TimeoutError:
                            yield ": heartbeat\n\n"
                            continue
                        except ConnectionClosedOK:
                            break
                        event = json.loads(raw)
                        if not isinstance(event, dict) or event.get("type") not in RELAYED_EVENTS:
                            continue
                        if event["type"] == "error":
                            event = INVESTIGATION_FAILED
                        yield "data: " + json.dumps(event).replace(key, "[redacted]") + "\n\n"
                        # The service closes the stream after either one, so the relay ends with it.
                        if event["type"] in ("complete", "error"):
                            terminal = True
                            return
                if not terminal and not await request.is_disconnected():
                    logger.warning("Radio relay missing terminal event: attempt=%s code=RADIO_INCOMPLETE", attempt)
                    yield "data: " + json.dumps(INVESTIGATION_FAILED) + "\n\n"
            except Exception as exc:  # noqa: BLE001 — upstream errors may carry request details
                logger.warning("Radio relay ended early: attempt=%s reason=%s", attempt, type(exc).__name__)
                if not await request.is_disconnected():
                    yield "data: " + json.dumps(CONNECTION_FAILED) + "\n\n"
            finally:
                leases.release(store, "radio_lease", lease_id, user, attempt)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
    return router
