"""Service authentication and explicit command boundaries."""

import json

import pytest

from memos.skill_management.api import dispatch
from memos.skill_management.domain import ManagementError


class Service:
    def __init__(self):
        self.calls = []

    def call(self, action, body, actor):
        self.calls.append((action, body, actor))
        return {"ok": True}


def request(action="list", body=None, **headers):
    service = Service()
    default = {
        "Authorization": "Bearer " + "x" * 32,
        "X-Skill-Actor": "account:owner",
        "X-Skill-Capability": "manage",
        "X-Skill-Project": "project-one",
    }
    default.update(headers)
    status, response = dispatch(
        service,
        "x" * 32,
        "/v1/skills/" + action,
        default,
        json.dumps(body or {"projectId": "project-one"}).encode(),
    )
    return status, response, service.calls


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "Bearer forged"}, 401),
        ({"X-Skill-Actor": ""}, 403),
        ({"X-Skill-Capability": "admin"}, 403),
        ({"X-Skill-Project": "project-two"}, 403),
    ],
)
def test_trusted_service_identity_required(headers, status):
    actual, _, calls = request(**headers)
    assert actual == status
    assert calls == []


def test_viewer_cannot_evaluate_and_editor_cannot_review():
    for action, capability in [("evaluate", "view"), ("review_prepare", "write")]:
        status, _, calls = request(action, **{"X-Skill-Capability": capability})
        assert status == 403 and not calls


def test_run_requires_write_and_does_not_accept_client_mode_or_paths():
    body = {
        "projectId": "project-one",
        "skillId": "skill",
        "revision": "a" * 64,
        "caseId": "dev-one",
        "operationId": "normal-one",
    }
    assert request("run", body, **{"X-Skill-Capability": "view"})[0] == 403
    assert request("run", body, **{"X-Skill-Capability": "write"})[0] == 200
    for field in ("mode", "path", "command"):
        assert request("run", {**body, field: "forged"})[0] == 400


def test_reconcile_is_manage_only_and_cannot_upload_evidence():
    body = {"projectId": "project-one", "jobId": "a" * 64, "operationId": "recovery-one"}
    assert request("job_reconcile", body, **{"X-Skill-Capability": "write"})[0] == 403
    assert request("job_reconcile", body)[0] == 200
    for key in ("path", "report", "mode", "validation"):
        assert request("job_reconcile", {**body, key: "forged"})[0] == 400


@pytest.mark.parametrize(
    "body",
    [
        {"projectId": "project-one", "path": "/private"},
        {"projectId": "project-one", "actor": "forged"},
        {"query": "missing project"},
    ],
)
def test_unknown_fields_and_missing_project_rejected(body):
    status, _, calls = request(body=body)
    assert status in {400, 403} and not calls


def test_duplicate_keys_and_nonfinite_json_rejected():
    for body in [b'{"projectId":"x","projectId":"y"}', b'{"projectId":NaN}']:
        status, _ = dispatch(
            Service(),
            "x" * 32,
            "/v1/skills/list",
            {
                "Authorization": "Bearer " + "x" * 32,
                "X-Skill-Actor": "owner",
                "X-Skill-Capability": "manage",
                "X-Skill-Project": "x",
            },
            body,
        )
        assert status == 400


def test_public_result_and_actor_are_bound():
    status, result, calls = request()
    assert status == 200 and result == {"data": {"ok": True}}
    assert calls == [("list", {"projectId": "project-one"}, "account:owner")]


def test_adjudication_has_manage_only_nested_contract_and_no_evidence_upload():
    from test_adjudication_contract import facts

    base = {"projectId": "project-one", "skillId": "synthetic-repair", "revision": "a" * 64}
    preparation = {**base, "assessmentId": "b" * 64}
    assert request("adjudication_prepare", preparation)[0] == 200
    assert request("adjudication_prepare", preparation, **{"X-Skill-Capability": "write"})[0] == 403
    body = {
        **base,
        "expectedSequence": 1,
        "operationId": "judge-1",
        "reason": "Human review",
        "packageId": "c" * 64,
        "packageHash": "d" * 64,
        "judgment": facts()[3],
        "supersedes": None,
    }
    status, _, calls = request("adjudicate", body)
    assert status == 200 and calls[0][2] == "account:owner"
    assert request("adjudicate", body, **{"X-Skill-Capability": "view"})[0] == 403
    assert request("adjudicate", {**body, "judgment": {"passed": True}})[0] == 400
    assert request("adjudicate", {**body, "path": "/private/evidence"})[0] == 400
    assert request("record_assessment", {**base, "assessment": {"passed": True}})[0] == 404


def test_errors_do_not_expose_exception_body():
    class Broken:
        def call(self, *_):
            raise RuntimeError("secret provider URL and password")

    status, body = dispatch(
        Broken(),
        "x" * 32,
        "/v1/skills/list",
        {
            "Authorization": "Bearer " + "x" * 32,
            "X-Skill-Actor": "owner",
            "X-Skill-Capability": "view",
            "X-Skill-Project": "x",
        },
        b'{"projectId":"x"}',
    )
    assert status == 503 and "secret" not in json.dumps(body)


def test_conflict_exposes_only_sequence():
    class Conflict:
        def call(self, *_):
            raise ManagementError("state_conflict", 409, {"currentSequence": 4, "uri": "secret"})

    status, body = dispatch(
        Conflict(),
        "x" * 32,
        "/v1/skills/list",
        {
            "Authorization": "Bearer " + "x" * 32,
            "X-Skill-Actor": "owner",
            "X-Skill-Capability": "view",
            "X-Skill-Project": "x",
        },
        b'{"projectId":"x"}',
    )
    assert status == 409 and body["error"]["details"] == {"currentSequence": 4}
