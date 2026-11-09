"""Twelve retained executions qualify one suite's protocol, never Agent coverage."""

from __future__ import annotations

import hashlib
import tempfile

from pathlib import Path

from memos.skill_branch_extensions.broker import (
    BROKER_VERSION,
    RESULT_VERSION,
    VisibleCheckBroker,
    check_visible_record,
    decode_public_result,
)
from memos.skill_branch_extensions.broker import implementation_digest as broker_digest
from memos.skill_branch_extensions.tasks import VARIANTS, get_suite, suite_manifest
from memos.skill_branch_extensions.verifier import (
    PROTOCOL,
    _groups,
    _no_symlinks,
    _timeout,
    check_receipt,
    implementation_digest,
    suite_identity,
    verify_suite,
)
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import snapshot_fingerprint
from memos.skill_experiments import recording_server
from memos.skill_verification.verifier import _read, _write


QUALIFICATION_PROTOCOL = "extension-qualification-1"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(suite_id):
    return {
        "protocol": QUALIFICATION_PROTOCOL,
        "qualificationDigest": _sha(Path(__file__)),
        "receiptProtocol": PROTOCOL,
        "verifierDigest": implementation_digest(),
        "brokerProtocol": BROKER_VERSION,
        "brokerDigest": broker_digest(),
        "resultProtocol": RESULT_VERSION,
        "clientDigest": _sha(Path(recording_server.__file__)),
        "suite": suite_manifest(get_suite(suite_id)),
    }


def _candidate(suite, variant):
    return f"candidates/{suite.suite_id}/{variant}"


def _materialize_variant(suite, destination, variant):
    # Host-only construction. The public preparation API still exposes initial only.
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in suite.variants[variant].items():
        path = destination / name
        path.write_text(content)
        path.chmod(0o600)
    return destination


def _expected(suite, variant, mode):
    failed = list(suite.expected_failures[variant][mode])
    checks = [
        {"name": row["id"], "status": "failed" if row["id"] in failed else "passed"}
        for row in suite.checks
    ]
    status = "failed" if failed else "passed"
    return status, failed, _groups(suite, checks, status)


def _hidden(suite, variant, root):
    relative = _candidate(suite, variant)
    path = root / "verification/runs" / suite.suite_id / variant / "receipt.json"
    receipt = _read(path)
    checked = check_receipt(path, root / relative, root / "verification")
    failures = [c["name"] for c in receipt["checks"] if c["status"] == "failed"]
    expected_status, expected_failed, expected_groups = _expected(suite, variant, "hidden")
    return {
        "suite": suite_identity(suite),
        "variant": variant,
        "candidate": relative,
        "candidateSnapshot": snapshot_fingerprint(root / relative),
        "receipt": path.relative_to(root).as_posix(),
        "receiptSha256": _sha(path),
        "status": checked.verification_status,
        "checks": receipt["checks"],
        "groupStatus": receipt["groupStatus"],
        "receiptMatches": checked.matches,
        "matchesExpectation": checked.matches
        and receipt["suite"] == suite_identity(suite)
        and checked.verification_status == expected_status
        and failures == expected_failed
        and receipt["groupStatus"] == expected_groups,
    }


def _visible(suite, variant, root):
    relative = _candidate(suite, variant)
    path = root / "visible" / suite.suite_id / variant / "check-1.json"
    record = _read(path)
    response_path = path.parent / "client-response.json"
    _no_symlinks(response_path)
    delivered = _read(response_path)
    matched = check_visible_record(path, root / relative)
    report = decode_public_result(suite.suite_id, record["result"])
    expected_status, expected_failed, expected_groups = _expected(suite, variant, "public")
    failures = [c["name"] for c in report["checks"] if c["status"] == "failed"]
    return {
        "suite": suite_identity(suite),
        "variant": variant,
        "candidate": relative,
        "record": path.relative_to(root).as_posix(),
        "recordSha256": _sha(path),
        "responseSha256": _sha(response_path),
        "responseMatches": delivered == record["result"],
        "exitCode": record["result"]["exit_code"],
        "status": report["status"],
        "checks": report["checks"],
        "groupStatus": report["groupStatus"],
        "recordMatches": matched,
        "matchesExpectation": matched
        and delivered == record["result"]
        and record["suite_identity"] == suite_identity(suite)
        and report["status"] == expected_status
        and failures == expected_failed
        and report["groupStatus"] == expected_groups
        and record["result"]["execution_error"] is None,
    }


