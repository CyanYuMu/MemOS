"""Bounded capture supervision, independent of semantic consumers and gate locks.

A random noncredential ownership marker supplements birth-checked descendants.
Only new same-UID processes are inspected; unrelated environment contents never
leave the local matching function. This is not a container for hostile programs
that intentionally erase their inherited environment.
"""

from __future__ import annotations

import math
import os
import queue
import re
import secrets
import selectors
import signal
import subprocess
import threading
import time

from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


OUTPUT_LIMIT = 16 * 1024 * 1024
LINE_LIMIT = 2 * 1024 * 1024
QUEUE_LIMIT = 64
JOIN_TIMEOUT = 0.2
OWNER_KEY = "MEMOS_CAPTURE_OWNER"


def _process_table():
    """Read public process identity metadata; lstart retains existing birth checks."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,uid=,lstart=,stat="],
        capture_output=True,
        text=True,
        timeout=0.5,
        check=True,
    )
    table = {}
    for row in result.stdout.splitlines():
        values = row.split()
        if len(values) == 9 and all(value.isdigit() for value in values[:3]):
            table[int(values[0])] = (
                int(values[1]),
                int(values[2]),
                " ".join(values[3:8]),
                values[8],
            )
    if os.getpid() not in table:
        raise OSError("process_identity_unavailable")
    return table


def _signal_owned_group(leader: int, sig: int) -> None:
    """The unreaped leader reserves its PGID, including an all-zombie Darwin group."""
    try:
        os.killpg(leader, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=True,
        )
        for row in result.stdout.splitlines():
            values = row.split()
            if len(values) == 3 and values[1] == str(leader) and not values[2].startswith("Z"):
                raise


def _has_owner(pid, marker, birth):
    """Inspect only a candidate's environment; return solely the exact match bit."""
    result = subprocess.run(
        ["/bin/ps", "eww", "-p", str(pid), "-o", "command="],
        capture_output=True,
        timeout=0.5,
        check=False,
    )
    if result.returncode:
        current = _process_table().get(pid)
        if current is not None and current[2] == birth and not current[3].startswith("Z"):
            raise OSError("process_environment_unavailable")
        return False
    expected = OWNER_KEY.encode() + b"=" + marker.encode()
    return re.search(rb"(?:^|\s)" + re.escape(expected) + rb"(?:\s|$)", result.stdout) is not None


def _marked_processes(baseline, marker):
    candidates = _process_table()
    owned = {}
    for pid, (_, uid, birth, state) in candidates.items():
        if uid != os.getuid() or state.startswith("Z") or baseline.get(pid) == birth:
            continue
        if _has_owner(pid, marker, birth):
            owned[pid] = birth
    return owned


