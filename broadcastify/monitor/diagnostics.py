"""Bounded server diagnostics without provider text, payloads, URLs or traceback source lines."""
import json
import logging
import re
from pathlib import Path


def _label(value):
    """Keep only machine codes and source identifiers, never arbitrary descriptive text."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value) else None


def log_failure(logger, operation, exc, *, level=logging.ERROR, session_id=None):
    """Log exception types, AWS codes and frame positions for a bounded cause chain."""
    causes, seen = [], set()
    while exc is not None and id(exc) not in seen and len(causes) < 4:
        seen.add(id(exc))
        response = getattr(exc, "response", None)
        code = (response.get("Error") or {}).get("Code") if isinstance(response, dict) else None
        frames, trace = [], exc.__traceback__
        while trace is not None:
            frames.append({
                "file": _label(Path(trace.tb_frame.f_code.co_filename).name),
                "function": _label(trace.tb_frame.f_code.co_name),
                "line": trace.tb_lineno,
            })
            trace = trace.tb_next
        causes.append({"type": type(exc).__name__, "aws_code": _label(code), "frames": frames[-8:]})
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    logger.log(level, "radio_failure %s", json.dumps({
        "operation": operation, "session_id": session_id, "causes": causes,
    }, sort_keys=True))
