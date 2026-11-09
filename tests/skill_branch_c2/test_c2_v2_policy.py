"""Budget qualification cannot accept missing or contradictory report fields."""

import pytest

from memos.skill_branch_c2.execution_policy import policy_identity, report_eligibility


def report():
    return {
        "status": "passed",
        "task_accepted": True,
        "group": "none",
        "execution_policy": policy_identity(),
        "capture": {"status": "complete"},
        "loading": {"status": "not_requested"},
        "process_evidence": {
            "complete": True,
            "maxCycles": 2,
            "cyclesUsed": 1,
            "budgetExceeded": False,
            "unverifiedEdits": False,
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        "bool_count",
        "bool_limit",
        "missing_process",
        "overbudget",
        "flag_disagrees",
        "incomplete",
        "tail_edit",
        "wrong_loading",
        "wrong_policy",
        "wrong_group",
        "missing_flag",
    ],
)
def test_invalid_or_overbudget_report_never_qualifies(change):
    value = report()
    assert report_eligibility(value, "none")["qualified"]
    process = value["process_evidence"]
    if change == "bool_count":
        process["cyclesUsed"] = True
    elif change == "bool_limit":
        process["maxCycles"] = True
    elif change == "missing_process":
        value.pop("process_evidence")
    elif change == "overbudget":
        process.update(cyclesUsed=3, budgetExceeded=True)
    elif change == "flag_disagrees":
        process.update(cyclesUsed=3, budgetExceeded=False)
    elif change == "incomplete":
        process["complete"] = False
    elif change == "tail_edit":
        process["unverifiedEdits"] = True
    elif change == "wrong_loading":
        value["loading"]["status"] = "verified"
    elif change == "wrong_policy":
        value["execution_policy"]["digest"] = "a" * 64
    elif change == "wrong_group":
        value["group"] = "skill"
    else:
        process.pop("budgetExceeded")
    result = report_eligibility(value, "none")
    assert not result["qualified"] and result["issues"]
    assert value["status"] == "passed"
