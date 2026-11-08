"""Execute trusted dev oracles against sealed candidates and bind their results.

This is a local verification harness, not a security sandbox for hostile Python.
Receipts assume the collector and artifact directory are trusted.
"""

import hashlib
import json
import math
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

from memos.skill_evaluation.cases import CaseSpec, get_case
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint


VALIDATOR_VERSION = "0.1"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class VerificationResult:
    status: str
    receipt_path: Path


@dataclass(frozen=True)
class ReceiptCheck:
    matches: bool
    verification_status: str
    reasons: tuple[str, ...]
    invalid: bool = False


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _validator_digest() -> str:
    directory = Path(__file__).parent
    return _digest(
        _json_bytes(
            {
                name: _digest((directory / name).read_bytes())
                for name in ("verifier.py", "snapshots.py")
            }
        )
    )


def _environment() -> dict[str, Any]:
    return {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "packages": sorted(
            (distribution.metadata.get("Name", ""), distribution.version)
            for distribution in distributions()
        ),
    }


def _identity(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Invalid run or attempt identity")
    return value


def _case_identity(case: CaseSpec) -> dict[str, str]:
    return {
        "case_id": case.case_id,
        "revision": case.revision,
        "split": case.split,
        "lineage_id": case.lineage_id,
    }


def _write_json(path: Path, value: Any) -> None:
    with path.open("xb") as stream:
        stream.write(_json_bytes(value))


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "sha256": _digest(data), "size": len(data)}


def _read_artifact(reference: dict[str, Any], root: Path) -> bytes:
    relative = Path(reference["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Artifact path escapes its root")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path escapes its root")
    data = path.read_bytes()
    if _digest(data) != reference["sha256"] or len(data) != reference["size"]:
        raise ValueError("Artifact integrity mismatch")
    return data


def _initial_tree(case: CaseSpec, target: Path) -> None:
    target.mkdir()
    for name, content in case.initial_files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _set_writable(root: Path, writable: bool) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod((mode | stat.S_IWUSR) if writable else (mode & ~0o222))


def _stop_group(process: subprocess.Popen, *, terminate: bool) -> bool:
    """Clean only the process group created for this verification."""
    if os.name != "posix":
        if process.poll() is not None:
            return False
        process.kill()
        process.wait()
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM if terminate else signal.SIGKILL)
    except ProcessLookupError:
        return False
    if terminate:
        # Allow a bounded grace period, then stop surviving descendants too.
        deadline = time.monotonic() + 0.1
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
        time.sleep(max(0, deadline - time.monotonic()))
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    return True


