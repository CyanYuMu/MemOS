"""Real subprocess boundaries, without model calls or inherited credentials."""

import json
import os
import signal
import subprocess
import sys
import threading

from pathlib import Path

import pytest

from memos.skill_release_v2.execution import capture_native_process


class Consumer:
    def __init__(self, *, reject=False):
        self.lines = []
        self.reject = reject

    def feed(self, line):
        if self.reject:
            raise ValueError("untrusted input with secret text")
        self.lines.append(json.loads(line))

    def finish(self):
        return {"complete": bool(self.lines), "issues": []}


def run(tmp_path, program, *, timeout=3, consumer=None, cancelled=None):
    consumer = consumer or Consumer()
    failures = []
    result = capture_native_process(
        [sys.executable, "-I", "-S", "-B", "-c", program],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        consumer=consumer,
        output=tmp_path / "capture",
        timeout=timeout,
        invalidate=failures.append,
        cancellation_event=cancelled,
    )
    return result, failures, consumer


def test_retains_complete_native_bytes_and_separate_stderr(tmp_path):
    result, failures, consumer = run(
        tmp_path, 'import sys; print("{\\"native\\":1}"); print("diagnostic",file=sys.stderr)'
    )
    assert result["returncode"] == 0 and result["capture"]["complete"]
    assert failures == [] and consumer.lines == [{"native": 1}]
    assert (tmp_path / "capture" / "stdout.jsonl").read_text() == '{"native":1}\n'
    assert (tmp_path / "capture" / "stderr.txt").read_text() == "diagnostic\n"
    assert (tmp_path / "capture" / "stderr.txt").stat().st_mode & 0o777 == 0o600


def test_parse_failure_closes_run_and_does_not_echo_untrusted_exception(tmp_path):
    result, failures, _ = run(tmp_path, 'print("{\\"native\\":1}")', consumer=Consumer(reject=True))
    assert result["issues"] == ["native_consumer_rejected"]
    assert failures == ["native_consumer_rejected"]
    assert "secret" not in json.dumps(result)


def test_truncated_final_line_cannot_be_complete(tmp_path):
    result, failures, consumer = run(tmp_path, 'import sys; sys.stdout.write("{}")')
    assert "truncated_stream_line" in result["issues"]
    assert failures and consumer.lines == []


def test_real_timeout_terminates_owned_process(tmp_path):
    result, failures, _ = run(tmp_path, "import time; time.sleep(30)", timeout=0.3)
    assert result["timed_out"] and "native_timeout" in failures
    assert result["elapsed_seconds"] < 5


def test_cancel_before_start_does_not_execute_program(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    marker = tmp_path / "should-not-exist"
    result, failures, _ = run(
        tmp_path,
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
        cancelled=cancelled,
    )
    assert not marker.exists()
    assert result["cancelled"] and result["returncode"] is None
    assert failures == ["native_cancelled"]


def test_nonzero_native_exit_invalidates_even_when_json_is_well_formed(tmp_path):
    result, failures, _ = run(tmp_path, 'import sys; print("{}"); sys.exit(3)')
    assert result["returncode"] == 3
    assert "native_process_failed" in failures


def test_evidence_directory_is_never_overwritten(tmp_path):
    run(tmp_path, 'print("{}")')
    before = (tmp_path / "capture" / "stdout.jsonl").read_bytes()
    with pytest.raises(FileExistsError):
        run(tmp_path, 'print("{\\"changed\\":true}")')
    assert (tmp_path / "capture" / "stdout.jsonl").read_bytes() == before


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_budgets_do_not_launch(tmp_path, timeout):
    with pytest.raises(ValueError, match="invalid_capture_budget"):
        run(tmp_path, 'print("{}")', timeout=timeout)


def test_closed_output_limit_is_not_a_success(tmp_path):
    result, failures, _ = run(tmp_path, 'print("x" * (2 * 1024 * 1024 + 1))')
    assert "native_output_limit" in result["issues"]
    assert "native_output_limit" in failures


def test_private_directory_mode(tmp_path):
    run(tmp_path, 'print("{}")')
    assert Path(tmp_path / "capture").stat().st_mode & 0o777 == 0o700


def test_real_native_consumer_receives_complete_jsonl_frames(tmp_path):
    from tests.skill_release_v2.test_native import stream

    consumer, materials, material, calls, *rows = stream(tmp_path)
    materials["tool-1"] = material
    raw = "".join(json.dumps(row) + "\n" for row in rows)
    result, failures, _ = run(tmp_path, f"print({raw!r}, end='')", consumer=consumer)
    assert result["capture"]["complete"] and failures == []
    assert len(calls) == 1


def test_short_lived_leader_cannot_leave_an_unobserved_child(tmp_path):
    pid_file = tmp_path / "owned-child.pid"
    program = (
        "import pathlib,subprocess,sys,time; time.sleep(.04); "
        "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(20)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); print('{{}}')"
    )
    run(tmp_path, program)
    pid = int(pid_file.read_text())
    status = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
    ).stdout.strip()
    try:
        assert not status or status.startswith("Z"), "owned child survived native EOF"
    finally:
        if status and not status.startswith("Z"):
            os.kill(pid, signal.SIGKILL)


class BlockingConsumer(Consumer):
    def __init__(self, release):
        super().__init__()
        self.release = release

    def feed(self, line):
        self.release.wait(2)
        super().feed(line)


