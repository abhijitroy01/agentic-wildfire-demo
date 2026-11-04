"""Cancellable blocking transport in one owned spawned process, with bounded JSON pipes."""
import importlib
import json
import math
import multiprocessing
import re

from traffic_simulator.errors import IntegrationError


class TransportError(IntegrationError):
    """Safe transport/domain failure; reason contains only a sanitized exception type."""


def failure(exc):
    if isinstance(exc, IntegrationError):
        return {"code": exc.code, "message": exc.message, "status": exc.status,
                "retryable": exc.retryable, "retry_after": exc.retry_after, "reason": exc.reason}
    kind = type(exc).__name__
    return {"code": "TRANSPORT_FAILED", "message": "Blocking transport operation failed.", "status": 503,
            "reason": kind if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", kind) else "Exception"}


def progress_value(value):
    """Only bounded server-authored phase text and a finite fraction may cross the progress channel."""
    if (not isinstance(value, dict) or set(value) != {"phase", "fraction"}
            or not isinstance(value["phase"], str) or not 0 < len(value["phase"]) <= 256
            or type(value["fraction"]) not in (int, float)
            or not math.isfinite(value["fraction"]) or not 0 <= value["fraction"] <= 1):
        raise TransportError("TRANSPORT_PROGRESS_INVALID", "Transport progress does not match its contract.", 503)
    return value


def perform(module_name, operation, encoded, maximum_bytes, connection, report_progress):
    """Only curated domain errors cross the pipe; SDK/HTTP text, headers and bodies never do."""
    try:
        try:
            module = importlib.import_module(module_name)
            function = getattr(module, operation)

            def progress(phase, fraction):
                value = progress_value({"phase": phase, "fraction": fraction})
                message = json.dumps({"progress": value}, allow_nan=False, separators=(",", ":")).encode()
                if len(message) > min(4096, maximum_bytes):
                    raise TransportError("TRANSPORT_PROGRESS_INVALID",
                                         "Transport progress exceeded its byte limit.", 503)
                connection.send_bytes(message)

            payload = json.loads(encoded)
            result = function(payload, progress=progress) if report_progress else function(payload)
            envelope = {"ok": True, "result": result}
            raw = json.dumps(envelope, allow_nan=False, separators=(",", ":")).encode()
            if len(raw) > maximum_bytes:
                raise TransportError("TRANSPORT_OUTPUT_LIMIT", "Transport result exceeded its byte limit.", 503)
        except Exception as exc:  # noqa: BLE001 — isolate arbitrary upstream failures without copying their text
            normalize = getattr(module, "transport_failure", None) if "module" in locals() else None
            error = normalize(exc, operation) if normalize else exc
            raw = json.dumps({"ok": False, "error": failure(error)},
                             allow_nan=False).encode()
        if len(raw) <= maximum_bytes:
            connection.send_bytes(raw)
    finally:
        connection.close()


def stop_process(process):
    """Bound shutdown grace, not operation duration; interruption never leaves an owned child running."""
    if process.is_alive():
        process.terminate()
    process.join(timeout=2)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)


def run_transport(module_name, operation, payload, maximum_bytes, check_cancel, progress=None):
    """Run a trusted server operation with no wall deadline; callback controls cancellation and lease fencing."""
    if (not isinstance(module_name, str) or not re.fullmatch(
            r"(?:backend|traffic_simulator)(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+", module_name)
            or not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z_0-9]*", operation)
            or type(maximum_bytes) is not int or maximum_bytes < 1024):
        raise TransportError("TRANSPORT_INPUT_INVALID", "Transport operation does not match its contract.", 503)
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise TransportError("TRANSPORT_INPUT_INVALID", "Transport payload must be finite JSON.", 503) from None
    if len(encoded) > maximum_bytes:
        raise TransportError("TRANSPORT_INPUT_LIMIT", "Transport payload exceeded its byte limit.", 503)
    check_cancel()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=perform, args=(module_name, operation, encoded, maximum_bytes, sender,
                                                   progress is not None),
                              daemon=True, name="blocking-transport")
    try:
        try:
            process.start()
        except (OSError, RuntimeError):
            raise TransportError("TRANSPORT_FAILED", "Local transport could not start.", 503) from None
        sender.close()
        while True:
            while not receiver.poll(0.1):
                check_cancel()
                if not process.is_alive():
                    raise TransportError("TRANSPORT_EXITED", "Local transport exited without a result.", 503)
            check_cancel()
            try:
                envelope = json.loads(receiver.recv_bytes(maxlength=maximum_bytes))
            except (EOFError, OSError, ValueError):
                raise TransportError("TRANSPORT_EXITED", "Local transport returned no bounded result.", 503) from None
            check_cancel()
            if isinstance(envelope, dict) and set(envelope) == {"progress"}:
                value = progress_value(envelope["progress"])
                if progress is not None:
                    progress(value["phase"], value["fraction"])
                continue
            if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                raise TransportError("TRANSPORT_FAILED", "Local transport returned an invalid result.", 503)
            if not envelope["ok"]:
                raise TransportError(**envelope["error"])
            return envelope["result"]
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            stop_process(process)
            if not process.is_alive():
                process.close()