def _execute(oracle: Path, candidate: Path, report: Path, job: Path, timeout: float) -> dict:
    started = time.monotonic()
    timed_out = False
    background = False
    environment = {"PATH": os.defpath}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with (job / "stdout.txt").open("wb") as stdout, (job / "stderr.txt").open("wb") as stderr:
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(oracle), str(candidate), str(report)],
                cwd=candidate.parent,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            return {
                "returncode": None,
                "timed_out": False,
                "background_processes": False,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "start_error": str(error),
            }
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_group(process, terminate=True)
        finally:
            background = _stop_group(process, terminate=True)
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "background_processes": background,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def _assess(
    case: CaseSpec, report: Any, execution: dict, candidate: Path, unchanged: bool
) -> tuple[str, list, list[str]]:
    reasons = []
    if (
        not isinstance(execution, dict)
        or not {"returncode", "timed_out", "background_processes", "elapsed_seconds"}
        <= execution.keys()
        or type(execution["timed_out"]) is not bool
        or type(execution["background_processes"]) is not bool
        or type(execution["elapsed_seconds"]) not in (int, float)
        or not math.isfinite(execution["elapsed_seconds"])
        or execution["elapsed_seconds"] < 0
        or (execution["returncode"] is not None and type(execution["returncode"]) is not int)
        or type(unchanged) is not bool
    ):
        return "error", [], ["invalid_execution_record"]
    if "start_error" in execution:
        if not isinstance(execution["start_error"], str):
            return "error", [], ["invalid_execution_record"]
        reasons.append("process_start_failed")
    if not unchanged:
        reasons.append("candidate_modified_during_verification")
    if execution.get("timed_out"):
        reasons.append("timeout")
    if execution.get("background_processes"):
        reasons.append("unsettled_background_process")
    if not isinstance(report, dict) or not isinstance(report.get("checks"), list):
        return "error", [], [*reasons, "missing_or_invalid_report"]
    checks = report["checks"]
    if any(
        not isinstance(check, dict)
        or not isinstance(check.get("name"), str)
        or not isinstance(check.get("status"), str)
        or check.get("status") not in {"passed", "failed", "error"}
        or not isinstance(check.get("detail"), str)
        for check in checks
    ):
        return "error", [], [*reasons, "invalid_check"]
    names = [check["name"] for check in checks]
    if len(names) != len(set(names)) or set(names) != set(case.required_checks):
        reasons.append("incomplete_check_set")
    imports = report.get("imported_files", {})
    if not isinstance(imports, dict) or set(imports) != {"caller", "fake_graph"}:
        reasons.append("missing_import_identity")
    else:
        for name, path in imports.items():
            if not isinstance(path, str) or Path(path) != candidate / f"{name}.py":
                reasons.append("wrong_import_identity")
    statuses = {check["status"] for check in checks}
    expected = "error" if "error" in statuses else "failed" if "failed" in statuses else "passed"
    if execution.get("returncode") != {"passed": 0, "failed": 1, "error": 2}[expected]:
        reasons.append("exit_report_mismatch")
    return ("error" if reasons else expected), checks, reasons


