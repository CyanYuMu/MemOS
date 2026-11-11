"""Unit plan, scoring and immutability of the none-vs-skill boundary comparison."""

import json

from types import SimpleNamespace

import pytest

from memos.skill_boundary import experiment
from memos.skill_p5.tasks import SUITES


def test_plan_runs_both_arms_on_every_suite_with_counterbalanced_order():
    units = experiment.plan_units(2)
    assert len(units) == len(SUITES) * 2 * 2
    assert len({u["id"] for u in units}) == len(units)
    for rep in (1, 2):
        rows = [u for u in units if u["repetition"] == rep]
        for suite in SUITES:
            assert [u["group"] for u in rows if u["suiteId"] == suite] in (
                ["none", "skill"],
                ["skill", "none"],
            )
        firsts = [next(u["group"] for u in rows if u["suiteId"] == s) for s in SUITES]
        assert firsts.count("none") == firsts.count("skill")
    # A repetition swaps the arm that runs first on each suite.
    first = {(u["suiteId"]): u["group"] for u in reversed(units) if u["repetition"] == 1}
    second = {(u["suiteId"]): u["group"] for u in reversed(units) if u["repetition"] == 2}
    assert all(first[s] != second[s] for s in SUITES)


@pytest.mark.parametrize("value", [0, 4, True, "1", None])
def test_repetitions_are_bounded(value):
    with pytest.raises(ValueError, match="boundary_invalid_repetitions"):
        experiment.plan_units(value)


def _report(
    group,
    *,
    status="passed",
    accepted=True,
    capture="complete",
    loading=None,
    reasons=(),
    capture_reasons=(),
    process_issues=(),
):
    return {
        "group": group,
        "status": status,
        "task_accepted": accepted,
        "task_outcome": {"kind": "healthy", "eligible": accepted, "reasons": list(reasons)},
        "capture": {"status": capture, "reasons": list(capture_reasons)},
        "loading": {"status": loading or ("verified" if group == "skill" else "not_requested")},
        "process_evidence": {
            "complete": not process_issues,
            "issues": list(process_issues),
            "maxCycles": 2,
            "cyclesUsed": 1,
            "budgetExceeded": False,
            "unverifiedEdits": False,
        },
        "execution_policy": experiment.policy_identity(),
    }


def _write_run(tmp_path, report, cost=0.12):
    (tmp_path / "report.json").write_text(json.dumps(report))
    event = {
        "kind": "native_result",
        "payload": {
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": cost,
            "num_turns": 5,
        },
    }
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n")
    return tmp_path / "report.json"


@pytest.mark.parametrize("group", ["none", "skill"])
def test_compliant_runs_keep_behavioural_pass_or_fail(tmp_path, group):
    passed = experiment.score(_write_run(tmp_path, _report(group)), group, 10)
    assert passed["status"] == "passed" and passed["executionIssues"] == []
    assert passed["reportedCostUsd"] == 0.12 and passed["numTurns"] == 5
    report = _report(group, status="failed", accepted=False, reasons=["health_unproven"])
    failed = experiment.score(_write_run(tmp_path, report), group, 10)
    assert failed["status"] == "failed"
    assert failed["outcomeReasons"] == ["health_unproven"]


@pytest.mark.parametrize(
    "fault",
    [
        {"capture": "partial"},
        {"loading": "unknown"},
        {"status": "unknown"},
    ],
)
def test_noncompliant_execution_is_unknown_not_a_behavioural_failure(tmp_path, fault):
    report = _report("skill", **fault)
    result = experiment.score(_write_run(tmp_path, report), "skill", 10)
    assert result["status"] == "unknown"


def test_none_arm_that_loaded_a_skill_is_not_comparable(tmp_path):
    result = experiment.score(_write_run(tmp_path, _report("none", loading="verified")), "none", 1)
    assert result["status"] == "unknown"
    assert "loading_unverified" in result["executionIssues"]


