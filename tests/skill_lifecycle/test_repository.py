"""Lifecycle transactions use isolated real Neo4j projects, never product graphs."""

import importlib.util
import uuid

from pathlib import Path

import pytest

from memos.skill_drafting.domain import hash_json
from memos.skill_drafting.graph import edge_key
from memos.skill_management.domain import ManagementError


_spec = importlib.util.spec_from_file_location(
    "lifecycle_synthetic_fixtures",
    Path(__file__).parents[1] / "skill_management" / "management_fixtures.py",
)
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


def register(repo, *, name="synthetic-repair", revision="a" * 64, requires=(), challenges=()):
    candidate, manifest = _fixtures.candidate_for(repo.project_id)
    candidate.update(skill_id=name, revision=revision, requires=list(requires))
    candidate["content_hash"] = hash_json(
        {k: v for k, v in candidate.items() if k != "content_hash"}
    )
    manifest.update(
        skill_id=name, revision=revision, candidate_content_hash=candidate["content_hash"]
    )
    skill = {"kind": "Skill", "key": name, "payload": {"skill_id": name}}
    rev = {
        "kind": "SkillRevision",
        "key": name + "@" + revision,
        "payload": {
            "skill_id": name,
            "revision": revision,
            "status": "candidate",
            "requires": list(requires),
            "candidate": candidate,
        },
    }
    left, right = ({k: v[k] for k in ("kind", "key")} for v in (skill, rev))
    edges = [
        {
            "kind": "HAS_REVISION",
            "key": edge_key("HAS_REVISION", left, right, {}),
            "source": left,
            "target": right,
            "payload": {},
        }
    ]
    for dependency in requires:
        target = {
            "kind": "SkillRevision",
            "key": dependency["skill_id"] + "@" + dependency["revision"],
        }
        edges.append(
            {
                "kind": "REQUIRES",
                "key": edge_key("REQUIRES", right, target, {}),
                "source": right,
                "target": target,
                "payload": {},
            }
        )
    repo.store_bundle([skill, rev], edges)
    repo.register_candidate(
        candidate, manifest, "/synthetic/lifecycle/" + name, challenges=list(challenges)
    )
    return candidate


def bind(repo, c):
    subjects = [
        {"subjectId": "1" * 64, "kind": "memory", "expectedDigest": "2" * 64},
        {"subjectId": "3" * 64, "kind": "tool", "expectedDigest": "4" * 64},
    ]
    repo.bind_lifecycle(c["skill_id"], c["revision"], subjects=subjects, actor="synthetic-host")
    return subjects


def scan(repo, c, statuses=("healthy", "healthy")):
    detail = repo.get_revision(c["skill_id"], c["revision"])
    observations = [
        {
            "subjectId": s["subjectId"],
            "status": status,
            "observedDigest": s["expectedDigest"]
            if status == "healthy"
            else "5" * 64
            if status == "changed"
            else None,
            "reason": "synthetic_probe",
        }
        for s, status in zip(
            repo.lifecycle_subjects(c["skill_id"], c["revision"]), statuses, strict=True
        )
    ]
    return repo.record_observations(
        c["skill_id"],
        c["revision"],
        observations=observations,
        expected_sequence=detail["management"]["sequence"],
        actor="synthetic-host",
        reason="Synthetic host scan",
        operation_id=uuid.uuid4().hex,
    )


def review(repo, c, decision, **kwargs):
    package = repo.prepare_impact(c["skill_id"], c["revision"], actor="delegated-tester")
    return repo.review_impact(
        c["skill_id"],
        c["revision"],
        package_id=package["packageId"],
        package_hash=package["packageHash"],
        expected_sequence=package["sequence"],
        operation_id=uuid.uuid4().hex,
        actor="delegated-tester",
        reason="Synthetic delegated review with explicit test evidence",
        decision=decision,
        root_ids=kwargs.get("root_ids", []),
        validation_ids=kwargs.get("validation_ids", []),
        replacement_revision=kwargs.get("replacement_revision"),
    )


