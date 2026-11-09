"""Server-issued native run identities prove execution order, not import time."""

import uuid

import pytest

from test_repository import bind, qualify, register, review, scan

from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import ManagementError


def begin(repo, c, *, batch=None, unit="repair", mode="independent_validation"):
    return repo.begin_execution(
        c["skill_id"],
        c["revision"],
        batch_id=batch or hash_json(uuid.uuid4().hex),
        unit_id=unit,
        case_id="dev-synthetic-fresh",
        group="skill",
        mode=mode,
        content_hash=c["content_hash"],
        export_hash="d" * 64,
        actor="synthetic-host",
        operation_id=uuid.uuid4().hex,
    )


def finish(repo, ticket):
    return repo.finish_execution(
        ticket["ticketId"],
        report_sha256="1" * 64,
        events_sha256="2" * 64,
        initial_receipt_sha256="3" * 64,
        receipt_sha256="4" * 64,
        actor="synthetic-host",
        operation_id=uuid.uuid4().hex,
    )


def fixed_export(repo, c):
    detail = repo.get_revision(c["skill_id"], c["revision"])
    repo.bind_export(
        c["skill_id"],
        c["revision"],
        export_hash="d" * 64,
        projection_version="synthetic",
        expected_sequence=detail["management"]["sequence"],
        operation_id="test-export",
        actor="test",
        reason="Synthetic execution test export",
    )


def attach_assessment(repo, c, validation, ticket, *, mismatched=False):
    def seed(records, nodes, edges):
        state = repo._state(records, c["skill_id"], c["revision"])
        assessment = {
            "schemaVersion": 6,
            "skillId": c["skill_id"],
            "revision": c["revision"],
            "validationId": validation["validationId"],
            "batchId": ticket["batchId"],
            "executionQualification": {"trial": {"unit": ticket["unitId"]}},
            "runId": ticket["runId"],
            "attemptId": ticket["attemptId"],
            "caseId": ticket["caseId"],
            "contentHash": c["content_hash"],
            "exportHash": "d" * 64,
            "reportSha256": "9" * 64 if mismatched else "1" * 64,
            "eventsSha256": "2" * 64,
            "initialVerification": {"receiptSha256": "3" * 64},
            "verification": {"receiptSha256": "4" * 64},
        }
        assessment_id = hash_json(assessment)
        repo._put(records, "assessment", assessment_id, assessment)
        state.setdefault("currentAssessments", {})[validation["validationId"]] = assessment_id

    repo._run(seed, mutation=True)


def qualified_ticket(repo, c, ticket, *, mismatched=False):
    # This isolated graph fixture substitutes only the trusted qualification
    # output. No real Claude execution or efficacy is asserted by this unit test.
    validation = qualify(
        repo,
        c,
        uuid.uuid4().hex[:8],
        actual=True,
        run_id=ticket["runId"],
        attempt_id=ticket["attemptId"],
        case_id=ticket["caseId"],
        receipt_sha="4" * 64,
    )
    attach_assessment(repo, c, validation, ticket, mismatched=mismatched)
    return validation


