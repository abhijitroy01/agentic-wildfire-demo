"""Visitor-owned firetruck control, streaming and evidence-backed reports."""
import asyncio
import base64
import gzip
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from contextlib import suppress
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import Field, model_validator
from traffic_simulator import settings as simulator_settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, owned, record

from backend import api_settings as settings
from backend import leases, runtime_client, security_events, trusted_context
from backend.api_contracts import Contract, Identifier
from backend.demo_access import public_demo, same_origin, session_lookup
from backend.dependencies import store_for

# Provider messages, request paths and traceback text can carry URLs, payloads or keys.
logger = logging.getLogger(__name__)


def failure_diagnostic(operation, exc, **fields):
    """Log only fixed operations, opaque IDs, numeric status and bounded provider codes/types."""
    response = getattr(exc, "response", None)
    provider_code = (response.get("Error") or {}).get("Code") if isinstance(response, dict) else None
    code = getattr(exc, "code", None) or provider_code
    code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", code) else None
    logger.warning("firetruck_failure %s", json.dumps({
        "operation": operation, "cause_type": type(exc).__name__, "code": code, **fields,
    }, sort_keys=True))


class FireLocation(Contract):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    name: str = Field(default="Fire scenario", max_length=160)


class StartRequest(Contract):
    request_id: Identifier
    fire_location: FireLocation
    initial_truck_count: int = Field(default=4, ge=2, le=12)
    max_truck_count: int = Field(default=12, ge=4, le=20)

    @model_validator(mode="after")
    def counts(self):
        """Reject a firetruck run whose initial truck count exceeds the maximum."""
        if self.initial_truck_count > self.max_truck_count:
            raise ValueError("Initial trucks cannot exceed the maximum")
        return self


class ReportRequest(Contract):
    request_id: Identifier
    question: str = Field(default="Explain the suppression stages and resource activity recorded in this run.",
                          min_length=1, max_length=2000)


async def upstream(method, path, body=None, owner=None):
    """Call the private firetruck service; owner is the visitor's digest, which binds every run route."""
    operation = {"GET": "read_run", "PUT": "start_run", "DELETE": "stop_run"}.get(method, "unknown")
    if method == "GET" and path == "/health":
        operation = "health"
    method_label = method if method in ("GET", "PUT", "DELETE") else "UNKNOWN"
    provider_status = None
    base = os.getenv("FIRETRUCK_SERVICE_URL", "").rstrip("/")
    key = os.getenv("FIRETRUCK_SERVICE_KEY", "")
    parsed = urlsplit(base)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            (parsed.scheme == "http" and not parsed.hostname.endswith(".internal")) or
            parsed.path or parsed.query or parsed.fragment or parsed.username or len(key) < 32):
        exc = IntegrationError("FIRETRUCK_NOT_CONFIGURED", "Firetruck service is not configured.", 503)
        failure_diagnostic(operation, exc, method=method_label, status=exc.status, provider_status=None)
        raise exc
    # Destination comes only from deployment. No redirect, proxy, user URL or
    # browser-returned WebSocket endpoint is ever followed.
    headers = {"Authorization": "Bearer "+key, **({"X-Firetruck-Owner": owner} if owner else {})}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:  # noqa: SIM117
            async with client.stream(method, base+path, json=body, headers=headers) as response:
                provider_status = response.status_code
                if response.status_code >= 400:
                    status = response.status_code if response.status_code in (404, 409, 410, 429) else 503
                    raise IntegrationError("FIRETRUCK_UNAVAILABLE", "Firetruck service could not complete the request.",
                                           status, retryable=status in (409, 429, 503))
                if response.status_code >= 300:
                    raise IntegrationError("FIRETRUCK_UNAVAILABLE", "Unexpected service redirect.", 503)
                chunks, length = [], 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > 2*1024*1024:
                        raise IntegrationError("FIRETRUCK_INVALID", "Simulation evidence exceeds its limit.", 502)
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict):
                    raise TypeError("Invalid service response")
                return value
    except IntegrationError as exc:
        failure_diagnostic(operation, exc, method=method_label, status=exc.status,
                           provider_status=provider_status)
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        failure_diagnostic(operation, exc, method=method_label, status=503, provider_status=provider_status)
        raise IntegrationError("FIRETRUCK_UNAVAILABLE", "Firetruck service is unavailable.", 503) from None


def run_view(value):
    """Return a firetruck run for the browser; the UI builds the run's stream URL from its own origin."""
    return {"session_id": value["run_id"], "fire_location": value["configuration"]["fire_location"],
            "status": value["status"], "created_at": value["created_at"]}


