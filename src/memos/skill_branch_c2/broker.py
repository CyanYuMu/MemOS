"""Selected-suite public checks and retained delivery, without hidden inputs."""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import threading

from contextlib import suppress
from pathlib import Path

from memos.skill_adapter.events import sanitize_public
from memos.skill_adapter.isolation import IsolationUnavailableError
from memos.skill_branch_c2.tasks import get_suite, suite_digest
from memos.skill_branch_c2.verifier import (
    _assess,
    _groups,
    _no_symlinks,
    _timeout,
    _valid_checks,
    policy_violations,
    suite_identity,
)
from memos.skill_branch_c2.verifier import implementation_digest as verifier_digest
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.verifier import _environment, _set_writable
from memos.skill_verification.executor import OUTPUT_LIMIT, execute_program
from memos.skill_verification.verifier import _phase_artifacts, _read, _write, check_phase_artifacts


BROKER_VERSION = "c2-visible-1"


def implementation_digest() -> str:
    return hash_json(
        {
            "broker": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "dependencies": verifier_digest(),
        }
    )


def visible_phase(suite_id: str) -> dict:
    task = get_suite(suite_id)
    return {"id": "extension_visible", "program": task.public_program, "files": {}, "arguments": []}


def _failure(reason: str) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": reason,
    }


RESULT_VERSION = "c2-visible-result-1"


def _public_report(task, checks, groups, status):
    return {
        "schemaVersion": 1,
        "protocol": RESULT_VERSION,
        "suiteId": task.suite_id,
        "suiteDigest": suite_digest(task),
        "taskId": task.task_id,
        "checkMapping": list(task.checks),
        "checks": checks,
        "groupStatus": groups,
        "status": status,
    }


def _result(task, execution: dict, job: Path, candidate: Path, unchanged: bool) -> dict:
    # Retain process streams as artifacts, but deliver only the fixed structured
    # report. Candidate stdout cannot forge a public check or leak host paths.
    for name in ("stdout", "stderr"):
        path = job / (name + ".txt")
        if path.is_symlink() or path.stat().st_size > OUTPUT_LIMIT:
            raise ValueError("invalid_visible_output")
    status, checks, groups, reasons = _assess(task, job, execution, candidate, unchanged, [])
    report = _public_report(task, checks, groups, status)
    return {
        "exit_code": execution.get("returncode"),
        "stdout": json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False),
        "stderr": "",
        "truncated": bool(execution.get("output_limited")),
        "timed_out": bool(execution.get("timed_out")),
        "execution_error": reasons[0] if status == "error" else None,
    }


def decode_public_result(suite_id: str, result: dict) -> dict:
    """Validate the existing MCP envelope and the selected suite's public schema."""

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_public_result_key")
            value[key] = item
        return value

    try:
        task = get_suite(suite_id)
        required = {"exit_code", "stdout", "stderr", "truncated", "timed_out", "execution_error"}
        if (
            not isinstance(result, dict)
            or not required <= set(result)
            or set(result) - required - {"evidence_ref"}
            or not isinstance(result["stdout"], str)
            or len(result["stdout"].encode()) > OUTPUT_LIMIT
            or not isinstance(result["stderr"], str)
            or type(result["truncated"]) is not bool
            or type(result["timed_out"]) is not bool
            or (result["exit_code"] is not None and type(result["exit_code"]) is not int)
            or (
                result["execution_error"] is not None
                and (
                    not isinstance(result["execution_error"], str) or not result["execution_error"]
                )
            )
        ):
            raise ValueError("public_envelope_invalid")
        if "evidence_ref" in result:
            ref = result["evidence_ref"]
            if (
                not isinstance(ref, dict)
                or set(ref) != {"check_index", "candidate_snapshot"}
                or type(ref["check_index"]) is not int
                or not 1 <= ref["check_index"] <= 100
                or not isinstance(ref["candidate_snapshot"], str)
                or len(ref["candidate_snapshot"]) != 64
                or any(c not in "0123456789abcdef" for c in ref["candidate_snapshot"])
            ):
                raise ValueError("public_evidence_reference_invalid")
        report = json.loads(result["stdout"], object_pairs_hook=unique)
        expected = _public_report(task, [], {}, "error")
        if (
            not isinstance(report, dict)
            or set(report) != set(expected)
            or type(report["schemaVersion"]) is not int
            or any(
                report[k] != expected[k]
                for k in expected
                if k not in {"checks", "groupStatus", "status"}
            )
            or report["status"] not in ("passed", "failed", "error")
        ):
            raise ValueError("public_report_identity_invalid")
        status, checks = report["status"], report["checks"]
        if status == "error":
            if (
                not result["execution_error"]
                or (checks != [] and not _valid_checks(task, checks))
                or report["groupStatus"] != _groups(task, [], "error")
            ):
                raise ValueError("public_error_result_invalid")
        else:
            if (
                not _valid_checks(task, checks)
                or any(c["status"] == "error" for c in checks)
                or result["execution_error"] is not None
                or result["truncated"]
                or result["timed_out"]
                or result["exit_code"] != int(any(c["status"] != "passed" for c in checks))
                or status != ("failed" if result["exit_code"] else "passed")
                or report["groupStatus"] != _groups(task, checks, status)
            ):
                raise ValueError("public_checks_invalid")
        return report
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ValueError("extension_public_result_invalid") from None


