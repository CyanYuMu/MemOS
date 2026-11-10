"""Sealed final-patch receipts for the two new same-contract task instances."""

import hashlib
import json
import tempfile

from pathlib import Path
from types import SimpleNamespace

from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import snapshot_fingerprint
from memos.skill_evaluation.tasks import candidate_policy_violations
from memos.skill_management.domain import require
from memos.skill_transfer.tasks import TASKS, case_digest, get_case, materialize_case
from memos.skill_verification.executor import execute_program, implementation_digest


def sha(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 8_000_000)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def run_checks(case, candidate, output, *, hidden=True, secret_values=(), cancellation_event=None):
    source = case.oracle_source if hidden else case.initial_files["visible_checks.py"]
    outcome = execute_program(
        source,
        candidate,
        output,
        10,
        secret_values=secret_values,
        cancellation_event=cancellation_event,
    )
    fault = any(
        outcome.get(k)
        for k in ("timed_out", "start_error", "cancelled", "output_limited", "limit_reasons")
    )
    path = output / "report.json"
    report = json.loads(path.read_text()) if path.is_file() else {}
    checks = report.get("checks", [])
    expected_names = ["update_contract"] + (
        [
            "mixed_actions_order_empty_content"
            if case.case_id == TASKS[0]
            else "version_other_actions_no_write",
            "invalid_parameters_no_write",
        ]
        if hidden
        else []
    )
    valid = (
        isinstance(checks, list)
        and [c.get("name") for c in checks] == expected_names
        and all(type(c.get("passed")) is bool for c in checks)
    )
    passed = valid and all(c["passed"] for c in checks)
    status = (
        "error"
        if fault or not valid or outcome.get("returncode") != (0 if passed else 1)
        else "passed"
        if passed
        else "failed"
    )
    return {"status": status, "checks": checks, "execution": outcome}


def verify_candidate(
    case_id,
    candidate,
    artifact_root,
    *,
    run_id,
    attempt_id,
    secret_values=(),
    cancellation_event=None,
):
    case = get_case(case_id)
    candidate, root = Path(candidate).resolve(), Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    snapshot = snapshot_fingerprint(candidate)
    policy = candidate_policy_violations(case, candidate)
    result = (
        {"status": "failed", "checks": [], "execution": None}
        if policy
        else run_checks(
            case,
            candidate,
            root / "hidden",
            secret_values=secret_values,
            cancellation_event=cancellation_event,
        )
    )
    require(snapshot_fingerprint(candidate) == snapshot, "transfer_candidate_changed")
    artifacts = {str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()}
    body = {
        "protocol": "transfer-receipt-1",
        "caseId": case_id,
        "taskDigest": case_digest(case),
        "candidateSnapshot": snapshot,
        "runId": run_id,
        "attemptId": attempt_id,
        "policyViolations": policy,
        **result,
        "artifacts": artifacts,
        "verifierDigest": verifier_digest(),
    }
    receipt = {**body, "receiptId": hash_json(body)}
    path = root / "receipt.json"
    write(path, receipt)
    return SimpleNamespace(status=result["status"], receipt_path=path)


def check_receipt(receipt_path, candidate, artifact_root):
    try:
        root, path = Path(artifact_root).resolve(), Path(receipt_path)
        require(not path.is_symlink() and path.resolve() == root / "receipt.json")
        sha(path)
        value = json.loads(path.read_text())
        body = {k: v for k, v in value.items() if k != "receiptId"}
        require(hash_json(body) == value["receiptId"] and value["protocol"] == "transfer-receipt-1")
        case = get_case(value["caseId"])
        require(
            value["taskDigest"] == case_digest(case)
            and value["verifierDigest"] == verifier_digest()
            and value["candidateSnapshot"] == snapshot_fingerprint(Path(candidate))
            and value["policyViolations"] == candidate_policy_violations(case, Path(candidate))
        )
        actual = {
            str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file() and p != path
        }
        require(actual == value["artifacts"])
        if not value["policyViolations"]:
            require((root / "hidden/program.py").read_text() == case.oracle_source)
        return SimpleNamespace(matches=True)
    except (ValueError, OSError, KeyError, TypeError):
        return SimpleNamespace(matches=False)


def verifier_digest():
    return hash_json(
        {
            "verifier": sha(Path(__file__)),
            "tasks": sha(Path(__file__).with_name("tasks.py")),
            "executor": implementation_digest(),
        }
    )


def audit(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in TASKS:
        for variant in ("initial", "reference", "skip", "lenient", "weaken_checks"):
            with tempfile.TemporaryDirectory(prefix="memos-transfer-selftest-") as temp:
                candidate = materialize_case(task, Path(temp) / "candidate", variant)
                target = output / (task + "-" + variant)
                result = verify_candidate(
                    task, candidate, target, run_id="selftest", attempt_id=variant
                )
                expected = "passed" if variant == "reference" else "failed"
                rows.append(
                    {
                        "task": task,
                        "variant": variant,
                        "expected": expected,
                        "status": result.status,
                        "receiptSha256": sha(result.receipt_path),
                        "matched": check_receipt(result.receipt_path, candidate, target).matches,
                    }
                )
    value = {
        "verifierDigest": verifier_digest(),
        "rows": rows,
        "passed": all(r["status"] == r["expected"] and r["matched"] for r in rows),
    }
    write(output / "qualification.json", {**value, "qualificationId": hash_json(value)})
    return value


def require_qualification(directory):
    path = Path(directory) / "qualification.json"
    sha(path)
    value = json.loads(path.read_text())
    require(
        value["qualificationId"]
        == hash_json({k: v for k, v in value.items() if k != "qualificationId"})
        and value["passed"]
        and value["verifierDigest"] == verifier_digest()
    )
    require(
        [(r["task"], r["variant"]) for r in value["rows"]]
        == [
            (t, v)
            for t in TASKS
            for v in ("initial", "reference", "skip", "lenient", "weaken_checks")
        ]
    )
    for row in value["rows"]:
        require(
            row["matched"]
            and row["status"] == row["expected"]
            and row["receiptSha256"]
            == sha(Path(directory) / (row["task"] + "-" + row["variant"]) / "receipt.json")
        )
        with tempfile.TemporaryDirectory(prefix="memos-transfer-qualification-") as temp:
            candidate = materialize_case(row["task"], Path(temp) / "candidate", row["variant"])
            root = Path(directory) / (row["task"] + "-" + row["variant"])
            require(
                check_receipt(root / "receipt.json", candidate, root).matches,
                "transfer_qualification_changed",
            )
    return {"qualificationId": value["qualificationId"], "verifierDigest": value["verifierDigest"]}
