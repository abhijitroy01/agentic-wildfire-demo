"""
Audio source contract.

A single abstraction over "where MP3 bytes come from" — S3 replay for
demos and offline analysis, live wire feeds (Broadcastify Icecast, RTSP,
P25) for production. Each implementation yields raw MP3 chunks; the
FFmpeg pipeline downstream handles decoding to PCM.

**Pacing.** Per the [Amazon Transcribe streaming best
practices](https://docs.aws.amazon.com/transcribe/latest/dg/streaming.html),
audio "should be as close to real-time as possible." That makes pacing a
single-axis decision: a `speed_multiplier`.

- Replay sources (S3) honor the multiplier with `await asyncio.sleep`,
  pacing audio at ``multiplier × real-time``. Each source clamps to a
  safe upper bound to keep Transcribe happy.
- Live wire sources accept the parameter for signature symmetry but
  ignore it: the wire enforces real-time by physics. There is nothing
  to speed up.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class AudioSource(ABC):
    """Abstract base class for audio sources.

    Implementations must yield raw MP3 bytes from ``stream()`` and honor
    ``should_stop()`` for graceful early termination. ``speed_multiplier``
    is best-effort: replay sources use it to compress wall-clock, live
    sources ignore it.
    """

    def __init__(self) -> None:
        self._stop_requested = False

    @abstractmethod
    async def stream(
        self,
        start_time: float,
        *,
        speed_multiplier: float = 1.0,
        start_seconds: float = 0.0,
    ) -> AsyncIterator[bytes]:
        """Yield MP3 audio chunks.

        Args:
            start_time: Unix timestamp when streaming started. Used by
                replay sources as the wall-clock anchor for "audio at
                ``start_seconds`` was yielded at this wall time."
            speed_multiplier: How fast to deliver bytes relative to
                real-time audio duration. ``1.0`` is real-time. Values
                ``> 1.0`` are honored by replay sources up to a per-source
                safe ceiling (typically 3-4× to keep Amazon Transcribe
                streaming healthy). Live wire sources ignore the value
                because the wire enforces 1×.
            start_seconds: Optional offset within the source media to
                begin streaming from. Sources that don't support
                repositioning may ignore this.

        Yields:
            MP3 audio chunks (bytes).
        """

    @abstractmethod
    async def close(self) -> None:
        """Cleanup resources. Idempotent."""

    # ------------------------------------------------------------------
    # Stop signaling
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop_requested = True

    def should_stop(self) -> bool:
        return self._stop_requested

    def reset_stop(self) -> None:
        self._stop_requested = False
