"""AgentCore Runtime calls shared by the agent turn, investigation, firetruck report and radio brokers.

A Runtime-backed request spends one unit of the shared daily agent budget, sends one AG-UI payload to the pinned
Runtime endpoint and consumes its event stream within a deadline and a byte budget. A Runtime failure surfaces as an
exception, so each caller reports one sanitized error and never the Runtime's own text.
"""
import codecs
import json
import os
import re
import time
from uuid import NAMESPACE_URL, uuid5

import boto3
from botocore.config import Config
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest

from backend import budgets, trusted_context

# Transport safety bounds apply before JSON parsing, including incomplete/heartbeat frames.
MAX_RUNTIME_BYTES = 10 * 1024 * 1024
MAX_RUNTIME_FRAME_BYTES = 1024 * 1024


def agui_payload(thread_id, run_id, message_id, question, forwarded_props):
    """Build the AG-UI request for one Runtime invocation: a single user message plus the forwarded properties."""
    return {"threadId": thread_id, "runId": run_id, "state": {}, "tools": [], "context": [],
            "messages": [{"id": message_id, "role": "user", "content": question}],
            "forwardedProps": forwarded_props}


def events_from_bytes(chunks):
    """Parse chunked UTF-8 AG-UI frames, including CRLF and split codepoints."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer, total = "", 0
    for chunk in chunks:
        total += len(chunk)
        if total > MAX_RUNTIME_BYTES:
            raise ValueError("Runtime stream exceeded its size limit")
        buffer += decoder.decode(chunk)
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            if len(frame.encode()) > MAX_RUNTIME_FRAME_BYTES:
                raise ValueError("Runtime frame exceeded its size limit")
            data = "\n".join(line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:"))
            if data:
                yield json.loads(data)
        if len(buffer.encode()) > MAX_RUNTIME_FRAME_BYTES:
            raise ValueError("Runtime frame exceeded its size limit")


def deadline_chunks(stream, deadline):
    """Bound reads even when a peer sends only comments or slowly fills a frame.

    StreamingBody.read(n) waits to fill n bytes. Reading one byte prevents a
    trickling peer from extending that wait; buffer bytes for the frame parser.
    Its public socket timeout also bounds a silent read by the remaining time.
    No abandoned reader thread or timer retains an investigation slot.
    """
    buffered = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Runtime deadline reached")
        stream.set_socket_timeout(min(190, remaining))
        chunk = stream.read(1)
        if not chunk:
            if buffered:
                yield bytes(buffered)
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Runtime deadline reached")
        buffered.extend(chunk)
        if len(buffered) >= 1024 or chunk == b"\n":
            yield bytes(buffered)
            buffered.clear()


def spend_agent_request(store):
    """Count one AgentCore Runtime (Bedrock) request in today's shared budget, else 429 DAILY_LIMIT_REACHED.

    Agent turns, investigations and firetruck reports each spend one before the Runtime is invoked, and a radio
    investigation spends one before the radio service starts transcribing.
    """
    from backend import api_settings as settings
    budgets.spend_daily(store, "agent_requests", settings.agent_requests_per_day(),
                        "Daily demo limit reached for AI agent requests. Try again tomorrow; the limit resets at "
                        "midnight UTC.")


def invoke_runtime(payload, *, timeout_seconds=None):
    """Use the pinned execution version while preserving the signed thread/Memory identity."""
    from backend import api_settings as settings
    arn = settings.runtime_arn()
    if not arn:
        raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure the FireGUard AgentCore runtime ARN.", 503)
    # The Runtime ARN names its Region; never fall back to a hardcoded one.
    region = trusted_context.arn_region(arn, "SIMULATION_RUNTIME_ARN")
    endpoint = os.getenv("SIMULATION_RUNTIME_ENDPOINT", "")
    version = os.getenv("SIMULATION_RUNTIME_VERSION", "")
    if endpoint not in {"RELEASE_A", "RELEASE_B"} or not re.fullmatch(r"[1-9][0-9]{0,4}", version):
        raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure a versioned FireGUard Runtime endpoint.", 503)
    execution_session = str(uuid5(NAMESPACE_URL, digest([arn, version, payload["threadId"]])))
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise TimeoutError("Runtime deadline reached")
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    connect_timeout = min(10, timeout_seconds / 2) if timeout_seconds is not None else 10
    read_timeout = min(190, timeout_seconds - connect_timeout) if timeout_seconds is not None else 190
    client = boto3.client("bedrock-agentcore", region_name=region,
                          config=Config(read_timeout=read_timeout, connect_timeout=connect_timeout,
                                        retries={"max_attempts": 0}))
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn, qualifier=endpoint, runtimeSessionId=execution_session,
        payload=json.dumps(payload).encode(), contentType="application/json", accept="text/event-stream")
    stream = response["response"]
    try:
        if response.get("runtimeSessionId", execution_session) != execution_session:
            raise RuntimeError("Runtime returned a different execution session")
        chunks = deadline_chunks(stream, deadline) if deadline is not None else stream.iter_chunks(chunk_size=1024)
        for event in events_from_bytes(chunks):
            yield event
            if event.get("type") == "RUN_STARTED":
                yield {"type": "CUSTOM", "name": "runtime_binding", "value": {
                    "endpoint": endpoint, "version": version, "execution_session": execution_session,
                    "conversation_id": payload["threadId"]}}
            if event.get("type") == "RUN_ERROR":
                raise RuntimeError("Runtime reported failure")
            if event.get("type") == "RUN_FINISHED":
                if (event.get("outcome") or {}).get("type", "success") != "success":
                    raise RuntimeError("Runtime did not complete")
                return
        raise RuntimeError("Runtime stream ended without terminal completion")
    finally:
        stream.close()


def runtime_events(payload, deadline, *, max_bytes=MAX_RUNTIME_BYTES):
    """Invoke the Runtime and yield its AG-UI events while `deadline` (epoch seconds) and `max_bytes` allow.

    Every caller consumes the stream through here: past the deadline or the byte budget it raises TimeoutError, and a
    RUN_ERROR or an unsuccessful RUN_FINISHED raises RuntimeError before the event is passed on, so neither reaches
    a browser and the caller reports one sanitized failure.
    """
    total = 0
    for event in invoke_runtime(payload, timeout_seconds=max(0, deadline - time.time())):
        total += len(json.dumps(event).encode())
        if time.time() >= deadline or total > max_bytes:
            raise TimeoutError("Runtime deadline or byte budget reached")
        if event.get("type") == "RUN_ERROR":
            raise RuntimeError("Runtime reported failure")
        if (event.get("type") == "RUN_FINISHED"
                and (event.get("outcome") or {}).get("type", "success") != "success"):
            raise RuntimeError("Runtime did not complete")
        yield event
