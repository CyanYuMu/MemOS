"""Experiment freeze rejects drift and keeps source selection independent of tasks."""

import copy
import json

from types import SimpleNamespace

import pytest

from memos.skill_experiments import manifest as module


def test_raw_selection_is_order_invariant_and_redacts_host_paths():
    facts = [
        {
            "source_id": "b",
            "fact_id": "2",
            "category": "memory",
            "value": "x file:///Users/private/a",
            "value_sha256": "h",
        },
        {
            "source_id": "a",
            "fact_id": "1",
            "category": "contract",
            "value": "Keep tenant scope.",
            "value_sha256": "j",
        },
    ]
    first = module.raw_context({"facts": facts}, 4096)
    assert first == module.raw_context({"facts": list(reversed(facts))}, 4096)
    assert first[1]["selectedFactIds"] == ["1", "2"]
    assert "/Users/" not in first[0] and "file:///" not in first[0]
    assert "Keep tenant scope." in first[0]


def test_raw_selection_never_splits_facts_or_exceeds_capacity():
    facts = [
        {
            "source_id": "a",
            "fact_id": "1",
            "category": "memory",
            "value": "中" * 600,
            "value_sha256": "h",
        }
    ]
    text, selection = module.raw_context({"facts": facts}, 1024)
    assert len(text.encode()) <= 1024
    assert selection["selectedFactIds"] == []
    assert selection["omittedFactIds"] == ["1"]


@pytest.fixture
def freeze_setup(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "candidate.json").write_text('{"frozen":true}')
    loaded = {
        "candidate": {
            "skill_id": "repair",
            "revision": "a" * 64,
            "content_hash": "b" * 64,
            "source_refs": [{"case_id": "source", "lineage_id": "source-l", "source_id": "s"}],
        },
        "manifest": {"manifest_id": "c" * 64},
        "packet": {
            "watermark": ["s"],
            "facts": [
                {
                    "source_id": "s",
                    "fact_id": "f",
                    "category": "memory",
                    "value": "source record",
                    "value_sha256": "h",
                }
            ],
        },
    }
    cases = [
        SimpleNamespace(
            case_id=f"holdout-{i}",
            revision="1",
            split="holdout",
            lineage_id=f"family-{i}",
            related_lineages=(),
            assessment_kind="repair" if i < 3 else "boundary_repair",
        )
        for i in range(7)
    ]
    monkeypatch.setattr(module, "load_candidate", lambda _: copy.deepcopy(loaded))
    monkeypatch.setattr(module, "list_cases", lambda _: cases)
    monkeypatch.setattr(module, "case_digest", lambda c: module.hash_json(c.case_id))
    monkeypatch.setattr(
        module, "export_identity", lambda _: {"exportHash": "d" * 64, "projectionVersion": "test"}
    )
    monkeypatch.setattr(module, "native_skill", lambda _: SimpleNamespace(body="fixed skill"))
    monkeypatch.setattr(
        module,
        "runtime_binding",
        lambda _: {
            "adapterDigest": "e" * 64,
            "model": "fixed-model",
            "timeoutSeconds": 240,
            "maxTurns": 12,
            "maxBudgetUsd": 3,
        },
    )
    monkeypatch.setattr(
        module, "verify_calibration", lambda *args, **kwargs: [{"sha256": "f" * 64}]
    )
    config = SimpleNamespace(timeout_seconds=240, max_turns=12, max_budget_usd=3)
    return candidate, config, cases, loaded


def test_freeze_has_exact_21_units_and_rejects_drift(tmp_path, freeze_setup):
    candidate, config, _, _ = freeze_setup
    output = tmp_path / "experiment"
    frozen = module.freeze_experiment(
        candidate, output, config, project_id="project", calibration_reports=[]
    )
    assert len(frozen["cells"]) == 21
    assert len({(c["caseId"], c["group"]) for c in frozen["cells"]}) == 21
    assert module.load_experiment(output, config) == frozen
    (output / "input/raw-memory.txt").write_text("changed")
    with pytest.raises(ValueError, match="input"):
        module.load_experiment(output, config)


def test_rehashed_manifest_cannot_change_task_asset(tmp_path, freeze_setup):
    candidate, config, _, _ = freeze_setup
    output = tmp_path / "experiment"
    frozen = module.freeze_experiment(
        candidate, output, config, project_id="project", calibration_reports=[]
    )
    frozen["cases"][0]["assetDigest"] = "0" * 64
    payload = {k: v for k, v in frozen.items() if k not in {"manifestHash", "experimentId"}}
    frozen["manifestHash"] = frozen["experimentId"] = module.hash_json(payload)
    (output / "manifest.json").write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match="task"):
        module.load_experiment(output, config)


def test_source_family_overlap_blocks_freeze(tmp_path, freeze_setup):
    candidate, config, cases, _ = freeze_setup
    cases[0].related_lineages = ("source-l",)
    with pytest.raises(ValueError, match="overlap"):
        module.freeze_experiment(
            candidate, tmp_path / "experiment", config, project_id="project", calibration_reports=[]
        )


def test_scoring_runtime_drift_cannot_reuse_frozen_identity(tmp_path, freeze_setup, monkeypatch):
    candidate, config, _, _ = freeze_setup
    output = tmp_path / "experiment"
    module.freeze_experiment(
        candidate, output, config, project_id="project", calibration_reports=[]
    )
    prior = module.runtime_binding(config)
    monkeypatch.setattr(module, "runtime_binding", lambda _: {**prior, "scoringDigest": "changed"})
    with pytest.raises(ValueError, match="runtime_changed"):
        module.load_experiment(output, config)


def test_calibration_requires_three_actual_dev_groups(tmp_path):
    with pytest.raises(ValueError, match="calibration"):
        module.verify_calibration([], SimpleNamespace(), "x")
