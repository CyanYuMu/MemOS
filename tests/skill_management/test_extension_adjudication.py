"""Schema three binds branch decisions to delivered checkpoints, not final prose."""

import copy

import pytest

from test_branch_adjudication import branch_facts

from memos.skill_branch_extensions import governance
from memos.skill_branch_extensions.tasks import get_suite, suite_digest
from memos.skill_drafting.domain import hash_json
from memos.skill_governance.repository import _contract_for
from memos.skill_management.domain import ManagementError, validate_report


@pytest.fixture(autouse=True)
def synthetic_collector(monkeypatch):
    monkeypatch.setattr(governance, "_collector_digest", lambda: "6" * 64)


def extension_facts():
    candidate, old_validation, assessment, decision = branch_facts()
    suite = get_suite("combined-c1-v1")
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
        return name

    def check(index):
        event(f"check{index}_start", "tool_started", tool_name="mcp__memos_run__run_visible_checks")
        event(f"check{index}_result", "tool_completed", is_error=False)

    def declare(rule):
        event(rule + "_decl", "rule_declaration", rule_id=rule)

    def edit(name):
        event(name, "tool_started", tool_name="Edit")
        event(name + "_done", "tool_completed", is_error=False)

    check(1)
    declare("confirm_contract")
    declare("repair_scoped_reads")
    edit("edit_scoped")
    check(2)
    declare("verify_scoped_reads")
    declare("repair_dream_update")
    edit("edit_dream")
    check(3)
    declare("verify_dream_update")
    event("exit", "assistant_text", text="Synthetic explicit success exit")
    checks = [{"name": row["id"], "status": "passed"} for row in suite.checks]
    checkpoints = []
    for index in (1, 2, 3):
        rows = copy.deepcopy(checks)
        for row in rows:
            if (index == 1 and row["name"] == "scoped.caller_contract") or (
                index < 3 and row["name"] == "dream.caller_contract"
            ):
                row["status"] = "failed"
        checkpoints.append(
            {
                "checkIndex": index,
                "snapshot": {1: "8", 2: "a", 3: "3"}[index] * 64,
                "recordSha256": str(index) * 64,
                "startedEvent": f"check{index}_start",
                "completedEvent": f"check{index}_result",
                "checks": rows,
                "groupStatus": {
                    "scoped": "failed" if index == 1 else "passed",
                    "dream": "failed" if index < 3 else "passed",
                },
                "cyclesUsed": index - 1,
                "editEventRefs": []
                if index == 1
                else ["edit_scoped" if index == 2 else "edit_dream"],
            }
        )
    assessment.update(
        schemaVersion=3,
        collectorVersion="extension-review-1",
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
            "version": "extension-process-1",
            "complete": True,
            "issues": [],
            "maxCycles": 2,
            "cyclesUsed": 2,
            "budgetExceeded": False,
            "unverifiedEdits": False,
            "checkpoints": checkpoints,
        },
        intermediateVerifications=[
            {
                "snapshot": "a" * 64,
                "checkIndices": [2],
                "receiptSha256": "b" * 64,
                "protocol": "extension-receipt-1",
                "matches": True,
                "status": "failed",
                "checks": copy.deepcopy(checkpoints[1]["checks"]),
            }
        ],
    )
    for field in ("initialVerification", "verification"):
        assessment[field]["protocol"] = "extension-receipt-1"
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
            refs(1, "scoped"),
        ),
        (
            "repair_scoped_reads#0",
            ["check1_result"],
            ["repair_scoped_reads_decl", "edit_scoped"],
            refs(2, "scoped"),
        ),
        (
            "verify_scoped_reads#0",
            ["check2_result"],
            ["verify_scoped_reads_decl", "edit_dream"],
            refs(2) + refs(3),
        ),
        (
            "repair_dream_update#0",
            ["check2_result"],
            ["repair_dream_update_decl", "edit_dream"],
            refs(3),
        ),
        ("verify_dream_update#0", ["check3_result"], ["verify_dream_update_decl", "exit"], refs(3)),
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


def sealed_result(c, v, a, d):
    return governance.adjudicate_assessment(c, v, governance.validate_assessment(c, v, a), d)


def test_three_checkpoints_can_support_five_human_reviewed_branches():
    c, v, a, d = extension_facts()
    result = sealed_result(c, v, a, d)
    assert result["branchCoverage"] == sorted(a["branchCandidates"])
    assert result["repairConfirmed"] and result["newIndependentSamples"] == 0
    assert a["schemaVersion"] == 3 and "assessmentId" not in a
    assert _contract_for(a) is governance


