"""Selected-suite receipts; no acceptance or coverage is borrowed from P4.

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

from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.verifier import (
    ReceiptCheck,
    VerificationResult,
    _environment,
    _identity,
    _set_writable,
)
from memos.skill_p5.tasks import ExtensionSuite, get_suite, suite_digest
from memos.skill_verification.executor import execute_program
from memos.skill_verification.verifier import (
    _phase_artifacts,
    _phase_fault,
    _read,
    _write,
    check_phase_artifacts,
    validator_digest,
)


PROTOCOL = "p5-receipt-1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def implementation_digest() -> str:
    # Bind only this protocol and its dependencies, not future runners/brokers.
    return hash_json(
        {
            "verifier": _sha(Path(__file__).read_bytes()),
            "executor_and_snapshot_helpers": validator_digest(),
        }
    )


def policy_violations(task: ExtensionSuite, candidate: Path) -> list[str]:
    """Protect the whole task inventory, including public checks and directories."""
    paths = {p.relative_to(candidate).as_posix(): p for p in candidate.rglob("*")}
    reasons = []
    if set(paths) != set(task.initial_files):
        reasons.append("task_inventory_changed")
    for name, path in sorted(paths.items()):
        if path.is_symlink() or not path.is_file():
            reasons.append("task_file_type_changed:" + name)
        elif path.stat().st_mode & 0o111:
            reasons.append("task_executable_changed:" + name)
        elif (
            name in task.initial_files
            and name not in task.editable_paths
            and path.read_bytes() != task.initial_files[name].encode()
        ):
            reasons.append("protected_task_changed:" + name)
    return reasons


def _phase(task: ExtensionSuite) -> dict:
    return {"id": "extension_oracle", "program": task.oracle, "files": {}, "arguments": []}


def suite_identity(task: ExtensionSuite) -> dict:
    return {
        "suiteId": task.suite_id,
        "taskId": task.task_id,
        "revision": task.revision,
        "lineageId": task.lineage_id,
        "purpose": task.purpose,
        "digest": suite_digest(task),
    }


def _valid_checks(task, checks):
    return (
        isinstance(checks, list)
        and all(isinstance(row, dict) for row in checks)
        and [row.get("name") for row in checks] == [row["id"] for row in task.checks]
        and all(
            row.get("status") in ("passed", "failed", "error")
            and set(row) <= {"name", "status", "error"}
            and ("error" not in row or isinstance(row["error"], str))
            for row in checks
        )
    )


def _groups(task, checks, status):
    # A process/report fault invalidates every group, even if some rows say passed.
    groups = dict.fromkeys((row["group"] for row in task.checks), "error")
    if status == "error":
        return groups
    for group in groups:
        ids = {row["id"] for row in task.checks if row["group"] == group}
        groups[group] = (
            "passed"
            if all(c["status"] == "passed" for c in checks if c["name"] in ids)
            else "failed"
        )
    return groups


def _assess(task, job, execution, executed_root, unchanged, policy):
    """Recompute from complete ordered checks, never from an asserted success."""
    checks = []
    try:
        report = _read(job / "report.json")
        checks = report["checks"]
        valid = (
            set(report) == {"checks", "imported"}
            and _valid_checks(task, checks)
            and report["imported"]
            == {name: str(executed_root / (name + ".py")) for name in ("caller", "fake_graph")}
        )
        if not valid:
            return "error", [], _groups(task, [], "error"), ["invalid_or_incomplete_checks"]
    except (OSError, ValueError, KeyError, TypeError):
        return "error", [], _groups(task, [], "error"), ["missing_or_invalid_report"]
    if (
        _phase_fault(execution)
        or any(
            execution.get(key) is not False
            for key in ("timed_out", "cancelled", "output_limited", "background_processes")
        )
        or execution.get("limit_reasons") != []
        or type(execution.get("returncode")) is not int
        or execution.get("returncode") != int(any(c["status"] != "passed" for c in checks))
        or unchanged is not True
        or any(c["status"] == "error" for c in checks)
    ):
        return "error", checks, _groups(task, checks, "error"), ["execution_or_check_error"]
    reasons = [*policy, *("check_failed:" + c["name"] for c in checks if c["status"] == "failed")]
    status = "failed" if reasons else "passed"
    return status, checks, _groups(task, checks, "error" if policy else status), reasons


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError("extension_timeout_invalid")


def _no_symlinks(path: Path) -> None:
    platform_aliases = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
    for item in (path.absolute(), *path.absolute().parents):
        if item.is_symlink() and platform_aliases.get(item) != item.resolve():
            raise ValueError("extension_artifact_symlink")


def verify_suite(
    suite_id: str,
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
    task = get_suite(suite_id)
    pinned_identity, pinned_implementation = suite_identity(task), implementation_digest()
    if cancellation_event is not None and not callable(getattr(cancellation_event, "is_set", None)):
        raise ValueError("invalid_cancellation_event")
    run_id, attempt_id = (
        _identity(uuid.uuid4().hex if run_id is None else run_id),
        _identity(uuid.uuid4().hex if attempt_id is None else attempt_id),
    )
    # Validate before resolving: otherwise links inside the store disappear.
    _no_symlinks(Path(candidate))
    _no_symlinks(Path(artifact_root))
    candidate, root = Path(candidate).resolve(), Path(artifact_root).resolve()
    if root.is_relative_to(candidate) or candidate.is_relative_to(root):
        raise ValueError("extension_artifacts_overlap_candidate")
    _no_symlinks(root / "snapshots")
    store = SnapshotStore(root / "snapshots")
    snapshot = store.capture(candidate)
    job = root / "runs" / run_id / attempt_id
    _no_symlinks(job)
    job.mkdir(parents=True, exist_ok=False, mode=0o700)
    environment, started = _environment(), datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="memos-extension-verify-", dir="/tmp") as temporary:
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
            status, checks, group_status, reasons = _assess(
                task, job / "phase", execution, restored, unchanged, policy
            )
            executed_root = str(restored)
        finally:
            _set_writable(restored, True)
    if (
        suite_identity(get_suite(suite_id)) != pinned_identity
        or implementation_digest() != pinned_implementation
    ):
        status, group_status = "error", _groups(task, checks, "error")
        reasons.append("extension_binding_changed")
    receipt = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "implementationDigest": pinned_implementation,
        "suite": pinned_identity,
        "checkMapping": list(task.checks),
        "groupStatus": group_status,
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
            raise ValueError("extension_receipt_symlink")
        path = path.resolve()
        _no_symlinks(path)
        _no_symlinks(path.with_suffix(".sha256"))
        if (
            not path.is_relative_to(root)
            or _sha(path.read_bytes()) != path.with_suffix(".sha256").read_text().strip()
        ):
            raise ValueError("extension_receipt_changed")
        receipt = _read(path)
        if (
            type(receipt.get("schemaVersion")) is not int
            or receipt.get("qualifiedAgentExperience") is not False
        ):
            raise ValueError("extension_receipt_types_changed")
        if set(receipt) != {
            "schemaVersion",
            "protocol",
            "implementationDigest",
            "suite",
            "checkMapping",
            "groupStatus",
            "runId",
            "attemptId",
            "candidateSnapshot",
            "candidateUnchanged",
            "executedCandidateRoot",
            "environment",
            "environmentDigest",
            "timeoutSeconds",
            "startedAt",
            "finishedAt",
            "execution",
            "phaseArtifacts",
            "policyViolations",
            "checks",
            "status",
            "reasons",
            "qualifiedAgentExperience",
            "branchCoverage",
        }:
            raise ValueError("extension_receipt_fields_changed")
        task = get_suite(receipt["suite"]["suiteId"])
        if receipt["status"] not in ("passed", "failed", "error"):
            raise ValueError("extension_status_invalid")
        status = receipt["status"]
        expected = {
            "schemaVersion": 1,
            "protocol": PROTOCOL,
            "implementationDigest": implementation_digest(),
            "suite": suite_identity(task),
            "checkMapping": list(task.checks),
            "environmentDigest": hash_json(_environment()),
            "qualifiedAgentExperience": False,
            "branchCoverage": [],
        }
        if any(receipt.get(k) != v for k, v in expected.items()):
            raise ValueError("extension_identity_changed")
        if hash_json(receipt["environment"]) != receipt["environmentDigest"]:
            raise ValueError("extension_environment_changed")
        if (
            path
            != root
            / "runs"
            / _identity(receipt["runId"])
            / _identity(receipt["attemptId"])
            / "receipt.json"
        ):
            raise ValueError("extension_attempt_changed")
        _timeout(receipt["timeoutSeconds"])
        _no_symlinks(root / "snapshots")
        store = SnapshotStore(root / "snapshots")
        store.verify(receipt["candidateSnapshot"])
        if (
            Path(candidate).is_symlink()
            or snapshot_fingerprint(Path(candidate)) != receipt["candidateSnapshot"]
        ):
            raise ValueError("extension_candidate_changed")
        with tempfile.TemporaryDirectory(prefix="memos-extension-recheck-") as temporary:
            restored = store.restore(receipt["candidateSnapshot"], Path(temporary) / "candidate")
            policy = policy_violations(task, restored)
        job = path.parent / "phase"
        if (
            policy != receipt["policyViolations"]
            or _phase_artifacts(job) != receipt["phaseArtifacts"]
        ):
            raise ValueError("extension_artifacts_or_policy_changed")
        executed_root = Path(receipt["executedCandidateRoot"])
        if not executed_root.is_absolute():
            raise ValueError("extension_execution_root_invalid")
        execution = check_phase_artifacts(
            _phase(task), job, candidate=executed_root, timeout=receipt["timeoutSeconds"]
        )
        if execution != receipt["execution"]:
            raise ValueError("extension_execution_changed")
        actual = _assess(task, job, execution, executed_root, receipt["candidateUnchanged"], policy)
        if actual != (status, receipt["checks"], receipt["groupStatus"], receipt["reasons"]):
            raise ValueError("extension_result_changed")
        return ReceiptCheck(True, status, ())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        # Do not put private paths, artifact text or exception messages in UI errors.
        return ReceiptCheck(
            False, status, ("invalid_extension_receipt:" + type(error).__name__,), True
        )