def test_blocked_consumer_cannot_delay_real_process_deadline(tmp_path):
    release = threading.Event()
    marker = tmp_path / "late-write"
    program = (
        "import pathlib,time; print('{}',flush=True); time.sleep(.3); "
        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(20)"
    )
    try:
        result, failures, _ = run(
            tmp_path, program, timeout=0.15, consumer=BlockingConsumer(release)
        )
        assert not marker.exists(), "blocked semantic consumer delayed actual process kill"
        assert result["timed_out"] and "native_timeout" in failures
        assert result["consumer_joined"] is False
        assert result["invalidation_joined"] is True
        assert not result["capture"]["complete"]
    finally:
        release.set()


def test_blocked_invalidation_cannot_delay_real_process_deadline(tmp_path):
    release = threading.Event()
    marker = tmp_path / "late-write"
    program = (
        "import pathlib,time; print('{}',flush=True); time.sleep(.3); "
        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(20)"
    )
    try:
        result = capture_native_process(
            [sys.executable, "-I", "-S", "-B", "-c", program],
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin"},
            consumer=Consumer(),
            output=tmp_path / "capture",
            timeout=0.15,
            invalidate=lambda reason: release.wait(2),
        )
        assert not marker.exists(), "blocked invalidation delayed actual process kill"
        assert result["timed_out"]
        assert result["invalidation_joined"] is False
    finally:
        release.set()


@pytest.mark.parametrize("detached", [False, True])
def test_short_lived_parent_marker_covers_unobserved_child_sessions(tmp_path, detached):
    pid_file = tmp_path / "owned-child.pid"
    program = (
        "import pathlib,subprocess,sys,time; time.sleep(.04); "
        "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(20)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        f"start_new_session={detached!r}); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); print('{{}}')"
    )
    result, _, _ = run(tmp_path, program)
    pid = int(pid_file.read_text())
    status = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
    ).stdout.strip()
    try:
        assert not status or status.startswith("Z"), "owned detached child survived"
        assert result["ownership_verified"] is True
        assert result["remaining_owned_processes"] == 0
    finally:
        if status and not status.startswith("Z"):
            os.kill(pid, signal.SIGKILL)


def test_same_uid_concurrent_unmarked_process_is_not_killed(tmp_path):
    foreign = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(20)"],
        env={"PATH": "/usr/bin:/bin"},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result, _, _ = run(tmp_path, 'import time; print("{}", flush=True); time.sleep(.1)')
        assert foreign.poll() is None
        assert result["ownership_verified"] is True
    finally:
        foreign.kill()
        foreign.wait(timeout=2)


def test_new_unmarked_process_started_during_capture_is_not_killed(tmp_path):
    ready = tmp_path / "native-started"
    foreign = []
    stop = threading.Event()

    def launch():
        while not ready.exists() and not stop.wait(0.005):
            continue
        if stop.is_set():
            return
        foreign.append(
            subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", "import time; time.sleep(20)"],
                env={"PATH": "/usr/bin:/bin"},
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    worker = threading.Thread(target=launch)
    worker.start()
    try:
        result, failures, _ = run(
            tmp_path,
            f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); "
            "print('{}',flush=True); time.sleep(.2)",
        )
        worker.join(1)
        assert foreign and foreign[0].poll() is None
        assert result["ownership_verified"] is True and failures == []
    finally:
        stop.set()
        worker.join(1)
        for process in foreign:
            process.kill()
            process.wait(timeout=2)


def test_blocked_finish_returns_incomplete_without_a_second_summary_call(tmp_path):
    release = threading.Event()

    class BlockedFinish(Consumer):
        def finish(self):
            release.wait(2)
            return super().finish()

    try:
        result, failures, _ = run(tmp_path, 'print("{}")', consumer=BlockedFinish())
        assert result["consumer_joined"] is False
        assert result["capture"]["complete"] is False
        assert "native_consumer_unjoined" in failures
        assert result["elapsed_seconds"] < 1
    finally:
        release.set()


def test_bounded_consumer_queue_failure_still_terminates_native_process(tmp_path):
    release = threading.Event()
    marker = tmp_path / "late-write"
    program = (
        "import pathlib,time; print('{}\\n'*200,flush=True); time.sleep(.3); "
        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(20)"
    )
    try:
        result, failures, _ = run(tmp_path, program, consumer=BlockingConsumer(release))
        assert "native_consumer_backpressure" in failures
        assert not marker.exists()
        assert result["capture"]["complete"] is False
    finally:
        release.set()


def test_marker_is_inherited_without_exposing_the_value(tmp_path):
    result, failures, consumer = run(
        tmp_path,
        'import json,os; print(json.dumps({"inherited": bool(os.environ.get("MEMOS_CAPTURE_OWNER"))}))',
    )
    assert consumer.lines == [{"inherited": True}]
    assert result["ownership_verified"] is True and failures == []
    assert "MEMOS_CAPTURE_OWNER" not in json.dumps(result)


def test_unavailable_ownership_scan_is_incomplete(tmp_path, monkeypatch):
    import memos.skill_release_v2.execution as execution

    def unavailable(*args):
        raise OSError("private process diagnostic")

    monkeypatch.setattr(execution, "_marked_processes", unavailable)
    result, failures, _ = run(tmp_path, 'print("{}")')
    assert result["ownership_verified"] is False
    assert result["capture"]["complete"] is False
    assert "native_ownership_unavailable" in failures
    assert "private" not in json.dumps(result)