@pytest.mark.parametrize(
    "change",
    [
        "mapping",
        "group",
        "cycle",
        "foreign_snapshot",
        "missing_hidden",
        "duplicate_hidden",
        "foreign_delivery",
        "missing_edit",
        "late_edit",
        "unknown_check",
        "bool_cycle",
        "final_snapshot",
    ],
)
def test_internally_inconsistent_facts_are_rejected(change):
    c, v, a, _ = extension_facts()
    cp = a["processVerification"]["checkpoints"]
    if change == "mapping":
        a["checkMapping"][0]["group"] = "dream"
    elif change == "group":
        cp[1]["groupStatus"]["scoped"] = "failed"
    elif change == "cycle":
        cp[1]["cyclesUsed"] = 0
    elif change == "foreign_snapshot":
        a["intermediateVerifications"][0]["snapshot"] = "c" * 64
    elif change == "missing_hidden":
        a["intermediateVerifications"] = []
    elif change == "duplicate_hidden":
        a["intermediateVerifications"] *= 2
    elif change == "foreign_delivery":
        cp[1]["completedEvent"] = "check3_result"
    elif change == "missing_edit":
        cp[1]["editEventRefs"] = []
    elif change == "late_edit":
        cp[1]["editEventRefs"] = ["edit_dream"]
    elif change == "unknown_check":
        cp[1]["checks"][0]["name"] = "foreign.check"
    elif change == "bool_cycle":
        cp[1]["cyclesUsed"] = True
    else:
        cp[-1]["snapshot"] = "e" * 64
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)


@pytest.mark.parametrize(
    "change",
    [
        "missing_check_ref",
        "final_only_ref",
        "late_declaration",
        "uncited_edit",
        "wrong_rule",
        "hidden_scoped_failure",
        "hidden_other_actions_failure",
        "missing_condition",
        "declaration_only",
        "foreign_branch",
    ],
)
def test_transition_cannot_be_claimed_from_final_success_alone(change):
    c, v, a, d = extension_facts()
    claim = d["branches"][2]
    d["branches"] = [claim]
    if change == "missing_check_ref":
        claim["checkRefs"].pop(0)
    elif change == "final_only_ref":
        claim["checkRefs"] = [r for r in claim["checkRefs"] if r.startswith("check:3:")]
    elif change == "late_declaration":
        declaration = next(e for e in a["events"] if e["id"] == "verify_scoped_reads_decl")
        a["events"].remove(declaration)
        a["events"].append(declaration)
    elif change == "uncited_edit":
        claim["actionRefs"] = ["verify_scoped_reads_decl", "exit"]
    elif change == "wrong_rule":
        next(e for e in a["events"] if e["id"] == "verify_scoped_reads_decl")["value"]["display"][
            "rule_id"
        ] = "confirm_contract"
    elif change.startswith("hidden_"):
        name = (
            "scoped.caller_contract" if change == "hidden_scoped_failure" else "dream.other_actions"
        )
        next(c for c in a["intermediateVerifications"][0]["checks"] if c["name"] == name)[
            "status"
        ] = "failed"
    elif change == "missing_condition":
        claim["conditionRefs"] = ["check3_result"]
    elif change == "declaration_only":
        claim["actionRefs"] = ["verify_scoped_reads_decl"]
    else:
        claim["branchId"] = "confirm_contract#4"
    with pytest.raises(ManagementError):
        sealed_result(c, v, a, d)


def test_behavior_repair_does_not_require_human_coverage():
    c, v, a, d = extension_facts()
    d.update(usageDecision="misused", branches=[])
    assert sealed_result(c, v, a, d)["repairConfirmed"]


def test_process_gaps_retain_facts_but_cannot_confirm_repair():
    c, v, a, d = extension_facts()
    a["processVerification"].update(complete=False, issues=["missing_tool_pair"])
    a["outcome"] = governance.assessment_outcome(a)
    assert a["outcome"] == "incomplete"
    with pytest.raises(ManagementError, match="evidence_insufficient"):
        sealed_result(c, v, a, d)
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[])
    assert not sealed_result(c, v, a, d)["repairConfirmed"]


def test_final_success_in_one_cycle_has_no_intermediate_transition():
    c, v, a, d = extension_facts()
    events = a["events"]
    removed = {"check2_start", "check2_result", "verify_scoped_reads_decl"}
    a["events"] = [event for event in events if event["id"] not in removed]
    checkpoints = a["processVerification"]["checkpoints"]
    final = checkpoints[-1]
    final.update(checkIndex=2, cyclesUsed=1, editEventRefs=["edit_scoped", "edit_dream"])
    a["processVerification"].update(cyclesUsed=1, checkpoints=[checkpoints[0], final])
    a["publicVerification"]["deliveryRefs"] = [
        {key: cp[key] for key in ("checkIndex", "startedEvent", "completedEvent")}
        for cp in (checkpoints[0], final)
    ]
    a["intermediateVerifications"] = []
    d.update(branches=[], eventRefs=[event["id"] for event in a["events"]])
    assert sealed_result(c, v, a, d)["repairConfirmed"]
    d["branches"] = [
        {
            "branchId": "verify_scoped_reads#0",
            "conditionRefs": ["check1_result"],
            "actionRefs": ["repair_dream_update_decl", "edit_dream"],
            "checkRefs": [f"check:2:{row['name']}" for row in a["checks"]],
            "reason": "Attempt to borrow final success",
        }
    ]
    with pytest.raises(ManagementError):
        sealed_result(c, v, a, d)


