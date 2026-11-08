"""Versioned branch receipts; no acceptance or coverage is borrowed from P4.

The host collector is trusted. Hashes bind retained execution and the final
patch; they are not signatures against a compromised collector/interpreter.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
import uuid

from datetime import datetime, timezone
from pathlib import Path

from memos.skill_branch_validation.tasks import BranchTask, get_task, task_digest
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.verifier import (
    ReceiptCheck,
    VerificationResult,
    _environment,
    _identity,
    _set_writable,
)
from memos.skill_verification.executor import execute_program
from memos.skill_verification.verifier import (
    _phase_artifacts,
    _phase_fault,
    _read,
    _write,
    check_phase_artifacts,
    validator_digest,
)


PROTOCOL = "branch-receipt-1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def implementation_digest() -> str:
    # Bind only this protocol and its dependencies, not future runners/brokers.
    return hash_json(
        {
            "verifier": _sha(Path(__file__).read_bytes()),
            "tasks": _sha(Path(__file__).with_name("tasks.py").read_bytes()),
            "executor_and_snapshot_helpers": validator_digest(),
        }
    )


def policy_violations(task: BranchTask, candidate: Path) -> list[str]:
    """Protect the whole task inventory, including public checks and directories."""
    paths = {p.relative_to(candidate).as_posix(): p for p in candidate.rglob("*")}
    reasons = []
    if set(paths) != set(task.initial_files):
        reasons.append("task_inventory_changed")
    for name, path in sorted(paths.items()):
        if path.is_symlink() or not path.is_file():
            reasons.append("task_file_type_changed:" + name)
        elif (
            name in task.initial_files
            and name not in task.editable_paths
            and path.read_bytes() != task.initial_files[name].encode()
        ):
            reasons.append("protected_task_changed:" + name)
    return reasons


def _phase(task: BranchTask) -> dict:
    return {"id": "branch_oracle", "program": task.oracle, "files": {}, "arguments": []}


def _task_identity(task: BranchTask) -> dict:
    return {
        "taskId": task.task_id,
        "revision": task.revision,
        "lineageId": task.lineage_id,
        "purpose": task.purpose,
        "digest": task_digest(task),
    }


def _assess(task, job, execution, executed_root, unchanged, policy):
    """Recompute from complete ordered checks, never from an asserted success."""
    checks = []
    try:
        report = _read(job / "report.json")
        checks = report["checks"]
        valid = (
            set(report) == {"checks", "imported"}
            and isinstance(checks, list)
            and all(isinstance(row, dict) for row in checks)
            and [row.get("name") for row in checks] == list(task.required_checks)
            and all(
                row.get("status") in ("passed", "failed", "error")
                and set(row) <= {"name", "status", "error"}
                and ("error" not in row or isinstance(row["error"], str))
                for row in checks
            )
            and report["imported"]
            == {name: str(executed_root / (name + ".py")) for name in ("caller", "fake_graph")}
        )
        if not valid:
            return "error", [], ["invalid_or_incomplete_checks"]
    except (OSError, ValueError, KeyError, TypeError):
        return "error", [], ["missing_or_invalid_report"]
    if (
        _phase_fault(execution)
        or execution.get("returncode") != int(any(c["status"] != "passed" for c in checks))
        or unchanged is not True
        or any(c["status"] == "error" for c in checks)
    ):
        return "error", checks, ["execution_or_check_error"]
    reasons = [*policy, *("check_failed:" + c["name"] for c in checks if c["status"] == "failed")]
    return ("failed" if reasons else "passed"), checks, reasons


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError("branch_timeout_invalid")


def _no_symlinks(path: Path) -> None:
    platform_aliases = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
    for item in (path.absolute(), *path.absolute().parents):
        if item.is_symlink() and platform_aliases.get(item) != item.resolve():
            raise ValueError("branch_artifact_symlink")


def verify_task(
    task_id: str,
    candidate: Path,
    artifact_root: Path,
    *,
    run_id: str | None = None,
    attempt_id: str | None = None,
    timeout: float = 15,
    cancellation_event=None,
    secret_values: tuple[str, ...] = (),
) -> VerificationResult:
    _timeout(timeout)
    task = get_task(task_id)
    run_id, attempt_id = (
        _identity(run_id or uuid.uuid4().hex),
        _identity(attempt_id or uuid.uuid4().hex),
    )
    # Validate before resolving: otherwise links inside the store disappear.
    _no_symlinks(Path(candidate))
    _no_symlinks(Path(artifact_root))
    candidate, root = Path(candidate).resolve(), Path(artifact_root).resolve()
    if root.is_relative_to(candidate) or candidate.is_relative_to(root):
        raise ValueError("branch_artifacts_overlap_candidate")
    _no_symlinks(root / "snapshots")
    store = SnapshotStore(root / "snapshots")
    snapshot = store.capture(candidate)
    job = root / "runs" / run_id / attempt_id
    _no_symlinks(job)
    job.mkdir(parents=True, exist_ok=False, mode=0o700)
    environment, started = _environment(), datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="memos-branch-verify-", dir="/tmp") as temporary:
        restored = store.restore(snapshot.snapshot_id, Path(temporary).resolve() / "candidate")
        policy = policy_violations(task, restored)
        _set_writable(restored, False)
        try:
            execution = execute_program(
                task.oracle,
                restored,
                job / "phase",
                timeout,
                cancellation_event=cancellation_event,
                secret_values=secret_values,
            )
            unchanged = snapshot_fingerprint(restored) == snapshot.snapshot_id
            status, checks, reasons = _assess(
                task, job / "phase", execution, restored, unchanged, policy
            )
            executed_root = str(restored)
        finally:
            _set_writable(restored, True)
    receipt = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "implementationDigest": implementation_digest(),
        "task": _task_identity(task),
        "runId": run_id,
        "attemptId": attempt_id,
        "candidateSnapshot": snapshot.snapshot_id,
        "candidateUnchanged": unchanged,
        "executedCandidateRoot": executed_root,
        "environment": environment,
        "environmentDigest": hash_json(environment),
        "timeoutSeconds": timeout,
        "startedAt": started,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "execution": execution,
        "phaseArtifacts": _phase_artifacts(job / "phase"),
        "policyViolations": policy,
        "checks": checks,
        "status": status,
        "reasons": reasons,
        "qualifiedAgentExperience": False,
        "branchCoverage": [],
    }
    path = job / "receipt.json"
    _write(path, receipt)
    path.with_suffix(".sha256").write_text(_sha(path.read_bytes()) + "\n")
    path.with_suffix(".sha256").chmod(0o600)
    store.verify(snapshot.snapshot_id)
    return VerificationResult(status, path)


def check_receipt(receipt_path: Path, candidate: Path, artifact_root: Path) -> ReceiptCheck:
    status = "unknown"
    try:
        _no_symlinks(Path(artifact_root))
        _no_symlinks(Path(receipt_path))
        _no_symlinks(Path(candidate))
        root = Path(artifact_root).resolve()
        path = Path(receipt_path).absolute()
        # Allow the platform's /tmp alias, not links within the artifact store.
        if path.is_symlink():
            raise ValueError("branch_receipt_symlink")
        path = path.resolve()
        _no_symlinks(path)
        _no_symlinks(path.with_suffix(".sha256"))
        if (
            not path.is_relative_to(root)
            or _sha(path.read_bytes()) != path.with_suffix(".sha256").read_text().strip()
        ):
            raise ValueError("branch_receipt_changed")
        receipt = _read(path)
        task = get_task(receipt["task"]["taskId"])
        if receipt["status"] not in ("passed", "failed", "error"):
            raise ValueError("branch_status_invalid")
        status = receipt["status"]
        expected = {
            "schemaVersion": 1,
            "protocol": PROTOCOL,
            "implementationDigest": implementation_digest(),
            "task": _task_identity(task),
            "environmentDigest": hash_json(_environment()),
            "qualifiedAgentExperience": False,
            "branchCoverage": [],
        }
        if any(receipt.get(k) != v for k, v in expected.items()):
            raise ValueError("branch_identity_changed")
        if hash_json(receipt["environment"]) != receipt["environmentDigest"]:
            raise ValueError("branch_environment_changed")
        if (
            path
            != root
            / "runs"
            / _identity(receipt["runId"])
            / _identity(receipt["attemptId"])
            / "receipt.json"
        ):
            raise ValueError("branch_attempt_changed")
        _timeout(receipt["timeoutSeconds"])
        _no_symlinks(root / "snapshots")
        store = SnapshotStore(root / "snapshots")
        store.verify(receipt["candidateSnapshot"])
        if (
            Path(candidate).is_symlink()
            or snapshot_fingerprint(Path(candidate)) != receipt["candidateSnapshot"]
        ):
            raise ValueError("branch_candidate_changed")
        with tempfile.TemporaryDirectory(prefix="memos-branch-recheck-") as temporary:
            restored = store.restore(receipt["candidateSnapshot"], Path(temporary) / "candidate")
            policy = policy_violations(task, restored)
        job = path.parent / "phase"
        if (
            policy != receipt["policyViolations"]
            or _phase_artifacts(job) != receipt["phaseArtifacts"]
        ):
            raise ValueError("branch_artifacts_or_policy_changed")
        executed_root = Path(receipt["executedCandidateRoot"])
        if not executed_root.is_absolute():
            raise ValueError("branch_execution_root_invalid")
        execution = check_phase_artifacts(
            _phase(task), job, candidate=executed_root, timeout=receipt["timeoutSeconds"]
        )
        if execution != receipt["execution"]:
            raise ValueError("branch_execution_changed")
        actual = _assess(task, job, execution, executed_root, receipt["candidateUnchanged"], policy)
        if actual != (status, receipt["checks"], receipt["reasons"]):
            raise ValueError("branch_result_changed")
        return ReceiptCheck(True, status, ())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        # Do not put private paths, artifact text or exception messages in UI errors.
        return ReceiptCheck(
            False, status, ("invalid_branch_receipt:" + type(error).__name__,), True
        )