def test_binding_scan_idempotence_and_no_automatic_restoration(repo):
    c = register(repo)
    assert repo.get_revision(c["skill_id"], c["revision"])["lifecycle"]["enabled"] is False
    bind(repo, c)
    first = scan(repo, c)
    second = scan(repo, c)
    assert second["changed"] is False and second["sequence"] == first["sequence"]
    bad = scan(repo, c, ("changed", "unavailable"))
    assert len(bad["lifecycle"]["roots"]) == 2
    restored = scan(repo, c)
    assert {r["status"] for r in restored["lifecycle"]["roots"]} == {"restored_pending"}
    assert restored["lifecycle"]["gate"]["status"] == "BLOCK"
    with pytest.raises(ManagementError, match="restoration_validation_required"):
        review(repo, c, "restore", root_ids=[bad["lifecycle"]["roots"][0]["rootId"]])


def test_requires_propagates_two_roots_and_stales_dependent_package(repo):
    a = register(repo)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    bind(repo, a)
    bind(repo, b)
    scan(repo, a)
    scan(repo, b)
    before = repo.prepare_impact(b["skill_id"], b["revision"], actor="owner")
    scan(repo, a, ("changed", "invalid"))
    detail = repo.get_revision(b["skill_id"], b["revision"])
    assert detail["management"]["sequence"] > before["sequence"]
    roots = detail["lifecycle"]["roots"]
    assert len(roots) == 2
    assert all(
        r["paths"] == [[b["skill_id"] + "@" + b["revision"], a["skill_id"] + "@" + a["revision"]]]
        for r in roots
    )


def test_challenge_disposition_preserves_original_and_requires_current_evidence(repo):
    c = register(repo, challenges=["Prior scope was not proven"])
    bind(repo, c)
    scan(repo, c)
    package = repo.prepare_impact(c["skill_id"], c["revision"], actor="delegate")
    challenge = package["currentChallengeIds"][0]
    args = {
        "package_id": package["packageId"],
        "package_hash": package["packageHash"],
        "expected_sequence": package["sequence"],
        "operation_id": "resolve",
        "actor": "delegate",
        "reason": "Delegated review: source observation verifies the documented limited scope",
        "challenge_id": challenge,
        "decision": "addressed",
        "evidence_ids": ["f" * 64],
        "replacement_revision": None,
    }
    with pytest.raises(ManagementError, match="evidence_not_current"):
        repo.resolve_challenge(c["skill_id"], c["revision"], **args)
    args["evidence_ids"] = [package["evidenceIds"][0]]
    result = repo.resolve_challenge(c["skill_id"], c["revision"], **args)
    assert result["lifecycle"]["decisions"][-1]["actor"] == "delegate"
    detail = repo.get_revision(c["skill_id"], c["revision"])
    assert detail["management"]["challenges"][0]["reason"] == "Prior scope was not proven"
    assert detail["management"]["challenges"][0]["status"] == "addressed"


def test_binding_cannot_be_rebased_after_tool_drift(repo):
    c = register(repo)
    subjects = bind(repo, c)
    subjects[0]["expectedDigest"] = "9" * 64
    with pytest.raises(ManagementError, match="immutable_conflict"):
        repo.bind_lifecycle(c["skill_id"], c["revision"], subjects=subjects, actor="host")


