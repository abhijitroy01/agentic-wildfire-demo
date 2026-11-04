"""One process admission bound for general and saved paid investigations."""
import threading
from functools import cache


@cache
def investigation_slots():
    """Hold capacity until the actual Runtime worker exits, even after a browser disconnects."""
    from backend import api_settings as settings
    return threading.BoundedSemaphore(settings.investigations_per_api_process())
