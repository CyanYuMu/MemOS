"""Opt-in graph transactions dispatch schema-three intermediate evidence without bypassing review."""

import copy

import pytest
import test_management_repository as shared

from test_extension_adjudication import extension_facts

from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import ManagementError


@pytest.fixture(name="repositories")
def branch_repositories():
    yield from shared.repositories.__wrapped__()


def test_extension_review_is_append_only_idempotent_and_still_cannot_publish(
    repositories, monkeypatch
):
    repo = repositories()
    candidate, validation, assessment, decision = extension_facts()
    candidate["project_id"] = repo.project_id
    candidate["content_hash"] = hash_json(
        {k: v for k, v in candidate.items() if k != "content_hash"}
    )
    manifest = {
        "schema_version": 1,
        "skill_id": candidate["skill_id"],
        "revision": candidate["revision"],
        "project_id": repo.project_id,
        "candidate_content_hash": candidate["content_hash"],
        "manifest_id": "c" * 64,
    }
    monkeypatch.setattr(shared, "candidate_for", lambda project: (candidate, manifest))
    shared.register(repo)
    report = {
        k: v for k, v in validation.items() if k not in {"skillId", "revision", "validationId"}
    }
    report.update(contentHash=candidate["content_hash"], purpose="management_test")
    validation = repo.record_validation(
        candidate["skill_id"],
        candidate["revision"],
        report=report,
        actor="synthetic-branch-run",
        operation_id="branch-validate",
    )
    validation.pop("sequence")
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
        candidate["skill_id"],
        candidate["revision"],
        assessment=assessment,
        actor="synthetic-branch-collector",
        operation_id="branch-assess",
    )
    assert registered == repo.record_assessment(
        candidate["skill_id"],
        candidate["revision"],
        assessment=assessment,
        actor="synthetic-branch-collector",
        operation_id="branch-assess",
    )
    before = copy.deepcopy(
        repo.get_revision(candidate["skill_id"], candidate["revision"])["validations"]
    )
    package = repo.prepare_adjudication(
        candidate["skill_id"],
        candidate["revision"],
        assessment_id=registered["assessmentId"],
        source_watermark=["b" * 64],
        actor="synthetic-human",
    )
    kwargs = {
        "package_id": package["packageId"],
        "package_hash": package["packageHash"],
        "expected_sequence": package["sequence"],
        "operation_id": "branch-adjudicate",
        "actor": "synthetic-human",
        "reason": "Synthetic transaction only",
        "source_watermark": ["b" * 64],
        "decision": decision,
    }
    invalid = copy.deepcopy(kwargs)
    invalid["decision"]["branches"][2]["actionRefs"] = ["verify_scoped_reads_decl", "exit"]
    invalid["operation_id"] = "extension-invalid-transition"
    with pytest.raises(ManagementError, match="extension_branch_process_insufficient"):
        repo.adjudicate(candidate["skill_id"], candidate["revision"], **invalid)
    unchanged = repo.get_revision(candidate["skill_id"], candidate["revision"])
    assert unchanged["governance"]["adjudications"] == []
    assert unchanged["management"]["sequence"] == package["sequence"]
    result = repo.adjudicate(candidate["skill_id"], candidate["revision"], **kwargs)
    assert result == repo.adjudicate(candidate["skill_id"], candidate["revision"], **kwargs)
    detail = repo.get_revision(candidate["skill_id"], candidate["revision"])
    assert detail["validations"] == before
    assert len(detail["governance"]["adjudications"]) == 1
    assert sum(row["status"] == "covered" for row in detail["governance"]["coverage"]) == 5
    assert "branch_coverage_missing" in detail["management"]["readiness"]["reasons"]
    assert "boundary_coverage_missing" in detail["management"]["readiness"]["reasons"]
    release = repo.prepare_review(
        candidate["skill_id"],
        candidate["revision"],
        source_watermark=["b" * 64],
        actor="synthetic-human",
    )
    with pytest.raises(ManagementError, match="not_ready"):
        shared.publish(repo, candidate, release)
    assert repo.get_revision(candidate["skill_id"], candidate["revision"])["releases"] == []
