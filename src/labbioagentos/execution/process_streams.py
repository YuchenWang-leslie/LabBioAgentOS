"""Bound process-stream capture before bytes enter the control-plane outcome."""

from __future__ import annotations

import asyncio
import os
import selectors
import subprocess
import time
from collections.abc import Callable

from .backend import ProcessOutcome


MAX_CAPTURED_STREAM_BYTES = 16_777_216
_CHUNK_BYTES = 65_536


def capture_process(
    process: subprocess.Popen, *, timeout_seconds: float, stop: Callable[[], None],
) -> ProcessOutcome:
    """Drain both pipes concurrently; the caller owns stopping the side effect.

    On overflow/timeout, do not mistake killing a client for stopping its job.
    A failed owner stop propagates rather than producing a confirmed outcome.
    """
    started = time.monotonic()
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    timed_out = limited = False
    stop_attempted = False
    try:
        with selectors.DefaultSelector() as selector:
            for stream in buffers:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() and not limited:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    stream = key.fileobj
                    buffer = buffers[stream]
                    room = MAX_CAPTURED_STREAM_BYTES - len(buffer)
                    chunk = os.read(stream.fileno(), min(_CHUNK_BYTES, room + 1))
                    if not chunk:
                        selector.unregister(stream)
                    else:
                        buffer.extend(chunk[:room])
                        if len(chunk) > room:
                            limited = True
                            timed_out = time.monotonic() - started >= timeout_seconds
                            break
            if not (timed_out or limited):
                try:
                    process.wait(timeout=max(0, timeout_seconds - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    timed_out = True
        if timed_out or limited:
            try:
                stop_attempted = True
                stop()
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        return ProcessOutcome(
            exit_code=None if timed_out or limited else process.returncode,
            stdout=bytes(buffers[process.stdout]), stderr=bytes(buffers[process.stderr]),
            duration_seconds=time.monotonic() - started, timed_out=timed_out,
            output_limit_exceeded=limited,
        )
    except (OSError, ValueError):
        try:
            if not stop_attempted:
                stop()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        raise
    finally:
        for stream in buffers:
            stream.close()


async def read_bounded_stream(stream, overflow: asyncio.Event) -> bytes:
    buffer = bytearray()
    while True:
        room = MAX_CAPTURED_STREAM_BYTES - len(buffer)
        chunk = await stream.read(_CHUNK_BYTES)
        if not chunk:
            return bytes(buffer)
        buffer.extend(chunk[:room])
        if len(chunk) > room:
            overflow.set()
            # Keep draining without retaining bytes while the owner stops the
            # job. A paused unread asyncio pipe can otherwise prevent wait().
