"""Fixed fire-risk transport entrypoints; numerical computation remains in the private modeler."""
import httpx
from fire_risk_modeler.landscape import LandscapeError
from fire_risk_modeler.models import FireObservation, LandscapeAssessmentRequest
from traffic_simulator.errors import IntegrationError


def transport_failure(exc, operation):
    """Preserve curated scientific errors and explicit model admission separately from provider throttling."""
    if isinstance(exc, LandscapeError):
        return IntegrationError(exc.failure.code, exc.failure.message, 503 if exc.retryable else 422,
                                retryable=exc.retryable, retry_after=exc.retry_after,
                                reason=":".join(str(value) for value in (
                                    exc.role, exc.stage, exc.provider_status) if value is not None))
    if operation == "model" and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        delay = exc.response.headers.get("Retry-After", "")
        if delay.isdecimal() and len(delay) <= 10 and 0 < int(delay) <= 2147483647:
            return IntegrationError("MODEL_BUSY", "Fire risk model capacity is temporarily full.", 429,
                                    retryable=True, retry_after=int(delay))
    return exc


def deadline(value):
    """JSON null means no operation deadline; infinity is never serialized through the pipe."""
    return float("inf") if value is None else value


def weather(payload):
    from backend import fire_risk_broker
    fires = [FireObservation.model_validate(value) for value in payload["fires"]]
    return fire_risk_broker.fetch_weather(fires, deadline(payload["deadline"]), payload["operation_id"])


def landscape(payload):
    from fire_risk_modeler.landscape_acquisition import prepare_landscape
    fire = FireObservation.model_validate(payload["fire"])
    return prepare_landscape(fire, payload["radius_m"], payload["limits"]).model_dump(mode="json")


def model(payload):
    from backend import fire_risk_broker
    request = LandscapeAssessmentRequest.model_validate(payload["request"])
    return fire_risk_broker.invoke_model(request, deadline(payload["deadline"])).model_dump(mode="json")
