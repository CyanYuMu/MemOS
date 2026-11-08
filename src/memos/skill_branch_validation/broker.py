"""Fixed public checks for the independent branch registry, without hidden inputs."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import tempfile
import threading

from contextlib import suppress
from pathlib import Path

from memos.skill_adapter.events import sanitize_public
from memos.skill_branch_validation.tasks import get_task, task_digest
from memos.skill_branch_validation.verifier import _no_symlinks, _timeout, policy_violations
from memos.skill_branch_validation.verifier import implementation_digest as verifier_digest
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.verifier import _set_writable
from memos.skill_verification.executor import OUTPUT_LIMIT, execute_program
from memos.skill_verification.verifier import _phase_artifacts, _read, _write, check_phase_artifacts


BROKER_VERSION = "branch-visible-1"


def implementation_digest() -> str:
    return hash_json(
        {
            "broker": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "dependencies": verifier_digest(),
        }
    )


def visible_phase(task_id: str) -> dict:
    task = get_task(task_id)
    # The wrapper embeds only the already-public locked checks. It never reads
    # oracle/reference fields or executes the candidate's replaceable test file.
    program = (
        "import sys\nfrom pathlib import Path\n"
        "root = Path(sys.argv[1]).resolve()\n"
        "sys.path.insert(0, str(root))\n"
        f"exec(compile({task.initial_files['visible_checks.py']!r}, str(root / 'visible_checks.py'), 'exec'), "
        "{'__name__': '__main__', '__file__': str(root / 'visible_checks.py')})\n"
    )
    return {"id": "branch_visible", "program": program, "files": {}, "arguments": []}


def _failure(reason: str) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": reason,
    }


def _result(task, execution: dict, job: Path) -> dict:
    streams = {}
    for name in ("stdout", "stderr"):
        path = job / (name + ".txt")
        if path.is_symlink() or path.stat().st_size > OUTPUT_LIMIT:
            raise ValueError("invalid_visible_output")
        streams[name] = path.read_text()
    reason = None
    if execution.get("cancelled"):
        reason = "test_cancelled"
    elif execution.get("timed_out"):
        reason = "test_timeout"
    elif execution.get("start_error"):
        reason = "test_start_failed"
    elif execution.get("output_limited") or execution.get("limit_reasons"):
        reason = "test_resource_limit"
    elif execution.get("background_processes") or execution.get("returncode") not in (0, 1):
        reason = "test_execution_failed"
    else:
        # Exit zero alone is not evidence that every fixed check ran. The public
        # assets have a deliberately simple ordered, fully matched output schema.
        rows = [
            re.fullmatch(r"([a-z_]+): (passed|failed \([A-Za-z_][A-Za-z0-9_]*\))", line)
            for line in streams["stdout"].splitlines()
        ]
        if (
            not all(rows)
            or [row[1] for row in rows] != list(task.required_checks)
            or execution["returncode"] != int(any(row[2] != "passed" for row in rows))
        ):
            reason = "test_execution_audit_invalid"
    return {
        "exit_code": execution.get("returncode"),
        **streams,
        "truncated": bool(execution.get("output_limited")),
        "timed_out": bool(execution.get("timed_out")),
        "execution_error": reason,
    }


class _Cancellation:
    def __init__(self, local, external):
        self.local, self.external = local, external

    def is_set(self):
        return self.local.is_set() or (self.external is not None and self.external.is_set())


class VisibleCheckBroker:
    """One fixed action, bounded execution, immutable public evidence and cleanup."""

    def __init__(
        self,
        task_id: str,
        workspace: Path,
        evidence: Path,
        *,
        max_calls: int = 20,
        timeout: float = 15,
        secret_values: tuple[str, ...] = (),
        cancellation_event=None,
    ):
        if type(max_calls) is not int or not 0 < max_calls <= 100:
            raise ValueError("invalid_visible_budget")
        _timeout(timeout)
        if cancellation_event is not None and not callable(
            getattr(cancellation_event, "is_set", None)
        ):
            raise ValueError("invalid_cancellation_event")
        _no_symlinks(Path(workspace))
        _no_symlinks(Path(evidence))
        self.workspace, self.evidence = Path(workspace).resolve(), Path(evidence).resolve()
        if self.workspace.is_relative_to(self.evidence) or self.evidence.is_relative_to(
            self.workspace
        ):
            raise ValueError("visible_evidence_overlaps_workspace")
        self.task, self.phase = get_task(task_id), visible_phase(task_id)
        self.max_calls, self.timeout, self.secret_values = max_calls, timeout, secret_values
        sanitize_public({}, secret_values)
        self.records: list[dict] = []
        self._calls = 0
        self._stop, self._active = threading.Event(), threading.Event()
        self._cancellation = _Cancellation(self._stop, cancellation_event)
        self._temporary = self._thread = self._server = None

    @property
    def active(self) -> bool:
        return self._active.is_set()

    def __enter__(self):
        self.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.store = SnapshotStore(self.evidence / "snapshots")
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix="memos-branch-broker-", dir="/tmp")
            self.socket_path = Path(self._temporary.name).resolve() / "checks.sock"
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            self._server.listen(2)
            self._server.settimeout(0.1)
            self._thread = threading.Thread(
                target=self._serve, name="memos-branch-visible", daemon=True
            )
            self._thread.start()
        except BaseException:
            if self._server is not None:
                self._server.close()
            if self._temporary is not None:
                self._temporary.cleanup()
            raise
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=4)
        self._server.close()
        if self._thread.is_alive():
            raise RuntimeError("test_broker_cleanup_failed")
        self._temporary.cleanup()

    def _serve(self):
        while not self._cancellation.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            with connection:
                try:
                    connection.settimeout(1)
                    data = bytearray()
                    while b"\n" not in data and len(data) <= 1024:
                        chunk = connection.recv(1025 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if not data.endswith(b"\n") or len(data) > 1024:
                        raise ValueError("invalid_request")
                    # Compare key/value pairs as well as the object to reject
                    # duplicate fields instead of silently applying last-wins.
                    pairs = json.loads(data, object_pairs_hook=lambda value: value)
                    if pairs != [("action", "run_visible_checks")]:
                        raise ValueError("invalid_request")
                    result = self._run()
                except (ValueError, OSError, RecursionError):
                    result = _failure("invalid_broker_request")
                with suppress(OSError):
                    connection.sendall(json.dumps(result, ensure_ascii=False).encode() + b"\n")

    def _run(self) -> dict:
        if self._cancellation.is_set():
            return _failure("test_cancelled")
        if self._calls >= self.max_calls:
            return _failure("test_budget_exhausted")
        self._calls += 1
        index = self._calls
        try:
            _no_symlinks(self.workspace)
            _no_symlinks(self.evidence / "snapshots")
            snapshot = self.store.capture(self.workspace)
            with tempfile.TemporaryDirectory(
                prefix="memos-branch-visible-", dir="/tmp"
            ) as temporary:
                candidate = self.store.restore(
                    snapshot.snapshot_id, Path(temporary).resolve() / "candidate"
                )
                if policy_violations(self.task, candidate):
                    return _failure("protected_task_changed")
                phase_job = f"checks/check-{index}/branch_visible"
                job = self.evidence / phase_job
                _no_symlinks(job)
                _set_writable(candidate, False)
                self._active.set()
                try:
                    execution = execute_program(
                        self.phase["program"],
                        candidate,
                        job,
                        self.timeout,
                        secret_values=self.secret_values,
                        cancellation_event=self._cancellation,
                    )
                    unchanged = snapshot_fingerprint(candidate) == snapshot.snapshot_id
                finally:
                    self._active.clear()
                    _set_writable(candidate, True)
                result = _result(self.task, execution, job)
                if not unchanged:
                    result["execution_error"] = "candidate_changed_during_checks"
                result["evidence_ref"] = {
                    "check_index": index,
                    "candidate_snapshot": snapshot.snapshot_id,
                }
                record = {
                    "schema_version": 1,
                    "broker_version": BROKER_VERSION,
                    "implementation_digest": implementation_digest(),
                    "task_id": self.task.task_id,
                    "asset_digest": task_digest(self.task),
                    "check_index": index,
                    "candidate_snapshot": snapshot.snapshot_id,
                    "candidate_unchanged": unchanged,
                    "executed_candidate_root": str(candidate),
                    "timeout_seconds": self.timeout,
                    "phase_job": phase_job,
                    "execution": execution,
                    "phase_artifacts": _phase_artifacts(job),
                    "result": result,
                }
                record["record_digest"] = hash_json(record)
                _write(self.evidence / f"check-{index}.json", record)
                self.records.append(record)
                return result
        except (ValueError, OSError, TypeError, RecursionError):
            return _failure("test_input_or_isolation_invalid")


def check_visible_record(record_path: Path, candidate: Path) -> bool:
    """Recompute retained public results and fixed execution policy without a rerun."""
    try:
        path, candidate = Path(record_path), Path(candidate)
        _no_symlinks(path)
        _no_symlinks(candidate)
        path = path.resolve()
        record = _read(path)
        index = record["check_index"]
        if type(index) is not int or not 1 <= index <= 100 or path.name != f"check-{index}.json":
            return False
        if record["record_digest"] != hash_json(
            {k: v for k, v in record.items() if k != "record_digest"}
        ):
            return False
        task, phase = get_task(record["task_id"]), visible_phase(record["task_id"])
        expected_job = f"checks/check-{index}/branch_visible"
        if (
            record["schema_version"] != 1
            or record["broker_version"] != BROKER_VERSION
            or record["implementation_digest"] != implementation_digest()
            or record["asset_digest"] != task_digest(task)
            or record["phase_job"] != expected_job
            or type(record["candidate_unchanged"]) is not bool
        ):
            return False
        _timeout(record["timeout_seconds"])
        root = path.parent
        _no_symlinks(root / "snapshots")
        store = SnapshotStore(root / "snapshots")
        store.verify(record["candidate_snapshot"])
        if snapshot_fingerprint(candidate) != record["candidate_snapshot"]:
            return False
        with tempfile.TemporaryDirectory(prefix="memos-branch-visible-recheck-") as temporary:
            sealed = store.restore(record["candidate_snapshot"], Path(temporary) / "candidate")
            if policy_violations(task, sealed):
                return False
        job = root / expected_job
        _no_symlinks(job)
        if _phase_artifacts(job) != record["phase_artifacts"]:
            return False
        executed_root = Path(record["executed_candidate_root"])
        if not executed_root.is_absolute():
            return False
        execution = check_phase_artifacts(
            phase, job, candidate=executed_root, timeout=record["timeout_seconds"]
        )
        result = _result(task, execution, job)
        if not record["candidate_unchanged"]:
            result["execution_error"] = "candidate_changed_during_checks"
        result["evidence_ref"] = {
            "check_index": index,
            "candidate_snapshot": record["candidate_snapshot"],
        }
        return record["execution"] == execution and record["result"] == result
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError):
        return False
