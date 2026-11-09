"""Lifecycle service commands invoke only the registered host observer."""

from types import SimpleNamespace

from memos.skill_management.service import Project, SkillService


class Repository:
    def __init__(self):
        self.scans = []

    def get_revision(self, skill_id, revision):
        return {
            "candidate": {"skill_id": skill_id, "revision": revision, "source_refs": []},
            "management": {"sequence": 3},
        }

    def scan_lifecycle(self, skill_id, revision, **kwargs):
        self.scans.append(kwargs)
        assert kwargs["observer"]([{"kind": "tool"}]) == ["host-observation"]
        return {"sequence": 3, "changed": False, "lifecycle": {"enabled": True}}

    def prepare_impact(self, skill_id, revision, *, actor):
        return {"projectId": "private-graph-id", "actor": actor, "packageHash": "a" * 64}


def test_scan_and_prepare_use_host_observer_and_public_project_alias(tmp_path):
    repo = Repository()
    service = SkillService(
        {"public": Project(repo, lambda _: {}, observe=lambda _: ["host-observation"])},
        tmp_path,
        SimpleNamespace(),
        {},
    )
    key = {"projectId": "public", "skillId": "skill", "revision": "a" * 64}
    result = service.call(
        "impact_scan",
        {**key, "expectedSequence": 3, "operationId": "scan", "reason": "Delegated scan"},
        "owner",
    )
    assert result["changed"] is False
    assert repo.scans[-1]["expected_sequence"] == 3
    prepared = service.call("impact_prepare", key, "owner")
    assert prepared["projectId"] == "public" and prepared["actor"] == "owner"
    assert len(repo.scans) == 2
    service.executor.shutdown()


def test_blocked_normal_admission_does_not_repeat_slow_source_read(tmp_path):
    repo = Repository()
    repo.admission = lambda *_: {"allowed": False, "reason": "qualification_unavailable"}

    def forbidden_source(_):
        raise AssertionError("Blocked normal admission must not perform a second source GET")

    service = SkillService(
        {"public": Project(repo, forbidden_source, observe=lambda _: ["host-observation"])},
        tmp_path,
        SimpleNamespace(),
        {},
    )
    result = service._grant(service.projects["public"], "skill", "a" * 64, "dev-one", "normal")
    assert result["allowed"] is False
    service.executor.shutdown()