def requested_run_id(owner, request_id):
    return str(uuid5(NAMESPACE_URL, digest([owner, "firetruck", str(request_id)])))


def requested_report_id(owner, run_id, request_id):
    return str(uuid5(NAMESPACE_URL, digest([owner, str(run_id), str(request_id), "report"])))


async def read_run(store, owner, run_id):
    """Read an owned run's latest evidence from the service and keep the saved status in step."""
    saved = owned(store, "firetruck", str(run_id), owner)
    result = await upstream("GET", f"/internal/runs/{saved['run_id']}", owner=digest(owner))
    if result.get("session_id") != saved["run_id"] or result.get("status") not in (
            "starting", "running", "stopped", "interrupted"):
        raise IntegrationError("FIRETRUCK_INVALID", "Invalid simulation evidence.", 502)
    if saved["status"] != result["status"]:
        revision = saved["revision"]
        saved.update(revision=revision+1, status=result["status"])
        try:
            commit(store, [(saved, revision)])
        except IntegrationError as exc:
            if exc.code != "REVISION_CONFLICT":
                raise
    return result


def report_artifact(store, report_id, value=None):
    """Read or write a firetruck report as gzipped JSON in S3 or the local data folder."""
    key = f"reports/{UUID(report_id)}/report.json.gz"
    if value is not None:
        raw = gzip.compress(json.dumps(value, allow_nan=False).encode())
        if store.cloud:
            store.s3.put_object(Bucket=simulator_settings.BUCKET, Key=key, Body=raw,
                               ContentType="application/json", ContentEncoding="gzip")
        else:
            target = store.root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(raw)
            temporary.replace(target)
        return
    if store.cloud:
        body = store.s3.get_object(Bucket=simulator_settings.BUCKET, Key=key)["Body"]
        try:
            raw = body.read()
        finally:
            body.close()
    else:
        raw = (store.root / key).read_bytes()
    return json.loads(gzip.decompress(raw))


# The report page's only inline style. Its SHA-256 hash is the page's sole style source in the
# Content-Security-Policy, so the page needs no 'unsafe-inline'.
REPORT_STYLE = ("body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 24px;color:#173047}\n"
                "section{border:1px solid #ccd7dd;border-radius:12px;padding:20px;margin:20px 0}\n"
                "pre{white-space:pre-wrap;overflow-wrap:anywhere}td,th{padding:8px;text-align:left}\n"
                "small{color:#536779}")
