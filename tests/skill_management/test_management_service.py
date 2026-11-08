"""Orchestration tests keep host-only artifacts out of management responses."""

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from memos.skill_management.domain import ManagementError
from memos.skill_management.service import Project, SkillService, public


class Repository:
    def __init__(self):
        self.created = self.finished = self.validated = 0

    def get_revision(self, skill_id, revision):
        return {
            "candidate": {
                "skill_id": skill_id,
                "revision": revision,
                "content_hash": "b" * 64,
                "source_refs": [{"source_id": "source-one"}],
            },
            "management": {"state": "candidate", "sequence": 4},
            "validations": [],
            "jobs": [],
            "releases": [],
            "artifactDirectory": "/private/candidate",
        }

    def create_job(self, *_, **kwargs):
        self.created += 1
        return {"jobId": "job-one", "status": "queued", "created": True}

    def find_job(self, *_, **kwargs):
        return None

    def claim_job(self, _):
        return {"status": "running"}

    def finish_job(self, _, **kwargs):
        self.finished += 1
        self.result = kwargs

    def record_validation(self, *_, **kwargs):
        self.validated += 1
        return {"validationId": "validation-one"}

    def admission(self, *_):
        return {"allowed": False, "reason": "unpublished"}


class Pool:
    def submit(self, fn, *args):
        self.pending = (fn, args)
        return Future()


@pytest.fixture
def service(monkeypatch, tmp_path):
    import memos.skill_management.service as module

    repo = Repository()
    project = Project(repo, lambda _: {"watermark": ["source-one"]})
    pool = Pool()
    svc = SkillService({"public-project": project}, tmp_path, SimpleNamespace(), {}, executor=pool)
    monkeypatch.setattr(
        module,
        "dev_case",
        lambda case: None if case == "dev-one" else (_ for _ in ()).throw(ValueError()),
    )
    monkeypatch.setattr(module, "case_options", lambda: [{"caseId": "dev-one"}])
    monkeypatch.setattr(
        module, "export_identity", lambda _: {"exportHash": "c" * 64, "contentHash": "b" * 64}
    )
    return svc, repo, pool


def body(**values):
    return {"projectId": "public-project", "skillId": "skill", "revision": "a" * 64, **values}


def test_detail_does_not_leak_artifact_or_receipt_paths(service):
    svc, _, _ = service
    value = svc.call("detail", body(), "owner")
    assert "artifactDirectory" not in value
    assert value["projectId"] == "public-project"
    assert public({"receipt": {"uri": "file:///private/a", "sha256": "hash"}}) == {
        "receipt": {"sha256": "hash"}
    }


def test_unmapped_project_and_hidden_case_rejected_before_job(service):
    svc, repo, _ = service
    for values in [body(projectId="foreign"), body(caseId="holdout-secret", operationId="one")]:
        with pytest.raises(ManagementError):
            svc.call("evaluate", values, "owner")
    assert repo.created == 0


def test_normal_denial_does_not_create_job(service):
    svc, repo, _ = service
    with pytest.raises(ManagementError, match="not_ready"):
        svc.start("public-project", "skill", "a" * 64, "dev-one", "op-one", "owner", mode="normal")
    assert repo.created == 0


def test_source_failure_blocks_evaluation_before_job(service):
    svc, repo, _ = service
    svc.projects["public-project"].check_sources = lambda _: (_ for _ in ()).throw(
        ValueError("private")
    )
    with pytest.raises(ManagementError, match="source_unavailable"):
        svc.call("evaluate", body(caseId="dev-one", operationId="one"), "owner")
    assert repo.created == 0


def test_worker_exception_is_unknown_and_not_validation(service, monkeypatch):
    import memos.skill_management.service as module

    svc, repo, pool = service
    monkeypatch.setattr(
        module,
        "execute_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    svc.call("evaluate", body(caseId="dev-one", operationId="one"), "owner")
    fn, args = pool.pending
    fn(*args)
    assert repo.finished == 1 and repo.validated == 0
    assert (
        repo.result["status"] == "unknown" and repo.result["error_code"] == "execution_unverified"
    )


def test_busy_queue_and_job_replay(service):
    svc, repo, _ = service
    first = svc.call("evaluate", body(caseId="dev-one", operationId="one"), "owner")
    assert svc.call("evaluate", body(caseId="dev-one", operationId="one"), "owner") == first
    with pytest.raises(ManagementError, match="busy"):
        svc.call("evaluate", body(caseId="dev-one", operationId="two"), "owner")
    assert repo.created == 1
