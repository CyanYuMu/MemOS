"""Fixed host plans preserve task bytes while moving fork ownership to the host."""

import hashlib

import pytest

from memos.skill_evaluation.tasks import get_case, list_cases
from memos.skill_verification.plans import acceptance_plan, digest_json, visible_plan


def test_fixture_acceptance_preserves_contract_checks_and_runs_all_four_orders():
    case = get_case("holdout-fixture")
    original = case.oracle_source
    plan = acceptance_plan(case.case_id)
    assert len(plan["phases"]) == 5
    oracle, *orders = plan["phases"]
    assert oracle["program"] == original.replace(
        '    check("fixture_isolation", fixture_isolation)\n', ""
    )
    assert plan["source_sha256"] == hashlib.sha256(original.encode()).hexdigest()
    assert [len(phase["arguments"]) - 2 for phase in orders] == [1, 1, 2, 2]
    assert orders[-1]["arguments"][2:] == list(reversed(orders[-2]["arguments"][2:]))
    assert all("--rootdir=" in phase["program"] for phase in orders)
    assert all("--confcutdir=" in phase["program"] for phase in orders)
    assert all("ExecutionAudit" in phase["program"] for phase in orders)
    assert get_case(case.case_id).oracle_source == original


@pytest.mark.parametrize("case", list_cases("holdout"), ids=lambda c: c.case_id)
def test_plan_identity_binds_all_effective_input_and_non_fixture_oracle_is_unchanged(case):
    plan = acceptance_plan(case.case_id)
    assert plan["plan_digest"] == digest_json({k: v for k, v in plan.items() if k != "plan_digest"})
    assert plan == acceptance_plan(case.case_id)
    if case.case_id != "holdout-fixture":
        assert len(plan["phases"]) == 1
        assert plan["phases"][0]["program"] == case.oracle_source
    plan["phases"][0]["program"] += "\n# changed\n"
    assert plan["plan_digest"] != digest_json({k: v for k, v in plan.items() if k != "plan_digest"})


def test_fixture_visible_plan_only_uses_registered_public_test_content():
    case = get_case("holdout-fixture")
    plan = visible_plan(case.case_id)
    assert len(plan["phases"]) == 1
    phase = plan["phases"][0]
    assert phase["files"] == {"test_visible.py": case.initial_files["test_visible.py"]}
    assert "fixture_isolation" not in phase["program"]
    assert "test_external.py" not in str(plan)
    assert all("test_visible.py::test_" in arg for arg in phase["arguments"][2:])


def test_unregistered_task_cannot_request_a_plan():
    with pytest.raises((KeyError, ValueError)):
        acceptance_plan("arbitrary-command")
