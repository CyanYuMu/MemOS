from copy import deepcopy

import pytest

from memos.skill_release.execution_policy import task_outcome
from memos.skill_release.tasks import get_suite


def facts(suite_id, *, changed=False):
    task = get_suite(suite_id)
    initial = [
        {
            "name": row["id"],
            "status": "failed"
            if row["id"] in task.expected_failures["initial"]["hidden"]
            else "passed",
        }
        for row in task.checks
    ]
    final = [
        {
            "name": row["id"],
            "status": "failed"
            if row["id"] in task.expected_failures["reference"]["hidden"]
            else "passed",
        }
        for row in task.checks
    ]
    if task.outcome in {"out_of_scope", "tool_failure"}:
        final = deepcopy(initial)
    return {
        "initialChecks": initial,
        "finalChecks": final,
        "initialStatus": "failed" if any(r["status"] == "failed" for r in initial) else "passed",
        "finalStatus": "failed" if any(r["status"] == "failed" for r in final) else "passed",
        "initialSnapshot": "a" * 64,
        "finalSnapshot": ("b" if changed else "a") * 64,
        "changed": changed,
        "receiptsMatch": True,
        "publicVerified": True,
        "faultVerified": task.fault is not None,
        "publicInitialChecks": deepcopy(initial),
        "publicFinalChecks": [] if task.fault else deepcopy(final),
        "processComplete": True,
        "budgetExceeded": False,
        "unverifiedEdits": False,
    }


@pytest.mark.parametrize(
    "suite,changed",
    [
        ("update-repair-r1", True),
        ("update-healthy-r1", False),
        ("scope-outside-r1", False),
        ("contract-missing-r1", False),
        ("update-unresolved-r1", True),
        ("check-unavailable-r1", False),
    ],
)
def test_each_fixed_outcome_requires_its_own_actual_facts(suite, changed):
    result = task_outcome(suite, facts(suite, changed=changed))
    assert result == {"kind": get_suite(suite).outcome, "eligible": True, "reasons": []}


@pytest.mark.parametrize(
    "key,value",
    [
        ("receiptsMatch", False),
        ("publicVerified", False),
        ("processComplete", False),
        ("budgetExceeded", True),
        ("unverifiedEdits", True),
        ("publicFinalChecks", []),
        ("initialSnapshot", "b" * 64),
    ],
)
def test_green_final_without_binding_and_process_is_not_qualification(key, value):
    observed = facts("update-repair-r1", changed=True)
    observed[key] = value
    assert task_outcome("update-repair-r1", observed)["eligible"] is False


def test_health_change_and_unobserved_fault_cannot_be_confirmed():
    observed = facts("update-healthy-r1", changed=True)
    assert not task_outcome("update-healthy-r1", observed)["eligible"]
    observed = facts("check-unavailable-r1")
    observed["faultVerified"] = False
    assert not task_outcome("check-unavailable-r1", observed)["eligible"]


def test_remaining_failure_must_be_reproduced_by_public_and_hidden_checks():
    observed = facts("update-unresolved-r1", changed=True)
    observed["publicFinalChecks"] = facts("update-healthy-r1")["finalChecks"]
    assert not task_outcome("update-unresolved-r1", observed)["eligible"]


@pytest.mark.parametrize("suite", ["update-unresolved-r1", "check-unavailable-r1"])
def test_stop_cannot_hide_new_business_regressions_or_substitute_scoped_failure(suite):
    observed = facts(suite, changed=True)
    observed["finalChecks"] = [
        {
            "name": row["name"],
            "status": "failed" if row["name"] == "scoped.caller_contract" else "passed",
        }
        for row in observed["finalChecks"]
    ]
    observed["finalStatus"] = "failed"
    if suite == "update-unresolved-r1":
        observed["publicFinalChecks"] = deepcopy(observed["finalChecks"])
    assert not task_outcome(suite, observed)["eligible"]
