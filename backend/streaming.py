"""Bounded AG-UI server-sent event framing and worker relay."""
import asyncio
import json
import queue
import time


def sse(event):
    """Encode one AG-UI event as a server-sent event frame."""
    return "data: " + json.dumps(event) + "\n\n"


async def relay(channel, deadline, poll_seconds, at_deadline, before_poll=None):
    """Stream a worker thread's queued AG-UI events as SSE frames until the worker ends or `deadline` passes.

    The worker puts event dicts on `channel` and then None. Each idle poll sends a heartbeat comment and waits
    poll_seconds. before_poll(), when given, returns frames to send before each poll; at `deadline` (epoch seconds)
    the relay sends at_deadline()'s frames and stops.
    """
    while time.time() < deadline:
        if before_poll:
            for frame in await before_poll():
                yield frame
        try:
            event = channel.get_nowait()
        except queue.Empty:
            yield ": heartbeat\n\n"
            await asyncio.sleep(poll_seconds)
            continue
        if event is None:
            return
        yield sse(event)
    for frame in await at_deadline():
        yield frame
