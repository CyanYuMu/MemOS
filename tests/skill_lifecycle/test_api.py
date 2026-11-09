"""Private API accepts decisions, never uploaded host probe results."""

import json

import pytest

from memos.skill_management.api import dispatch


class Service:
    def call(self, action, body, actor):
        return {"action": action, "actor": actor}


def call(action, body, capability="manage"):
    headers = {
        "Authorization": "Bearer " + "x" * 32,
        "X-Skill-Actor": "delegated-owner",
        "X-Skill-Capability": capability,
        "X-Skill-Project": "project",
    }
    return dispatch(Service(), "x" * 32, "/v1/skills/" + action, headers, json.dumps(body).encode())


def body(action):
    value = {"projectId": "project", "skillId": "skill", "revision": "a" * 64}
    if action != "impact_prepare":
        value.update(
            expectedSequence=1, operationId="op", reason="Delegated review authorized by user"
        )
    if action == "impact_review":
        value.update(
            packageId="b" * 64,
            packageHash="c" * 64,
            decision="retain",
            rootIds=[],
            validationIds=[],
            replacementRevision=None,
        )
    if action == "challenge_resolve":
        value.update(
            packageId="b" * 64,
            packageHash="c" * 64,
            challengeId="d" * 64,
            decision="addressed",
            evidenceIds=["e" * 64],
            replacementRevision=None,
        )
    return value


@pytest.mark.parametrize(
    "action", ["impact_scan", "impact_prepare", "impact_review", "challenge_resolve"]
)
def test_lifecycle_manage_routes(action):
    assert call(action, body(action))[0] == 200
    assert call(action, body(action), "write")[0] == 403
    assert call(action, {**body(action), "observations": []})[0] == 400


@pytest.mark.parametrize(
    "update",
    [
        {"decision": "restore"},
        {"rootIds": ["a" * 64, "a" * 64]},
        {"replacementRevision": "d" * 64},
        {"validationIds": None},
        {"decision": "supersede"},
        {"expectedSequence": True},
    ],
)
def test_invalid_review_combinations(update):
    assert call("impact_review", {**body("impact_review"), **update})[0] == 400
