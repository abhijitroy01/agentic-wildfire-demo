"""
Transcription Client
Handles Amazon Transcribe Streaming for real-time speech-to-text.
Supports early termination via stop flag.
"""

import asyncio
import inspect
import logging
import time

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

from broadcastify.monitor.constants import SAMPLE_RATE
from broadcastify.monitor.diagnostics import log_failure
from broadcastify.shared.config import (
    AWS_REGION,
    TRANSCRIBE_LANGUAGE_CODE,
    TRANSCRIBE_PARTIAL_STABILITY,
    TRANSCRIBE_VOCABULARY_NAME,
    TRANSCRIPT_WINDOW_SECONDS,
)
from broadcastify.shared.logger import LogMessages, text_size

logger = logging.getLogger(__name__)


# Allowed values per the Transcribe Streaming API
# (https://docs.aws.amazon.com/transcribe/latest/APIReference/API_streaming_StartStreamTranscription.html).
# An invalid env value is silently dropped so a typo can't take down streaming.
_VALID_STABILITY_LEVELS = {"high", "medium", "low"}


def _streaming_accuracy_kwargs() -> dict:
    """
    Build the optional kwargs that improve streaming accuracy on noisy radio
    audio.

    Returns kwargs dict — empty when neither feature is configured. Keys:
      - vocabulary_name: domain-term vocabulary registered in the account
      - enable_partial_results_stabilization + partial_results_stability:
        favor stable-and-readable partials over the default which optimizes
        purely for stability.
    """
    kwargs: dict = {}
    if TRANSCRIBE_VOCABULARY_NAME:
        kwargs["vocabulary_name"] = TRANSCRIBE_VOCABULARY_NAME
    if (
        TRANSCRIBE_PARTIAL_STABILITY
        and TRANSCRIBE_PARTIAL_STABILITY.lower() in _VALID_STABILITY_LEVELS
    ):
        kwargs["enable_partial_results_stabilization"] = True
        kwargs["partial_results_stability"] = TRANSCRIBE_PARTIAL_STABILITY.lower()
    return kwargs


class TranscriptHandler(TranscriptResultStreamHandler):
    """Handles transcript events from Amazon Transcribe."""

    def __init__(
        self,
        output_stream,
        feed_name,
        callback,
        window_seconds=15,
        stop_check=None,
    ):
        super().__init__(output_stream)
        self.feed_name = feed_name
        self.callback = callback
        self.transcript_buffer = []
        self.window_seconds = window_seconds
        self.start_time = None
        self.stop_check = stop_check  # Function to check if we should stop

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        """Detect from finalized text so an unfinished address cannot trigger a handoff."""
        # Check if we should stop
        if self.stop_check and self.stop_check():
            return

        if self.start_time is None:
            self.start_time = time.time()

        results = transcript_event.transcript.results

        for result in results:
            # Check stop flag again
            if self.stop_check and self.stop_check():
                return

            for alt in result.alternatives:
                transcript = alt.transcript

                if transcript.strip():
                    elapsed = time.time() - self.start_time

                    # Log partial and final results (size only)
                    if result.is_partial:
                        logger.debug(
                            LogMessages.TRANSCRIPTION_PARTIAL.format(
                                elapsed=elapsed, size=text_size(transcript)
                            )
                        )
                    else:
                        logger.debug(
                            LogMessages.TRANSCRIPTION_FINAL.format(
                                elapsed=elapsed, size=text_size(transcript)
                            )
                        )
                    if result.is_partial:
                        continue

                    # Keep completed utterances, not repeated prefixes of one utterance.
                    self.transcript_buffer.append({
                        "text": transcript,
                        "elapsed": elapsed,
                        "is_partial": result.is_partial,
                    })

                    # Keep only last N seconds
                    self._cleanup_buffer(elapsed)

                    # Check each completed utterance in the current window. An async
                    # callback is awaited, so slow detection runs off the event loop.
                    window_text = " ".join([
                        t["text"] for t in self.transcript_buffer
                    ])
                    outcome = self.callback(window_text, self.feed_name, elapsed)
                    if inspect.isawaitable(outcome):
                        await outcome

    def _cleanup_buffer(self, current_elapsed):
        """Keep only transcripts within window."""
        cutoff = current_elapsed - self.window_seconds

        self.transcript_buffer = [
            t for t in self.transcript_buffer
            if t["elapsed"] > cutoff
        ]


class TranscriptionClient:
    """Manages Amazon Transcribe streaming connection."""

    def __init__(self):
        self.region = AWS_REGION
        self.client = TranscribeStreamingClient(region=self.region)

    async def transcribe_stream(
        self,
        audio_stream,
        feed_name,
        on_transcript,
        stop_check=None,
    ):
        """
        Start transcription stream.

        Args:
            audio_stream: AsyncIterator yielding PCM audio chunks
            feed_name: Name of the feed being transcribed
            on_transcript: Callback function(transcript, feed_name, elapsed_time);
                a coroutine function is awaited before the next transcript event
            stop_check: Optional function that returns True if we should stop
        """
        logger.info(LogMessages.TRANSCRIPTION_STARTED.format(feed=feed_name))

        def should_stop():
            return bool(stop_check and stop_check())

        handler_task = None
        stream = None
        ended = False
        try:
            # Start stream transcription
            stream = await self.client.start_stream_transcription(
                language_code=TRANSCRIBE_LANGUAGE_CODE,
                media_sample_rate_hz=SAMPLE_RATE,
                media_encoding="pcm",
                **_streaming_accuracy_kwargs(),
            )

            # Create handler for results with window
            handler = TranscriptHandler(
                stream.output_stream,
                feed_name,
                on_transcript,
                TRANSCRIPT_WINDOW_SECONDS,
                stop_check=should_stop,
            )

            # Start processing results in background
            handler_task = asyncio.create_task(handler.handle_events())

            # Send audio chunks to Transcribe
            async for chunk in audio_stream:
                if should_stop():
                    logger.info("[%s] Stopping audio input - detection found", feed_name)
                    break
                if handler_task.done():
                    # Surface a failed detector immediately instead of streaming
                    # the rest of a long recording with a dead result consumer.
                    await handler_task
                    raise RuntimeError("Transcribe output ended before audio input")
                await stream.input_stream.send_audio_event(audio_chunk=chunk)

            # Close stream
            await stream.input_stream.end_stream()
            ended = True

            # If stopping early, don't wait for handler - cancel it
            if should_stop():
                logger.info("[%s] Cancelling transcription handler", feed_name)
                handler_task.cancel()
                try:
                    await handler_task
                except asyncio.CancelledError:
                    pass
                logger.info(
                    LogMessages.TRANSCRIPTION_STOPPED.format(feed=feed_name))
            else:
                # Wait for handler to finish processing all transcripts
                logger.debug("[%s] Waiting for transcription processing to complete...", feed_name)
                await handler_task
                logger.info(
                    LogMessages.TRANSCRIPTION_STOPPED.format(feed=feed_name))

        except asyncio.CancelledError:
            logger.info("[%s] Transcription cancelled", feed_name)
            raise
        # Other failures propagate to the investigation's safe cause diagnostics.
        finally:
            if handler_task is not None:
                if not handler_task.done():
                    handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
            if stream is not None and not ended:
                try:
                    await asyncio.wait_for(stream.input_stream.end_stream(), timeout=5)
                except Exception as exc:  # noqa: BLE001 — close must not replace the failure being raised
                    log_failure(logger, "transcribe_input_cleanup", exc, level=logging.DEBUG)
