"""Receipt-bound host orchestration for the fixed, independently sandboxed plans.

The artifact collector is trusted. As in the original verifier, a candidate and
the oracle share an interpreter; this is not protection of oracle internals from
hostile Python reflection. Candidate OS privileges remain read-only/no-fork.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path

from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.tasks import (
    candidate_policy_violations,
    case_digest,
    get_case,
    score_result,
)
from memos.skill_evaluation.verifier import (
    ReceiptCheck,
    VerificationResult,
    _assess,
    _case_identity,
    _environment,
    _identity,
    _initial_origin,
    _initial_tree,
    _set_writable,
)
from memos.skill_evaluation.verifier import (
    _validator_digest as legacy_validator_digest,
)
from memos.skill_verification.plans import acceptance_plan, digest_json


SCHEMA_VERSION = 3
VALIDATOR_VERSION = "p41.1"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _write(path: Path, value) -> None:
    with path.open("xb") as stream:
        stream.write(_json_bytes(value))
    path.chmod(0o600)


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("invalid_verification_artifact")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_verification_field")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("invalid_verification_record")
    return value


def _artifact(path: Path, root: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid_verification_artifact")
    data = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha(data), "size": len(data)}


def _read_artifact(reference: dict, root: Path) -> bytes:
    relative = Path(reference["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != reference["path"]:
        raise ValueError("invalid_verification_artifact_path")
    path = root / relative
    if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root)):
        raise ValueError("verification_artifact_symlink")
    if not path.resolve().is_relative_to(root) or _artifact(path, root) != reference:
        raise ValueError("verification_artifact_changed")
    return path.read_bytes()


def _phase_artifacts(job: Path) -> dict:
    if job.is_symlink() or not job.is_dir():
        raise ValueError("verification_phase_directory_invalid")
    result = {}
    for path in sorted(job.rglob("*")):
        if path.is_symlink():
            raise ValueError("verification_phase_symlink")
        if path.is_file():
            result[path.relative_to(job).as_posix()] = _artifact(path, job)
    return result


def implementation_digest() -> str:
    from memos.skill_verification.executor import implementation_digest as executor_digest

    return digest_json(
        {
            "verifier": _sha(Path(__file__).read_bytes()),
            "plans": _sha(Path(__file__).with_name("plans.py").read_bytes()),
            "executor": executor_digest(),
            "legacy_policy_and_snapshots": legacy_validator_digest(),
        }
    )


def validator_digest() -> str:
    """Public identity consumed by selftest qualification and new runners."""
    return implementation_digest()


def check_phase_artifacts(phase: dict, job: Path, *, candidate: Path, timeout: float) -> dict:
    """Validate retained phase artifacts without needing the deleted temp tree."""
    from memos.skill_verification import executor

    job = Path(job)
    if job.is_symlink() or any(parent.is_symlink() for parent in job.parents):
        raise ValueError("verification_phase_symlink")
    job = job.resolve()
    files = _phase_artifacts(job)
    required = {
        "program.py",
        "control-manifest.json",
        "isolation.sb",
        "stdout.txt",
        "stderr.txt",
        "execution.json",
    }
    if not required <= set(files):
        raise ValueError("verification_phase_artifact_missing")
    if (job / "program.py").read_text() != phase["program"]:
        raise ValueError("verification_phase_program_changed")
    actual_control = {
        name.removeprefix("control-files/"): (job / name).read_text()
        for name in files
        if name.startswith("control-files/")
    }
    if actual_control != phase["files"]:
        raise ValueError("verification_phase_files_changed")
    allowed = required | {"control-files/" + name for name in phase["files"]} | {"report.json"}
    if not set(files) <= allowed:
        raise ValueError("verification_phase_unexpected_artifact")
    execution = _read(job / "execution.json")
    isolation = execution["isolation"]
    if (
        isolation["implementation_digest"] != executor.implementation_digest()
        or isolation["backend"] != executor.BACKEND
    ):
        raise ValueError("verification_executor_changed")
    if execution["program_sha256"] != _sha(phase["program"].encode()):
        raise ValueError("verification_program_binding_changed")
    if execution["control_files_sha256"] != {
        name: _sha(value.encode()) for name, value in phase["files"].items()
    }:
        raise ValueError("verification_control_binding_changed")
    if execution["arguments"] != phase["arguments"]:
        raise ValueError("verification_arguments_changed")
    manifest = {
        "executor_version": executor.EXECUTOR_VERSION,
        "program_sha256": _sha(phase["program"].encode()),
        "control_files_sha256": {
            name: _sha(value.encode()) for name, value in phase["files"].items()
        },
        "arguments": phase["arguments"],
        "effective_arguments": phase["arguments"] or ["{candidate}", "{report}"],
        "timeout_seconds": float(timeout),
    }
    if _read(job / "control-manifest.json") != manifest or any(
        execution.get(key) != value for key, value in manifest.items()
    ):
        raise ValueError("verification_control_manifest_changed")
    profile = executor.profile_from_record(
        isolation["profile_spec"], candidate_path=Path(candidate), job=job
    )
    if (job / "isolation.sb").read_text() != profile or isolation["profile_sha256"] != _sha(
        profile.encode()
    ):
        raise ValueError("verification_profile_changed")
    if execution["timeout_seconds"] != timeout or execution["limits"] != executor.policy_limits(
        timeout
    ):
        raise ValueError("verification_limits_changed")
    spec = isolation["profile_spec"]
    replacements = {
        "candidate": str(candidate),
        "control": spec["control_root"],
        "scratch": spec["writable_scratch"][0],
        "report": spec["writable_scratch"][0] + "/report.json",
    }
    if execution["resolved_arguments"] != [
        arg.format_map(replacements) for arg in manifest["effective_arguments"]
    ]:
        raise ValueError("verification_resolved_arguments_changed")
    if any(
        type(execution[key]) is not bool
        for key in ("timed_out", "cancelled", "background_processes", "output_limited")
    ) or (execution["returncode"] is not None and type(execution["returncode"]) is not int):
        raise ValueError("verification_execution_invalid")
    if not isinstance(execution["limit_reasons"], list) or any(
        not isinstance(reason, str) for reason in execution["limit_reasons"]
    ):
        raise ValueError("verification_limits_invalid")
    if (
        type(execution["elapsed_seconds"]) not in (int, float)
        or not math.isfinite(execution["elapsed_seconds"])
        or execution["elapsed_seconds"] < 0
    ):
        raise ValueError("verification_time_invalid")
    if any(
        (job / name).stat().st_size > executor.OUTPUT_LIMIT for name in ("stdout.txt", "stderr.txt")
    ) or (
        (job / "report.json").exists()
        and (job / "report.json").stat().st_size > executor.REPORT_LIMIT
    ):
        raise ValueError("verification_output_limit_exceeded")
    return execution


def _phase_fault(execution: dict) -> bool:
    return bool(
        execution.get("timed_out")
        or execution.get("cancelled")
        or execution.get("output_limited")
        or execution.get("background_processes")
        or execution.get("start_error")
        or execution.get("limit_reasons")
    )


def fixture_phase_status(phase: dict, job: Path, execution: dict) -> str:
    """Recompute actual pytest coverage; a zero exit without stage facts is not success."""
    try:
        audit = _read(job / "report.json")
        code = execution["returncode"]
        if (
            _phase_fault(execution)
            or code not in (0, 1)
            or set(audit) != {"schema_version", "runner_exit_code", "events"}
            or audit["schema_version"] != 1
            or type(audit["runner_exit_code"]) is not int
            or audit["runner_exit_code"] != code
        ):
            return "error"
        expected = [
            [argument.rsplit("::", 1)[-1], stage]
            for argument in phase["arguments"][2:]
            for stage in ("setup", "call", "teardown")
        ]
        events = audit["events"]
        if (
            not isinstance(events, list)
            or len(events) != len(expected)
            or any(not isinstance(row, list) or len(row) != 4 for row in events)
            or [row[:2] for row in events] != expected
            or any(row[2] not in ("passed", "failed") or row[3] is not False for row in events)
            or (code == 1) != any(row[2] == "failed" for row in events)
        ):
            return "error"
        return "passed" if code == 0 else "failed"
    except (OSError, ValueError, KeyError, TypeError):
        return "error"


def _merged(plan: dict, phase_records: list[dict], root: Path, stopped_reason: str | None):
    """Derive the merged behavior report from the original, retained phase facts."""
    reports = []
    for record in phase_records:
        job = root / record["job"]
        report = _read(job / "report.json") if (job / "report.json").is_file() else None
        reports.append(report)
    merged = reports[0] if reports else None
    if merged is not None:
        merged = json.loads(json.dumps(merged))
    incomplete = len(phase_records) != len(plan["phases"])
    if len(plan["phases"]) > 1:
        orders = phase_records[1:]
        outcomes = [
            fixture_phase_status(phase, root / record["job"], record["execution"])
            for phase, record in zip(plan["phases"][1:], orders, strict=False)
        ]
        fault = incomplete or "error" in outcomes
        failed = "failed" in outcomes
        check = {
            "name": "fixture_isolation",
            "status": "error" if fault else "failed" if failed else "passed",
            "detail": "fixture execution incomplete or invalid"
            if fault
            else "fixture order violates contract"
            if failed
            else "contract satisfied",
        }
        if merged is not None and isinstance(merged.get("checks"), list):
            merged["checks"].append(check)
    records = [r["execution"] for r in phase_records]
    fault = incomplete or any(_phase_fault(r) for r in records)
    statuses = {c.get("status") for c in (merged or {}).get("checks", []) if isinstance(c, dict)}
    expected_exit = (
        2 if fault or not statuses or "error" in statuses else 1 if "failed" in statuses else 0
    )
    # The oracle phase has its own exit/report contract. An unrelated process
    # error must not be hidden by computing a fresh success exit for the merge.
    if records:
        raw_statuses = {
            c.get("status") for c in (reports[0] or {}).get("checks", []) if isinstance(c, dict)
        }
        oracle_exit = (
            2
            if "error" in raw_statuses or not raw_statuses
            else 1
            if "failed" in raw_statuses
            else 0
        )
        if records[0]["returncode"] != oracle_exit:
            expected_exit = 2
    aggregate = {
        "returncode": expected_exit,
        "timed_out": stopped_reason == "timeout" or any(r.get("timed_out") for r in records),
        "cancelled": stopped_reason == "cancelled" or any(r.get("cancelled") for r in records),
        "background_processes": any(r.get("background_processes") for r in records),
        "output_limited": any(r.get("output_limited") for r in records),
        "elapsed_seconds": round(sum(r["elapsed_seconds"] for r in records), 6),
        "phase_count": len(records),
        "stopped_reason": stopped_reason,
        "plan_incomplete": incomplete,
    }
    return merged, aggregate


def _assess_new(case, report, execution, candidate, unchanged, policy):
    status, checks, reasons = _assess(case, report, execution, candidate, unchanged, policy)
    if execution.get("cancelled"):
        return "error", checks, [*reasons, "cancelled"]
    if execution.get("plan_incomplete"):
        return "error", checks, [*reasons, "execution_plan_incomplete"]
    return status, checks, reasons


def verify_candidate(
    case_id: str,
    candidate: Path,
    artifact_root: Path,
    *,
    run_id=None,
    attempt_id=None,
    timeout: float = 10,
    cancellation_event=None,
    secret_values: tuple[str, ...] = (),
) -> VerificationResult:
    from memos.skill_verification.executor import execute_program

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("verification_timeout_invalid")
    case, plan = get_case(case_id), acceptance_plan(case_id)
    run_id, attempt_id = (
        _identity(run_id or uuid.uuid4().hex),
        _identity(attempt_id or uuid.uuid4().hex),
    )
    candidate, root = Path(candidate).resolve(), Path(artifact_root).resolve()
    if root.is_relative_to(candidate):
        raise ValueError("verification_artifacts_inside_candidate")
    store = SnapshotStore(root / "snapshots")
    snapshot = store.capture(candidate)
    job = root / "runs" / run_id / attempt_id
    job.mkdir(parents=True, exist_ok=False)
    _write(job / "plan.json", plan)
    (job / "oracle.py").write_text(case.oracle_source)
    environment = _environment()
    started_at = datetime.now(timezone.utc).isoformat()
    phase_records, stopped_reason = [], None
    with tempfile.TemporaryDirectory(prefix="memos-p41-verify-", dir="/tmp") as temporary:
        work = Path(temporary).resolve()
        _initial_tree(case, work / "initial")
        initial = store.capture(work / "initial")
        restored = store.restore(snapshot.snapshot_id, work / "candidate")
        policy = candidate_policy_violations(case, restored)
        _set_writable(restored, False)
        try:
            started = time.monotonic()
            for phase in plan["phases"]:
                remaining = timeout - (time.monotonic() - started)
                if cancellation_event is not None and cancellation_event.is_set():
                    stopped_reason = "cancelled"
                    break
                if remaining <= 0:
                    stopped_reason = "timeout"
                    break
                phase_job = job / "phases" / phase["id"]
                execution = execute_program(
                    phase["program"],
                    restored,
                    phase_job,
                    remaining,
                    files=phase["files"],
                    arguments=tuple(phase["arguments"]),
                    secret_values=secret_values,
                    cancellation_event=cancellation_event,
                )
                phase_records.append(
                    {
                        "id": phase["id"],
                        "job": phase_job.relative_to(root).as_posix(),
                        "timeout_seconds": remaining,
                        "execution": execution,
                        "artifacts": _phase_artifacts(phase_job),
                    }
                )
                if execution.get("cancelled"):
                    stopped_reason = "cancelled"
                    break
                if execution.get("timed_out"):
                    stopped_reason = "timeout"
                    break
            report, execution = _merged(plan, phase_records, root, stopped_reason)
            unchanged = snapshot_fingerprint(restored) == snapshot.snapshot_id
            status, checks, reasons = _assess_new(
                case, report, execution, restored, unchanged, policy
            )
            executed_root = str(restored)
        finally:
            _set_writable(restored, True)
    _write(job / "merged-result.json", {"report": report, "execution": execution})
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "validator_digest": implementation_digest(),
        "case": _case_identity(case),
        "run_id": run_id,
        "attempt_id": attempt_id,
        "initial_snapshot": initial.snapshot_id,
        "initial_snapshot_origin": _initial_origin(case),
        "asset_digest": case_digest(case),
        "candidate_snapshot": snapshot.snapshot_id,
        "contract_digest": _sha(case.contract.encode()),
        "oracle_digest": _sha(case.oracle_source.encode()),
        "plan_digest": plan["plan_digest"],
        "environment": environment,
        "environment_digest": digest_json(environment),
        "executed_candidate_root": executed_root,
        "candidate_unchanged": unchanged,
        "workspace_policy_violations": policy,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout,
        "execution": execution,
        "phases": phase_records,
        "checks": checks,
        "status": status,
        "reasons": reasons,
        "qualified_agent_experience": False,
        "scoring": score_result(case, status),
        "artifacts": {
            name: _artifact(job / filename, root)
            for name, filename in (
                ("plan", "plan.json"),
                ("oracle", "oracle.py"),
                ("merged_result", "merged-result.json"),
            )
        },
    }
    path = job / "receipt.json"
    _write(path, receipt)
    path.with_suffix(".sha256").write_text(_sha(path.read_bytes()) + "\n")
    store.verify(snapshot.snapshot_id)
    return VerificationResult(status, path)


def check_receipt(receipt_path: Path, candidate: Path, artifact_root: Path) -> ReceiptCheck:
    reasons, status = [], "unknown"
    try:
        root = Path(artifact_root).resolve()
        raw_path = Path(receipt_path)
        if raw_path.is_symlink():
            raise ValueError("verification_receipt_symlink")
        path = raw_path.resolve()
        if (
            not path.is_relative_to(root)
            or _sha(path.read_bytes()) != path.with_suffix(".sha256").read_text().strip()
        ):
            raise ValueError("verification_receipt_changed")
        receipt = _read(path)
        case, plan = (
            get_case(receipt["case"]["case_id"]),
            acceptance_plan(receipt["case"]["case_id"]),
        )
        status = receipt["status"]
        expected = {
            "schema_version": SCHEMA_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "validator_digest": implementation_digest(),
            "case": _case_identity(case),
            "asset_digest": case_digest(case),
            "contract_digest": _sha(case.contract.encode()),
            "oracle_digest": _sha(case.oracle_source.encode()),
            "plan_digest": plan["plan_digest"],
            "environment_digest": digest_json(_environment()),
            "initial_snapshot_origin": _initial_origin(case),
            "qualified_agent_experience": False,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("verification_identity_changed")
        if digest_json(receipt["environment"]) != receipt["environment_digest"]:
            raise ValueError("verification_environment_changed")
        if (
            path
            != root
            / "runs"
            / _identity(receipt["run_id"])
            / _identity(receipt["attempt_id"])
            / "receipt.json"
        ):
            raise ValueError("verification_attempt_changed")
        timeout = receipt["timeout_seconds"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("verification_timeout_invalid")
        store = SnapshotStore(root / "snapshots")
        store.verify(receipt["candidate_snapshot"])
        store.verify(receipt["initial_snapshot"])
        if snapshot_fingerprint(Path(candidate)) != receipt["candidate_snapshot"]:
            raise ValueError("candidate_mismatch")
        with tempfile.TemporaryDirectory(prefix="memos-p41-recheck-") as temporary:
            temporary = Path(temporary)
            _initial_tree(case, temporary / "initial")
            if snapshot_fingerprint(temporary / "initial") != receipt["initial_snapshot"]:
                raise ValueError("verification_initial_changed")
            restored = store.restore(receipt["candidate_snapshot"], temporary / "candidate")
            policy = candidate_policy_violations(case, restored)
            if policy != receipt["workspace_policy_violations"]:
                raise ValueError("verification_file_policy_changed")
        contents = {name: _read_artifact(ref, root) for name, ref in receipt["artifacts"].items()}
        if (
            set(contents) != {"plan", "oracle", "merged_result"}
            or json.loads(contents["plan"]) != plan
            or contents["oracle"] != case.oracle_source.encode()
        ):
            raise ValueError("verification_plan_artifact_changed")
        records = receipt["phases"]
        if not isinstance(records, list) or len(records) > len(plan["phases"]):
            raise ValueError("verification_phase_count_changed")
        for index, record in enumerate(records):
            phase = plan["phases"][index]
            job = path.parent / "phases" / phase["id"]
            if (
                record["id"] != phase["id"]
                or record["job"] != job.relative_to(root).as_posix()
                or not 0 < record["timeout_seconds"] <= timeout
            ):
                raise ValueError("verification_phase_order_changed")
            if _phase_artifacts(job) != record["artifacts"]:
                raise ValueError("verification_phase_artifacts_changed")
            actual = check_phase_artifacts(
                phase,
                job,
                candidate=Path(receipt["executed_candidate_root"]),
                timeout=record["timeout_seconds"],
            )
            if actual != record["execution"]:
                raise ValueError("verification_phase_execution_changed")
        stopped = receipt["execution"]["stopped_reason"]
        if stopped not in (None, "timeout", "cancelled"):
            raise ValueError("verification_stopped_reason_invalid")
        report, execution = _merged(plan, records, root, stopped)
        if execution != receipt["execution"] or json.loads(contents["merged_result"]) != {
            "report": report,
            "execution": execution,
        }:
            raise ValueError("verification_merge_changed")
        actual = _assess_new(
            case,
            report,
            execution,
            Path(receipt["executed_candidate_root"]),
            receipt["candidate_unchanged"],
            policy,
        )
        if actual != (status, receipt["checks"], receipt["reasons"]) or receipt[
            "scoring"
        ] != score_result(case, status):
            raise ValueError("verification_result_changed")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        reasons.append(f"invalid_receipt:{type(error).__name__}:{error}")
    return ReceiptCheck(not reasons, status, tuple(reasons), bool(reasons))
