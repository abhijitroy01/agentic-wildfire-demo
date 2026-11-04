"""Retained SUMO browser artifact readers and stable failure mapping."""
import gzip
import json
import zlib

from botocore.exceptions import BotoCoreError, ClientError
from traffic_simulator.artifact_io import transient
from traffic_simulator.errors import IntegrationError

ARTIFACTS = ("result", "network", "sources", "manifest")


def artifact_for(store, job, name):
    """Read a completed run's retained browser artifact (one of ARTIFACTS).

    Errors: 404 for any other name, 409 not ready or undecodable, 410 missing, 503 storage outage.
    """
    if name not in ARTIFACTS:
        raise IntegrationError("ARTIFACT_NOT_FOUND", "Artifact not found.", 404)
    if job["status"] != "completed":
        raise IntegrationError("RESULT_NOT_READY", "Run has not completed.", 409)
    return retained_artifact(store, job["id"], name)


def retained_artifact(store, job_id, name):
    """Decode retained evidence after the owning route has checked identity and readiness."""
    try:
        value = store.artifact(job_id, name)
    except FileNotFoundError:
        raise IntegrationError("ARTIFACT_UNAVAILABLE", f"The retained {name} is unavailable.", 410) from None
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise IntegrationError("ARTIFACT_UNAVAILABLE", f"The retained {name} is unavailable.", 410) from None
        raise IntegrationError("ARTIFACT_STORE_UNAVAILABLE", "Result storage is unavailable.", 503,
                               retryable=transient(exc)) from None
    except BotoCoreError as exc:
        raise IntegrationError("ARTIFACT_STORE_UNAVAILABLE", "Result storage is unavailable.", 503,
                               retryable=transient(exc)) from None
    except (gzip.BadGzipFile, EOFError, zlib.error, json.JSONDecodeError, UnicodeError):
        raise IntegrationError("ARTIFACT_INVALID", f"The retained {name} cannot be decoded.", 409) from None
    if not isinstance(value, dict):
        raise IntegrationError("ARTIFACT_INVALID", f"The retained {name} is invalid.", 409)
    return value


def result_for(store, job):
    """Read a completed run's retained simulation result with the artifact_for error mapping."""
    return artifact_for(store, job, "result")
