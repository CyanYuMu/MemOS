"""Synthetic C2 review inputs; never actual run evidence or approval."""

import copy

from memos.skill_branch_c2.tasks import get_suite, suite_digest
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION, get_task, task_digest
from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import validate_report


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


def branch_facts(task_id="branch-scoped-table"):
    task = get_task(task_id)
    candidate, _ = candidate_for("synthetic-branch-review")
    candidate["revision"] = CANDIDATE_REVISION
    candidate["rules"] = [
        {
            "rule_id": name,
            "condition": "Synthetic",
            "action": "Synthetic",
            "branches": [{"when": "Synthetic", "target": target} for target in targets],
        }
        for name, targets in (
            (
                "confirm_contract",
                [
                    "exit:not_applicable",
                    "exit:needs_evidence",
                    "exit:success",
                    "repair_scoped_reads",
                    "repair_dream_update",
                    "handle_unresolved",
                ],
            ),
            (
                "repair_scoped_reads",
                ["verify_scoped_reads", "exit:needs_evidence", "handle_unresolved"],
            ),
            (
                "verify_scoped_reads",
                [
                    "repair_dream_update",
                    "verify_dream_update",
                    "exit:success",
                    "handle_unresolved",
                    "exit:needs_evidence",
                ],
            ),
            (
                "repair_dream_update",
                ["verify_dream_update", "exit:needs_evidence", "handle_unresolved"],
            ),
            ("verify_dream_update", ["exit:success", "exit:needs_evidence", "handle_unresolved"]),
            ("handle_unresolved", ["exit:needs_evidence", "exit:unresolved"]),
        )
    ]
    candidate["content_hash"] = hash_json(
        {k: v for k, v in candidate.items() if k != "content_hash"}
    )
    raw = report(candidate)
    raw.update(
        caseId=task.task_id,
        caseRevision=task.revision,
        lineageId=task.lineage_id,
        purpose="actual",
        branchCoverage=[],
        boundaryCoverage=[],
    )
    raw["scope"]["caseIds"] = [task.task_id]
    rules = [branch.rsplit("#", 1)[0] for branch in task.target_branches]
    raw["skillUsage"]["declaredRuleIds"] = rules
    validation = validate_report(candidate, raw)
    events = [
        {
            "id": "before_start",
            "kind": "tool_started",
            "value": {"display": {"tool_name": "mcp__memos_run__run_visible_checks"}},
        },
        {
            "id": "before_result",
            "kind": "tool_completed",
            "value": {"display": {"result": "initial failed"}},
        },
        {"id": "edit", "kind": "tool_started", "value": {"display": {"tool_name": "Edit"}}},
        {
            "id": "after_start",
            "kind": "tool_started",
            "value": {"display": {"tool_name": "mcp__memos_run__run_visible_checks"}},
        },
        {
            "id": "after_result",
            "kind": "tool_completed",
            "value": {"display": {"result": "final passed"}},
        },
        *[
            {
                "id": rule + "_decl",
                "kind": "rule_declaration",
                "value": {"display": {"rule_id": rule}},
            }
            for rule in rules
        ],
    ]
    plan = {
        "candidateRevision": candidate["revision"],
        "caseId": task.task_id,
        "caseDigest": task_digest(task),
        "eligibleBranches": list(task.target_branches),
    }
    assessment = {
        "schemaVersion": 2,
        "collectorVersion": "branch-review-1",
        "collectorDigest": "6" * 64,
        **{
            key: validation[key]
            for key in (
                "validationId",
                "contentHash",
                "exportHash",
                "runId",
                "attemptId",
                "caseId",
                "caseRevision",
                "lineageId",
                "split",
                "purpose",
            )
        },
        "reportSha256": "1" * 64,
        "eventsSha256": "2" * 64,
        "candidateSnapshot": "3" * 64,
        "captureComplete": True,
        "loadVerified": True,
        "behaviorChanged": True,
        "originalStatus": "passed",
        "verification": {
            "status": "passed",
            "receiptSha256": "4" * 64,
            "protocol": "branch-receipt-1",
            "matches": True,
            "sameFinalPatch": True,
            "posthoc": False,
        },
        "initialVerification": {
            "status": "failed",
            "receiptSha256": "7" * 64,
            "protocol": "branch-receipt-1",
            "matches": True,
            "candidateSnapshot": "8" * 64,
        },
        "publicVerification": {
            "initialExitCode": 1,
            "finalExitCode": 0,
            "recordsVerified": True,
            "deliveriesVerified": True,
            "deliveryRefs": [
                {
                    "checkIndex": 1,
                    "startedEvent": "before_start",
                    "completedEvent": "before_result",
                },
                {"checkIndex": 2, "startedEvent": "after_start", "completedEvent": "after_result"},
            ],
        },
        "outcome": "repair_candidate",
        "batchId": "9" * 64,
        "taskDigest": task_digest(task),
        "checks": [{"name": name, "status": "passed"} for name in task.required_checks],
        "events": events,
        "branchCandidates": list(task.target_branches),
        "coveragePlanHash": hash_json(plan),
    }
    decision = {
        "taskDecision": "repair_confirmed",
        "historyDecision": "retain",
        "usageDecision": "correct",
        "applicability": "in_scope",
        "boundary": None,
        "violations": "none",
        "reason": "Synthetic review only",
        "eventRefs": [event["id"] for event in events],
        "branches": [
            {
                "branchId": branch,
                "conditionRefs": ["before_result" if i < 2 else "after_result", rules[i] + "_decl"],
                "actionRefs": ["edit" if i < 2 else "after_start"],
                "checkRefs": list(task.required_checks),
                "reason": "Synthetic condition, action and check",
            }
            for i, branch in enumerate(task.target_branches)
        ],
    }
    return candidate, validation, assessment, decision