def audit_protocol(
    suite_id: str, output: Path, *, timeout: float = 15, cancellation_event=None, progress=None
) -> dict:
    """Run all six variants through hidden verifier and unchanged public client."""
    _timeout(timeout)
    if cancellation_event is not None and not callable(getattr(cancellation_event, "is_set", None)):
        raise ValueError("invalid_cancellation_event")
    _no_symlinks(Path(output))
    root = Path(output).resolve()
    binding = _binding(suite_id)
    suite = get_suite(suite_id)
    if suite_manifest(suite) != binding["suite"]:
        raise ValueError("extension_qualification_binding_changed")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    hidden, visible = [], []
    for variant in VARIANTS:
        if cancellation_event is not None and cancellation_event.is_set():
            raise ValueError("extension_qualification_cancelled")
        candidate = _materialize_variant(suite, root / _candidate(suite, variant), variant)
        verify_suite(
            suite_id,
            candidate,
            root / "verification",
            run_id=suite.suite_id,
            attempt_id=variant,
            timeout=timeout,
            cancellation_event=cancellation_event,
        )
        row = _hidden(suite, variant, root)
        hidden.append(row)
        if progress:
            progress(
                {
                    "stage": "hidden",
                    "suiteId": suite_id,
                    "variant": variant,
                    "status": row["status"],
                    "matchesExpectation": row["matchesExpectation"],
                }
            )
        with VisibleCheckBroker(
            suite_id,
            candidate,
            root / "visible" / suite.suite_id / variant,
            timeout=timeout,
            cancellation_event=cancellation_event,
        ) as broker:
            response = recording_server._request_visible_checks(
                {
                    "test_broker_socket": str(broker.socket_path),
                    "test_broker_timeout_seconds": timeout + 5,
                }
            )
        _write(root / "visible" / suite.suite_id / variant / "client-response.json", response)
        row = _visible(suite, variant, root)
        visible.append(row)
        if progress:
            progress(
                {
                    "stage": "visible",
                    "suiteId": suite_id,
                    "variant": variant,
                    "status": row["status"],
                    "matchesExpectation": row["matchesExpectation"],
                }
            )
    result = {
        "schemaVersion": 1,
        "purpose": "extension_protocol_qualification",
        "binding": binding,
        "hidden": hidden,
        "visible": visible,
        "modelCalls": 0,
        "qualifiedAgentExperience": False,
        "branchCoverage": [],
        "humanApprovals": 0,
        "success": binding == _binding(suite_id)
        and all(r["matchesExpectation"] for r in hidden + visible),
    }
    result["qualificationId"] = hash_json(result)
    _write(root / "summary.json", result)
    if result["success"]:
        require_qualification(root, suite_id=suite_id)
    return result


def require_qualification(directory: Path, *, suite_id: str) -> dict:
    """Replay the complete fixed matrix, artifact policy, and actual client delivery."""
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
        binding = _binding(suite_id)
        suite = get_suite(suite_id)
        fixed = {
            "schemaVersion": 1,
            "purpose": "extension_protocol_qualification",
            "binding": binding,
            "modelCalls": 0,
            "qualifiedAgentExperience": False,
            "branchCoverage": [],
            "humanApprovals": 0,
            "success": True,
        }
        if (
            any(result[k] != v for k, v in fixed.items())
            or type(result["schemaVersion"]) is not int
            or type(result["modelCalls"]) is not int
            or type(result["humanApprovals"]) is not int
            or result["qualifiedAgentExperience"] is not False
            or result["success"] is not True
            or result["qualificationId"]
            != hash_json({k: v for k, v in result.items() if k != "qualificationId"})
            or suite_manifest(suite) != binding["suite"]
        ):
            raise ValueError("qualification_binding_changed")
        hidden, visible = [], []
        for variant in VARIANTS:
            candidate = root / _candidate(suite, variant)
            _no_symlinks(candidate)
            with tempfile.TemporaryDirectory(prefix="memos-extension-asset-") as temporary:
                expected = _materialize_variant(suite, Path(temporary) / "candidate", variant)
                if snapshot_fingerprint(expected) != snapshot_fingerprint(candidate):
                    raise ValueError("qualification_variant_changed")
            hidden.append(_hidden(suite, variant, root))
            visible.append(_visible(suite, variant, root))
        if (
            result["hidden"] != hidden
            or result["visible"] != visible
            or not all(row["matchesExpectation"] for row in hidden + visible)
            or binding != _binding(suite_id)
        ):
            raise ValueError("qualification_matrix_changed")
        return {"qualificationId": result["qualificationId"], "binding": result["binding"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ValueError("extension_qualification_invalid") from None
