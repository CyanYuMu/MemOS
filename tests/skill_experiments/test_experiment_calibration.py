"""Append-only reassessment cannot rewrite or simply trust an earlier score."""

import copy
import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.runner import RunConfig
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import verify_candidate
from memos.skill_experiments import calibration as module
from memos.skill_experiments.manifest import GROUPS, raw_context, runtime_binding, sha


@pytest.fixture
def calibration_setup(tmp_path, monkeypatch):
    root = tmp_path / "calibration"
    root.mkdir()
    candidate = tmp_path / "candidate-artifact"
    candidate.mkdir()
    loaded = {
        "candidate": {"revision": "a" * 64},
        "manifest": {"manifest_id": "b" * 64},
        "packet": {
            "facts": [
                {
                    "source_id": "s",
                    "fact_id": "f",
                    "category": "memory",
                    "value": "source facts",
                    "value_sha256": "c" * 64,
                }
            ]
        },
    }
    monkeypatch.setattr(module, "load_candidate", lambda _: copy.deepcopy(loaded))
    monkeypatch.setattr(module, "export_identity", lambda _: {"exportHash": "d" * 64})
    config = RunConfig(cli_path=Path(sys.executable), model="claude-pinned-test")
    binding = runtime_binding(config)
    raw_hash = raw_context(loaded["packet"], 32768)[1]["sha256"]
    case = get_case("dev-tenant-isolation")
    for group in GROUPS:
        directory = root / group
        directory.mkdir()
        initial = materialize_case(case.case_id, directory / "initial")
        snapshot = SnapshotStore(directory / "snapshots").capture(initial)
        final = materialize_case(case.case_id, directory / "candidate", "reference")
        accepted = verify_candidate(
            case.case_id,
            final,
            directory / "validation",
            run_id="run-" + group,
            attempt_id="attempt",
        )
        receipt = json.loads(accepted.receipt_path.read_text())
        events = directory / "events.jsonl"
        events.write_text("")
        context = {
            "case_id": case.case_id,
            "case_revision": case.revision,
            "lineage_id": case.lineage_id,
            "split": "dev",
            "run_id": "run-" + group,
            "attempt_id": "attempt",
            "initial_snapshot": snapshot.snapshot_id,
        }
        if group == "skill":
            context["skill_revision"] = loaded["candidate"]["revision"]
        report = {
            "group": group,
            "run_context": context,
            "initial_snapshot": snapshot.snapshot_id,
            "candidate_snapshot": receipt["candidate_snapshot"],
            "case_asset_digest": case_digest(case),
            "receipt": str(accepted.receipt_path),
            "receipt_sha256": sha(accepted.receipt_path),
            "events_sha256": sha(events),
            "task_accepted": True,
            "context_matches": True,
            "capture": {
                "status": "complete",
                "native_outcome": "reported_success",
                "reasons": [],
                "rule_declarations": [],
                "native_result": {"total_cost_usd": 0.1},
            },
            "execution": {"returncode": 0, "timed_out": False, "elapsed_seconds": 0.1},
            "loading": {"status": "verified" if group == "skill" else "not_requested"},
            "skill_export": {"export_hash": "d" * 64} if group == "skill" else None,
            "experience": {
                "readObserved": group != "none",
                "sha256": raw_hash if group == "raw_memory" else None,
            },
            "requested_model": config.model,
            "effort": config.effort,
            "max_turns": config.max_turns,
            "max_budget_usd": config.max_budget_usd,
            "timeout_seconds": config.timeout_seconds,
            "adapter_sha256": binding["adapterDigest"],
            "cli_sha256": binding["cliHash"],
        }
        (directory / "report.json").write_text(json.dumps(report))
    (root / "summary.json").write_text(
        json.dumps(
            {
                "purpose": "dev_calibration",
                "results": [
                    {"group": g, "status": "unknown", "scoringVersion": "p4.1"} for g in GROUPS
                ],
            }
        )
    )
    return root, candidate, config, loaded


def create(setup, output):
    root, candidate, config, _ = setup
    return module.create_reassessment(root, candidate, config, output)


def test_append_only_reassessment_reverifies_three_receipts_and_retains_unknown_summary(
    calibration_setup, tmp_path
):
    root, candidate, config, _ = calibration_setup
    original = (root / "summary.json").read_bytes()
    output = tmp_path / "reassessment.json"
    record = create(calibration_setup, output)
    assert all(score["status"] == "passed" for score in record["scores"])
    assert record["originalSummarySha256"] == sha(root / "summary.json")
    assert record["scoringVersion"] == "p4.2" and len(record["reportHashes"]) == 3
    assert (root / "summary.json").read_bytes() == original
    assert module.verify_reassessment(output, root, candidate, config) == record
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        create(calibration_setup, output)
    assert json.loads(output.read_text()) == record


@pytest.mark.parametrize("target", ["summary", "report", "record", "candidate", "scorer"])
def test_changed_bindings_cannot_reuse_reassessment(
    calibration_setup, tmp_path, monkeypatch, target
):
    root, candidate, config, loaded = calibration_setup
    output = tmp_path / "reassessment.json"
    record = create(calibration_setup, output)
    if target == "summary":
        p = root / "summary.json"
        p.write_text(p.read_text() + "\n")
    elif target == "report":
        p = root / "none/report.json"
        p.write_text(p.read_text() + "\n")
    elif target == "record":
        record["scores"][0]["reportedCostUsd"] = 0
        record["recordId"] = hash_json({k: v for k, v in record.items() if k != "recordId"})
        output.write_text(json.dumps(record))
    elif target == "candidate":
        loaded["candidate"]["revision"] = "e" * 64
    else:
        real_binding = module.runtime_binding
        monkeypatch.setattr(
            module, "runtime_binding", lambda cfg: {**real_binding(cfg), "scoringDigest": "f" * 64}
        )
    with pytest.raises(ValueError):
        module.verify_reassessment(output, root, candidate, config)


def test_failed_actual_evidence_cannot_be_reassessed_as_passed(calibration_setup, tmp_path):
    root, _, _, _ = calibration_setup
    task = root / "none/candidate/fake_graph.py"
    task.write_text(task.read_text() + "\n# changed after acceptance\n")
    output = tmp_path / "reassessment.json"
    with pytest.raises(ValueError):
        create(calibration_setup, output)
    assert not output.exists()


def test_rehashed_success_record_still_recomputes_current_scores(
    calibration_setup, tmp_path, monkeypatch
):
    root, candidate, config, _ = calibration_setup
    output = tmp_path / "reassessment.json"
    create(calibration_setup, output)
    real_score = module.score_run
    monkeypatch.setattr(
        module,
        "score_run",
        lambda *args, **kwargs: {**real_score(*args, **kwargs), "status": "unknown"},
    )
    with pytest.raises(ValueError):
        module.verify_reassessment(output, root, candidate, config)
