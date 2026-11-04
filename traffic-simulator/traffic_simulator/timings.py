"""Per-phase durations of one simulation job: wall-clock seconds by phase name, safe for public job views.

The worker records queue_wait, discovery, osm_source_metadata, osm_source_download, osm_extract or network_cache_hit,
netconvert, network_load,
public_feeds, tomtom_capture, input_bundle (or input_restore and network_restore for saved inputs), one
policy_<name> entry per policy and simulation (all policies), or result_reuse instead of those two when an identical
earlier run's result is reused, then artifact_writes and total (creation to the final status). The map holds only
these names and numbers: never URLs, keys, coordinates or provider data.
"""
import threading
import time
from contextlib import contextmanager


class Timings:
    """Collect one job's phase durations in seconds; a phase measured more than once adds up.

    The worker fetches a run's public feeds in a background thread while it prepares the road network, so phases
    can overlap (and add up to more than total) and are recorded under a lock.
    """

    def __init__(self):
        self.seconds = {}
        self.lock = threading.Lock()

    def add(self, phase, seconds):
        """Add seconds to the named phase's total."""
        with self.lock:
            self.seconds[phase] = self.seconds.get(phase, 0.0) + seconds

    @contextmanager
    def measure(self, phase):
        """Time the enclosed block as part of the named phase, also when it raises."""
        start = time.monotonic()
        try:
            yield
        finally:
            self.add(phase, time.monotonic() - start)

    def public(self):
        """Return the timings map for the job record and the job log line: phase name -> seconds, to 0.01 s."""
        with self.lock:
            return {phase: round(seconds, 2) for phase, seconds in self.seconds.items()}
