"""Opt-in transactional tests in isolated projects of the independent Skill graph."""

import copy
import json
import os
import threading
import uuid

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from management_fixtures import candidate_for, report

from memos.skill_drafting.graph import edge_key, node_key
from memos.skill_management.domain import ManagementError
from memos.skill_management.repository import ManagementRepository


@pytest.fixture
def repositories():
    path = os.environ.get("MEMOS_SKILL_GRAPH_TEST_CONFIG")
    if not path:
        pytest.skip("dedicated Skill Graph configuration was not supplied")
    config = json.loads(Path(path).read_text())
    project = "p3-test-" + uuid.uuid4().hex
    opened = []

    def make(suffix=""):
        r = ManagementRepository(
            **{k: config[k] for k in ("uri", "user", "password", "database", "memory_database_id")},
            project_id=project + suffix,
        )
        opened.append(r)
        return r

    yield make
    for r in opened:
        r.close()


def register(repo, *, challenges=()):
    c, m = candidate_for(repo.project_id)
    skill = {"kind": "Skill", "key": c["skill_id"], "payload": {"skill_id": c["skill_id"]}}
    payload = {
        "skill_id": c["skill_id"],
        "revision": c["revision"],
        "status": "candidate",
        "requires": [],
        "candidate": c,
    }
    revision = {
        "kind": "SkillRevision",
        "key": node_key("SkillRevision", payload),
        "payload": payload,
    }
    left, right = ({k: item[k] for k in ("kind", "key")} for item in (skill, revision))
    edge = {
        "kind": "HAS_REVISION",
        "key": edge_key("HAS_REVISION", left, right, {}),
        "source": left,
        "target": right,
        "payload": {},
    }
    repo.store_bundle([skill, revision], [edge])
    result = repo.register_candidate(c, m, "/synthetic/candidate", challenges=list(challenges))
    return c, m, result


def qualified(repo):
    c, m, _ = register(repo)
    value = repo.record_validation(
        c["skill_id"],
        c["revision"],
        report=report(c),
        actor="synthetic-evaluator",
        operation_id="validation-1",
    )
    package = repo.prepare_review(
        c["skill_id"], c["revision"], source_watermark=["b" * 64], actor="synthetic-owner"
    )
    assert package["readiness"]["status"] == "PASS"
    return c, m, value, package


def publish(repo, c, package, *, operation_id="publication-1"):
    return repo.publish(
        c["skill_id"],
        c["revision"],
        package_id=package["packageId"],
        package_hash=package["packageHash"],
        expected_sequence=package["sequence"],
        operation_id=operation_id,
        actor=package["actor"],
        reason="Synthetic management transaction test, not a real human publication",
        source_watermark=["b" * 64],
    )


def test_registration_is_immutable_and_scoped(repositories):
    repo = repositories()
    c, m, first = register(repo, challenges=["Needs independent evidence"])
    assert first["candidate"] == c
    assert (
        repo.register_candidate(
            c, m, "/synthetic/candidate", challenges=["Needs independent evidence"]
        )
        == first
    )
    assert repo.list_candidates()["total"] == 1
    assert repositories("-other").list_candidates()["total"] == 0
    with pytest.raises(ManagementError, match="immutable_conflict"):
        repo.register_candidate(c, m, "/different/artifact")


def test_dev_success_never_publishes_and_candidate_stays_unchanged(repositories):
    repo = repositories()
    c, _, _ = register(repo)
    repo.record_validation(
        c["skill_id"],
        c["revision"],
        report=report(c, kind="isolated_dev"),
        actor="runner",
        operation_id="validation-dev",
    )
    package = repo.prepare_review(
        c["skill_id"], c["revision"], source_watermark=["b" * 64], actor="owner"
    )
    with pytest.raises(ManagementError, match="not_ready"):
        publish(repo, c, package)
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert detail["candidate"] == c and detail["releases"] == []
    assert repo.admission(c["skill_id"], c["revision"], "dev-synthetic-one")["allowed"] is False


