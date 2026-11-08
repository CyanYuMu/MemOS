"""Release readiness never promotes structural or isolated-dev results."""

import copy

import pytest

from management_fixtures import candidate, report, state

from memos.skill_management.domain import ManagementError, readiness, validate_report


def test_dev_pass_and_agent_declarations_cannot_enable_publication():
    c = candidate()
    result = readiness(state(c), [report(c, kind="isolated_dev")], [])
    assert result["status"] == "NEEDS_EVIDENCE"
    assert "independent_validation_missing" in result["reasons"]


def test_exact_independent_coverage_can_be_reviewed_without_auto_publishing():
    c = candidate()
    result = readiness(state(c), [report(c)], [])
    assert result["status"] == "PASS"
    assert state(c)["state"] == "candidate"


@pytest.mark.parametrize("field", ["branchCoverage", "boundaryCoverage"])
def test_missing_required_coverage_stays_unknown(field):
    c = candidate()
    r = report(c)
    r[field].pop()
    assert readiness(state(c), [r], [])["status"] == "NEEDS_EVIDENCE"


def test_failures_cannot_be_averaged_away_by_passes():
    c = candidate()
    result = readiness(state(c), [report(c), report(c, status="failed", suffix="failure")], [])
    assert result["status"] != "PASS"
    assert "validation_failure_unresolved" in result["reasons"]


def test_pending_job_blocks_new_publish_even_with_good_validation():
    c = candidate()
    result = readiness(state(c), [report(c)], [{"status": "running"}])
    assert "evaluation_pending" in result["reasons"]


def test_unknown_dependencies_and_open_challenge_block_readiness():
    c = candidate()
    c["requires"] = [{"skill_id": "unknown", "revision": "1"}]
    s = state(c)
    s["challenges"] = [{"status": "open", "severity": "block"}]
    result = readiness(s, [report(c)], [])
    assert result["status"] == "BLOCK"
    assert {"open_challenges", "unsupported_dependencies"} <= set(result["reasons"])


@pytest.mark.parametrize(
    "change",
    [
        {"contentHash": "f" * 64},
        {"branchCoverage": ["not-a-rule#0"]},
        {"receiptVerified": True, "receipt": None},
        {"extra": "untrusted"},
        {"kind": []},
        {"taskResult": {"status": "passed", "accepted": True, "receiptMatches": "yes"}},
        {"skillUsage": {"loading": "verified", "capture": "complete"}},
    ],
)
def test_report_rejects_foreign_bindings_and_unsupported_fields(change):
    c = candidate()
    r = report(c)
    r.update(change)
    with pytest.raises(ManagementError):
        validate_report(c, r)


def test_report_is_content_bound_and_does_not_mutate_input():
    c = candidate()
    r = report(c)
    before = copy.deepcopy(r)
    result = validate_report(c, r)
    assert r == before
    assert len(result["validationId"]) == 64
    assert result["skillId"] == c["skill_id"]
    assert result["revision"] == c["revision"]


@pytest.mark.parametrize(
    "source_field,report_field", [("case_id", "caseId"), ("lineage_id", "lineageId")]
)
def test_independent_evaluation_cannot_reuse_a_source_task_or_lineage(source_field, report_field):
    c = candidate()
    r = report(c)
    c["source_refs"][0][source_field] = r[report_field]
    with pytest.raises(ManagementError, match="source_task_overlap"):
        validate_report(c, r)
