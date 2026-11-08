"""Map verified candidate artifacts to provenance edges, without publication."""

from __future__ import annotations

import hashlib
import json

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_drafting.graph import edge_key, node_key
from memos.skill_drafting.sources import recheck_packet


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("graph_bundle_artifact_mismatch")
        value[key] = item
    return value


def _ref(path: Path, expected: dict) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("graph_bundle_artifact_mismatch")
    path = path.resolve()
    raw = path.read_bytes()
    # An intact packet cannot justify a URI whose original file has since changed.
    # Bind the artifact hash to the exact bytes whose document we compare here.
    try:
        actual = json.loads(raw, object_pairs_hook=_unique_object)
        if hash_json(actual) != hash_json(expected):
            raise ValueError("graph_bundle_artifact_mismatch")
    except (ValueError, TypeError, RecursionError):
        raise ValueError("graph_bundle_artifact_mismatch") from None
    return {"uri": path.as_uri(), "sha256": hashlib.sha256(raw).hexdigest()}


def _build(directory: Path, loaded: dict) -> tuple[list[dict], list[dict]]:
    candidate, packet = loaded["candidate"], loaded["packet"]
    nodes, edges = {}, {}

    def node(kind: str, payload: dict) -> dict:
        key = node_key(kind, payload)
        value = {"kind": kind, "key": key, "payload": payload}
        identity = (kind, key)
        if identity in nodes and nodes[identity] != value:
            raise ValueError("graph_bundle_identity_conflict")
        nodes[identity] = value
        return {"kind": kind, "key": key}

    def edge(kind: str, source: dict, target: dict, payload: dict | None = None) -> None:
        payload = {} if payload is None else payload
        key = edge_key(kind, source, target, payload)
        value = {"kind": kind, "key": key, "source": source, "target": target, "payload": payload}
        identity = (kind, key)
        if identity in edges and edges[identity] != value:
            raise ValueError("graph_bundle_identity_conflict")
        edges[identity] = value

    skill = node("Skill", {"skill_id": candidate["skill_id"], "type": "procedure"})
    revision = node(
        "SkillRevision",
        {
            "skill_id": candidate["skill_id"],
            "revision": candidate["revision"],
            "status": "candidate",
            "requires": [],
            "purpose": candidate["purpose"],
            "title": candidate["title"],
            "candidate": candidate,
            "artifact": _ref(directory / "candidate.json", candidate),
            "input_artifact": _ref(directory / "packet.json", packet),
        },
    )
    edge("HAS_REVISION", skill, revision)
    validation_report = loaded["validation"]
    validation = node(
        "Validation",
        {
            "validation_id": validation_report["validation_id"],
            "kind": "structural",
            "status": "passed",
            "semantic_review": "pending",
            "independent_task_validation": "not_run",
            "report": validation_report,
            "artifact": _ref(directory / "validation.json", validation_report),
        },
    )
    edge("EVALUATES", validation, revision)
    source_evidence = {}
    for source in packet["sources"]:
        snapshot, evidence = source["snapshot"], source["evidence"]
        case, context = evidence["case"], evidence["context"]
        case_node = node(
            "CaseRevision",
            {
                "case_id": case["case_id"],
                "case_revision": case["revision"],
                "lineage_id": case["lineage_id"],
                "split": case["split"],
                "contract": case["contract"],
                "asset_digest": case["asset_digest"],
            },
        )
        run_node = node(
            "Run",
            {
                "run_id": context["run_id"],
                "attempt_id": context["attempt_id"],
                "case_id": case["case_id"],
                "case_revision": case["revision"],
                "agent": "claude_code",
                "model": evidence["environment"]["requested_model"],
                "cli_version": evidence["environment"]["cli_version"],
                "group": "source",
                "task_status": evidence["final"]["status"],
                "qualification": evidence["qualification"],
            },
        )
        evidence_node = node(
            "EvidenceRevision",
            {
                "evidence_revision": evidence["evidence_revision"],
                "run_id": context["run_id"],
                "attempt_id": context["attempt_id"],
                "case_id": case["case_id"],
                "case_revision": case["revision"],
                **_ref(Path(snapshot["evidence_path"]), evidence),
                "qualification": evidence["qualification"],
                "producer": "trusted_local_collector",
            },
        )
        memory_node = node(
            "MemorySnapshot",
            {
                **{
                    key: snapshot[key]
                    for key in (
                        "snapshot_id",
                        "memory_id",
                        "content_sha256",
                        "body_sha256",
                        "observed_at",
                        "service_identity",
                        "cube_id",
                        "user_id",
                        "namespace",
                        "memory",
                        "sources",
                    )
                },
                "evidence_revision": evidence["evidence_revision"],
                "artifact": _ref(
                    Path(snapshot["snapshot_path"]),
                    {k: v for k, v in snapshot.items() if k != "snapshot_path"},
                ),
            },
        )
        edge("OF_CASE", run_node, case_node)
        edge("FROM_RUN", evidence_node, run_node)
        edge("USES_RUN", validation, run_node, {"usage": "source_evidence_check"})
        edge("REFERENCES", memory_node, evidence_node)
        edge("DERIVED_FROM", revision, memory_node)
        source_evidence[source["source_id"]] = evidence_node
    for rule in candidate["rules"]:
        rule_node = node(
            "Rule",
            {
                "skill_id": candidate["skill_id"],
                "revision": candidate["revision"],
                "rule_id": rule["rule_id"],
                "rule": rule,
                # Frozen EVIDENCE_LINK endpoints are original EvidenceRevision.
                # Exact Memory citations stay typed on the Rule; they are never
                # relabelled as an Evidence JSON Pointer merely to fit an edge.
                "memory_citations": [
                    link
                    for link in rule["evidence_links"]
                    if link["target_kind"] == "MemorySnapshot"
                ],
            },
        )
        edge("HAS_RULE", revision, rule_node)
        for link in rule["evidence_links"]:
            if link["target_kind"] == "EvidenceRevision":
                edge("EVIDENCE_LINK", rule_node, source_evidence[link["source_id"]], link)
    return list(nodes.values()), list(edges.values())


def build_bundle(directory: Path) -> tuple[list[dict], list[dict]]:
    """Read the sealed artifact contract before projecting its graph records."""
    directory = Path(directory).resolve()
    return _build(directory, load_candidate(directory))


def import_candidate(directory: Path, bridge, repository) -> dict:
    """Recheck live sources and immutable files before one atomic graph import."""
    directory = Path(directory).resolve()
    loaded = load_candidate(directory)
    if repository.project_id != loaded["packet"]["project_id"]:
        raise ValueError("candidate_project_mismatch")
    recheck_packet(bridge, loaded["packet"])
    reloaded = load_candidate(directory)
    if reloaded["manifest"] != loaded["manifest"]:
        raise ValueError("candidate_artifact_changed")
    nodes, edges = _build(directory, reloaded)
    return repository.store_bundle(nodes, edges)
