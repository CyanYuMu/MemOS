"""New decision transport leaves frozen schema decision enums unchanged."""

import pytest

from memos.skill_governance.domain import validate_judgment_shape as frozen_shape
from memos.skill_management.domain import ManagementError
from memos.skill_p5.governance import validate_judgment_shape


@pytest.mark.parametrize("task", ["healthy_confirmed", "unresolved_confirmed"])
def test_new_shape_is_explicit_and_frozen_shape_still_rejects(task):
    decision = {
        "taskDecision": task,
        "historyDecision": "retain",
        "usageDecision": "correct",
        "applicability": "in_scope",
        "boundary": None if task == "healthy_confirmed" else "unresolved",
        "violations": "none",
        "reason": "Synthetic transport contract test",
        "eventRefs": [],
        "branches": [],
    }
    assert validate_judgment_shape(decision) == decision
    with pytest.raises(ManagementError):
        frozen_shape(decision)
    with pytest.raises(ManagementError):
        validate_judgment_shape({**decision, "actor": "forged"})


@pytest.mark.parametrize("failure", ["selection_before_observation", "target_not_cited"])
def test_entry_claim_requires_one_cited_observation_selection_edit_chain(failure):
    from memos.skill_p5.governance import _validate_claim

    def declaration(rule, phase):
        return {"kind": "rule_declaration", "value": {"display": {"rule_id": rule, "phase": phase}}}

    events = {
        "source": declaration("check_update_contract", "select"),
        "target": declaration("repair_and_verify_update", "select"),
        "observed": {"kind": "tool_completed", "value": {"display": {}}},
        "edit": {"kind": "tool_started", "value": {"display": {"tool_name": "Edit"}}},
        "late": declaration("check_update_contract", "check"),
    }
    if failure == "target_not_cited":
        events = {key: events[key] for key in ("observed", "source", "target", "edit", "late")}
    candidate = {
        "rules": [
            {
                "rule_id": "check_update_contract",
                "branches": [{"target": "repair_and_verify_update"}],
            }
        ]
    }
    assessment = {
        "processVerification": {
            "checkpoints": [
                {
                    "checkIndex": 1,
                    "checks": [{"name": "dream.caller_contract", "status": "failed"}],
                    "completedEvent": "observed",
                }
            ]
        },
        "publicVerification": {"fault": None},
        "checks": [],
    }
    claim = {
        "branchId": "check_update_contract#0",
        "conditionRefs": ["observed"],
        "actionRefs": [key for key in events if failure != "target_not_cited" or key != "target"],
        "checkRefs": ["check:1:dream.caller_contract"],
    }
    with pytest.raises(ManagementError):
        _validate_claim(candidate, assessment, claim, events, {"applicability": "in_scope"})
