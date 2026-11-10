"""Explicit coverage upgrades preserve old baselines and require fresh recovery."""

import copy

import pytest

from test_repository import bind, publish, qualify, register, scan

from memos.skill_management.domain import ManagementError


def upgrade(repo, candidate, **overrides):
    detail = repo.get_revision(candidate["skill_id"], candidate["revision"])
    args = {
        "additions": [{"subjectId": "6" * 64, "kind": "tool", "expectedDigest": "7" * 64}],
        "expected_binding": repo.lifecycle_binding_id(candidate["skill_id"], candidate["revision"]),
        "expected_sequence": detail["management"]["sequence"],
        "actor": "synthetic-maintainer",
        "operation_id": "upgrade-one",
        "reason": "Add normal orchestration monitoring",
    }
    args.update(overrides)
    return repo.extend_lifecycle(candidate["skill_id"], candidate["revision"], **args)


def setup_paused(repo):
    c = register(repo)
    bind(repo, c)
    scan(repo, c)
    qualify(repo, c)
    publish(repo, c)
    detail = repo.get_revision(c["skill_id"], c["revision"])
    repo.set_status(
        c["skill_id"],
        c["revision"],
        action="pause",
        expected_sequence=detail["management"]["sequence"],
        actor="owner",
        reason="Maintenance",
        operation_id="pause",
    )
    return c


def test_upgrade_preserves_binding_records_and_does_not_restore(repo):
    c = setup_paused(repo)
    old = copy.deepcopy(repo.lifecycle_subjects(c["skill_id"], c["revision"]))
    binding = repo.lifecycle_binding_id(c["skill_id"], c["revision"])
    detail = repo.get_revision(c["skill_id"], c["revision"])
    sequence = detail["management"]["sequence"]
    result = upgrade(repo, c)
    assert result["previousBindingId"] == binding and result["bindingId"] != binding
    assert result["state"] == "paused"
    subjects = repo.lifecycle_subjects(c["skill_id"], c["revision"])
    assert all(s in subjects for s in old) and len(subjects) == 3
    assert repo.get_revision(c["skill_id"], c["revision"])["lifecycle"]["gate"]["status"] == "BLOCK"
    # An exact retry returns the original transaction; it cannot advance epochs.
    assert upgrade(repo, c, expected_binding=binding, expected_sequence=sequence) == result

    def audit(records, nodes, edges):
        state = repo._state(records, c["skill_id"], c["revision"])
        assert records[("lifecycle_binding", binding)]["subjects"] == old
        assert state["pausedExecutionSequence"] == state["lifecycleOrdinal"]
        assert state["pausedValidationIds"]

    repo._run(audit)


@pytest.mark.parametrize("fault", ["not_paused", "stale_binding", "replace_subject", "busy"])
def test_invalid_upgrade_is_rejected_without_rebinding(repo, fault):
    c = setup_paused(repo)
    binding = repo.lifecycle_binding_id(c["skill_id"], c["revision"])
    overrides = {}
    if fault == "stale_binding":
        overrides["expected_binding"] = "0" * 64
    elif fault == "replace_subject":
        overrides["additions"] = [
            {"subjectId": "1" * 64, "kind": "memory", "expectedDigest": "9" * 64}
        ]
    else:

        def alter(records, nodes, edges):
            state = repo._state(records, c["skill_id"], c["revision"])
            if fault == "not_paused":
                state["state"] = "published"
            else:
                repo._put(
                    records,
                    "execution_ticket",
                    "f" * 64,
                    {"skillId": c["skill_id"], "revision": c["revision"], "ticketId": "f" * 64},
                )

        repo._run(alter, mutation=True)
    with pytest.raises(ManagementError):
        upgrade(repo, c, **overrides)
    assert repo.lifecycle_binding_id(c["skill_id"], c["revision"]) == binding


def test_normal_monitor_detects_file_drift(tmp_path):
    from memos.skill_lifecycle.observers import observe_tool
    from memos.skill_management.maintenance import normal_monitor_subject

    cli = tmp_path / "cli"
    cli.write_text("#!/bin/sh\necho test-cli\n")
    cli.chmod(0o700)
    implementation = tmp_path / "p5_runtime.py"
    implementation.write_text("version = 1\n")
    subject = normal_monitor_subject(cli, paths=[implementation])
    assert observe_tool(subject)["status"] == "healthy"
    implementation.write_text("version = 2\n")
    assert observe_tool(subject)["status"] == "changed"


def test_upgrade_rejects_pre_upgrade_validation_and_requires_new_execution(repo):
    from test_execution import begin, finish, qualified_ticket
    from test_repository import review

    c = setup_paused(repo)
    old = qualify(repo, c, "before-upgrade", actual=True)
    upgrade(repo, c)
    scan(repo, c, ("healthy", "healthy", "healthy"))
    with pytest.raises(ManagementError):
        review(repo, c, "restore", validation_ids=[old["validationId"]], root_ids=[])
    ticket = begin(repo, c)
    finish(repo, ticket)
    fresh = qualified_ticket(repo, c, ticket)
    review(repo, c, "restore", validation_ids=[fresh["validationId"]], root_ids=[])
    assert repo.get_revision(c["skill_id"], c["revision"])["management"]["state"] == "published"
