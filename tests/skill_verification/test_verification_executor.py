"""Real OS tests for one owned, non-forking verification process."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

from pathlib import Path

import pytest

from memos.skill_verification.executor import (
    OUTPUT_LIMIT,
    REPORT_LIMIT,
    execute_program,
    implementation_digest,
    policy_limits,
    profile_from_record,
)


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox backend")


def candidate(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "value.txt").write_text("unchanged")
    return root


def test_success_preserves_fixed_inputs_and_records_execution_identity(tmp_path):
    root = candidate(tmp_path)
    program = (
        "import json,sys\nfrom pathlib import Path\n"
        "assert Path('note.txt').read_text() == 'fixed'\n"
        "Path(sys.argv[2]).write_text(json.dumps({'value':Path(sys.argv[1], 'value.txt').read_text()}))\n"
        "print('verified')\n"
    )
    job = tmp_path / "job"
    result = execute_program(program, root, job, 5, files={"note.txt": "fixed"})
    assert result["returncode"] == 0
    assert not result["timed_out"] and not result["cancelled"]
    assert not result["background_processes"] and not result["output_limited"]
    assert result["isolation"]["implementation_digest"] == implementation_digest()
    assert result["program_sha256"] == hashlib.sha256(program.encode()).hexdigest()
    assert json.loads((job / "report.json").read_text()) == {"value": "unchanged"}
    assert (job / "stdout.txt").read_text() == "verified\n"
    assert (root / "value.txt").read_text() == "unchanged"
    assert json.loads((job / "execution.json").read_text()) == result
    assert (job / "program.py").read_text() == program
    assert "(deny process-fork)" in (job / "isolation.sb").read_text()


def test_explicit_placeholders_and_files_are_bound(tmp_path):
    root = candidate(tmp_path)
    result = execute_program(
        "import json,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({'text':Path(sys.argv[2]).read_text(),'cwd':str(Path.cwd())}))\n",
        root,
        tmp_path / "job",
        5,
        files={"nested/fixed.txt": "known"},
        arguments=("{report}", "{control}/nested/fixed.txt"),
    )
    assert result["returncode"] == 0
    assert json.loads((tmp_path / "job/report.json").read_text())["text"] == "known"
    assert result["arguments"] == ["{report}", "{control}/nested/fixed.txt"]


def test_profile_can_be_rebuilt_after_cleanup_and_rejects_broader_roots(tmp_path):
    root = candidate(tmp_path)
    job = tmp_path / "job"
    result = execute_program("pass", root, job, 5)
    spec = result["isolation"]["profile_spec"]
    assert not Path(spec["control_root"]).exists()
    assert (
        profile_from_record(spec, candidate_path=root, job=job)
        == (job / "isolation.sb").read_text()
    )
    for key, value in (
        ("runtime_read_roots", ["/Users"]),
        ("control_root", "/private/control"),
        ("writable_scratch", ["/private/tmp"]),
        ("forbidden_roots", []),
        ("workspace", "/"),
    ):
        changed = copy.deepcopy(spec)
        changed[key] = value
        with pytest.raises(ValueError):
            profile_from_record(changed, candidate_path=root, job=job)
    with pytest.raises(ValueError):
        profile_from_record({**spec, "network": True}, candidate_path=root, job=job)


def test_process_has_actual_cpu_file_and_descriptor_caps(tmp_path):
    root = candidate(tmp_path)
    result = execute_program(
        "import json,resource,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[2]).write_text(json.dumps({name:list(resource.getrlimit(getattr(resource,name))) "
        "for name in ['RLIMIT_CPU','RLIMIT_FSIZE','RLIMIT_NOFILE']}))",
        root,
        tmp_path / "job",
        2.2,
    )
    assert result["returncode"] == 0
    assert result["limits"] == policy_limits(2.2)
    caps = json.loads((tmp_path / "job/report.json").read_text())
    assert caps == {
        "RLIMIT_CPU": [3, 3],
        "RLIMIT_FSIZE": [REPORT_LIMIT, REPORT_LIMIT],
        "RLIMIT_NOFILE": [64, 64],
    }


def test_candidate_cannot_read_hidden_write_inputs_connect_or_fork(tmp_path):
    root = candidate(tmp_path)
    secret = tmp_path / "hidden.txt"
    secret.write_text("host sentinel")
    program = f"""
import json, os, socket, sys
from pathlib import Path
results = {{}}
def attempt(name, operation):
    try:
        operation()
    except PermissionError:
        results[name] = 'denied'
    else:
        results[name] = 'allowed'
