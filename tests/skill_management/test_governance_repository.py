"""Opt-in Neo4j decisions preserve original facts and reject stale authority."""

import copy

import pytest

from management_fixtures import report
from test_adjudication_contract import facts
from test_management_repository import register
from test_management_repository import repositories as shared_repositories

from memos.skill_management.domain import ManagementError


@pytest.fixture(name="repositories")
def governance_repositories():
    yield from shared_repositories.__wrapped__()


def setup_review(repo):
    c, _, _ = register(repo)
    value = report(c, status="unknown")
    # All fixtures remain management_test: no actual human publication is manufactured.
    validation = repo.record_validation(
        c["skill_id"],
        c["revision"],
        report=value,
        actor="synthetic-evaluator",
        operation_id="validate",
    )
    validation.pop("sequence")
    _, _, assessment, decision = facts()
    for key in (
        "validationId",
        "contentHash",
        "exportHash",
        "runId",
        "attemptId",
        "caseId",
        "caseRevision",
        "lineageId",
        "split",
        "purpose",
    ):
        assessment[key] = validation[key]
    registered = repo.record_assessment(
        c["skill_id"],
        c["revision"],
        assessment=assessment,
        actor="host-test-verifier",
        operation_id="assess",
    )
    package = repo.prepare_adjudication(
        c["skill_id"],
        c["revision"],
        assessment_id=registered["assessmentId"],
        source_watermark=["b" * 64],
        actor="synthetic-owner",
    )
    return c, validation, registered, decision, package


def decide(repo, c, decision, package, **overrides):
    kwargs = {
        "package_id": package["packageId"],
        "package_hash": package["packageHash"],
        "expected_sequence": package["sequence"],
        "operation_id": "judge",
        "source_watermark": ["b" * 64],
        "actor": "synthetic-owner",
        "reason": "Synthetic contract test, not a real human judgment",
        "decision": decision,
        "supersedes": None,
    }
    kwargs.update(overrides)
    return repo.adjudicate(c["skill_id"], c["revision"], **kwargs)


def test_decision_append_preserves_unknown_and_contributes_only_proven_branch(repositories):
    repo = repositories()
    c, validation, assessment, decision, package = setup_review(repo)
    before = copy.deepcopy(repo.get_revision(c["skill_id"], c["revision"])["validations"])
    result = decide(repo, c, decision, package)
    assert decide(repo, c, decision, package) == result
    after = repo.get_revision(c["skill_id"], c["revision"])
    assert after["validations"] == before and after["validations"][0]["status"] == "unknown"
    assert after["governance"]["coverage"][0]["status"] == "covered"
    assert sum(row["status"] == "covered" for row in after["governance"]["coverage"]) == 1
    assert "validation_unknown" not in after["management"]["readiness"]["reasons"]
    assert after["management"]["readiness"]["status"] == "NEEDS_EVIDENCE"
    assert len(after["governance"]["adjudications"]) == 1
    with pytest.raises(ManagementError, match="operation_conflict"):
        decide(repo, c, {**decision, "reason": "changed"}, package)


def test_new_assessment_invalidates_package_without_rewriting_old_review(repositories):
    repo = repositories()
    c, _, assessment, decision, package = setup_review(repo)
    update = {k: v for k, v in assessment.items() if k not in {"assessmentId", "sequence"}}
    update["reportSha256"] = "9" * 64
    repo.record_assessment(
        c["skill_id"],
        c["revision"],
        assessment=update,
        actor="host-test-verifier",
        operation_id="assess-new",
    )
    with pytest.raises(ManagementError, match="state_conflict"):
        decide(repo, c, decision, package)
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert len(detail["governance"]["assessments"]) == 2
    assert detail["governance"]["adjudications"] == []


def test_correction_explicitly_supersedes_decision_and_withdraws_coverage(repositories):
    repo = repositories()
    c, _, assessment, decision, package = setup_review(repo)
    first = decide(repo, c, decision, package)
    next_package = repo.prepare_adjudication(
        c["skill_id"],
        c["revision"],
        assessment_id=assessment["assessmentId"],
        source_watermark=["b" * 64],
        actor="synthetic-owner",
    )
    correction = {
        **decision,
        "taskDecision": "needs_evidence",
        "historyDecision": "retain",
        "usageDecision": "unknown",
        "branches": [],
    }
    with pytest.raises(ManagementError, match="adjudication_conflict"):
        decide(repo, c, correction, next_package, operation_id="correct")
    decide(
        repo,
        c,
        correction,
        next_package,
        operation_id="correct",
        supersedes=first["adjudicationId"],
    )
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert len(detail["governance"]["adjudications"]) == 2
    assert all(row["status"] == "missing" for row in detail["governance"]["coverage"])
    assert "validation_unknown" in detail["management"]["readiness"]["reasons"]


def test_actor_project_and_package_bindings_cannot_be_reused(repositories):
    repo = repositories()
    c, _, _, decision, package = setup_review(repo)
    with pytest.raises(ManagementError, match="review_mismatch"):
        decide(repo, c, decision, package, actor="another-owner")
    with pytest.raises(ManagementError, match="not_found"):
        decide(repositories("-other"), c, decision, package)
    with pytest.raises(ManagementError, match="source_changed"):
        decide(repo, c, decision, package, source_watermark=["c" * 64])


def test_new_evidence_revokes_qualification_without_erasing_counterexample(repositories):
    repo = repositories()
    c, _, assessment, decision, package = setup_review(repo)
    decision.update(taskDecision="failed", historyDecision="retain", branches=[])
    decide(repo, c, decision, package)
    update = {k: v for k, v in assessment.items() if k not in {"assessmentId", "sequence"}}
    update["reportSha256"] = "9" * 64
    repo.record_assessment(
        c["skill_id"],
        c["revision"],
        assessment=update,
        actor="host-test-verifier",
        operation_id="assess-new",
    )
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert detail["management"]["readiness"]["status"] == "BLOCK"
    assert "counterexample_unresolved" in detail["management"]["readiness"]["reasons"]
