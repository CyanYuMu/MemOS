"""Qualification from exact locked variants; this is not Agent effectiveness evidence."""

from __future__ import annotations

import hashlib
import json
import tempfile

from pathlib import Path

from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_evaluation.snapshots import snapshot_fingerprint
from memos.skill_evaluation.tasks import case_digest, list_cases, materialize_case
from memos.skill_verification.broker import VisibleCheckBroker, check_visible_record
from memos.skill_verification.executor import implementation_digest
from memos.skill_verification.plans import acceptance_plan, digest_json, visible_plan
from memos.skill_verification.verifier import check_receipt, validator_digest, verify_candidate


SELFTEST_VERSION = "p41-assets-1"
VARIANTS = ("initial", "reference", "unsafe")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("invalid_qualification_artifact")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_qualification_key")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("invalid_qualification_artifact")
    return value


def _write(path: Path, value: dict):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    path.chmod(0o600)


def _cases():
    cases = tuple(sorted(list_cases("holdout"), key=lambda case: case.case_id))
    if len(cases) != 7:
        raise ValueError("qualification_requires_seven_locked_tasks")
    return cases


def _binding() -> dict:
    directory = Path(__file__).parent
    return {
        "selftest_version": SELFTEST_VERSION,
        "selftest_digest": digest_json(
            {name: _sha(directory / name) for name in ("selftest.py", "broker.py", "__main__.py")}
        ),
        "executor_digest": implementation_digest(),
        "validator_digest": validator_digest(),
        "cases": [
            {
                "case_id": case.case_id,
                "asset_digest": case_digest(case),
                "acceptance_plan": acceptance_plan(case.case_id)["plan_digest"],
                "visible_plan": visible_plan(case.case_id)["plan_digest"],
            }
            for case in _cases()
        ],
    }


def _expected(case, variant: str) -> str:
    if variant == "reference":
        return "needs_review" if case.assessment_kind == "manual_boundary" else "passed"
    return "failed"


def _visible_expected(case, variant: str) -> int:
    # The semantic ambiguity is intentionally unresolved by reference diagnosis;
    # the visible assertion still fails and must not be weakened to turn green.
    return 0 if variant == "reference" and case.assessment_kind != "manual_boundary" else 1


def _safe_artifact(root: Path, relative: str) -> Path:
    path = Path(relative)
    if (
        path.is_absolute()
        or path.as_posix() != relative
        or any(part in (".", "..") for part in path.parts)
    ):
        raise ValueError("invalid_qualification_path")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("qualification_symlink")
    if not current.resolve().is_relative_to(root):
        raise ValueError("invalid_qualification_path")
    return current