attempt('hidden', lambda: Path({str(secret)!r}).read_text())
attempt('candidate_write', lambda: Path(sys.argv[1], 'value.txt').write_text('bad'))
attempt('control_write', lambda: Path(__file__).write_text('bad'))
attempt('network', lambda: socket.socket().connect(('127.0.0.1', 1)))
attempt('fork', os.fork)
Path(sys.argv[2]).write_text(json.dumps(results))
"""
    result = execute_program(program, root, tmp_path / "job", 5)
    assert result["returncode"] == 0
    assert set(json.loads((tmp_path / "job/report.json").read_text()).values()) == {"denied"}
    assert (root / "value.txt").read_text() == "unchanged"


def test_env_omits_credentials_and_redacts_all_public_output(tmp_path, monkeypatch):
    root = candidate(tmp_path)
    secret = "fixture-private-token-784672"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    program = (
        "import json,os,sys\nfrom pathlib import Path\n"
        "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        f"print({secret!r}); print({secret!r}, file=sys.stderr)\n"
        f"Path(sys.argv[2]).write_text(json.dumps({{'diagnostic': {secret!r}}}))\n"
    )
    result = execute_program(program, root, tmp_path / "job", 5, secret_values=(secret,))
    assert result["returncode"] == 0
    for name in ("stdout.txt", "stderr.txt", "report.json"):
        assert secret not in (tmp_path / "job" / name).read_text()


def test_timeout_reaps_own_pid_without_touching_an_unrelated_process(tmp_path):
    root = candidate(tmp_path)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        start = time.monotonic()
        result = execute_program(
            "import os,time\nprint(os.getpid(),flush=True)\ntime.sleep(30)",
            root,
            tmp_path / "job",
            0.25,
        )
        assert result["timed_out"] is True
        assert time.monotonic() - start < 3
        pid = int((tmp_path / "job/stdout.txt").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_closed_output_pipes_cannot_bypass_wall_deadline(tmp_path):
    root = candidate(tmp_path)
    started = time.monotonic()
    result = execute_program(
        "import os,time\nos.close(1); os.close(2); time.sleep(30)",
        root,
        tmp_path / "job",
        0.25,
    )
    assert result["timed_out"]
    assert time.monotonic() - started < 3


def test_cancellation_reaps_running_process_and_does_not_start_if_already_cancelled(tmp_path):
    root = candidate(tmp_path)
    cancellation = threading.Event()
    timer = threading.Timer(0.25, cancellation.set)
    timer.start()
    try:
        result = execute_program(
            "import os,time\nprint(os.getpid(),flush=True)\ntime.sleep(30)",
            root,
            tmp_path / "running",
            5,
            cancellation_event=cancellation,
        )
    finally:
        timer.cancel()
    assert result["cancelled"] is True and result["timed_out"] is False
    pid = int((tmp_path / "running/stdout.txt").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    result = execute_program(
        "raise RuntimeError('must not run')",
        root,
        tmp_path / "cancelled",
        5,
        cancellation_event=cancellation,
    )
    assert result["cancelled"] and result["returncode"] is None
    assert (tmp_path / "cancelled/stdout.txt").read_text() == ""


def test_output_and_file_limits_do_not_return_success(tmp_path):
    root = candidate(tmp_path)
    result = execute_program(
        "import os\nwhile True: os.write(1,b'x'*65536)", root, tmp_path / "output", 5
    )
    assert result["output_limited"] and result["returncode"] != 0
    assert (tmp_path / "output/stdout.txt").stat().st_size <= OUTPUT_LIMIT
    result = execute_program(
        f"import sys\nfrom pathlib import Path\nPath(sys.argv[2]).write_bytes(b'x'*{REPORT_LIMIT * 2})",
        root,
        tmp_path / "file",
        5,
    )
    assert result["returncode"] != 0
    assert (
        not (tmp_path / "file/report.json").exists()
        or (tmp_path / "file/report.json").stat().st_size <= REPORT_LIMIT
    )


def test_symlink_report_is_rejected_without_reading_target(tmp_path):
    root = candidate(tmp_path)
    result = execute_program(
        "import os,sys\nos.symlink('/etc/passwd',sys.argv[2])", root, tmp_path / "job", 5
    )
    assert result["output_limited"]
    assert not (tmp_path / "job/report.json").exists()


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "main.py", "MAIN.py", "a/../b", "a\\b", "."]
)
def test_invalid_control_paths_rejected_before_execution(tmp_path, name):
    root = candidate(tmp_path)
    with pytest.raises(ValueError):
        execute_program("", root, tmp_path / "job", 5, files={name: "bad"})
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("files", [{"a": "one", "a/b": "two"}, {"A": "one", "a": "two"}])
def test_control_file_aliases_cannot_overwrite_each_other(tmp_path, files):
    root = candidate(tmp_path)
    with pytest.raises(ValueError):
        execute_program("", root, tmp_path / "job", 5, files=files)
    assert not (tmp_path / "job").exists()


@pytest.mark.parametrize("arg", ["{unknown}", "{candidate!r}", "{candidate:20}", "{candidate.x}"])
def test_invalid_arguments_rejected_before_execution(tmp_path, arg):
    root = candidate(tmp_path)
    with pytest.raises(ValueError):
        execute_program("", root, tmp_path / "job", 5, arguments=(arg,))
    assert not (tmp_path / "job").exists()


def test_existing_job_cannot_be_overwritten(tmp_path):
    root = candidate(tmp_path)
    job = tmp_path / "job"
    job.mkdir()
    (job / "retained").write_text("first")
    with pytest.raises(FileExistsError):
        execute_program("", root, job, 5)
    assert (job / "retained").read_text() == "first"


def test_start_failure_is_a_retained_nonpassing_result(tmp_path, monkeypatch):
    from memos.skill_verification import executor

    root = candidate(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("private system detail")

    monkeypatch.setattr(executor.subprocess, "Popen", fail)
    result = execute_program("", root, tmp_path / "job", 5)
    assert result["returncode"] is None and result["start_error"] == "OSError"
    assert "private system detail" not in json.dumps(result)
    assert (tmp_path / "job/stdout.txt").read_text() == ""
