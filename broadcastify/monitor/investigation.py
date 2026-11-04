"""Prerecorded investigation behind the private service gate (monitor/private.py)."""
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from broadcastify.monitor.auth import key_is_strong, key_matches
from broadcastify.monitor.diagnostics import log_failure
from broadcastify.monitor.events.websocket_handler import WebSocketEventHandler
from broadcastify.monitor.monitors.poc_monitor import POCMonitor
from broadcastify.monitor.websocket_manager import WebSocketManager
from broadcastify.shared.config import PRESIGNED_URL_EXPIRY_SECONDS, S3_BUCKET, log_config
from broadcastify.shared.logger import redact_api_keys_in_server_logs

logger = logging.getLogger(__name__)
redact_api_keys_in_server_logs()
ws_manager = WebSocketManager()
active_sessions = {}


@asynccontextmanager
async def lifespan(_):
    """Validate only the deployed investigation and stop its active recordings on shutdown."""
    log_config()
    if not key_is_strong(os.getenv("API_KEY", "")):
        raise RuntimeError("API_KEY must contain at least 32 characters")
    yield
    for session in active_sessions.values():
        if monitor := session.get("monitor"):
            monitor.request_stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    """Report that the service process is up (container health checks)."""
    return {"status": "healthy"}


@app.websocket("/ws/monitor/start")
async def monitor(websocket: WebSocket, api_key: str | None = Query(None), playback: bool = Query(False)):
    """Accept only the service credential supplied by the private route gate."""
    if not key_matches(api_key, os.getenv("API_KEY", "")):
        await websocket.close(code=1008)
        return
    await run_monitor(websocket, "LA County Fire", playback)


async def send_error_and_close(websocket, message, error_type, reason):
    try:
        await websocket.send_json({"type": "error", "data": {"message": message, "error_type": error_type}})
        await websocket.close(code=1011, reason=reason)
    except (WebSocketDisconnect, RuntimeError):
        return  # A disconnected client must not mask or duplicate the original failure.
    except Exception as exc:  # noqa: BLE001 — best-effort error delivery must retain the original outcome
        log_failure(logger, "error_delivery", exc, level=logging.DEBUG)


def _presign_s3_key(s3_key: str) -> str | None:
    """Presign a single S3 key for browser playback.

    Auth/throttle/network errors propagate as ``ClientError`` so the
    caller surfaces them instead of silently dropping files from the
    playlist. Returns None only when ``generate_presigned_url`` itself
    declines to sign (which it shouldn't for a well-formed key).
    """
    from broadcastify.shared.aws_clients import AWSClients
    s3 = AWSClients.s3()
    # The investigation boundary logs safe cause fields once; never log the object key or signed URL.
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )



async def run_monitor(websocket: WebSocket, feed_name: str, playback: bool):
    """Run one recording investigation and stream its events over the accepted WebSocket."""
    await websocket.accept()

    # Create session
    session_id = f"mon-{str(uuid.uuid4())[:8]}"
    await ws_manager.connect(session_id, websocket, already_accepted=True)

    logger.info("WebSocket connected for monitoring: %s", session_id)

    # Track session for graceful shutdown
    active_sessions[session_id] = {
        "status": "running",
        "start_time": datetime.now(UTC).isoformat(),
        "feed_name": feed_name,
        "monitor": None,
        "result": None,
    }

    try:
        await websocket.send_json({
            "type": "connected",
            "data": {"session_id": session_id, "status": "starting"}
        })

        event_handler = WebSocketEventHandler(ws_manager, session_id, on_disconnect=lambda: monitor.request_stop())

        async def audio_source(file_key):
            url = _presign_s3_key(file_key)
            if url:
                await websocket.send_json({"type": "simulation_audio", "data": {"url": url, "file": file_key.rsplit("/", 1)[-1]}})

        monitor = POCMonitor(
            feed_name=feed_name,
            event_handler=event_handler,
            on_audio_source=audio_source if playback else None,
        )

        active_sessions[session_id]["monitor"] = monitor

        result = await monitor.run()
        if event_handler.disconnected:
            active_sessions[session_id]["status"] = "interrupted"
            return
        if not result.success:
            # Retain the cause for type/code/frame diagnostics without rendering its message.
            raise RuntimeError("Radio processing failed") from result.error

        active_sessions[session_id]["result"] = result
        active_sessions[session_id]["status"] = "completed"

        await websocket.send_json({
            "type": "complete",
            "data": {
                "success": result.success,
                "total_detections": result.total_detections,
                "sources_processed": result.sources_processed,
                "first_detection": {
                    "location": result.first_detection.location,
                    "keywords": result.first_detection.keywords,
                    "confidence": result.first_detection.confidence,
                } if result.first_detection else None,
            }
        })

        # Close WebSocket with proper code (RFC 6455: 1000 = Normal Closure)
        await websocket.close(code=1000, reason="Monitoring complete")

    except WebSocketDisconnect:
        if session_id in active_sessions:
            active_sessions[session_id]["status"] = "interrupted"
    except Exception as exc:  # noqa: BLE001 — boundary reports failure without exposing provider details
        log_failure(logger, "monitor_processing", exc, session_id=session_id)
        if session_id in active_sessions:
            active_sessions[session_id]["status"] = "error"
        await send_error_and_close(websocket, "Radio monitoring could not complete. Please retry.",
                                   "monitoring_error", "Monitoring error")

    finally:
        await ws_manager.disconnect(session_id)
        # Clean up session
        active_sessions.pop(session_id, None)
        logger.info("WebSocket disconnected for monitoring: %s", session_id)
