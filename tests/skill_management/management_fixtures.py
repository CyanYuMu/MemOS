"""Explicit synthetic management fixtures, never source or efficacy evidence."""

import copy

from memos.skill_drafting.domain import hash_json


def candidate():
    value = {
        "skill_id": "synthetic-repair",
        "revision": "a" * 64,
        "project_id": "replaced-by-test-project",
        "title": "Synthetic management test",
        "purpose": "repair",
        "tags": ["management_test"],
        "requires": [],
        "status": "candidate",
        "source_refs": [{"source_id": "b" * 64}],
        "rules": [
            {
                "rule_id": "R1",
                "condition": "Synthetic condition",
                "action": "Synthetic action",
                "branches": [
                    {"when": name, "target": "exit:" + name}
                    for name in ("success", "not_applicable", "needs_evidence", "unresolved")
                ],
            }
        ],
    }
    value["content_hash"] = hash_json(value)
    return value


def candidate_for(project):
    value = candidate()
    value["project_id"] = project
    value["content_hash"] = hash_json({k: v for k, v in value.items() if k != "content_hash"})
    manifest = {
        "schema_version": 1,
        "skill_id": value["skill_id"],
        "revision": value["revision"],
        "project_id": project,
        "candidate_content_hash": value["content_hash"],
        "manifest_id": "c" * 64,
    }
    return value, manifest


def report(value, *, kind="independent_task", status="passed", suffix="one"):
    return {
        "kind": kind,
        "status": status,
        "contentHash": value["content_hash"],
        "exportHash": "d" * 64,
        "caseId": "dev-synthetic-" + suffix,
        "caseRevision": "1",
        "lineageId": "synthetic-" + suffix,
        "split": "dev" if kind == "isolated_dev" else "holdout",
        "runId": "run-" + suffix,
        "attemptId": "attempt-" + suffix,
        "taskResult": {"status": status, "accepted": status == "passed", "receiptMatches": True},
        "skillUsage": {
            "loading": "verified",
            "capture": "complete",
            "usageReview": "unreviewed",
            "declaredRuleIds": ["R1"],
        },
        "receiptVerified": True,
        "loadVerified": True,
        "receipt": {"uri": "file:///synthetic/receipt-" + suffix + ".json", "sha256": "e" * 64},
        "branchCoverage": ["R1#0", "R1#1", "R1#2", "R1#3"] if kind == "independent_task" else [],
        "boundaryCoverage": ["not_applicable", "needs_evidence", "unresolved"]
        if kind == "independent_task"
        else [],
        "scope": {
            "caseIds": ["dev-synthetic-" + suffix],
            "agent": "claude_code",
            "mode": "controlled_task",
        },
        "purpose": "management_test",
    }


def state(value):
    return {
        "candidate": copy.deepcopy(value),
        "state": "candidate",
        "sequence": 0,
        "challenges": [],
        "exportHash": "d" * 64,
    }
