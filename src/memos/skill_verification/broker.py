"""One-run visible checks using versioned, fixed host plans and sealed candidates."""

from __future__ import annotations

import json
import math
import re
import socket
import tempfile
import threading

from contextlib import suppress
from pathlib import Path

from memos.skill_adapter.events import sanitize_public
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.tasks import candidate_policy_violations, case_digest, get_case
from memos.skill_verification.executor import OUTPUT_LIMIT, execute_program
from memos.skill_verification.plans import digest_json, visible_plan


BROKER_VERSION = "p41-visible-1"


def _failure(reason: str) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": reason,
    }


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("invalid_visible_artifact")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_visible_key")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("invalid_visible_artifact")
    return value


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    path.chmod(0o600)


def _result(execution: dict, job: Path, phase: dict) -> dict:
    from memos.skill_verification.verifier import fixture_phase_status

    streams = {}
    for name in ("stdout", "stderr"):
        path = job / f"{name}.txt"
        if path.is_symlink() or path.stat().st_size > OUTPUT_LIMIT:
            raise ValueError("invalid_visible_output")
        streams[name] = path.read_text(encoding="utf-8")
    reason = None
    if execution.get("cancelled"):
        reason = "test_cancelled"
    elif execution.get("timed_out"):
        reason = "test_timeout"
    elif execution.get("start_error"):
        reason = "test_start_failed"
    elif execution.get("output_limited") or execution.get("limit_reasons"):
        reason = "test_resource_limit"
    elif execution.get("returncode") not in (0, 1):
        reason = "test_execution_failed"
    elif (
        phase["kind"] == "fixture_pytest" and fixture_phase_status(phase, job, execution) == "error"
    ):
        reason = "test_execution_audit_invalid"
    value = {
        "exit_code": execution.get("returncode"),
        **streams,
        "truncated": bool(execution.get("output_limited")),
        "timed_out": bool(execution.get("timed_out")),
        "execution_error": reason,
    }
    return value


class _Cancellation:
    def __init__(self, local, external):
        self.local, self.external = local, external

    def is_set(self):
        return self.local.is_set() or (self.external is not None and self.external.is_set())


class VisibleCheckBroker:
    """Accept only a fixed action; keep hidden acceptance out of the Agent response.

    The host starts each fixed visible plan itself. In particular, pytest does
    not need permission to fork from candidate code. Exit cancels the active
    sandbox before its socket and temporary directory are removed.
    """

    def __init__(
        self,
        case_id: str,
        workspace: Path,
        evidence: Path,
        *,
        max_calls: int = 20,
        timeout: float = 10,
        secret_values: tuple[str, ...] = (),
        cancellation_event=None,
    ):
        if type(max_calls) is not int or not 0 < max_calls <= 100:
            raise ValueError("invalid_visible_budget")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("invalid_visible_timeout")
        if cancellation_event is not None and not callable(
            getattr(cancellation_event, "is_set", None)
        ):
            raise ValueError("invalid_cancellation_event")
        self.case = get_case(case_id)
        self.plan = visible_plan(case_id)
        # The single visible phase is a reviewed contract. Multiple hidden
        # acceptance phases cannot accidentally be routed to this endpoint.
        if self.plan["purpose"] != "visible" or len(self.plan["phases"]) != 1:
            raise ValueError("invalid_visible_plan")
        self.workspace, self.evidence = Path(workspace).resolve(), Path(evidence).resolve()
        if self.evidence.is_relative_to(self.workspace) or self.workspace.is_relative_to(
            self.evidence
        ):
            raise ValueError("visible_evidence_overlaps_workspace")
        self.store = SnapshotStore(self.evidence / "snapshots")
        self.max_calls, self.timeout = max_calls, timeout
        self.secret_values = secret_values
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
        self._temporary = tempfile.TemporaryDirectory(prefix="memos-p41-broker-", dir="/tmp")
        self.socket_path = Path(self._temporary.name).resolve() / "checks.sock"
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self._server.listen(2)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, name="memos-p41-visible", daemon=True)
        self._thread.start()
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
                    request = json.loads(data)
                    if request != {"action": "run_visible_checks"}:
                        raise ValueError("invalid_request")
                    result = self._run()
                except (ValueError, TimeoutError, OSError):
                    result = _failure("invalid_broker_request")
                with suppress(OSError):
                    connection.sendall(json.dumps(result, ensure_ascii=False).encode() + b"\n")

    def _run(self) -> dict:
        if self._cancellation.is_set():
            return _failure("test_cancelled")
        if self._calls >= self.max_calls:
            return _failure("test_budget_exhausted")
        self._calls += 1
        try:
            snapshot = self.store.capture(self.workspace)
            with tempfile.TemporaryDirectory(prefix="memos-p41-visible-", dir="/tmp") as temporary:
                candidate = self.store.restore(snapshot.snapshot_id, Path(temporary) / "candidate")
                if (
                    candidate_policy_violations(self.case, candidate)
                    or (candidate / "visible_checks.py").read_bytes()
                    != self.case.initial_files["visible_checks.py"].encode()
                ):
                    return _failure("protected_task_changed")
                index = len(self.records) + 1
                phase = self.plan["phases"][0]
                phase_job = f"checks/check-{index}/{phase['id']}"
                job = self.evidence / phase_job
                self._active.set()
                try:
                    execution = execute_program(
                        phase["program"],
                        candidate,
                        job,
                        self.timeout,
                        files=phase["files"],
                        arguments=tuple(phase["arguments"]),
                        secret_values=self.secret_values,
                        cancellation_event=self._cancellation,
                    )
                finally:
                    self._active.clear()
                result = _result(execution, job, phase)
                result["evidence_ref"] = {
                    "check_index": index,
                    "candidate_snapshot": snapshot.snapshot_id,
                }
                record = {
                    "schema_version": 1,
                    "broker_version": BROKER_VERSION,
                    "case_id": self.case.case_id,
                    "asset_digest": case_digest(self.case),
                    "check_index": index,
                    "candidate_snapshot": snapshot.snapshot_id,
                    "executed_candidate_root": str(candidate.resolve()),
                    "timeout_seconds": self.timeout,
                    "plan_digest": self.plan["plan_digest"],
                    "phase_job": phase_job,
                    "profile_sha256": execution["isolation"]["profile_sha256"],
                    "execution": execution,
                    "result": result,
                }
                record["record_digest"] = digest_json(record)
                _write(self.evidence / f"check-{index}.json", record)
                self.records.append(record)
                return result
        except (ValueError, OSError, TypeError):
            return _failure("test_input_or_isolation_invalid")


