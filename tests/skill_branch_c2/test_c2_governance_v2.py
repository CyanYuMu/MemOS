"""Host calibration deviations cannot be waived into Skill branch coverage."""

import copy

import pytest

from c2_governance_v2_fixture import c2_v2_facts, refresh_plan

from memos.skill_branch_c2 import governance_v2
from memos.skill_governance.repository import _contract_for
from memos.skill_management.domain import ManagementError


@pytest.fixture(autouse=True)
def synthetic_collector(monkeypatch):
    monkeypatch.setattr(governance_v2, "_collector_digest", lambda: "6" * 64)


def result(candidate, validation, assessment, decision):
    sealed = governance_v2.validate_assessment(candidate, validation, assessment)
    return governance_v2.adjudicate_assessment(candidate, validation, sealed, decision)


def test_qualified_plan_retains_four_reviewed_branch_contracts():
    c, v, a, d = c2_v2_facts()
    decision = result(c, v, a, d)
    assert decision["repairConfirmed"] and decision["qualifiedIndependent"]
    assert decision["branchCoverage"] == sorted(a["branchCandidates"])
    assert _contract_for(a) is governance_v2


def test_three_cycle_none_can_preserve_repair_and_usage_but_not_any_coverage():
    c, v, a, d = c2_v2_facts()
    a["executionQualification"]["calibration"].update(cyclesUsed=3, budgetExceeded=True)
    refresh_plan(a)
    assert a["outcome"] == "repair_candidate"
    with pytest.raises(ManagementError, match="execution_plan_unqualified"):
        result(c, v, a, d)
    d["branches"] = []
    judged = result(c, v, a, d)
    assert judged["repairConfirmed"]
    assert judged["decision"]["usageDecision"] == "correct"
    assert not judged["qualifiedIndependent"] and not judged["branchCoverage"]


@pytest.mark.parametrize(
    "field",
    [
        "cyclesUsed",
        "maxCycles",
        "planQualified",
        "unitQualified",
        "issues",
        "planIssues",
        "missing",
        "extra",
    ],
)
def test_calibration_qualification_cannot_trust_flags_or_malformed_types(field):
    c, v, a, _ = c2_v2_facts()
    plan = a["executionQualification"]
    unit = plan["calibration"]
    if field in {"cyclesUsed", "maxCycles"}:
        unit[field] = True
    elif field == "planQualified":
        plan[field] = False
    elif field == "unitQualified":
        unit["qualified"] = False
    elif field == "issues":
        unit["issues"] = ["fabricated"]
    elif field == "planIssues":
        plan[field] = ["calibration:fabricated"]
    elif field == "missing":
        del unit["policyMatches"]
    else:
        unit["arbitrary"] = "value"
    with pytest.raises(ManagementError):
        governance_v2.validate_assessment(c, v, a)


@pytest.mark.parametrize(
    "field",
    [
        "runId",
        "attemptId",
        "reportSha256",
        "eventsSha256",
        "initialReceiptSha256",
        "receiptSha256",
        "status",
        "taskAccepted",
        "captureComplete",
        "loading",
        "processComplete",
        "maxCycles",
        "cyclesUsed",
        "budgetExceeded",
        "unverifiedEdits",
    ],
)
def test_current_trial_must_match_the_bound_skill_assessment(field):
    c, v, a, _ = c2_v2_facts()
    trial = a["executionQualification"]["trial"]
    if isinstance(trial[field], bool):
        trial[field] = not trial[field]
    elif isinstance(trial[field], int):
        trial[field] += 1
    elif field.endswith("Sha256"):
        trial[field] = "a" * 64
    elif field == "loading":
        trial[field] = "unknown"
    elif field == "status":
        trial[field] = "incomplete"
    else:
        trial[field] += "foreign"
    refresh_plan(a)
    with pytest.raises(ManagementError, match="execution_trial_binding"):
        governance_v2.validate_assessment(c, v, a)


@pytest.mark.parametrize(
    "change",
    [
        "batch",
        "protocol",
        "policy",
        "batchProtocol",
        "same_run",
        "same_attempt",
        "swapped_units",
        "schema4",
    ],
)
def test_execution_plan_rejects_cross_batch_and_cross_version_splicing(change):
    c, v, a, _ = c2_v2_facts()
    plan = a["executionQualification"]
    if change == "batch":
        plan["batchId"] = "f" * 64
    elif change == "protocol":
        plan["protocol"] = "c2-plan-0"
    elif change == "policy":
        plan["policyDigest"] = "f" * 64
    elif change == "batchProtocol":
        plan["batchProtocol"] = "c2-batch-1"
    elif change == "same_run":
        plan["calibration"]["runId"] = plan["trial"]["runId"]
    elif change == "same_attempt":
        plan["calibration"]["attemptId"] = plan["trial"]["attemptId"]
    elif change == "swapped_units":
        plan["calibration"], plan["trial"] = plan["trial"], plan["calibration"]
    else:
        a["schemaVersion"] = 4
    with pytest.raises(ManagementError):
        governance_v2.validate_assessment(c, v, a)


def test_no_plan_cannot_be_downgraded_to_old_schema_validation():
    c, v, a, _ = c2_v2_facts()
    del a["executionQualification"]
    with pytest.raises(ManagementError):
        governance_v2.validate_assessment(c, v, a)


def test_qualified_plan_does_not_waive_original_branch_timing():
    c, v, a, d = c2_v2_facts()
    d["branches"] = [copy.deepcopy(d["branches"][2])]
    d["branches"][0]["actionRefs"] = ["verify_scoped_reads_decl", "exit"]
    with pytest.raises(ManagementError, match="extension_branch_process_insufficient"):
        result(c, v, a, d)