def test_ticket_identity_single_use_and_management_sequence_stability(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    fixed_export(repo, c)
    before = repo.get_revision(c["skill_id"], c["revision"])["management"]["sequence"]
    ticket = begin(repo, c)
    assert len(ticket["runId"]) == 64 and len(ticket["attemptId"]) == 32
    assert repo.get_revision(c["skill_id"], c["revision"])["management"]["sequence"] == before
    completed = finish(repo, ticket)
    assert (
        completed["status"] == "completed"
        and completed["finishedSequence"] > ticket["startedSequence"]
    )
    assert repo.get_revision(c["skill_id"], c["revision"])["management"]["sequence"] == before
    with pytest.raises(ManagementError, match="execution_unit_already_started"):
        begin(repo, c, batch=ticket["batchId"], unit=ticket["unitId"])


def test_normal_ticket_requires_published_admission(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    fixed_export(repo, c)
    with pytest.raises(ManagementError, match="release_unavailable"):
        begin(repo, c, mode="normal")


def test_native_completed_ticket_after_recovery_allows_restore(repo):
    c = register(repo)
    bind(repo, c)
    fixed_export(repo, c)
    bad = scan(repo, c, ("changed", "healthy"))
    scan(repo, c)
    ticket = begin(repo, c)
    finish(repo, ticket)
    validation = qualified_ticket(repo, c, ticket)
    result = review(
        repo,
        c,
        "restore",
        root_ids=[bad["lifecycle"]["roots"][0]["rootId"]],
        validation_ids=[validation["validationId"]],
    )
    assert result["lifecycle"]["roots"] == []


@pytest.mark.parametrize("failure", ["before_root", "unfinished", "digest", "drift"])
def test_restoration_rejects_stale_or_unfinished_or_mismatched_ticket(repo, failure):
    c = register(repo)
    bind(repo, c)
    fixed_export(repo, c)
    scan(repo, c)
    if failure == "before_root":
        ticket = begin(repo, c)
        finish(repo, ticket)
    bad = scan(repo, c, ("changed", "healthy"))
    scan(repo, c)
    if failure != "before_root":
        ticket = begin(repo, c)
        if failure == "drift":
            scan(repo, c, ("healthy", "invalid"))
        if failure != "unfinished":
            finish(repo, ticket)
        if failure == "drift":
            scan(repo, c)
    validation = qualified_ticket(repo, c, ticket, mismatched=failure == "digest")
    with pytest.raises(ManagementError, match="restoration_execution_"):
        review(
            repo,
            c,
            "restore",
            root_ids=[bad["lifecycle"]["roots"][0]["rootId"]],
            validation_ids=[validation["validationId"]],
        )


def test_published_normal_ticket_preserves_both_admissions(repo):
    from test_repository import publish

    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    qualify(repo, c, "fresh", actual=True)
    publish(repo, c)
    before = repo.admission(c["skill_id"], c["revision"], "dev-synthetic-fresh")
    assert before["allowed"]
    ticket = begin(repo, c, mode="normal")
    completed = finish(repo, ticket)
    assert completed["status"] == "completed"
    assert repo.admission(c["skill_id"], c["revision"], "dev-synthetic-fresh") == before


def test_execution_guard_scans_before_ticket_and_completion(repo):
    from memos.skill_lifecycle.execution import make_execution_guard

    c = register(repo)
    bind(repo, c)
    fixed_export(repo, c)
    calls = []

    def observe(subjects):
        calls.append(len(subjects))
        return [
            {
                "subjectId": s["subjectId"],
                "status": "healthy",
                "observedDigest": s["expectedDigest"],
                "reason": "synthetic_probe",
            }
            for s in subjects
        ]

    guard = make_execution_guard(repo, observe, c, actor="delegated-synthetic-host")
    ticket = guard.begin(
        {
            "batchId": "a" * 64,
            "unitId": "fixed-unit",
            "caseId": "dev-synthetic-fresh",
            "group": "skill",
            "mode": "independent_validation",
            "contentHash": c["content_hash"],
            "exportHash": "d" * 64,
        }
    )
    result = guard.finish(
        ticket,
        {
            "reportSha256": "1" * 64,
            "eventsSha256": "2" * 64,
            "initialReceiptSha256": "3" * 64,
            "receiptSha256": "4" * 64,
        },
    )
    assert calls == [2, 2] and result["status"] == "completed"


def test_finish_cannot_be_replaced_or_rebound_to_other_artifacts(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    fixed_export(repo, c)
    ticket = begin(repo, c)
    args = {
        "report_sha256": "1" * 64,
        "events_sha256": "2" * 64,
        "initial_receipt_sha256": "3" * 64,
        "receipt_sha256": "4" * 64,
        "actor": "synthetic-host",
        "operation_id": "completion-op",
    }
    completed = repo.finish_execution(ticket["ticketId"], **args)
    assert repo.finish_execution(ticket["ticketId"], **args) == completed
    with pytest.raises(ManagementError, match="operation_conflict"):
        repo.finish_execution(ticket["ticketId"], **{**args, "report_sha256": "5" * 64})
    with pytest.raises(ManagementError, match="execution_already_finished"):
        repo.finish_execution(ticket["ticketId"], **{**args, "operation_id": "new-completion"})


def test_bad_then_restored_during_execution_invalidates_ticket(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    fixed_export(repo, c)
    ticket = begin(repo, c)
    scan(repo, c, ("changed", "healthy"))
    scan(repo, c)
    assert finish(repo, ticket)["status"] == "invalid"


def test_dependency_pause_during_normal_execution_invalidates_completion(repo):
    from test_repository import publish

    a = register(repo)
    bind(repo, a)
    scan(repo, a)
    qualify(repo, a, "fresh", actual=True)
    publish(repo, a)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    bind(repo, b)
    scan(repo, b)
    qualify(repo, b, "fresh", actual=True)
    publish(repo, b)
    ticket = begin(repo, b, mode="normal")
    current = repo.get_revision(a["skill_id"], a["revision"])
    repo.set_status(
        a["skill_id"],
        a["revision"],
        action="pause",
        expected_sequence=current["management"]["sequence"],
        operation_id="dependency-pause",
        actor="test",
        reason="Dependency unavailable during native execution",
    )
    assert finish(repo, ticket)["status"] == "invalid"
    assert not repo.admission(b["skill_id"], b["revision"], "dev-synthetic-fresh")["allowed"]


def test_candidate_evaluation_ticket_does_not_require_publication(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    fixed_export(repo, c)
    ticket = begin(repo, c, mode="candidate_evaluation")
    assert ticket["mode"] == "candidate_evaluation"
    assert finish(repo, ticket)["status"] == "completed"