def verify_candidate(
    case_id: str,
    candidate: Path,
    artifact_root: Path,
    *,
    run_id: str | None = None,
    attempt_id: str | None = None,
    timeout: float = 10,
) -> VerificationResult:
    """Verify one captured dev candidate without changing the original directory."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    case = get_case(case_id)
    run_id = _identity(run_id or uuid.uuid4().hex)
    attempt_id = _identity(attempt_id or uuid.uuid4().hex)
    candidate = Path(candidate).resolve()
    root = Path(artifact_root).resolve()
    if root.is_relative_to(candidate):
        raise ValueError("Artifact storage must be outside the candidate")
    store = SnapshotStore(root / "snapshots")
    snapshot = store.capture(candidate)
    job = root / "runs" / run_id / attempt_id
    job.mkdir(parents=True, exist_ok=False)
    oracle = job / "oracle.py"
    oracle.write_text(case.oracle_source, encoding="utf-8")
    report_path = job / "oracle-result.json"
    environment = _environment()
    started = datetime.now(timezone.utc).isoformat()

    with tempfile.TemporaryDirectory(prefix="memos-verify-") as temporary:
        work = Path(temporary).resolve()
        _initial_tree(case, work / "initial")
        initial = store.capture(work / "initial")
        restored = store.restore(snapshot.snapshot_id, work / "candidate")
        _set_writable(restored, False)
        try:
            execution = _execute(oracle, restored, report_path, job, timeout)
            try:
                unchanged = snapshot_fingerprint(restored) == snapshot.snapshot_id
            except (ValueError, OSError):
                unchanged = False
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                report = None
            status, checks, reasons = _assess(case, report, execution, restored, unchanged)
            executed_root = str(restored)
        finally:
            _set_writable(restored, True)

    store.verify(snapshot.snapshot_id)
    artifacts = {
        "oracle": _artifact(oracle, root),
        "stdout": _artifact(job / "stdout.txt", root),
        "stderr": _artifact(job / "stderr.txt", root),
    }
    if report_path.is_file():
        artifacts["report"] = _artifact(report_path, root)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "validator_digest": _validator_digest(),
        "case": _case_identity(case),
        "run_id": run_id,
        "attempt_id": attempt_id,
        "initial_snapshot": initial.snapshot_id,
        "initial_snapshot_origin": "registered_dev_fixture",
        "candidate_snapshot": snapshot.snapshot_id,
        "contract_digest": _digest(case.contract.encode()),
        "oracle_digest": _digest(case.oracle_source.encode()),
        "environment": environment,
        "environment_digest": _digest(_json_bytes(environment)),
        "executed_candidate_root": executed_root,
        "candidate_unchanged": unchanged,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "execution": execution,
        "checks": checks,
        "status": status,
        "reasons": reasons,
        "artifacts": artifacts,
        "qualified_agent_experience": False,
    }
    receipt_path = job / "receipt.json"
    _write_json(receipt_path, receipt)
    (job / "receipt.sha256").write_text(_digest(receipt_path.read_bytes()) + "\n")
    return VerificationResult(status, receipt_path)


def check_receipt(receipt_path: Path, candidate: Path, artifact_root: Path) -> ReceiptCheck:
    """Recheck integrity and applicability; a matching failed receipt is still failed."""
    reasons = []
    status = "unknown"
    invalid = False
    root = Path(artifact_root).resolve()
    path = Path(receipt_path).resolve()
    try:
        if not path.is_relative_to(root):
            raise ValueError("Receipt is outside the artifact directory")
        data = path.read_bytes()
        if _digest(data) != path.with_suffix(".sha256").read_text().strip():
            raise ValueError("Receipt integrity mismatch")
        receipt = json.loads(data)
        if not isinstance(receipt, dict):
            raise ValueError("Invalid receipt")
        case = get_case(receipt["case"]["case_id"])
        status = receipt["status"]
        expected = {
            "schema_version": SCHEMA_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "validator_digest": _validator_digest(),
            "case": _case_identity(case),
            "contract_digest": _digest(case.contract.encode()),
            "oracle_digest": _digest(case.oracle_source.encode()),
            "environment_digest": _digest(_json_bytes(_environment())),
            "initial_snapshot_origin": "registered_dev_fixture",
            "qualified_agent_experience": False,
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                reasons.append(
                    "validator_mismatch" if key.startswith("validator") else f"{key}_mismatch"
                )
        if receipt["environment_digest"] != _digest(_json_bytes(receipt["environment"])):
            reasons.append("environment_integrity_mismatch")
        _identity(receipt["run_id"])
        _identity(receipt["attempt_id"])
        if path != root / "runs" / receipt["run_id"] / receipt["attempt_id"] / "receipt.json":
            reasons.append("run_attempt_mismatch")
        store = SnapshotStore(root / "snapshots")
        store.verify(receipt["candidate_snapshot"])
        store.verify(receipt["initial_snapshot"])
        with tempfile.TemporaryDirectory(prefix="memos-case-") as temporary:
            initial = Path(temporary) / "initial"
            _initial_tree(case, initial)
            if snapshot_fingerprint(initial) != receipt["initial_snapshot"]:
                reasons.append("initial_snapshot_mismatch")
        if snapshot_fingerprint(Path(candidate)) != receipt["candidate_snapshot"]:
            reasons.append("candidate_mismatch")
        references = receipt["artifacts"]
        if not isinstance(references, dict) or not {"oracle", "stdout", "stderr"} <= set(
            references
        ):
            raise ValueError("Missing required artifacts")
        contents = {key: _read_artifact(ref, root) for key, ref in references.items()}
        if _digest(contents["oracle"]) != receipt["oracle_digest"]:
            reasons.append("oracle_artifact_mismatch")
        report = json.loads(contents["report"]) if "report" in contents else None
        actual_status, checks, actual_reasons = _assess(
            case,
            report,
            receipt["execution"],
            Path(receipt["executed_candidate_root"]),
            receipt["candidate_unchanged"],
        )
        if (actual_status, checks, actual_reasons) != (
            status,
            receipt["checks"],
            receipt["reasons"],
        ):
            reasons.append("result_mismatch")
    except (OSError, ValueError, KeyError, TypeError) as error:
        invalid = True
        reasons.append(f"invalid_receipt: {error}")
    invalid = invalid or bool(
        {
            "result_mismatch",
            "run_attempt_mismatch",
            "environment_integrity_mismatch",
            "oracle_artifact_mismatch",
        }
        & set(reasons)
    )
    return ReceiptCheck(not reasons, status, tuple(dict.fromkeys(reasons)), invalid)