class _Cancellation:
    def __init__(self, local, external):
        self.local, self.external = local, external

    def is_set(self):
        return self.local.is_set() or (self.external is not None and self.external.is_set())


class VisibleCheckBroker:
    """One fixed action, bounded execution, immutable public evidence and cleanup."""

    def __init__(
        self,
        suite_id: str,
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
        self.task = get_suite(suite_id)
        self.phase = {
            "id": "extension_visible",
            "program": self.task.public_program,
            "files": {},
            "arguments": [],
        }
        self._identity = suite_identity(self.task)
        self._implementation = implementation_digest()
        self._environment = _environment()
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
            self._temporary = tempfile.TemporaryDirectory(
                prefix="memos-extension-broker-", dir="/tmp"
            )
            self.socket_path = Path(self._temporary.name).resolve() / "checks.sock"
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            self._server.listen(2)
            self._server.settimeout(0.1)
            self._thread = threading.Thread(
                target=self._serve, name="memos-extension-visible", daemon=True
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
            if (
                suite_identity(get_suite(self.task.suite_id)) != self._identity
                or implementation_digest() != self._implementation
                or _environment() != self._environment
            ):
                return _failure("extension_binding_changed")
            _no_symlinks(self.workspace)
            _no_symlinks(self.evidence / "snapshots")
            snapshot = self.store.capture(self.workspace)
            with tempfile.TemporaryDirectory(
                prefix="memos-extension-visible-", dir="/tmp"
            ) as temporary:
                candidate = self.store.restore(
                    snapshot.snapshot_id, Path(temporary).resolve() / "candidate"
                )
                if policy_violations(self.task, candidate):
                    return _failure("protected_task_changed")
                phase_job = f"checks/check-{index}/extension_visible"
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
                result = _result(self.task, execution, job, candidate, unchanged)
                if (
                    suite_identity(get_suite(self.task.suite_id)) != self._identity
                    or implementation_digest() != self._implementation
                    or _environment() != self._environment
                ):
                    return _failure("extension_binding_changed")
                report = decode_public_result(self.task.suite_id, result)
                result["evidence_ref"] = {
                    "check_index": index,
                    "candidate_snapshot": snapshot.snapshot_id,
                }
                record = {
                    "schema_version": 1,
                    "broker_version": BROKER_VERSION,
                    "implementation_digest": self._implementation,
                    "suite_identity": self._identity,
                    "check_mapping": list(self.task.checks),
                    "checks": report["checks"],
                    "group_status": report["groupStatus"],
                    "environment": self._environment,
                    "environment_digest": hash_json(self._environment),
                    "task_id": self.task.task_id,
                    "asset_digest": suite_digest(self.task),
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
        except (ValueError, OSError, TypeError, RecursionError, IsolationUnavailableError):
            return _failure("test_input_or_isolation_invalid")


def check_visible_record(record_path: Path, candidate: Path) -> bool:
    """Recompute retained public results and fixed execution policy without a rerun."""
    try:
        path, candidate = Path(record_path), Path(candidate)
        _no_symlinks(path)
        _no_symlinks(candidate)
        path = path.resolve()
        record = _read(path)
        if set(record) != {
            "schema_version",
            "broker_version",
            "implementation_digest",
            "suite_identity",
            "check_mapping",
            "checks",
            "group_status",
            "environment",
            "environment_digest",
            "task_id",
            "asset_digest",
            "check_index",
            "candidate_snapshot",
            "candidate_unchanged",
            "executed_candidate_root",
            "timeout_seconds",
            "phase_job",
            "execution",
            "phase_artifacts",
            "result",
            "record_digest",
        }:
            return False
        index = record["check_index"]
        if type(index) is not int or not 1 <= index <= 100 or path.name != f"check-{index}.json":
            return False
        if record["record_digest"] != hash_json(
            {k: v for k, v in record.items() if k != "record_digest"}
        ):
            return False
        task = get_suite(record["suite_identity"]["suiteId"])
        phase = visible_phase(task.suite_id)
        expected_job = f"checks/check-{index}/extension_visible"
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["broker_version"] != BROKER_VERSION
            or record["implementation_digest"] != implementation_digest()
            or record["asset_digest"] != suite_digest(task)
            or record["suite_identity"] != suite_identity(task)
            or record["task_id"] != task.task_id
            or record["check_mapping"] != list(task.checks)
            or record["environment_digest"] != hash_json(_environment())
            or record["environment_digest"] != hash_json(record["environment"])
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
        with tempfile.TemporaryDirectory(prefix="memos-extension-visible-recheck-") as temporary:
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
        result = _result(task, execution, job, executed_root, record["candidate_unchanged"])
        report = decode_public_result(task.suite_id, result)
        if record["checks"] != report["checks"] or record["group_status"] != report["groupStatus"]:
            return False
        result["evidence_ref"] = {
            "check_index": index,
            "candidate_snapshot": record["candidate_snapshot"],
        }
        return record["execution"] == execution and record["result"] == result
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError):
        return False
