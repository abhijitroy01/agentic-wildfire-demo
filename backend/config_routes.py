"""Public API configuration, independent of any capability router."""
from fastapi import APIRouter, Request

from backend.api_contracts import API_VERSION, CAPABILITY_VERSION, ErrorResponse


def create_config_router():
    router = APIRouter(
        prefix="/api/v1", tags=["Evacuation & Crew Access"],
        responses={status: {"model": ErrorResponse} for status in (401, 404, 409, 410, 422, 503)},
    )
    @router.get("/workspace-config", operation_id="simulation_workspace_config")
    def workspace_config(request: Request):
        """Report how this request is served (public_demo, local or closed) and which services are bound."""
        # Imported per request so the offline contract exporter never loads deployment settings.
        from backend import api_settings as settings
        from backend.demo_access import access_mode
        return {"access_mode": access_mode(request),
                "agent_configured": bool(settings.runtime_arn()),
                "geocoder_configured": bool(settings.geocoder_function()),
                "camera_configured": settings.camera_binding() is not None,
                "api_version": API_VERSION, "capability_version": CAPABILITY_VERSION}

    return router