def test_publish_is_atomic_idempotent_and_bound_to_exact_package(repositories):
    repo = repositories()
    c, _, _, package = qualified(repo)
    result = publish(repo, c, package)
    assert result == publish(repo, c, package)
    with pytest.raises(ManagementError, match="operation_conflict"):
        publish(repo, c, {**package, "packageHash": "f" * 64})
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert len(detail["releases"]) == 1 and detail["management"]["state"] == "published"
    assert detail["candidate"] == c
    assert (
        repo.admission(c["skill_id"], c["revision"], "dev-synthetic-one")["reason"]
        == "management_test_only"
    )
    admitted = repo.admission(
        c["skill_id"], c["revision"], "dev-synthetic-one", allow_management_test=True
    )
    assert admitted["allowed"] is True
    assert (
        repo.admission(
            c["skill_id"], c["revision"], "unvalidated-task", allow_management_test=True
        )["allowed"]
        is False
    )


def test_new_challenge_invalidates_old_review_and_cannot_partially_publish(repositories):
    repo = repositories()
    c, _, _, package = qualified(repo)
    repo.challenge(
        c["skill_id"],
        c["revision"],
        expected_sequence=package["sequence"],
        operation_id="challenge-1",
        actor="owner",
        reason="New contradictory evidence",
    )
    with pytest.raises(ManagementError, match="state_conflict"):
        publish(repo, c, package)
    assert repo.get_revision(c["skill_id"], c["revision"])["releases"] == []


def test_concurrent_challenge_and_publish_share_one_sequence_lock(repositories):
    repo, second = repositories(), repositories()
    c, _, _, package = qualified(repo)
    barrier = threading.Barrier(2)

    def action(r, challenge):
        barrier.wait(timeout=10)
        try:
            if challenge:
                r.challenge(
                    c["skill_id"],
                    c["revision"],
                    expected_sequence=package["sequence"],
                    operation_id="challenge-race",
                    actor="owner",
                    reason="Concurrent new evidence",
                )
            else:
                publish(r, c, package)
            return "committed"
        except ManagementError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(action, repo, False)
        b = pool.submit(action, second, True)
        outcomes = [a.result(timeout=60), b.result(timeout=60)]
    assert sorted(outcomes) == ["committed", "state_conflict"]


def test_jobs_are_claimed_once_and_restart_marks_incomplete_unknown(repositories):
    repo, second = repositories(), repositories()
    c, _, _ = register(repo)
    job = repo.create_job(
        c["skill_id"],
        c["revision"],
        case_id="dev-synthetic-one",
        actor="editor",
        operation_id="evaluation-1",
        export_hash="d" * 64,
    )
    again = repo.create_job(
        c["skill_id"],
        c["revision"],
        case_id="dev-synthetic-one",
        actor="editor",
        operation_id="evaluation-1",
        export_hash="d" * 64,
    )
    assert job["created"] is True and again["created"] is False
    assert repo.claim_job(job["jobId"])["status"] == "running"
    assert second.claim_job(job["jobId"]) is None
    assert second.recover_jobs() == 1
    final = repo.get_job(job["jobId"])
    assert final["status"] == "unknown" and final["errorCode"] == "service_restarted"
    with pytest.raises(ManagementError, match="job_terminal"):
        repo.finish_job(job["jobId"], status="passed")


def test_pause_revokes_admission_without_deleting_release(repositories):
    repo = repositories()
    c, _, _, package = qualified(repo)
    result = publish(repo, c, package)
    repo.set_status(
        c["skill_id"],
        c["revision"],
        action="pause",
        expected_sequence=result["sequence"],
        operation_id="pause-1",
        actor="owner",
        reason="Needs renewed review",
    )
    assert not repo.admission(
        c["skill_id"], c["revision"], "dev-synthetic-one", allow_management_test=True
    )["allowed"]
    assert len(repo.get_revision(c["skill_id"], c["revision"])["releases"]) == 1


def test_raw_graph_path_cannot_insert_authorized_release(repositories):
    repo = repositories()
    node = {
        "kind": "Release",
        "key": "fake",
        "payload": {
            "release_id": "fake",
            "authorized": True,
            "purpose": "management_test",
            "management_schema": 1,
        },
    }
    with pytest.raises(ManagementError, match="authority_required"):
        repo.store_bundle([node], [])


def test_source_watermark_change_cannot_reuse_review(repositories):
    repo = repositories()
    c, _, _, package = qualified(repo)
    with pytest.raises(ManagementError, match="source_changed"):
        repo.publish(
            c["skill_id"],
            c["revision"],
            package_id=package["packageId"],
            package_hash=package["packageHash"],
            expected_sequence=package["sequence"],
            operation_id="wrong-source",
            actor="owner",
            reason="Still requires original evidence",
            source_watermark=["c" * 64],
        )
    assert not repo.get_revision(c["skill_id"], c["revision"])["releases"]


