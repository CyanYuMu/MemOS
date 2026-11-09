"""Both arms are independently reread; report flags never establish plan eligibility."""

import hashlib
import json
import sys

import pytest

from c2_v2_run_fixture import make_pair, write_json

from memos.skill_branch_c2 import collector_v2 as collector
from memos.skill_drafting.domain import hash_json


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolated checks")


def collect(f):
    return collector.collect_trial(
        f["candidate"],
        f["export_hash"],
        f["run"],
        batch_directory=f["batch"],
        qualification_directory=f["qualification"],
        allowed_root=f["allowed"],
    )


@pytest.fixture
def pair(tmp_path, monkeypatch):
    f = make_pair(tmp_path, monkeypatch)
    monkeypatch.setattr(collector, "require_qualification", lambda *a, **k: f["proof"])
    return f


def test_two_real_isolated_synthetic_runs_are_bound_without_coverage(pair):
    result = collect(pair)
    a = result["assessment"]
    q = a["executionQualification"]
    assert a["schemaVersion"] == 5 and a["collectorVersion"] == "c2-review-2"
    assert q["planQualified"] and q["planIssues"] == []
    assert q["calibration"]["cyclesUsed"] == q["trial"]["cyclesUsed"] == 1
    assert q["trial"]["reportSha256"] == a["reportSha256"]
    assert q["calibration"]["runId"] != a["runId"]
    assert result["validation"]["branchCoverage"] == []


@pytest.mark.parametrize("arm", ["calibration", "trial"])
def test_overbudget_behavior_pass_retained_but_plan_disqualified(tmp_path, monkeypatch, arm):
    f = make_pair(tmp_path, monkeypatch, **{arm + "_options": {"over_budget": True}})
    monkeypatch.setattr(collector, "require_qualification", lambda *a, **k: f["proof"])
    a = collect(f)["assessment"]
    q = a["executionQualification"]
    assert a["outcome"] == "repair_candidate"
    assert q[arm]["status"] == "passed"
    assert not q["planQualified"]
    assert arm + ":repair_budget_exceeded" in q["planIssues"]


@pytest.mark.parametrize(
    "change",
    [
        "forged_cycles",
        "wrong_model",
        "wrong_cli",
        "wrong_group",
        "wrong_policy",
        "missing_delivery",
        "summary_green",
        "different_batch",
    ],
)
def test_calibration_is_reread_even_with_unchanged_skill_and_rehashed_report(pair, change):
    root = pair["calibration"]["run"]
    path = root / "report.json"
    r = json.loads(path.read_text())
    if change == "forged_cycles":
        r["process_evidence"]["cyclesUsed"] = 0
    elif change == "wrong_model":
        events_path = root / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        event = next(e for e in events if e["kind"] == "session_init")
        event["payload"]["model"] = "wrong"
        r["capture"]["discovered"] = [event]
        events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
        r["events_sha256"] = hashlib.sha256(events_path.read_bytes()).hexdigest()
    elif change == "wrong_cli":
        r["cli_version"] = "0.0.0"
    elif change == "wrong_group":
        r["group"] = "skill"
    elif change == "wrong_policy":
        r["execution_policy"]["digest"] = "f" * 64
    elif change == "missing_delivery":
        r["visible_delivery_refs"] = []
    elif change == "summary_green":
        (pair["batch"] / "summary.json").write_text('{"success":true,"planQualified":true}')
        r["process_evidence"]["budgetExceeded"] = True
    else:
        manifest_path = pair["batch"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["batchNonce"] = "f" * 32
        manifest["experimentId"] = hash_json(
            {k: v for k, v in manifest.items() if k != "experimentId"}
        )
        write_json(manifest_path, manifest)
        # Rewriting the outer report cannot rewrite the batch embedded in native
        # event and receipt identities, even when the rest of the plan is equal.
        r["experiment_id"] = manifest["experimentId"]
    write_json(path, r)
    with pytest.raises(ValueError):
        collect(pair)