def check_visible_record(record_path: Path, candidate: Path) -> bool:
    """Recheck a retained visible record and its exact candidate, without rerunning code."""
    from memos.skill_verification.verifier import check_phase_artifacts

    try:
        path = Path(record_path)
        record = _read(path)
        index = record["check_index"]
        if type(index) is not int or index < 1 or path.name != f"check-{index}.json":
            return False
        if record["record_digest"] != digest_json(
            {key: value for key, value in record.items() if key != "record_digest"}
        ):
            return False
        case, plan = get_case(record["case_id"]), visible_plan(record["case_id"])
        phase = plan["phases"][0]
        expected_job = f"checks/check-{index}/{phase['id']}"
        if (
            record["schema_version"] != 1
            or record["broker_version"] != BROKER_VERSION
            or record["asset_digest"] != case_digest(case)
            or record["plan_digest"] != plan["plan_digest"]
            or record["phase_job"] != expected_job
            or not re.fullmatch(r"[a-f0-9]{64}", record["candidate_snapshot"])
        ):
            return False
        root = path.resolve().parent
        store = SnapshotStore(root / "snapshots")
        store.verify(record["candidate_snapshot"])
        if snapshot_fingerprint(Path(candidate)) != record["candidate_snapshot"]:
            return False
        with tempfile.TemporaryDirectory(prefix="memos-p41-visible-check-") as temporary:
            sealed = store.restore(record["candidate_snapshot"], Path(temporary) / "candidate")
            if candidate_policy_violations(case, sealed):
                return False
            if (sealed / "visible_checks.py").read_bytes() != case.initial_files[
                "visible_checks.py"
            ].encode():
                return False
        job = root / expected_job
        if any(part.is_symlink() for part in (root / "snapshots", job.parent, job)):
            return False
        execution = check_phase_artifacts(
            phase,
            job,
            candidate=Path(record["executed_candidate_root"]),
            timeout=record["timeout_seconds"],
        )
        expected = _result(execution, job, phase)
        expected["evidence_ref"] = {
            "check_index": index,
            "candidate_snapshot": record["candidate_snapshot"],
        }
        return (
            execution == record["execution"]
            and execution["isolation"]["profile_sha256"] == record["profile_sha256"]
            and record["result"] == expected
        )
    except (ValueError, OSError, KeyError, TypeError):
        return False