def qualify(
    repo,
    c,
    suffix="one",
    *,
    actual=False,
    run_id=None,
    attempt_id=None,
    case_id=None,
    receipt_sha=None,
):
    report = _fixtures.report(c, suffix=suffix)
    report["receipt"]["sha256"] = receipt_sha or hash_json([c["skill_id"], suffix])
    if run_id is not None:
        report.update(runId=run_id, attemptId=attempt_id, caseId=case_id)
        report["scope"]["caseIds"] = [case_id]
    if actual:
        report["purpose"] = "actual"
    validation = repo.record_validation(
        c["skill_id"],
        c["revision"],
        report=report,
        actor="synthetic-evaluator",
        operation_id="validation-" + c["skill_id"] + suffix,
    )
    if actual:
        # Test-only host state setup: no model run or efficacy is asserted here.
        def seed(records, nodes, edges):
            state = repo._state(records, c["skill_id"], c["revision"])
            state.setdefault("qualification", {}).setdefault("contributions", {})[
                validation["validationId"]
            ] = {
                "adjudicationId": hash_json(["synthetic", suffix]),
                "branchCoverage": report["branchCoverage"],
                "boundaryCoverage": report["boundaryCoverage"],
            }
            repo._changed(records, state, "synthetic_test_qualification", "test")

        repo._run(seed, mutation=True)
    return validation


def publish(repo, c):
    package = repo.prepare_review(
        c["skill_id"], c["revision"], source_watermark=["b" * 64], actor="test"
    )
    return repo.publish(
        c["skill_id"],
        c["revision"],
        package_id=package["packageId"],
        package_hash=package["packageHash"],
        expected_sequence=package["sequence"],
        operation_id="publish-" + c["skill_id"] + c["revision"],
        actor="test",
        reason="Synthetic transaction fixture, not a real publication",
        source_watermark=["b" * 64],
    )