REPORT_CSP = ("default-src 'none'; style-src 'sha256-"
              + base64.b64encode(hashlib.sha256(REPORT_STYLE.encode()).digest()).decode()
              + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox")


def render_report(document):
    """Render the firetruck report page; every value is escaped and the model supplies no HTML."""
    evidence = document["evidence"]
    esc = lambda value: html.escape(str(value), quote=True)
    phases = "".join(f"<tr><td>{esc(p['phase'])}</td><td>{esc(p['elapsed_seconds'])} s</td></tr>"
                     for p in evidence.get("phase_history", []))
    # Running snapshots carry only the latest events; a finished run's evidence omits none.
    omitted = evidence.get("omitted_events") or 0
    earlier = (f"<p>{esc(omitted)} earlier events are counted but not stored in this evidence.</p>"
               if omitted else "")
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Firetruck simulation report</title>
<style>{REPORT_STYLE}</style><body><h1>Firetruck simulation report</h1>
<p>Synthetic scenario · {esc(evidence['status'])} · {esc(evidence['incident_elapsed_seconds'])} seconds</p>
<small>Run {esc(document['run_id'])} · Evidence {esc(document['evidence_hash'])}</small>
<section><h2>Recorded stages</h2><table><tr><th>Stage reached</th><th>Elapsed time</th></tr>{phases}</table>
<p>Stages absent from this table were not recorded as reached.</p></section>
<section><h2>AI explanation</h2><pre>{esc(document.get('narrative', 'Unavailable'))}</pre></section>
<section><h2>Recorded resource summary</h2><pre>{esc(json.dumps(evidence.get('summary'), indent=2))}</pre></section>
<section><h2>Final totals</h2><pre>{esc(json.dumps(evidence.get('final_stats'), indent=2))}</pre>
<p>Null values are unavailable, not zero.</p></section>
<section><h2>Event timeline</h2>{earlier}<pre>{esc(json.dumps(evidence.get('events', []), indent=2))}</pre></section>
<p>{esc(evidence.get('limitations', 'Synthetic activity; not validated suppression effectiveness or safety.'))}</p>
</body></html>"""


def generate_report(store, owner, report_id, document):
    """Write a firetruck report's AI narrative from owned evidence; the Runtime gets a firetruck_report context."""
    try:
        question = document["question"]
        # Keep full evidence in the artifact; send the summary, stage history and
        # a bounded event sample to the model with explicit omission accounting.
        evidence = document["evidence"]
        selected = {k: evidence.get(k) for k in (
            "status", "captured_at", "phase", "phase_history", "incident_elapsed_seconds",
            "summary", "final_stats", "limitations")}
        events = evidence.get("events", [])
        # Running evidence already omits its earlier events; count them with those left out here.
        selected.update(events=events[-80:],
                        omitted_events=int(evidence.get("omitted_events") or 0) + max(0, len(events)-80))
        context = {"evidence": selected, "question": question}
        # The context hash binds this exact evidence and question to the report purpose's signed context.
        token = trusted_context.sign({"sub": owner, "session_id": report_id, "context_hash": digest(context),
                                      "allowed_operations": []}, "firetruck_report")
        payload = runtime_client.agui_payload(report_id, report_id, report_id, question, {
            "firetruck_report_context": context, "firetruck_report_token": token})
        narrative = None
        for event in runtime_client.runtime_events(payload, time.time()+190):
            if event.get("type") == "STATE_SNAPSHOT":
                narrative = event.get("snapshot", {}).get("content", {}).get("response")
        if not isinstance(narrative, str) or not narrative.strip() or len(narrative) > 20000:
            raise ValueError("Missing bounded report narrative")
        saved = owned(store, "firetruck_report", report_id, owner)
        if saved["status"] != "running" or saved["lease_until"] < time.time():
            return
        document["narrative"] = narrative
        report_artifact(store, report_id, document)
        revision = saved["revision"]
        saved.update(revision=revision+1, status="completed")
        commit(store, [(saved, revision)])
    except Exception as exc:  # noqa: BLE001 — do not return model/provider/credential details
        logger.warning("Firetruck report %s did not complete: reason=%s", report_id, type(exc).__name__)
        saved = owned(store, "firetruck_report", report_id, owner)
        revision = saved["revision"]
        saved.update(revision=revision+1, status="failed")
        commit(store, [(saved, revision)])
    finally:
        leases.release(store, "firetruck_report_lease", digest(owner), owner, report_id)


def launch_report(store, user, report_id, document):
    threading.Thread(target=generate_report, args=(store, user, report_id, document), daemon=True).start()


def create_firetruck_router(owner_dependency):
    """Build the firetruck demonstration routes that relay to the private firetruck service."""
    router = APIRouter(prefix="/api/v1/firetruck", tags=["Firetruck demonstration"])

    @router.get("/health")
    async def health(user: str = Depends(owner_dependency)):
        return await upstream("GET", "/health")

    @router.get("/runs")
    def runs(request: Request, user: str = Depends(owner_dependency)):
        store = store_for(request)
        index = store.get(f"firetruck_owner:{digest(user)}") or {}
        return [run_view(owned(store, "firetruck", rid, user)) for rid in index.get("run_ids", [])]

    @router.post("/runs", status_code=201)
    async def start(body: StartRequest, request: Request, user: str = Depends(owner_dependency)):
        store = store_for(request)
        run_id = requested_run_id(user, body.request_id)
        configuration = body.model_dump(mode="json", exclude={"request_id"})
        previous = store.get(f"firetruck:{run_id}")
        if previous and previous["configuration"] != configuration:
            raise IntegrationError("IDEMPOTENCY_CONFLICT", "Request ID was reused with different inputs.", 409)
        if not previous:
            index_id = digest(user)
            saved = store.get(f"firetruck_owner:{index_id}")
            index = saved or record("firetruck_owner", index_id, user, run_ids=[])
            # Per-session limits from deployment/settings.toml (capacity.firetruck_*). A run older than its lifetime
            # has ended in the service, whatever status was last saved here, so it no longer counts as active.
            if len(index["run_ids"]) >= settings.firetruck_runs_per_session():
                raise IntegrationError("RUN_LIMIT", "This demonstration session has reached its run limit.", 429)
            active = [owned(store, "firetruck", rid, user) for rid in index["run_ids"]]
            lifetime = settings.firetruck_run_lifetime_minutes() * 60
            if sum(r["status"] in ("starting", "running") and r["created_at"]+lifetime > time.time()
                   for r in active) >= settings.firetruck_active_runs_per_session():
                raise IntegrationError("RUN_LIMIT", "Stop an active firetruck run before starting another.", 429)
            previous = record("firetruck", run_id, user, run_id=run_id, configuration=configuration, status="starting")
            # Expect the revision this request read, so a concurrent start that committed first conflicts here
            # instead of being overwritten.
            old = saved["revision"] if saved else None
            index.update(revision=(old or 0)+1, run_ids=[*index["run_ids"], run_id])
            commit(store, [(previous, None), (index, old)])
        if previous["status"] == "starting":
            try:
                await upstream("PUT", f"/internal/runs/{run_id}", configuration, owner=digest(user))
            except IntegrationError as exc:
                # A lost acknowledgement can follow a successful PUT. Keep its identity and active slot;
                # the same idempotent PUT or owned GET reconciles it without starting another run.
                if exc.status in (410, 429):
                    failed = owned(store, "firetruck", run_id, user)
                    revision = failed["revision"]
                    failed.update(revision=revision+1, status="failed")
                    with suppress(IntegrationError):
                        commit(store, [(failed, revision)])
                raise
            await read_run(store, user, run_id)
        return run_view(owned(store, "firetruck", run_id, user))

    @router.get("/requests/{request_id}")
    async def start_status(request_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        store = store_for(request)
        run_id = requested_run_id(user, request_id)
        saved = owned(store, "firetruck", run_id, user)
        if saved["status"] == "starting":
            try:
                await read_run(store, user, run_id)
            except IntegrationError as exc:
                if exc.status not in (404, 503):
                    raise
            saved = owned(store, "firetruck", run_id, user)
        return {"request_id": str(request_id), **run_view(saved)}

    @router.get("/runs/{run_id}")
    async def get(run_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        return await read_run(store_for(request), user, run_id)

    @router.delete("/runs/{run_id}")
    async def stop(run_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        saved = owned(store_for(request), "firetruck", str(run_id), user)
        return await upstream("DELETE", f"/internal/runs/{saved['run_id']}", owner=digest(user))

    @router.websocket("/runs/{run_id}/stream")
    async def stream(socket: WebSocket, run_id: UUID):
        """Stream an owned run's updates read-only; a refusal closes with 1008 and is a security event."""
        try:
            if not public_demo(socket):
                raise IntegrationError("DEMO_SESSION_REQUIRED", "Public workspace required.", 401)
            same_origin(socket)
            session, reason = session_lookup(socket)
            if not session:
                raise IntegrationError("DEMO_SESSION_REQUIRED", "Session required.", 401, reason=reason)
            owner = session["subject"]
            security_events.bind_owner(socket, owner)
            store = store_for(socket)
            owned(store, "firetruck", str(run_id), owner)
            lease_id = digest(owner)
            previous = store.get(f"firetruck_streams:{lease_id}")
            lease = previous or record("firetruck_streams", lease_id, owner, connections={})
            connections = {k: v for k, v in lease["connections"].items() if v > time.time()}
            if len(connections) >= settings.firetruck_streams_per_session():
                raise IntegrationError("STREAM_LIMIT", "Too many open simulation streams.", 429)
            connection = str(uuid4())
            deadline = min(session["expires"], time.time() + settings.firetruck_stream_lifetime_minutes() * 60)
            connections[connection] = deadline+30
            revision = previous["revision"] if previous else None
            lease.update(revision=(revision or 0)+1, connections=connections)
            commit(store, [(lease, revision)])
        except IntegrationError as exc:
            # WebSocket refusals bypass the HTTP error handler, so they are logged here.
            security_events.refused(socket, exc)
            await socket.close(code=1008)
            return
        try:
            await socket.accept()
            await socket.send_json({"event_type": "CONNECTED", "session_id": str(run_id)})
            while time.time() < deadline:
                result = await read_run(store, owner, run_id)
                await socket.send_json({**result, "event_type": "TRUCK_UPDATE"})
                if result["status"] in ("stopped", "interrupted"):
                    await socket.send_json({"event_type": "SIMULATION_STOPPED", "status": result["status"],
                                            "session_id": str(run_id)})
                    break
                try:
                    event = await asyncio.wait_for(socket.receive(), timeout=2)
                    # This is a read-only stream. Disconnect immediately, and reject
                    # application messages rather than buffering unconsumed input.
                    if event["type"] == "websocket.disconnect":
                        return
                    await socket.close(code=1008)
                    return
                except TimeoutError:
                    continue
        except IntegrationError as exc:
            failure_diagnostic("stream_read", exc, method="WEBSOCKET", status=exc.status, run_id=str(run_id))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            current = store.get(f"firetruck_streams:{lease_id}")
            if current and connection in current["connections"]:
                revision = current["revision"]
                current["connections"].pop(connection)
                current["revision"] += 1
                try:
                    commit(store, [(current, revision)])
                except IntegrationError:
                    pass  # An expired lease is also safe after concurrent disconnects.
            try:
                await socket.close()
            except RuntimeError:
                pass

    @router.post("/runs/{run_id}/reports", status_code=202)
    async def report(run_id: UUID, body: ReportRequest, request: Request, user: str = Depends(owner_dependency)):
        store = store_for(request)
        owned(store, "firetruck", str(run_id), user)
        report_id = requested_report_id(user, run_id, body.request_id)
        previous = store.get(f"firetruck_report:{report_id}")
        if previous:
            if previous["question"] != body.question:
                raise IntegrationError("IDEMPOTENCY_CONFLICT", "Request ID was reused.", 409)
            return {"report_id": report_id, "status": previous["status"]}
        if not settings.runtime_arn():
            raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure the report agent.", 503)
        evidence = await read_run(store, user, run_id)
        lock_id = digest(user)
        # The report's narrative is one AgentCore Runtime request from the shared daily agent budget, spent once
        # the owner's report lease (fenced by the report ID) is held.
        lease = leases.acquire(store, "firetruck_report_lease", lock_id, user, 210,
                               IntegrationError("REPORT_IN_PROGRESS", "Wait for the current report.", 409),
                               spend=runtime_client.spend_agent_request, attempt_id=report_id)
        saved = record("firetruck_report", report_id, user, run_id=str(run_id), question=body.question,
                       status="running", lease_until=lease["lease_until"])
        document = {"run_id": str(run_id), "question": body.question, "evidence": evidence,
                    "evidence_hash": digest(evidence)}
        try:
            commit(store, [(saved, None)])
            report_artifact(store, report_id, document)
            launch_report(store, user, report_id, document)
        except Exception as exc:  # noqa: BLE001 — known pre-launch failure, no provider invocation to replay
            failure_diagnostic("report_setup", exc, report_id=report_id)
            try:
                # A lost commit acknowledgement can leave a running receipt although no worker started.
                # Mark only this owner's matching receipt; the conditional commit never replaces a newer write.
                current = store.get(f"firetruck_report:{report_id}")
                if current and current.get("subject") == user and current.get("status") == "running":
                    revision = current["revision"]
                    current.update(revision=revision+1, status="failed")
                    commit(store, [(current, revision)])
            except Exception as cleanup_exc:  # noqa: BLE001 — best effort after the known setup failure
                failure_diagnostic("report_setup_receipt_cleanup", cleanup_exc, report_id=report_id)
            finally:
                try:
                    leases.release(store, "firetruck_report_lease", lock_id, user, report_id)
                except Exception as cleanup_exc:  # noqa: BLE001 — retain the original setup outcome
                    failure_diagnostic("report_setup_lease_cleanup", cleanup_exc, report_id=report_id)
            raise IntegrationError("REPORT_SETUP_FAILED", "Report preparation failed before generation.", 503) from None
        return {"report_id": report_id, "status": "running"}

    @router.get("/runs/{run_id}/report-requests/{request_id}")
    def requested_report_status(run_id: UUID, request_id: UUID, request: Request,
                                user: str = Depends(owner_dependency)):
        report_id = requested_report_id(user, run_id, request_id)
        saved = owned(store_for(request), "firetruck_report", report_id, user)
        status = saved["status"]
        if status == "running" and saved["lease_until"] < time.time():
            status = "interrupted"
        return {"report_id": report_id, "status": status}

    @router.get("/reports/{report_id}")
    def report_status(report_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        saved = owned(store_for(request), "firetruck_report", str(report_id), user)
        status = saved["status"]
        if status == "running" and saved["lease_until"] < time.time():
            status = "interrupted"
        # The UI links a completed report's page itself (/firetruck/reports/{report_id}/html).
        return {"report_id": str(report_id), "status": status}

    @router.get("/reports/{report_id}/html", response_class=HTMLResponse)
    def report_html(report_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        store = store_for(request)
        saved = owned(store, "firetruck_report", str(report_id), user)
        if saved["status"] != "completed":
            raise IntegrationError("REPORT_NOT_READY", "Report has not completed.", 409)
        return HTMLResponse(render_report(report_artifact(store, str(report_id))), headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": REPORT_CSP,
        })

    return router
