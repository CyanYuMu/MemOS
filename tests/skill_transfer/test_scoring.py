"""Passing patch checks cannot hide budget/native/capture/loading failures."""

import json

from types import SimpleNamespace

import pytest

from memos.skill_transfer import __main__ as batch


@pytest.mark.parametrize(
    "fault", ["none", "native_budget", "capture", "context", "loading", "receipt", "raw_unread"]
)
def test_scoring_keeps_conformance_separate_from_patch_success(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(
        batch, "check_receipt", lambda *a: SimpleNamespace(matches=fault != "receipt")
    )
    report = {
        "receipt": str(tmp_path / "validation/receipt.json"),
        "capture": {"status": "partial" if fault == "capture" else "complete"},
        "context_matches": fault != "context",
        "execution": {"returncode": 0, "timed_out": False},
        "loading": {"status": "unknown" if fault == "loading" else "verified"},
        "experience": {"readObserved": fault != "raw_unread"},
        "task_status": "passed",
        "task_accepted": True,
    }
    (tmp_path / "report.json").write_text(json.dumps(report))
    event = {
        "kind": "native_result",
        "payload": {
            "is_error": fault == "native_budget",
            "subtype": "error_max_turns" if fault == "native_budget" else "success",
            "total_cost_usd": 0.12,
        },
    }
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n")
    result = batch.score(report, tmp_path, "raw_memory" if fault == "raw_unread" else "skill", 30)
    assert result["status"] == ("passed" if fault == "none" else "unknown")
    assert result["taskAccepted"] is True
    assert result["branchCoverage"] == [] and result["ruleUsageReview"] == "unreviewed"
    assert result["reportedCostUsd"] == 0.12