def audit_tasks(
    output: Path, *, timeout: float = 10, cancellation_event=None, progress=None
) -> dict:
    """Exclusively create 21 acceptance and 14 visible checks without a model call."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    binding = _binding()
    artifacts = output / "verification"
    cases = []
    for case in _cases():
        item = {
            "case_id": case.case_id,
            "asset_digest": case_digest(case),
            "lineage_id": case.lineage_id,
            "results": [],
            "visible": [],
        }
        for variant in VARIANTS:
            if cancellation_event is not None and cancellation_event.is_set():
                raise ValueError("qualification_cancelled")
            if progress:
                progress({"case_id": case.case_id, "variant": variant, "stage": "started"})
            relative = f"candidates/{case.case_id}/{variant}"
            candidate = materialize_case(case.case_id, output / relative, variant)
            result = verify_candidate(
                case.case_id,
                candidate,
                artifacts,
                timeout=timeout,
                cancellation_event=cancellation_event,
            )
            receipt = _read(result.receipt_path)
            checked = check_receipt(result.receipt_path, candidate, artifacts)
            failures = {row["name"] for row in receipt["checks"] if row["status"] == "failed"}
            item["results"].append(
                {
                    "variant": variant,
                    "status": result.status,
                    "expected_status": _expected(case, variant),
                    "expected_checks_satisfied": set(case.expected_failure_checks.get(variant, ()))
                    <= failures,
                    "candidate": relative,
                    "candidate_snapshot": snapshot_fingerprint(candidate),
                    "receipt": result.receipt_path.relative_to(output).as_posix(),
                    "receipt_sha256": _sha(result.receipt_path),
                    "receipt_matches": checked.matches,
                }
            )
            if variant in ("initial", "reference"):
                evidence = output / "visible" / case.case_id / variant
                with VisibleCheckBroker(
                    case.case_id,
                    candidate,
                    evidence,
                    timeout=timeout,
                    cancellation_event=cancellation_event,
                ) as broker:
                    visible = _request_visible_checks(
                        {
                            "test_broker_socket": str(broker.socket_path),
                            "test_broker_timeout_seconds": timeout + 5,
                        }
                    )
                record_path = evidence / "check-1.json"
                item["visible"].append(
                    {
                        "variant": variant,
                        "candidate": relative,
                        "candidate_snapshot": snapshot_fingerprint(candidate),
                        "exit_code": visible["exit_code"],
                        "expected_exit_code": _visible_expected(case, variant),
                        "execution_error": visible.get("execution_error"),
                        "record": record_path.relative_to(output).as_posix(),
                        "record_sha256": _sha(record_path) if record_path.is_file() else None,
                        "record_matches": check_visible_record(record_path, candidate),
                    }
                )
            if progress:
                progress(
                    {
                        "case_id": case.case_id,
                        "variant": variant,
                        "stage": "finished",
                        "status": result.status,
                    }
                )
        cases.append(item)
    success = all(
        all(
            row["status"] == row["expected_status"]
            and row["receipt_matches"]
            and row["expected_checks_satisfied"]
            for row in case["results"]
        )
        and all(
            row["exit_code"] == row["expected_exit_code"]
            and not row["execution_error"]
            and row["record_matches"]
            for row in case["visible"]
        )
        for case in cases
    )
    summary = {
        "schema_version": 1,
        "purpose": "task_asset_self_test",
        "qualified_agent_experience": False,
        "effectiveness_evaluation": False,
        "new_model_calls": 0,
        "success": success,
        "binding": binding,
        "cases": cases,
    }
    # Do not issue qualification if the implementation changed during the run.
    if binding != _binding():
        summary["success"] = False
    summary["qualification_id"] = digest_json(summary)
    _write(output / "summary.json", summary)
    return summary


def require_qualification(directory: Path) -> dict:
    """Require the current implementation and exact locked initial/reference/unsafe evidence."""
    try:
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("qualification_symlink")
        root = directory.resolve()
        summary = _read(root / "summary.json")
        if (
            set(summary)
            != {
                "schema_version",
                "purpose",
                "qualified_agent_experience",
                "effectiveness_evaluation",
                "new_model_calls",
                "success",
                "binding",
                "cases",
                "qualification_id",
            }
            or summary["schema_version"] != 1
            or summary["purpose"] != "task_asset_self_test"
            or summary["qualified_agent_experience"] is not False
            or summary["effectiveness_evaluation"] is not False
            or summary["new_model_calls"] != 0
            or summary["success"] is not True
            or summary["binding"] != _binding()
            or summary["qualification_id"]
            != digest_json({k: v for k, v in summary.items() if k != "qualification_id"})
        ):
            raise ValueError("qualification_binding_changed")
        cases = _cases()
        if [item["case_id"] for item in summary["cases"]] != [case.case_id for case in cases]:
            raise ValueError("qualification_task_set_changed")
        artifacts = _safe_artifact(root, "verification")
        for case, item in zip(cases, summary["cases"], strict=True):
            if item["asset_digest"] != case_digest(case) or item["lineage_id"] != case.lineage_id:
                raise ValueError("qualification_task_changed")
            if tuple(row["variant"] for row in item["results"]) != VARIANTS or tuple(
                row["variant"] for row in item["visible"]
            ) != ("initial", "reference"):
                raise ValueError("qualification_variants_changed")
            variant_snapshots = {}
            for row in item["results"]:
                variant = row["variant"]
                relative = f"candidates/{case.case_id}/{variant}"
                candidate = _safe_artifact(root, relative)
                with tempfile.TemporaryDirectory(prefix="memos-p41-qualification-") as temporary:
                    expected = materialize_case(
                        case.case_id, Path(temporary) / "candidate", variant
                    )
                    expected_snapshot = snapshot_fingerprint(expected)
                variant_snapshots[variant] = expected_snapshot
                if (
                    row["candidate"] != relative
                    or row["candidate_snapshot"] != expected_snapshot
                    or snapshot_fingerprint(candidate) != expected_snapshot
                ):
                    raise ValueError("qualification_variant_mismatch")
                receipt_path = _safe_artifact(root, row["receipt"])
                if not receipt_path.is_relative_to(artifacts):
                    raise ValueError("qualification_receipt_path")
                receipt = _read(receipt_path)
                checked = check_receipt(receipt_path, candidate, artifacts)
                failures = {
                    check["name"] for check in receipt["checks"] if check["status"] == "failed"
                }
                if (
                    row["receipt_sha256"] != _sha(receipt_path)
                    or not checked.matches
                    or checked.verification_status != _expected(case, variant)
                    or row["expected_status"] != _expected(case, variant)
                    or row["status"] != checked.verification_status
                    or row["receipt_matches"] is not True
                    or receipt["case"]["case_id"] != case.case_id
                    or receipt["candidate_snapshot"] != expected_snapshot
                    or not set(case.expected_failure_checks.get(variant, ())) <= failures
                    or row["expected_checks_satisfied"] is not True
                ):
                    raise ValueError("qualification_receipt_unverified")
            for row in item["visible"]:
                variant = row["variant"]
                relative = f"candidates/{case.case_id}/{variant}"
                record_relative = f"visible/{case.case_id}/{variant}/check-1.json"
                record = _safe_artifact(root, record_relative)
                value = _read(record)
                if (
                    row["candidate"] != relative
                    or row["candidate_snapshot"] != variant_snapshots[variant]
                    or row["record"] != record_relative
                    or row["record_sha256"] != _sha(record)
                    or row["exit_code"] != _visible_expected(case, variant)
                    or row["expected_exit_code"] != _visible_expected(case, variant)
                    or row["execution_error"] is not None
                    or row["record_matches"] is not True
                    or value["case_id"] != case.case_id
                    or value["result"]["exit_code"] != row["exit_code"]
                    or value["result"]["execution_error"] is not None
                    or not check_visible_record(record, root / relative)
                ):
                    raise ValueError("qualification_visible_unverified")
        return summary
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise ValueError("verification_qualification_invalid") from error
