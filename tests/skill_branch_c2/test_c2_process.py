"""Ordered native facts, rather than final green checks, prove a repair process."""

import json

from copy import deepcopy

import pytest

from memos.skill_adapter.events import sanitize_public
from memos.skill_branch_c2.process import (
    build_process_evidence,
    initial_checks_match,
    select_intermediate_snapshots,
)
from memos.skill_branch_c2.tasks import get_suite, suite_digest
from memos.skill_branch_validation.runner import _visible_deliveries
from memos.skill_drafting.domain import hash_json


SUITE_ID = "scoped-healthy-dream-c2-v1"
VISIBLE = "mcp__memos_run__run_visible_checks"


class Timeline:
    def __init__(self):
        self.records = []
        self.events = []
        self.calls = 0

    def event(self, kind, call_id, payload):
        index = len(self.events) + 1
        self.events.append(
            {
                "event_id": f"run:attempt:{index:06d}",
                "sequence": index,
                "context": {"run_id": "run", "attempt_id": "attempt"},
                "producer": "native_stream",
                "kind": kind,
                "native_session_id": "session",
                "native_tool_call_id": call_id,
                "payload": sanitize_public(payload),
            }
        )
        return self.events[-1]["event_id"]

    def start(self, name):
        self.calls += 1
        call_id = f"tool-{self.calls}"
        return call_id, self.event("tool_started", call_id, {"tool_name": name, "tool_input": {}})

    def finish(self, call_id, result="done", error=False):
        return self.event("tool_completed", call_id, {"tool_result": result, "is_error": error})

    def edit(self, error=False):
        call_id, reference = self.start("Edit")
        self.finish(call_id, error=error)
        return reference

    def check(self, variant, snapshot=None):
        suite = get_suite(SUITE_ID)
        failures = suite.expected_failures[variant]["public"]
        checks = [
            {"name": row["id"], "status": "failed" if row["id"] in failures else "passed"}
            for row in suite.checks
        ]
        status = "failed" if failures else "passed"
        report = {
            "schemaVersion": 1,
            "protocol": "c2-visible-result-1",
            "suiteId": SUITE_ID,
            "suiteDigest": suite_digest(suite),
            "taskId": suite.task_id,
            "checkMapping": list(suite.checks),
            "checks": checks,
            "groupStatus": {
                group: "failed"
                if any(
                    row["name"].startswith(group + ".") and row["status"] == "failed"
                    for row in checks
                )
                else "passed"
                for group in ("scoped", "dream")
            },
            "status": status,
        }
        index = len(self.records) + 1
        snapshot = snapshot or hash_json(variant)
        result = {
            "exit_code": 1 if failures else 0,
            "stdout": json.dumps(report, sort_keys=True, separators=(",", ":")),
            "stderr": "",
            "truncated": False,
            "timed_out": False,
            "execution_error": None,
            "evidence_ref": {"check_index": index, "candidate_snapshot": snapshot},
        }
        self.records.append(
            {"check_index": index, "candidate_snapshot": snapshot, "result": result}
        )
        call_id, start = self.start(VISIBLE)
        end = self.finish(call_id, [{"type": "text", "text": json.dumps(result)}])
        return start, end

    def process(self):
        deliveries = _visible_deliveries(self.records, self.events, ())
        return build_process_evidence(SUITE_ID, self.records, self.events, deliveries)


def test_observed_dream_regression_retains_each_checkpoint_and_actual_edit_refs():
    timeline = Timeline()
    timeline.check("initial")
    first = timeline.edit()
    timeline.edit()
    _, intermediate = timeline.check("unsafe_update_arguments")
    second = timeline.edit()
    timeline.check("reference")
    result = timeline.process()
    assert result["complete"] and result["issues"] == []
    assert result["cyclesUsed"] == 2 and result["maxCycles"] == 2
    assert not result["budgetExceeded"] and not result["unverifiedEdits"]
    assert [row["cyclesUsed"] for row in result["checkpoints"]] == [0, 1, 2]
    assert result["checkpoints"][1]["completedEvent"] == intermediate
    assert result["checkpoints"][1]["editEventRefs"][0] == first
    assert result["checkpoints"][2]["editEventRefs"] == [second]
    assert result["checkpoints"][1]["recordSha256"] == hash_json(timeline.records[1])
    assert select_intermediate_snapshots(SUITE_ID, timeline.records) == []


def test_one_scoped_edit_pass_keeps_healthy_dream_without_intermediate():
    timeline = Timeline()
    timeline.check("initial")
    timeline.edit()
    timeline.check("reference")
    assert timeline.process()["cyclesUsed"] == 1
    assert timeline.process()["complete"]
    assert select_intermediate_snapshots(SUITE_ID, timeline.records) == []