def test_independent_roots_require_new_actual_qualification_and_clear_separately(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    old = qualify(repo, c, actual=True)
    bad = scan(repo, c, ("changed", "invalid"))
    scan(repo, c)
    roots = [r["rootId"] for r in bad["lifecycle"]["roots"]]
    with pytest.raises(ManagementError, match="restoration_validation_stale"):
        review(repo, c, "restore", root_ids=roots[:1], validation_ids=[old["validationId"]])
    synthetic = qualify(repo, c, "synthetic")
    with pytest.raises(ManagementError, match="restoration_validation_required"):
        review(repo, c, "restore", root_ids=roots[:1], validation_ids=[synthetic["validationId"]])
    from test_execution import begin, finish, qualified_ticket

    ticket = begin(repo, c)
    finish(repo, ticket)
    fresh = qualified_ticket(repo, c, ticket)
    one = review(repo, c, "restore", root_ids=roots[:1], validation_ids=[fresh["validationId"]])
    assert len(one["lifecycle"]["roots"]) == 1 and one["lifecycle"]["gate"]["status"] == "BLOCK"
    both = review(repo, c, "restore", root_ids=roots[1:], validation_ids=[fresh["validationId"]])
    assert both["lifecycle"]["roots"] == [] and both["lifecycle"]["gate"]["status"] == "PASS"
    assert len(both["lifecycle"]["observations"]) == 6


def test_published_dependency_is_bound_and_pause_withdraw_propagate(repo):
    a = register(repo)
    bind(repo, a)
    scan(repo, a)
    qualify(repo, a)
    release_a = publish(repo, a)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    bind(repo, b)
    scan(repo, b)
    qualify(repo, b)
    publish(repo, b)
    assert repo.admission(
        b["skill_id"], b["revision"], "dev-synthetic-one", allow_management_test=True
    )["allowed"]
    detail = repo.get_revision(b["skill_id"], b["revision"])
    assert detail["releases"][0]["dependencyReleases"] == [
        {"skillId": a["skill_id"], "revision": a["revision"], "releaseId": release_a["releaseId"]}
    ]
    current = repo.get_revision(a["skill_id"], a["revision"])
    repo.set_status(
        a["skill_id"],
        a["revision"],
        action="pause",
        expected_sequence=current["management"]["sequence"],
        operation_id="pause-a",
        actor="test",
        reason="Dependency no longer available",
    )
    assert not repo.admission(
        b["skill_id"], b["revision"], "dev-synthetic-one", allow_management_test=True
    )["allowed"]
    current = repo.get_revision(a["skill_id"], a["revision"])
    repo.set_status(
        a["skill_id"],
        a["revision"],
        action="withdraw",
        expected_sequence=current["management"]["sequence"],
        operation_id="withdraw-a",
        actor="test",
        reason="Permanent withdrawal",
    )
    fresh = qualify(repo, a, "fresh", actual=True)
    with pytest.raises(ManagementError, match="invalid_transition"):
        review(repo, a, "restore", validation_ids=[fresh["validationId"]])


def test_actual_supersedes_preserves_old_qualification_and_fixed_dependency(repo):
    old = register(repo)
    new = register(repo, revision="9" * 64)
    for c in (old, new):
        bind(repo, c)
        scan(repo, c)
    qualify(repo, new, actual=True)
    release = publish(repo, new)
    result = review(repo, old, "supersede", replacement_revision=new["revision"])
    assert result["state"] == "candidate"
    detail = repo.get_revision(old["skill_id"], old["revision"])
    assert detail["validations"] == [] and detail["releases"] == []
    graph = repo.inspect()
    replacements = [e for e in graph["edges"] if e["kind"] == "SUPERSEDES"]
    assert replacements[0]["payload"]["approval_ref"] == release["releaseId"]


def test_unpublished_dependency_diagnostic_package_contains_no_null_release(repo):
    a = register(repo)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    package = repo.prepare_impact(b["skill_id"], b["revision"], actor="owner")
    assert package["dependencyReleases"] == []
    detail = repo.get_revision(b["skill_id"], b["revision"])
    assert detail["lifecycle"]["dependencies"][0]["releaseId"] is None
    assert detail["lifecycle"]["gate"]["status"] == "BLOCK"


def test_atomic_closure_scan_is_replayable_and_rejects_concurrent_change(repo):
    a = register(repo)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    bind(repo, a)
    bind(repo, b)
    calls = []

    def observe(subjects):
        calls.append(subjects)
        return [
            {
                "subjectId": s["subjectId"],
                "status": "healthy",
                "observedDigest": s["expectedDigest"],
                "reason": "synthetic_probe",
            }
            for s in subjects
        ]

    sequence = repo.get_revision(b["skill_id"], b["revision"])["management"]["sequence"]
    args = {
        "observer": observe,
        "actor": "host",
        "reason": "Synthetic closure scan",
        "expected_sequence": sequence,
        "operation_id": "atomic-scan",
    }
    first = repo.scan_lifecycle(b["skill_id"], b["revision"], **args)
    assert len(calls) == 1
    assert repo.scan_lifecycle(b["skill_id"], b["revision"], **args) == first
    assert len(calls) == 1

    def racing(subjects):
        detail = repo.get_revision(a["skill_id"], a["revision"])
        repo.challenge(
            a["skill_id"],
            a["revision"],
            expected_sequence=detail["management"]["sequence"],
            operation_id=uuid.uuid4().hex,
            actor="owner",
            reason="Concurrent contradictory information",
        )
        return observe(subjects)

    with pytest.raises(ManagementError, match="state_conflict"):
        repo.scan_lifecycle(
            b["skill_id"],
            b["revision"],
            observer=racing,
            actor="host",
            reason="Race with review",
            operation_id="raced-scan",
        )
    assert len(repo.get_revision(b["skill_id"], b["revision"])["lifecycle"]["observations"]) == 2


def test_related_to_does_not_propagate_root_or_change_sequence(repo):
    a = register(repo)
    b = register(repo, name="related", revision="b" * 64)
    bind(repo, a)
    bind(repo, b)
    left = {"kind": "Skill", "key": a["skill_id"]}
    right = {"kind": "Skill", "key": b["skill_id"]}
    repo.store_bundle(
        [],
        [
            {
                "kind": "RELATED_TO",
                "key": edge_key("RELATED_TO", left, right, {}),
                "source": left,
                "target": right,
                "payload": {},
            }
        ],
    )
    before = repo.get_revision(b["skill_id"], b["revision"])["management"]["sequence"]
    scan(repo, a, ("changed", "invalid"))
    after = repo.get_revision(b["skill_id"], b["revision"])
    assert after["management"]["sequence"] == before
    assert after["lifecycle"]["roots"] == []


def test_validation_made_before_restored_observation_cannot_restore(repo):
    c = register(repo)
    bind(repo, c)
    changed = scan(repo, c, ("changed", "healthy"))
    premature = qualify(repo, c, "premature", actual=True)
    scan(repo, c)
    with pytest.raises(ManagementError, match="restoration_validation_context_mismatch"):
        review(
            repo,
            c,
            "restore",
            root_ids=[changed["lifecycle"]["roots"][0]["rootId"]],
            validation_ids=[premature["validationId"]],
        )


def test_manual_pause_restore_reopens_fixed_dependents_without_rebinding(repo):
    a = register(repo)
    bind(repo, a)
    scan(repo, a)
    qualify(repo, a)
    publish(repo, a)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    bind(repo, b)
    scan(repo, b)
    qualify(repo, b)
    publish(repo, b)
    original = repo.get_revision(b["skill_id"], b["revision"])["releases"][0]
    current = repo.get_revision(a["skill_id"], a["revision"])
    repo.set_status(
        a["skill_id"],
        a["revision"],
        action="pause",
        expected_sequence=current["management"]["sequence"],
        operation_id="manual-pause",
        actor="test",
        reason="Controlled lifecycle test pause",
    )
    assert not repo.admission(
        b["skill_id"], b["revision"], "dev-synthetic-one", allow_management_test=True
    )["allowed"]
    from test_execution import begin, finish, qualified_ticket

    ticket = begin(repo, a)
    finish(repo, ticket)
    fresh = qualified_ticket(repo, a, ticket)
    review(repo, a, "restore", validation_ids=[fresh["validationId"]])
    assert repo.admission(
        b["skill_id"], b["revision"], "dev-synthetic-one", allow_management_test=True
    )["allowed"]
    assert repo.get_revision(b["skill_id"], b["revision"])["releases"][0] == original


def test_actual_publication_cannot_depend_on_synthetic_release(repo):
    a = register(repo)
    qualify(repo, a)
    publish(repo, a)
    b = register(
        repo,
        name="dependent",
        revision="b" * 64,
        requires=[{"skill_id": a["skill_id"], "revision": a["revision"]}],
    )
    qualify(repo, b, actual=True)
    with pytest.raises(ManagementError, match="dependency_actual_release_required"):
        publish(repo, b)


def test_delayed_import_without_execution_ticket_never_restores(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    # The completed native run is deliberately not imported until after recovery.
    old_report = _fixtures.report(c, suffix="delayed")
    changed = scan(repo, c, ("changed", "healthy"))
    scan(repo, c)
    delayed = qualify(repo, c, "delayed", actual=True)
    assert old_report["runId"] == delayed["runId"]
    with pytest.raises(ManagementError, match="restoration_execution_required"):
        review(
            repo,
            c,
            "restore",
            root_ids=[changed["lifecycle"]["roots"][0]["rootId"]],
            validation_ids=[delayed["validationId"]],
        )


def test_lifecycle_public_dto_excludes_internal_locators_and_decision_details(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    review(repo, c, "retain")
    lifecycle = repo.get_revision(c["skill_id"], c["revision"])["lifecycle"]
    assert set(lifecycle["observations"][0]) == {
        "observationId",
        "subjectId",
        "kind",
        "expectedDigest",
        "observedDigest",
        "status",
        "previousObservationId",
        "detectorVersion",
        "reason",
        "observedAt",
    }
    assert set(lifecycle["decisions"][0]) == {
        "decisionId",
        "action",
        "actor",
        "reason",
        "decidedAt",
        "evidenceIds",
    }