def _kill_identified(owned):
    current = _process_table()
    for pid, birth in owned.items():
        value = current.get(pid)
        if (
            value is not None
            and value[1] == os.getuid()
            and value[2] == birth
            and not value[3].startswith("Z")
        ):
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def capture_native_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    consumer,
    output: Path,
    timeout: float,
    invalidate: Callable[[str], None],
    cancellation_event=None,
) -> dict:
    """Kill native execution on budget even if feed, finish or invalidation blocks."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_capture_budget")
    output = Path(output)
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("capture_path_symlink")
    output.mkdir(mode=0o700, exist_ok=False)
    issues, consumer_codes = [], []
    issue_lock = threading.Lock()
    failure = threading.Event()
    stopped = threading.Event()
    reader_done = threading.Event()
    frames = queue.Queue(maxsize=QUEUE_LIMIT)
    total = {"stdout": 0, "stderr": 0}
    capture = {"complete": False, "issues": ["native_not_started"]}
    tracked = {}
    streams = {}
    reader_thread = consumer_thread = tracker_thread = None
    process = None
    timed_out = cancelled = False
    ownership_verified = True
    remaining = 0
    marker = secrets.token_hex(32)
    baseline = {}
    started = time.monotonic()

    def fail(reason):
        with issue_lock:
            if reason not in issues:
                issues.append(reason)
        failure.set()

    def consumer_error(exc):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
            with issue_lock:
                if code not in consumer_codes:
                    consumer_codes.append(code)

    def read_output():
        selector = selectors.DefaultSelector()
        buffer = b""
        try:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map() and not stopped.is_set() and not failure.is_set():
                for key, _ in selector.select(timeout=0.02):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    channel = key.data
                    if total[channel] + len(data) > OUTPUT_LIMIT:
                        fail("native_output_limit")
                        break
                    streams[channel].write(data)
                    streams[channel].flush()
                    total[channel] += len(data)
                    if channel == "stderr":
                        continue
                    buffer += data
                    if len(buffer) > LINE_LIMIT:
                        fail("native_output_limit")
                        break
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            try:
                                frames.put_nowait(line)
                            except queue.Full:
                                fail("native_consumer_backpressure")
                                break
            if buffer.strip() and not stopped.is_set() and not failure.is_set():
                fail("truncated_stream_line")
        except Exception:
            fail("native_capture_failure")
        finally:
            selector.close()
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            for stream in streams.values():
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError:
                    fail("native_evidence_write_failed")
                finally:
                    stream.close()
            reader_done.set()

    def consume():
        nonlocal capture
        try:
            while True:
                try:
                    line = frames.get(timeout=0.02)
                except queue.Empty:
                    if reader_done.is_set() or stopped.is_set():
                        break
                    continue
                try:
                    consumer.feed(line.decode("utf-8") + "\n")
                except Exception as exc:
                    consumer_error(exc)
                    fail("native_consumer_rejected")
                    break
            try:
                capture = consumer.finish()
            except Exception as exc:
                consumer_error(exc)
                fail("native_consumer_finish_failed")
                capture = {"complete": False, "issues": ["native_consumer_finish_failed"]}
        except Exception:
            fail("native_consumer_failure")

    def track_children():
        while not stopped.is_set():
            try:
                table = _process_table()
                found = {process.pid}
                while True:
                    more = {pid for pid, value in table.items() if value[0] in found}
                    if more <= found:
                        break
                    found |= more
                tracked.update({pid: table[pid][2] for pid in found - {process.pid}})
            except (OSError, subprocess.SubprocessError):
                fail("native_ownership_unavailable")
                return
            stopped.wait(0.1)

    try:
        baseline = {pid: value[2] for pid, value in _process_table().items()}
        for channel, name in (("stdout", "stdout.jsonl"), ("stderr", "stderr.txt")):
            fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            streams[channel] = os.fdopen(fd, "wb")
        if cancellation_event is not None and cancellation_event.is_set():
            cancelled = True
            fail("native_cancelled")
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env={**environment, OWNER_KEY: marker},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            started = time.monotonic()
            reader_thread = threading.Thread(
                target=read_output, name="memos-native-reader", daemon=True
            )
            consumer_thread = threading.Thread(
                target=consume, name="memos-native-consumer", daemon=True
            )
            tracker_thread = threading.Thread(
                target=track_children, name="memos-native-ownership", daemon=True
            )
            for worker in (reader_thread, consumer_thread, tracker_thread):
                worker.start()
            while not failure.is_set() and not reader_done.is_set():
                if cancellation_event is not None and cancellation_event.is_set():
                    cancelled = True
                    fail("native_cancelled")
                    break
                remaining_budget = timeout - (time.monotonic() - started)
                if remaining_budget <= 0:
                    timed_out = True
                    fail("native_timeout")
                    break
                failure.wait(min(0.01, remaining_budget))
    except (OSError, subprocess.SubprocessError):
        ownership_verified = False
        fail("native_ownership_unavailable" if process is None else "native_capture_failure")
    except Exception:
        fail("native_capture_failure")
    finally:
        # No user callback, consumer join, process table scan or wait/reap may
        # precede this kill. The live/zombie leader reserves the owned PGID.
        if process is not None:
            try:
                _signal_owned_group(process.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                fail("native_cleanup_failed")
        stopped.set()
        if tracker_thread is not None:
            tracker_thread.join(JOIN_TIMEOUT)
            if tracker_thread.is_alive():
                ownership_verified = False
                fail("native_ownership_unavailable")
        if process is not None:
            try:
                owned = dict(tracked)
                for _ in range(3):
                    owned.update(_marked_processes(baseline, marker))
                    _kill_identified(owned)
                    time.sleep(0.01)
                current = _process_table()
                survivors = {
                    pid: birth
                    for pid, birth in owned.items()
                    if pid in current
                    and current[pid][2] == birth
                    and not current[pid][3].startswith("Z")
                }
                survivors.update(_marked_processes(baseline, marker))
                remaining = len(survivors)
                if remaining:
                    fail("native_owned_processes_remaining")
            except (OSError, subprocess.SubprocessError):
                ownership_verified = False
                fail("native_ownership_unavailable")
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                fail("native_cleanup_failed")
        else:
            for stream in streams.values():
                stream.close()
        if reader_thread is not None:
            reader_thread.join(JOIN_TIMEOUT)
        reader_joined = reader_thread is None or not reader_thread.is_alive()
        if not reader_joined:
            fail("native_reader_unjoined")
        if consumer_thread is not None:
            consumer_thread.join(JOIN_TIMEOUT)
        consumer_joined = consumer_thread is None or not consumer_thread.is_alive()
        if not consumer_joined:
            fail("native_consumer_unjoined")
            capture = {"complete": False, "issues": ["native_consumer_unjoined"]}
    if process is not None and process.returncode != 0 and not issues:
        fail("native_process_failed")
    if "native_ownership_unavailable" in issues:
        ownership_verified = False

    # A blocked gate lock can outlive cleanup; it must never postpone CLI kill
    # or cause a later synchronous summary to wait on the same lock.
    def invalidate_issues():
        for reason in tuple(issues):
            try:
                invalidate(reason)
            except Exception:
                fail("native_invalidation_failed")
                return

    invalidation_thread = None
    if issues:
        invalidation_thread = threading.Thread(
            target=invalidate_issues, name="memos-native-invalidate", daemon=True
        )
        invalidation_thread.start()
        invalidation_thread.join(JOIN_TIMEOUT)
    invalidation_joined = invalidation_thread is None or not invalidation_thread.is_alive()
    if not invalidation_joined:
        fail("native_invalidation_unjoined")
    if issues:
        capture = {**capture, "complete": False}
    return {
        "returncode": process.returncode if process else None,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_bytes": total["stdout"],
        "stderr_bytes": total["stderr"],
        "tracked_descendants": len(tracked),
        "issues": list(issues),
        "consumer_codes": list(consumer_codes),
        "capture": capture,
        "consumer_joined": consumer_joined,
        "invalidation_joined": invalidation_joined,
        "reader_joined": reader_joined,
        "ownership_verified": ownership_verified,
        "remaining_owned_processes": remaining,
    }