def test_failed_and_reverted_edits_count_conservatively_but_repeat_checks_do_not():
    timeline = Timeline()
    timeline.check("initial")
    timeline.check("initial")
    timeline.edit(error=True)
    timeline.edit()
    timeline.edit()  # May restore the initial bytes: still an edit pass.
    timeline.check("initial")
    timeline.edit()
    timeline.check("reference")
    result = timeline.process()
    assert result["complete"]
    assert [row["cyclesUsed"] for row in result["checkpoints"]] == [0, 0, 1, 2]


def test_overbudget_is_a_fact_distinct_from_missing_process_evidence():
    timeline = Timeline()
    timeline.check("initial")
    for variant in ("initial", "unsafe_update_arguments", "reference"):
        timeline.edit()
        timeline.check(variant)
    result = timeline.process()
    assert result["complete"] and result["budgetExceeded"]
    assert result["cyclesUsed"] == 3


@pytest.mark.parametrize(
    "change",
    [
        "missing_end",
        "missing_delivery",
        "extra_tool",
        "cross_session",
        "cross_attempt",
        "duplicate_event",
        "check_edit_overlap",
        "edit_check_overlap",
        "before_baseline",
        "after_final",
        "unexplained_change",
        "out_of_order",
        "wrong_result",
    ],
)
def test_ambiguous_or_unaccounted_process_is_incomplete(change):
    timeline = Timeline()
    if change == "before_baseline":
        timeline.edit()
    timeline.check("initial")
    if change == "extra_tool":
        call, _ = timeline.start("Bash")
        timeline.finish(call)
    if change != "unexplained_change":
        timeline.edit()
    timeline.check("reference")
    if change == "missing_end":
        timeline.events.pop(3)
    elif change == "missing_delivery":
        timeline.events.pop()
    elif change == "cross_session":
        timeline.events[2]["native_session_id"] = "other"
    elif change == "cross_attempt":
        timeline.events[2]["context"]["attempt_id"] = "other"
    elif change == "duplicate_event":
        timeline.events.append(deepcopy(timeline.events[-1]))
    elif change == "check_edit_overlap":
        timeline.events[1], timeline.events[2] = timeline.events[2], timeline.events[1]
    elif change == "edit_check_overlap":
        timeline.events[3], timeline.events[4] = timeline.events[4], timeline.events[3]
    elif change == "after_final":
        timeline.edit()
    elif change == "out_of_order":
        timeline.records.reverse()
    elif change == "wrong_result":
        timeline.records[1]["result"]["stdout"] = "invented"
    result = timeline.process()
    assert not result["complete"] and result["issues"]
    if change == "after_final":
        assert result["unverifiedEdits"]


def test_c2_keeps_observed_regressions_without_selecting_c1_intermediates():
    timeline = Timeline()
    timeline.check("initial")
    timeline.edit()
    timeline.check("unsafe_update_arguments")
    timeline.edit()
    timeline.check("reference")
    result = timeline.process()
    assert result["checkpoints"][1]["groupStatus"]["dream"] == "failed"
    assert select_intermediate_snapshots(SUITE_ID, timeline.records) == []


@pytest.mark.parametrize(
    "records,events,deliveries", [(None, [], []), ([], None, []), ([], [], {}), ([None], [], [])]
)
def test_invalid_process_container_types_fail_explicitly(records, events, deliveries):
    with pytest.raises(ValueError, match="process_inputs"):
        build_process_evidence(SUITE_ID, records, events, deliveries)


def test_initial_check_only_preserves_delivery_but_has_no_final_checkpoint():
    timeline = Timeline()
    _, delivered = timeline.check("initial")
    result = timeline.process()
    assert result["complete"] is False
    assert result["issues"] == ["checkpoint_incomplete"]
    assert result["cyclesUsed"] == 0 and not result["unverifiedEdits"]
    assert result["checkpoints"][0]["completedEvent"] == delivered


@pytest.mark.parametrize(
    "change", ["dream_failed", "missing", "extra_scoped", "reorder", "all_green"]
)
def test_initial_contract_requires_exact_scoped_failures_and_healthy_dream(change):
    timeline = Timeline()
    timeline.check("initial")
    checks = timeline.process()["checkpoints"][0]["checks"]
    assert initial_checks_match(SUITE_ID, checks)
    if change == "dream_failed":
        checks[-1]["status"] = "failed"
    elif change == "missing":
        checks.pop()
    elif change == "extra_scoped":
        checks[0]["status"] = "failed"
    elif change == "reorder":
        checks.reverse()
    else:
        for row in checks:
            row["status"] = "passed"
    assert not initial_checks_match(SUITE_ID, checks)
