"""Check provenance mapping independently of Neo4j and model generation."""

import copy
import json

from pathlib import Path
from types import SimpleNamespace

import pytest

from memos.skill_drafting import graph_bundle


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    evidence_path = tmp_path / "source-evidence.json"
    snapshot_path = tmp_path / "source-snapshot.json"
    evidence = {
        "evidence_revision": "e" * 64,
        "qualification": "qualified_success",
        "case": {
            "case_id": "source-example",
            "revision": "1",
            "lineage_id": "lineage",
            "split": "source",
            "contract": "Strict scope",
            "asset_digest": "d" * 64,
        },
        "context": {"run_id": "run", "attempt_id": "attempt"},
        "environment": {"cli_version": "test", "requested_model": "fixture-model"},
        "final": {"status": "passed"},
    }
    snapshot = {
        "snapshot_id": "f" * 64,
        "memory_id": "memory",
        "content_sha256": "c" * 64,
        "body_sha256": "b" * 64,
        "evidence_revision": evidence["evidence_revision"],
        "memory": "actual readback fixture",
        "sources": [{"content": "actual readback fixture"}],
        "observed_at": "2026-01-01T00:00:00Z",
        "service_identity": "service",
        "cube_id": "cube",
        "user_id": "owner",
        "namespace": "source",
        "evidence_path": str(evidence_path),
        "snapshot_path": str(snapshot_path),
    }
    source = {"source_id": "source", "snapshot": snapshot, "evidence": evidence}
    evidence_path.write_text(json.dumps(evidence))
    snapshot_path.write_text(
        json.dumps({k: v for k, v in snapshot.items() if k != "snapshot_path"})
    )
    link = {
        "link_id": "l" * 64,
        "fact_id": "fact",
        "source_id": "source",
        "target_kind": "EvidenceRevision",
        "target_id": evidence["evidence_revision"],
        "locator": "/case/contract",
        "value_sha256": "d" * 64,
        "kind": "precondition",
        "claim": "Read the contract",
        "producer": "model",
        "semantic_review": "pending",
    }
    memory_link = {
        **link,
        "link_id": "m" * 64,
        "target_kind": "MemorySnapshot",
        "target_id": snapshot["snapshot_id"],
        "locator": "/memory",
    }
    rule = {"rule_id": "R1", "evidence_links": [link, memory_link]}
    candidate = {
        "skill_id": "scope-repair",
        "revision": "a" * 64,
        "project_id": "project",
        "status": "candidate",
        "type": "procedure",
        "purpose": "repair",
        "title": "Scoped repair",
        "requires": [],
        "rules": [rule],
        "content_hash": "b" * 64,
    }
    packet = {"project_id": "project", "packet_id": "p" * 64, "sources": [source]}
    validation = {"validation_id": "v" * 64, "status": "passed", "semantic_review": "pending"}
    data = {
        "candidate": candidate,
        "packet": packet,
        "validation": validation,
        "manifest": {"fixture": True},
    }
    for name, value in (
        ("candidate.json", candidate),
        ("packet.json", packet),
        ("validation.json", validation),
    ):
        (tmp_path / name).write_text(json.dumps(value))
    monkeypatch.setattr(graph_bundle, "load_candidate", lambda directory: copy.deepcopy(data))
    monkeypatch.setattr(graph_bundle, "recheck_packet", lambda bridge, packet: None)
    return tmp_path, data


def test_bundle_preserves_rule_memory_and_original_evidence_links(loaded):
    directory, data = loaded
    nodes, edges = graph_bundle.build_bundle(directory)
    assert "Release" not in {n["kind"] for n in nodes}
    rule = next(n for n in nodes if n["kind"] == "Rule")
    assert rule["payload"]["memory_citations"][0]["target_kind"] == "MemorySnapshot"
    links = [e for e in edges if e["kind"] == "EVIDENCE_LINK"]
    assert len(links) == 1 and links[0]["target"]["kind"] == "EvidenceRevision"
    assert links[0]["payload"]["locator"] == "/case/contract"
    assert {"DERIVED_FROM", "REFERENCES", "FROM_RUN", "OF_CASE", "EVALUATES"} <= {
        e["kind"] for e in edges
    }
    validation = next(n for n in nodes if n["kind"] == "Validation")
    assert validation["payload"]["kind"] == "structural"


def test_foreign_project_cannot_import_even_valid_artifacts(loaded):
    directory, _ = loaded
    repo = SimpleNamespace(
        project_id="foreign", store_bundle=lambda *a: pytest.fail("must not write")
    )
    with pytest.raises(ValueError, match="candidate_project_mismatch"):
        graph_bundle.import_candidate(directory, object(), repo)


def test_changed_sources_prevent_graph_import(loaded, monkeypatch):
    directory, _ = loaded

    def changed(*args):
        raise ValueError("source_watermark_changed")

    monkeypatch.setattr(graph_bundle, "recheck_packet", changed)
    repo = SimpleNamespace(
        project_id="project", store_bundle=lambda *a: pytest.fail("must not write")
    )
    with pytest.raises(ValueError, match="source_watermark_changed"):
        graph_bundle.import_candidate(directory, object(), repo)


@pytest.mark.parametrize("target", ["snapshot_path", "evidence_path"])
def test_source_artifact_must_match_frozen_packet_before_graph_write(loaded, target):
    directory, data = loaded
    Path(data["packet"]["sources"][0]["snapshot"][target]).write_text('{"changed":true}')
    repo = SimpleNamespace(
        project_id="project", store_bundle=lambda *a: pytest.fail("must not write")
    )
    with pytest.raises(ValueError, match="graph_bundle_artifact_mismatch"):
        graph_bundle.import_candidate(directory, object(), repo)