def c2_facts():
    candidate, old_validation, assessment, decision = branch_facts()
    suite = get_suite("scoped-healthy-dream-c2-v1")
    raw = {
        k: v for k, v in old_validation.items() if k not in {"skillId", "revision", "validationId"}
    }
    raw.update(caseId=suite.task_id, caseRevision=suite.revision, lineageId=suite.lineage_id)
    raw["scope"]["caseIds"] = [suite.task_id]
    validation = validate_report(candidate, raw)
    for key in ("validationId", "caseId", "caseRevision", "lineageId"):
        assessment[key] = validation[key]
    events = []

    def event(name, kind, **payload):
        events.append({"id": name, "kind": kind, "value": {"display": payload}})

    def check(index):
        event(f"check{index}_start", "tool_started", tool_name="mcp__memos_run__run_visible_checks")
        event(f"check{index}_result", "tool_completed", is_error=False)

    def declare(rule):
        event(rule + "_decl", "rule_declaration", rule_id=rule)

    check(1)
    declare("confirm_contract")
    declare("repair_scoped_reads")
    event("edit_scoped", "tool_started", tool_name="Edit")
    event("edit_scoped_done", "tool_completed", is_error=False)
    check(2)
    declare("verify_scoped_reads")
    declare("verify_dream_update")
    event("exit", "assistant_text", text="Synthetic explicit success exit")
    checks = [{"name": row["id"], "status": "passed"} for row in suite.checks]
    checkpoints = []
    for index in (1, 2):
        rows = copy.deepcopy(checks)
        for row in rows:
            if index == 1 and row["name"] in suite.expected_failures["initial"]["public"]:
                row["status"] = "failed"
        checkpoints.append(
            {
                "checkIndex": index,
                "snapshot": ("8" if index == 1 else "3") * 64,
                "recordSha256": str(index) * 64,
                "startedEvent": f"check{index}_start",
                "completedEvent": f"check{index}_result",
                "checks": rows,
                "groupStatus": {"scoped": "failed" if index == 1 else "passed", "dream": "passed"},
                "cyclesUsed": index - 1,
                "editEventRefs": [] if index == 1 else ["edit_scoped"],
            }
        )
    assessment.update(
        schemaVersion=4,
        collectorVersion="c2-review-1",
        suiteId=suite.suite_id,
        taskDigest=suite_digest(suite),
        checkMapping=copy.deepcopy(list(suite.checks)),
        checks=checks,
        events=events,
        branchCandidates=list(suite.target_branches),
        coveragePlanHash=hash_json(
            {
                "candidateRevision": candidate["revision"],
                "caseId": suite.task_id,
                "caseDigest": suite_digest(suite),
                "eligibleBranches": list(suite.target_branches),
            }
        ),
        processVerification={
            "version": "c2-process-1",
            "complete": True,
            "issues": [],
            "maxCycles": 2,
            "cyclesUsed": 1,
            "budgetExceeded": False,
            "unverifiedEdits": False,
            "checkpoints": checkpoints,
        },
        intermediateVerifications=[],
    )
    for field in ("initialVerification", "verification"):
        assessment[field]["protocol"] = "c2-receipt-1"
    assessment["initialVerification"]["checks"] = copy.deepcopy(checkpoints[0]["checks"])
    assessment["publicVerification"]["deliveryRefs"] = [
        {k: row[k] for k in ("checkIndex", "startedEvent", "completedEvent")} for row in checkpoints
    ]

    def refs(index, group=None):
        return [
            f"check:{index}:{row['id']}"
            for row in suite.checks
            if group is None or row["group"] == group
        ]

    claims = [
        (
            "confirm_contract#3",
            ["check1_result"],
            ["confirm_contract_decl", "edit_scoped"],
            refs(1),
        ),
        (
            "repair_scoped_reads#0",
            ["check1_result"],
            ["repair_scoped_reads_decl", "edit_scoped"],
            refs(2, "scoped"),
        ),
        (
            "verify_scoped_reads#1",
            ["check2_result"],
            ["verify_scoped_reads_decl", "verify_dream_update_decl", "exit"],
            refs(2),
        ),
        ("verify_dream_update#0", ["check2_result"], ["verify_dream_update_decl", "exit"], refs(2)),
    ]
    decision["eventRefs"] = [row["id"] for row in events]
    decision["branches"] = [
        {
            "branchId": name,
            "conditionRefs": conditions,
            "actionRefs": actions,
            "checkRefs": checks_,
            "reason": "Synthetic chronological evidence",
        }
        for name, conditions, actions, checks_ in claims
    ]
    return candidate, validation, assessment, decision