def test_validation_identity_cannot_be_overwritten(repositories):
    repo = repositories()
    c, _, _ = register(repo)
    original = report(c)
    first = repo.record_validation(
        c["skill_id"], c["revision"], report=original, actor="runner", operation_id="validation-1"
    )
    assert first == repo.record_validation(
        c["skill_id"], c["revision"], report=original, actor="runner", operation_id="validation-1"
    )
    changed = copy.deepcopy(original)
    changed["status"] = "failed"
    with pytest.raises(ManagementError, match="operation_conflict"):
        repo.record_validation(
            c["skill_id"],
            c["revision"],
            report=changed,
            actor="runner",
            operation_id="validation-1",
        )


def test_queued_job_can_record_dispatch_failure_without_faking_execution(repositories):
    repo = repositories()
    c, _, _ = register(repo)
    job = repo.create_job(
        c["skill_id"],
        c["revision"],
        case_id="dev-synthetic-one",
        actor="editor",
        operation_id="dispatch-1",
        export_hash="d" * 64,
    )
    final = repo.finish_job(job["jobId"], status="unknown", error_code="dispatch_failed")
    assert final["startedAt"] is None and final["finishedAt"]
    assert final["status"] == "unknown" and final["validationId"] is None
    assert repo.claim_job(job["jobId"]) is None


def test_completed_job_must_match_its_immutable_host_validation(repositories):
    repo = repositories()
    c, _, _ = register(repo)
    job = repo.create_job(
        c["skill_id"],
        c["revision"],
        case_id="dev-synthetic-one",
        actor="editor",
        operation_id="evaluation-bind",
        export_hash="d" * 64,
    )
    repo.claim_job(job["jobId"])
    r = report(c, kind="isolated_dev")
    validation = repo.record_validation(
        c["skill_id"], c["revision"], report=r, actor="host", operation_id="validation-bind"
    )
    with pytest.raises(ManagementError, match="validation_mismatch"):
        repo.finish_job(
            job["jobId"],
            status="failed",
            validation_id=validation["validationId"],
            task_result=r["taskResult"],
            skill_usage=r["skillUsage"],
        )
    with pytest.raises(ManagementError, match="validation_mismatch"):
        repo.finish_job(
            job["jobId"],
            status="passed",
            validation_id=validation["validationId"],
            task_result={**r["taskResult"], "accepted": False},
            skill_usage=r["skillUsage"],
        )
    final = repo.finish_job(
        job["jobId"],
        status="passed",
        validation_id=validation["validationId"],
        task_result=r["taskResult"],
        skill_usage=r["skillUsage"],
    )
    assert final["status"] == "passed"


def test_pending_job_does_not_revoke_existing_admission_and_cross_project_job_is_hidden(
    repositories,
):
    repo = repositories()
    c, _, _, package = qualified(repo)
    publish(repo, c, package)
    job = repo.create_job(
        c["skill_id"],
        c["revision"],
        case_id="dev-synthetic-one",
        actor="editor",
        operation_id="evaluation-after-publish",
        export_hash="d" * 64,
    )
    assert repo.admission(
        c["skill_id"], c["revision"], "dev-synthetic-one", allow_management_test=True
    )["allowed"]
    with pytest.raises(ManagementError, match="not_found"):
        repositories("-other").get_job(job["jobId"])


def test_persisted_job_lookup_replays_completed_request_and_rejects_operation_reuse(repositories):
    repo = repositories()
    c, _, _ = register(repo)
    request = {
        "case_id": "dev-synthetic-one",
        "actor": "editor",
        "operation_id": "lookup-one",
        "export_hash": "d" * 64,
        "mode": "candidate_evaluation",
    }
    assert repo.find_job(c["skill_id"], c["revision"], **request) is None
    created = repo.create_job(c["skill_id"], c["revision"], **request)
    repo.finish_job(created["jobId"], status="unknown", error_code="dispatch_failed")
    # A different running job must not hide a completed idempotent request.
    active = repo.create_job(
        c["skill_id"], c["revision"], **{**request, "operation_id": "lookup-two"}
    )
    repo.claim_job(active["jobId"])
    found = repositories().find_job(c["skill_id"], c["revision"], **request)
    assert found["jobId"] == created["jobId"] and found["status"] == "unknown"
    with pytest.raises(ManagementError, match="operation_conflict"):
        repo.find_job(c["skill_id"], c["revision"], **{**request, "actor": "another-editor"})
