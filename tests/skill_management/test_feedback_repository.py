# ruff: noqa: F811
"""Isolated Neo4j transactions preserve original terminal facts."""

import copy

import pytest

from management_fixtures import report
from test_management_repository import register, repositories  # noqa: F401

from memos.skill_management.domain import ManagementError


def setup(repo):
    c, _, _ = register(repo)
    job = repo.create_job(
        c["skill_id"],
        c["revision"],
        case_id="dev-synthetic-one",
        actor="runner",
        operation_id="job",
        export_hash="d" * 64,
    )
    repo.finish_job(job["jobId"], status="unknown", error_code="execution_unverified")
    validation = repo.record_validation(
        c["skill_id"],
        c["revision"],
        report=report(c, kind="isolated_dev"),
        actor="runner",
        operation_id="validation-" + job["jobId"],
    )
    return c, repo.get_job(job["jobId"]), validation


def test_recovery_is_append_only_idempotent_and_not_publish_evidence(repositories):
    repo = repositories()
    c, original, v = setup(repo)
    args = {
        "validation_id": v["validationId"],
        "evidence_digest": "9" * 64,
        "actor": "owner",
        "operation_id": "recover",
    }
    recovered = repo.record_feedback_recovery(original["jobId"], **args)
    assert recovered["status"] == "unknown" and recovered["errorCode"] == "execution_unverified"
    assert recovered["feedbackRecovery"]["status"] == "passed"
    assert repo.record_feedback_recovery(original["jobId"], **args) == recovered
    assert repo.get_job(original["jobId"]) == recovered
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert detail["jobs"] == [recovered] and detail["management"]["readiness"]["status"] != "PASS"
    assert repo._run(lambda records, nodes, edges: original == records[("job", original["jobId"])])[
        0
    ]
    with pytest.raises(ManagementError, match="feedback_recovery_conflict"):
        repo.record_feedback_recovery(
            original["jobId"], **{**args, "operation_id": "other", "evidence_digest": "8" * 64}
        )


def test_graph_completion_must_match_original_ticket_and_artifacts(repositories):
    repo = repositories()
    _, job, _ = setup(repo)
    ticket = {
        k: job[k] for k in ("skillId", "revision", "caseId", "mode", "contentHash", "exportHash")
    }
    ticket.update(ticketId="7" * 64, group="skill", runId="8" * 64)
    completion = {"ticketId": ticket["ticketId"], "status": "completed", "reportSha256": "9" * 64}
    proof = {"ticket": ticket, "completion": completion, "artifacts": {"reportSha256": "9" * 64}}
    with pytest.raises(ManagementError):
        repo.verify_feedback_execution(job["jobId"], proof)

    def add(records, nodes, edges):
        repo._put(records, "execution_ticket", ticket["ticketId"], ticket)
        repo._put(records, "execution_finish", ticket["ticketId"], completion)

    repo._run(add, mutation=True)
    assert repo.verify_feedback_execution(job["jobId"], proof)
    tampered = copy.deepcopy(proof)
    tampered["completion"]["reportSha256"] = "0" * 64
    with pytest.raises(ManagementError, match="feedback_execution_mismatch"):
        repo.verify_feedback_execution(job["jobId"], tampered)
