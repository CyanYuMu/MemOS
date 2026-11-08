"""Rechecked protocol qualification, separate from Agent evidence or coverage."""

from __future__ import annotations

import hashlib
import tempfile

from pathlib import Path

from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_branch_validation.broker import (
    VisibleCheckBroker,
    check_visible_record,
)
from memos.skill_branch_validation.broker import (
    implementation_digest as broker_digest,
)
from memos.skill_branch_validation.tasks import list_tasks, manifest, materialize
from memos.skill_branch_validation.verifier import (
    PROTOCOL,
    _no_symlinks,
    check_receipt,
    implementation_digest,
    verify_task,
)
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import snapshot_fingerprint
from memos.skill_verification.verifier import _read, _write


VARIANTS = ("initial", "reference", "unsafe")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding():
    return {
        "protocol": "branch-qualification-1",
        "qualificationDigest": _sha(Path(__file__)),
        "receiptProtocol": PROTOCOL,
        "verifierDigest": implementation_digest(),
        "brokerDigest": broker_digest(),
        "tasks": manifest(),
    }


def _candidate(task, variant):
    return f"candidates/{task.task_id}/{variant}"


def _hidden(task, variant, root):
    relative = _candidate(task, variant)
    path = root / "verification/runs" / task.task_id / variant / "receipt.json"
    receipt = _read(path)
    checked = check_receipt(path, root / relative, root / "verification")
    failures = [c["name"] for c in receipt["checks"] if c["status"] == "failed"]
    expected = "failed" if task.expected_failures[variant] else "passed"
    return {
        "taskId": task.task_id,
        "variant": variant,
        "candidate": relative,
        "candidateSnapshot": snapshot_fingerprint(root / relative),
        "receipt": path.relative_to(root).as_posix(),
        "receiptSha256": _sha(path),
        "status": checked.verification_status,
        "receiptMatches": checked.matches,
        "matchesExpectation": checked.matches
        and checked.verification_status == expected
        and set(failures) == set(task.expected_failures[variant]),
    }


def _visible(task, variant, root):
    relative = _candidate(task, variant)
    path = root / "visible" / task.task_id / variant / "check-1.json"
    record = _read(path)
    response_path = path.parent / "client-response.json"
    _no_symlinks(response_path)
    delivered = _read(response_path)
    expected = 0 if variant == "reference" else 1
    matched = check_visible_record(path, root / relative)
    return {
        "taskId": task.task_id,
        "variant": variant,
        "candidate": relative,
        "record": path.relative_to(root).as_posix(),
        "recordSha256": _sha(path),
        "responseSha256": _sha(response_path),
        "responseMatches": delivered == record["result"],
        "exitCode": record["result"]["exit_code"],
        "recordMatches": matched,
        "matchesExpectation": matched
        and delivered == record["result"]
        and record["result"]["exit_code"] == expected
        and record["result"]["execution_error"] is None,
    }


def audit_protocol(
    output: Path, *, timeout: float = 15, cancellation_event=None, progress=None
) -> dict:
    """Execute six hidden and four visible checks; never call a provider."""
    _no_symlinks(Path(output))
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    binding = _binding()
    tasks = list_tasks()
    hidden, visible = [], []
    for task in tasks:
        for variant in VARIANTS:
            if cancellation_event is not None and cancellation_event.is_set():
                raise ValueError("branch_qualification_cancelled")
            candidate = materialize(task.task_id, root / _candidate(task, variant), variant)
            verify_task(
                task.task_id,
                candidate,
                root / "verification",
                run_id=task.task_id,
                attempt_id=variant,
                timeout=timeout,
                cancellation_event=cancellation_event,
            )
            row = _hidden(task, variant, root)
            hidden.append(row)
            if progress:
                progress(
                    {
                        "stage": "hidden",
                        "taskId": task.task_id,
                        "variant": variant,
                        "status": row["status"],
                        "matchesExpectation": row["matchesExpectation"],
                    }
                )
            if variant == "unsafe":
                continue
            with VisibleCheckBroker(
                task.task_id,
                candidate,
                root / "visible" / task.task_id / variant,
                timeout=timeout,
                cancellation_event=cancellation_event,
            ) as broker:
                response = _request_visible_checks(
                    {
                        "test_broker_socket": str(broker.socket_path),
                        "test_broker_timeout_seconds": timeout + 5,
                    }
                )
            _write(root / "visible" / task.task_id / variant / "client-response.json", response)
            visible.append(_visible(task, variant, root))
    result = {
        "schemaVersion": 1,
        "purpose": "branch_protocol_qualification",
        "binding": binding,
        "hidden": hidden,
        "visible": visible,
        "modelCalls": 0,
        "qualifiedAgentExperience": False,
        "branchCoverage": [],
        "humanApprovals": 0,
        "success": binding == _binding() and all(r["matchesExpectation"] for r in hidden + visible),
    }
    result["qualificationId"] = hash_json(result)
    _write(root / "summary.json", result)
    if result["success"]:
        require_qualification(root)
    return result


def require_qualification(directory: Path) -> dict:
    """Recompute the entire fixed matrix and retained facts before Agent admission."""
    try:
        _no_symlinks(Path(directory))
        root = Path(directory).resolve()
        _no_symlinks(root / "summary.json")
        result = _read(root / "summary.json")
        if set(result) != {
            "schemaVersion",
            "purpose",
            "binding",
            "hidden",
            "visible",
            "modelCalls",
            "qualifiedAgentExperience",
            "branchCoverage",
            "humanApprovals",
            "success",
            "qualificationId",
        }:
            raise ValueError("qualification_fields_changed")
        fixed = {
            "schemaVersion": 1,
            "purpose": "branch_protocol_qualification",
            "binding": _binding(),
            "modelCalls": 0,
            "qualifiedAgentExperience": False,
            "branchCoverage": [],
            "humanApprovals": 0,
            "success": True,
        }
        if any(result[k] != v for k, v in fixed.items()) or result["qualificationId"] != hash_json(
            {k: v for k, v in result.items() if k != "qualificationId"}
        ):
            raise ValueError("qualification_binding_changed")
        hidden, visible = [], []
        for task in list_tasks():
            for variant in VARIANTS:
                candidate = root / _candidate(task, variant)
                _no_symlinks(candidate)
                # Correct contents for each variant are needed, not merely a valid receipt.
                with tempfile.TemporaryDirectory(prefix="memos-branch-asset-") as temporary:
                    expected = materialize(task.task_id, Path(temporary) / "candidate", variant)
                    if snapshot_fingerprint(expected) != snapshot_fingerprint(candidate):
                        raise ValueError("qualification_variant_changed")
                hidden.append(_hidden(task, variant, root))
                if variant != "unsafe":
                    visible.append(_visible(task, variant, root))
        if (
            result["hidden"] != hidden
            or result["visible"] != visible
            or not all(row["matchesExpectation"] for row in hidden + visible)
        ):
            raise ValueError("qualification_matrix_changed")
        return {"qualificationId": result["qualificationId"], "binding": result["binding"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ValueError("branch_qualification_invalid") from None
