"""Qualification is based on current receipts and exact locked variants."""

import json
import sys

import pytest

from memos.skill_verification.selftest import audit_tasks, require_qualification


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolation backend")


@pytest.fixture(scope="module")
def qualification(tmp_path_factory):
    output = tmp_path_factory.mktemp("p41-qualification") / "audit"
    result = audit_tasks(output)
    assert result["success"], result
    return output, result


def test_actual_seven_tasks_have_twenty_one_sealed_acceptances_and_visible_checks(qualification):
    output, summary = qualification
    assert summary["purpose"] == "task_asset_self_test"
    assert not summary["qualified_agent_experience"] and not summary["effectiveness_evaluation"]
    assert len(summary["cases"]) == 7
    assert sum(len(case["results"]) for case in summary["cases"]) == 21
    assert sum(len(case["visible"]) for case in summary["cases"]) == 14
    manual = next(case for case in summary["cases"] if case["case_id"] == "holdout-ambiguous")
    assert [r["status"] for r in manual["results"]] == ["failed", "needs_review", "failed"]
    assert all(v["exit_code"] == 1 for v in manual["visible"])
    assert require_qualification(output) == summary


def test_audit_output_is_exclusive(qualification):
    output, _ = qualification
    before = (output / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        audit_tasks(output)
    assert (output / "summary.json").read_bytes() == before


def test_missing_qualification_cannot_grant_runner_admission(tmp_path):
    with pytest.raises(ValueError, match="qualification"):
        require_qualification(tmp_path)


@pytest.mark.parametrize("mutation", ["status", "case", "variant", "visible", "binding"])
def test_rehashed_summary_cannot_replace_locked_evidence(qualification, mutation):
    from memos.skill_verification.plans import digest_json

    output, _ = qualification
    path = output / "summary.json"
    original = path.read_bytes()
    summary = json.loads(original)
    if mutation == "status":
        summary["cases"][0]["results"][0]["status"] = "passed"
    elif mutation == "case":
        summary["cases"][0]["case_id"] = summary["cases"][1]["case_id"]
    elif mutation == "variant":
        initial, reference = summary["cases"][0]["results"][:2]
        for key in ("candidate", "receipt", "receipt_sha256", "candidate_snapshot"):
            initial[key] = reference[key]
    elif mutation == "visible":
        summary["cases"][0]["visible"][0]["exit_code"] = 0
    else:
        summary["binding"]["executor_digest"] = "0" * 64
    summary["qualification_id"] = digest_json(
        {k: v for k, v in summary.items() if k != "qualification_id"}
    )
    path.write_text(json.dumps(summary))
    try:
        with pytest.raises(ValueError, match="qualification"):
            require_qualification(output)
    finally:
        path.write_bytes(original)


def test_changed_candidate_invalidates_qualification(qualification):
    output, summary = qualification
    relative = summary["cases"][0]["results"][0]["candidate"]
    candidate = output / relative / "caller.py"
    original = candidate.read_bytes()
    candidate.write_bytes(original + b"\n# changed after self-test\n")
    try:
        with pytest.raises(ValueError, match="qualification"):
            require_qualification(output)
    finally:
        candidate.write_bytes(original)
