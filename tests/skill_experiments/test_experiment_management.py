"""Only host-registered experiments cross the authenticated control plane."""

import json

from types import SimpleNamespace

from memos.skill_management.api import dispatch
from memos.skill_management.service import Project, SkillService


def test_experiment_commands_require_capability_and_reject_host_paths():
    class Service:
        def call(self, action, body, actor):
            return {"action": action, "actor": actor}

    headers = {
        "Authorization": "Bearer secret",
        "X-Skill-Actor": "viewer",
        "X-Skill-Capability": "view",
        "X-Skill-Project": "project",
    }
    for action, body, expected in [
        ("experiments_list", {"projectId": "project"}, 200),
        ("experiment_detail", {"projectId": "project", "experimentId": "a" * 64}, 200),
        (
            "experiment_start",
            {"projectId": "project", "experimentId": "a" * 64, "operationId": "run"},
            403,
        ),
        (
            "experiment_detail",
            {"projectId": "project", "experimentId": "a" * 64, "directory": "/private"},
            400,
        ),
    ]:
        code, _ = dispatch(
            Service(), "secret", "/v1/skills/" + action, headers, json.dumps(body).encode()
        )
        assert code == expected


def test_empty_experiment_registry_is_backward_compatible(tmp_path):
    service = SkillService(
        {"project": Project(SimpleNamespace(close=lambda: None), lambda _: {})}, tmp_path, None, {}
    )
    try:
        assert service.call("experiments_list", {"projectId": "project"}, "viewer") == {
            "items": [],
            "total": 0,
        }
    finally:
        service.close()