def test_third_edit_cycle_keeps_repair_fact_but_forbids_correct_usage():
    c, v, a, d = extension_facts()
    process = a["processVerification"]
    checkpoint = copy.deepcopy(process["checkpoints"][-1])
    checkpoint.update(
        checkIndex=4,
        startedEvent="check4_start",
        completedEvent="check4_result",
        cyclesUsed=3,
        editEventRefs=["edit_extra"],
    )
    extra = [
        {"id": "edit_extra", "kind": "tool_started", "value": {"display": {"tool_name": "Write"}}},
        {"id": "extra_done", "kind": "tool_completed", "value": {"display": {"is_error": False}}},
        {
            "id": "check4_start",
            "kind": "tool_started",
            "value": {"display": {"tool_name": "mcp__memos_run__run_visible_checks"}},
        },
        {
            "id": "check4_result",
            "kind": "tool_completed",
            "value": {"display": {"is_error": False}},
        },
    ]
    a["events"][-2:-2] = extra
    process["checkpoints"].append(checkpoint)
    process.update(cyclesUsed=3, budgetExceeded=True)
    a["publicVerification"]["deliveryRefs"].append(
        {key: checkpoint[key] for key in ("checkIndex", "startedEvent", "completedEvent")}
    )
    d.update(branches=[], eventRefs=[event["id"] for event in a["events"]])
    with pytest.raises(ManagementError, match="usage_budget_or_process_unverified"):
        sealed_result(c, v, a, d)
    d["usageDecision"] = "misused"
    assert sealed_result(c, v, a, d)["repairConfirmed"]


@pytest.mark.parametrize("field,value", [("initialExitCode", 0), ("finalExitCode", 1)])
def test_verified_public_exit_codes_must_match_checkpoint_checks(field, value):
    c, v, a, _ = extension_facts()
    a["publicVerification"][field] = value
    a["outcome"] = governance.assessment_outcome(a)
    with pytest.raises(ManagementError, match="extension_public_status"):
        governance.validate_assessment(c, v, a)


def test_error_receipt_with_retained_checks_does_not_prove_hidden_transition():
    c, v, a, d = extension_facts()
    a["intermediateVerifications"][0]["status"] = "error"
    d["branches"] = [d["branches"][2]]
    with pytest.raises(ManagementError, match="extension_branch_process_insufficient"):
        sealed_result(c, v, a, d)


@pytest.mark.parametrize("groups", [{}, {"scoped": "error", "dream": "error"}])
def test_unparseable_public_error_can_be_retained_as_incomplete(groups):
    c, v, a, d = extension_facts()
    process = a["processVerification"]
    process.update(complete=False, issues=["public_result_unverified"])
    process["checkpoints"][1].update(checks=[], groupStatus=groups)
    a["intermediateVerifications"] = []
    a["outcome"] = "incomplete"
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[])
    assert not sealed_result(c, v, a, d)["repairConfirmed"]


def test_single_delivered_baseline_can_be_retained_as_incomplete():
    c, v, a, d = extension_facts()
    a["events"] = a["events"][:2]
    process = a["processVerification"]
    process.update(complete=False, issues=["no_final_check"], cyclesUsed=0)
    process["checkpoints"] = process["checkpoints"][:1]
    a["publicVerification"].update(finalExitCode=1, recordsVerified=False)
    a["publicVerification"]["deliveryRefs"] = a["publicVerification"]["deliveryRefs"][:1]
    a["intermediateVerifications"] = []
    a["outcome"] = "incomplete"
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[], eventRefs=[])
    assert not sealed_result(c, v, a, d)["repairConfirmed"]


@pytest.mark.parametrize("field", ["initialExitCode", "finalExitCode"])
@pytest.mark.parametrize("exit_code", [7, -9])
def test_abnormal_public_exit_is_retained_without_qualification(field, exit_code):
    c, v, a, d = extension_facts()
    a["publicVerification"][field] = exit_code
    a["publicVerification"]["recordsVerified"] = False
    a["processVerification"].update(complete=False, issues=["public_result_error"])
    a["outcome"] = governance.assessment_outcome(a)
    assert a["outcome"] == "incomplete"
    sealed = governance.validate_assessment(c, v, a)
    assert sealed["publicVerification"][field] == exit_code
    with pytest.raises(ManagementError, match="evidence_insufficient"):
        governance.adjudicate_assessment(c, v, sealed, d)
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[])
    result = governance.adjudicate_assessment(c, v, sealed, d)
    assert not result["repairConfirmed"] and not result["branchCoverage"]


@pytest.mark.parametrize("exit_code", [7, -9, True, False])
def test_abnormal_or_boolean_exit_cannot_be_a_complete_public_process(exit_code):
    c, v, a, _ = extension_facts()
    a["publicVerification"]["finalExitCode"] = exit_code
    a["outcome"] = governance.assessment_outcome(a)
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)


@pytest.mark.parametrize("exit_code", [True, False])
def test_boolean_exit_is_invalid_even_for_incomplete_evidence(exit_code):
    c, v, a, _ = extension_facts()
    a["publicVerification"]["finalExitCode"] = exit_code
    a["processVerification"].update(complete=False, issues=["public_result_error"])
    a["outcome"] = governance.assessment_outcome(a)
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)
