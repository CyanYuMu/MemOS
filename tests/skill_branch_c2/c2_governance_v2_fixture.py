"""Synthetic qualified two-unit facts for schema-five contract tests."""

import copy

from c2_governance_fixture import c2_facts

from memos.skill_branch_c2 import execution_policy


def refresh_plan(value):
    plan = value["executionQualification"]
    for key in ("calibration", "trial"):
        unit = plan[key]
        unit["issues"] = execution_policy.unit_issues(unit)
        unit["qualified"] = not unit["issues"]
    plan["planIssues"] = [
        key + ":" + issue for key in ("calibration", "trial") for issue in plan[key]["issues"]
    ]
    plan["planQualified"] = not plan["planIssues"]


def c2_v2_facts():
    candidate, validation, assessment, decision = c2_facts()
    process = assessment["processVerification"]
    trial = {
        "unit": "c2-skill",
        "group": "skill",
        "runId": assessment["runId"],
        "attemptId": assessment["attemptId"],
        "reportSha256": assessment["reportSha256"],
        "eventsSha256": assessment["eventsSha256"],
        "initialReceiptSha256": assessment["initialVerification"]["receiptSha256"],
        "receiptSha256": assessment["verification"]["receiptSha256"],
        "status": "passed",
        "taskAccepted": validation["taskResult"]["accepted"],
        "captureComplete": assessment["captureComplete"],
        "loading": "verified",
        "processComplete": process["complete"],
        "maxCycles": process["maxCycles"],
        "cyclesUsed": process["cyclesUsed"],
        "budgetExceeded": process["budgetExceeded"],
        "unverifiedEdits": process["unverifiedEdits"],
        "policyMatches": True,
        "qualified": True,
        "issues": [],
    }
    calibration = copy.deepcopy(trial)
    calibration.update(
        unit="c2-none",
        group="none",
        runId="run-calibration",
        attemptId="attempt-calibration",
        loading="not_requested",
    )
    for key in ("reportSha256", "eventsSha256", "initialReceiptSha256", "receiptSha256"):
        calibration[key] = "f" * 64
    assessment.update(
        schemaVersion=5,
        collectorVersion="c2-review-2",
        executionQualification={
            "protocol": "c2-plan-1",
            "policyDigest": execution_policy.policy_identity()["digest"],
            "batchProtocol": "c2-batch-2",
            "batchId": assessment["batchId"],
            "manifestSha256": "e" * 64,
            "calibration": calibration,
            "trial": trial,
            "planQualified": True,
            "planIssues": [],
        },
    )
    refresh_plan(assessment)
    return candidate, validation, assessment, decision
