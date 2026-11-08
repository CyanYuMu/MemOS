"""Input-plan tests use explicit synthetic bridge responses, never model calls."""

import copy
import json

from pathlib import Path
from types import SimpleNamespace

import pytest

from memos.skill_drafting import sources


@pytest.fixture
def prepared(monkeypatch):
    # Evidence's own cryptographic/run verification is tested by its owner.
    # Here the boundary is whether all planned real readbacks are consumed.
    monkeypatch.setattr(sources, "validate_packet", lambda packet: None)
    evidence = {}
    snapshots = {}
    entries = []
    for index, qualification in enumerate(
        ("qualified_success", "qualified_success", "qualified_failure")
    ):
        key = f"evidence-{index}"
        case = "source-one" if index != 1 else "source-two"
        evidence[key] = {
            "evidence_revision": key,
            "qualification": qualification,
            "case": {
                "case_id": case,
                "split": "source",
                "lineage_id": case,
                "contract": "Observed contract",
            },
            "preflight": {
                "variants": {"initial": {"checks": [{"name": "behavior", "status": "failed"}]}}
            },
            "final": {
                "checks": [{"name": "behavior", "status": "passed" if index < 2 else "failed"}]
            },
            "changes": {"files": [], "text_diffs": []},
            "environment": {"tools": ["Read"]},
            "unknowns": {"failure_attribution": "unknown", "root_cause": "unknown"},
        }
        snapshots[key] = {
            "snapshot_id": f"observation-{index}",
            "memory_id": f"memory-{index}",
            "content_sha256": f"content-{index}",
            "evidence_revision": key,
            "memory": f"Actual readback {index}",
            "evidence_path": key,
            "case_id": case,
            "lineage_id": case,
            "qualification": qualification,
            "split": "source",
            "service_identity": "service",
            "cube_id": "cube",
            "user_id": "owner",
            "namespace": "source-pool",
        }
        entries.append(
            {"evidence_path": key, "evidence_revision": key, "memory_ids": [f"memory-{index}"]}
        )
    monkeypatch.setattr(sources, "verify_evidence", lambda p: copy.deepcopy(evidence[Path(p).name]))

    class Bridge:
        client = SimpleNamespace(service_identity="service")
        cube_id, user_id, namespace = "cube", "owner", "source-pool"
        missing = False
        fail = None

        def __init__(self):
            self.calls = []

        def _row(self, operation):
            if self.missing:
                raise ValueError("unknown_source_operation")
            for item in entries:
                if (
                    sources.hash_json(
                        ["service", "cube", "owner", "source-pool", item["evidence_revision"]]
                    )
                    == operation
                ):
                    return {"memory_ids": json.dumps(item["memory_ids"])}
            raise AssertionError("wrong_operation")

        def ingest(self, path):
            key = Path(path).name
            self.calls.append(key)
            return {
                "status": "unavailable" if self.fail == key else "completed",
                "memory_ids": [snapshots[key]["memory_id"]],
                "snapshots": [key],
            }

        def _read_snapshot(self, path):
            return copy.deepcopy(snapshots[Path(path).name])

    plan = {
        "schema_version": 1,
        "project_id": "project",
        "scope": {
            "service_identity": "service",
            "cube_id": "cube",
            "user_id": "owner",
            "namespace": "source-pool",
        },
        "sources": entries,
    }
    return Bridge(), plan, snapshots, evidence


def test_packet_contains_readback_body_and_failure_without_inflating_cases(prepared):
    bridge, plan, _, _ = prepared
    packet = sources.collect_packet(bridge, plan)
    assert len(packet["sources"]) == 3
    assert len(packet["independent_lineages"]) == 2
    assert {f["value"] for f in packet["facts"] if f["category"] == "memory"} == {
        "Actual readback 0",
        "Actual readback 1",
        "Actual readback 2",
    }
    assert any(f["qualification"] == "qualified_failure" for f in packet["facts"])


def test_failed_input_is_not_silently_omitted(prepared):
    bridge, plan, _, _ = prepared
    bridge.fail = "evidence-2"
    with pytest.raises(ValueError, match="source_readback_unavailable"):
        sources.collect_packet(bridge, plan)


def test_unknown_delivery_cannot_trigger_a_new_memory_post(prepared):
    bridge, plan, _, _ = prepared
    bridge.missing = True
    with pytest.raises(ValueError, match="source_not_registered"):
        sources.collect_packet(bridge, plan)
    assert bridge.calls == []


def test_foreign_scope_is_rejected_before_readback(prepared):
    bridge, plan, _, _ = prepared
    plan["scope"]["cube_id"] = "other"
    with pytest.raises(ValueError, match="source_scope_mismatch"):
        sources.collect_packet(bridge, plan)
    assert bridge.calls == []


def test_recheck_uses_content_not_observation_time(prepared):
    bridge, plan, snapshots, _ = prepared
    packet = sources.collect_packet(bridge, plan)
    snapshots["evidence-0"]["snapshot_id"] = "later-observation"
    sources.recheck_packet(bridge, packet)
    snapshots["evidence-0"]["content_sha256"] = "changed-content"
    with pytest.raises(ValueError, match="source_watermark_changed"):
        sources.recheck_packet(bridge, packet)


def test_one_lineage_or_duplicate_plan_cannot_trigger_distillation(prepared):
    bridge, plan, _, _ = prepared
    plan["sources"] = [plan["sources"][0], plan["sources"][2]]
    with pytest.raises(ValueError, match="insufficient_independent_sources"):
        sources.collect_packet(bridge, plan)
    plan["sources"] = [plan["sources"][0], plan["sources"][0]]
    with pytest.raises(ValueError, match="duplicate_source_plan"):
        sources.collect_packet(bridge, plan)
