"""Cancellable P4 native capture; cancellation never leaves a model call running.

P1's birth-checked descendant cleanup and public-event projection are reused.
This separate version keeps all source adapter hashes unchanged.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

from contextlib import suppress
from typing import TYPE_CHECKING

from memos.skill_adapter.runner import _children, _consume, _terminate


if TYPE_CHECKING:
    from pathlib import Path
    from threading import Event

    from memos.skill_adapter.events import EventRecorder


def capture_process(
    command: list[str],
    cwd: Path,
    environment: dict,
    journal: EventRecorder,
    timeout: float,
    *,
    cancellation_event: Event | None = None,
) -> dict:
    """Bound capture and terminate only this run's leader and tracked descendants."""
    started = time.monotonic()
    if cancellation_event is not None and cancellation_event.is_set():
        journal.mark_incomplete("cancelled")
        return {
            "returncode": None,
            "timed_out": False,
            "cancelled": True,
            "elapsed_seconds": 0.0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "tracked_descendants": 0,
        }
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pending_hooks: set[str] = set()
    declarations: dict = {}
    buffer = b""
    total = stderr_bytes = 0
    timed_out = cancelled = limited = False
    tracked: dict[int, str] = {}
    next_scan = 0.0
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() and not limited:
            now = time.monotonic()
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                journal.mark_incomplete("cancelled")
                # Capture any descendants born after the last regular scan before
                # cleanup; birth identities prevent signaling an unrelated PID.
                if process.poll() is None:
                    tracked |= _children(process.pid)
                break
            if now >= next_scan and process.poll() is None:
                tracked |= _children(process.pid)
                next_scan = now + 0.1
            if now - started > timeout:
                timed_out = True
                journal.mark_incomplete("timeout")
                break
            for key, _ in selector.select(timeout=0.05):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_bytes += len(data)
                    continue
                total += len(data)
                buffer += data
                if total > 16 * 1024 * 1024 or len(buffer) > 2 * 1024 * 1024:
                    journal.mark_incomplete("output_limit")
                    limited = True
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError("invalid stream item")
                        _consume(message, journal, pending_hooks, declarations)
                    except (ValueError, TypeError):
                        journal.mark_incomplete("invalid_stream_json")
        if buffer.strip():
            journal.mark_incomplete("truncated_stream_line")
        if pending_hooks:
            journal.mark_incomplete("unpaired_hook")
    finally:
        try:
            _terminate(process, tracked)
        finally:
            # Even a process-control error must not leak the host's descriptors.
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3)
            selector.close()
            process.stdout.close()
            process.stderr.close()
    if process.returncode != 0:
        journal.mark_incomplete("native_process_failed")
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_bytes": total,
        "stderr_bytes": stderr_bytes,
        "tracked_descendants": len(tracked),
    }