def test_aggregate_counts_discordant_pairs_without_dropping_unknowns():
    rows = [
        {"suiteId": "a", "repetition": 1, "group": "none", "status": "failed"},
        {"suiteId": "a", "repetition": 1, "group": "skill", "status": "passed"},
        {"suiteId": "b", "repetition": 1, "group": "none", "status": "passed"},
        {"suiteId": "b", "repetition": 1, "group": "skill", "status": "passed"},
        {"suiteId": "c", "repetition": 1, "group": "none", "status": "unknown"},
        {"suiteId": "c", "repetition": 1, "group": "skill", "status": "passed"},
    ]
    for row in rows:
        row.update(elapsedSeconds=1.0, reportedCostUsd=0.1)
    summary = experiment.aggregate(rows)
    groups = {g["group"]: g for g in summary["groups"]}
    assert groups["none"] == {
        "group": "none",
        "total": 3,
        "passed": 1,
        "failed": 1,
        "unknown": 1,
        "passedIgnoringProcess": 0,
        "processIssueRuns": 0,
        "elapsedSeconds": 3.0,
        "reportedCostUsd": 0.3,
    }
    assert summary["pairs"] == {
        "comparable": 2,
        "bothPassed": 1,
        "bothFailed": 0,
        "skillOnlyPassed": 1,
        "noneOnlyPassed": 0,
        "withUnknown": 1,
    }


def test_run_refuses_reuse_and_never_touches_management(tmp_path, monkeypatch):
    identity = {"experimentId": "e" * 64, "revision": "r" * 64, "units": [], "batchNonce": "n" * 32}
    monkeypatch.setattr(
        experiment,
        "binding",
        lambda *a, **k: (dict(identity), SimpleNamespace(), None, None),
    )
    output = tmp_path / "batch"
    experiment.freeze("cfg", "cand", "qual", output, repetitions=1)
    with pytest.raises(FileExistsError):
        experiment.freeze("cfg", "cand", "qual", output, repetitions=1)
    monkeypatch.setattr(
        experiment, "run_extension_task", lambda *a, **k: pytest.fail("unexpected run")
    )
    first = experiment.run("cfg", "cand", "qual", output)
    assert first["complete"] is True and first["publicationEvidence"] is False
    assert first["managementTickets"] is False
    with pytest.raises(FileExistsError):
        experiment.run("cfg", "cand", "qual", output)


def test_changed_inputs_block_the_batch_before_any_agent_run(tmp_path, monkeypatch):
    frozen = {"experimentId": "e" * 64, "units": [], "batchNonce": "n" * 32}
    monkeypatch.setattr(
        experiment, "binding", lambda *a, **k: (dict(frozen), SimpleNamespace(), None, None)
    )
    output = tmp_path / "batch"
    experiment.freeze("cfg", "cand", "qual", output, repetitions=1)
    changed = {**frozen, "experimentId": "f" * 64}
    monkeypatch.setattr(
        experiment, "binding", lambda *a, **k: (changed, SimpleNamespace(), None, None)
    )
    with pytest.raises(ValueError, match="boundary_inputs_changed"):
        experiment.run("cfg", "cand", "qual", output)
    assert not (output / "started.json").exists()


@pytest.mark.parametrize(("extra", "expected"), [((), "passed"), (("repair_unproven",), "failed")])
def test_process_order_gap_is_unknown_but_patch_result_stays_visible(tmp_path, extra, expected):
    report = _report(
        "none",
        status="incomplete",
        accepted=False,
        capture="partial",
        capture_reasons=["process_evidence_incomplete"],
        process_issues=["check_edit_overlap"],
        reasons=["process_incomplete", *extra],
    )
    result = experiment.score(_write_run(tmp_path, report), "none", 1)
    assert result["status"] == "unknown"
    assert result["processIssues"] == ["check_edit_overlap"]
    assert result["outcomeIgnoringProcess"] == expected


def test_other_capture_gaps_never_get_a_secondary_result(tmp_path):
    report = _report(
        "skill",
        status="incomplete",
        accepted=False,
        capture="partial",
        capture_reasons=["native_result_missing"],
        reasons=["process_incomplete"],
    )
    result = experiment.score(_write_run(tmp_path, report), "skill", 1)
    assert result["status"] == "unknown" and result["outcomeIgnoringProcess"] is None


def test_secondary_pairs_use_the_process_independent_result():
    rows = [
        {
            "suiteId": "a",
            "repetition": 1,
            "group": "none",
            "status": "unknown",
            "outcomeIgnoringProcess": "passed",
        },
        {
            "suiteId": "a",
            "repetition": 1,
            "group": "skill",
            "status": "passed",
            "outcomeIgnoringProcess": "passed",
        },
    ]
    summary = experiment.aggregate(rows)
    assert summary["pairs"]["withUnknown"] == 1
    assert summary["pairsIgnoringProcess"]["bothPassed"] == 1
    groups = {g["group"]: g for g in summary["groups"]}
    assert groups["none"]["passedIgnoringProcess"] == 1
    assert groups["none"]["processIssueRuns"] == 0
