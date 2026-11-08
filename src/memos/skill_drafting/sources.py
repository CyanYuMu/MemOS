"""Collect a declared source set without silently dropping missing counterexamples."""

from __future__ import annotations

import copy
import json
import re

from pathlib import Path

from memos.skill_drafting.domain import hash_json, validate_packet
from memos.skill_experience.evidence import verify_evidence


def _scope(bridge) -> dict:
    return {
        "service_identity": bridge.client.service_identity,
        "cube_id": bridge.cube_id,
        "user_id": bridge.user_id,
        "namespace": bridge.namespace,
    }


def validate_plan(plan: dict, bridge) -> None:
    if (
        not isinstance(plan, dict)
        or set(plan) != {"schema_version", "project_id", "scope", "sources"}
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or not isinstance(plan["project_id"], str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", plan["project_id"])
        or not isinstance(plan["sources"], list)
        or not 2 <= len(plan["sources"]) <= 20
    ):
        raise ValueError("invalid_source_plan")
    if plan["scope"] != _scope(bridge):
        raise ValueError("source_scope_mismatch")
    evidence_ids, memory_ids = set(), set()
    for item in plan["sources"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"evidence_path", "evidence_revision", "memory_ids"}
            or any(
                not isinstance(item[k], str) or not item[k]
                for k in ("evidence_path", "evidence_revision")
            )
            or not isinstance(item["memory_ids"], list)
            or not 1 <= len(item["memory_ids"]) <= 20
            or any(not isinstance(m, str) or not m for m in item["memory_ids"])
        ):
            raise ValueError("invalid_source_plan")
        if (
            item["evidence_revision"] in evidence_ids
            or any(m in memory_ids for m in item["memory_ids"])
            or len(set(item["memory_ids"])) != len(item["memory_ids"])
        ):
            raise ValueError("duplicate_source_plan")
        evidence_ids.add(item["evidence_revision"])
        memory_ids.update(item["memory_ids"])


def _facts(source: dict) -> list[dict]:
    snapshot, evidence = source["snapshot"], source["evidence"]
    facts = []

    def add(category: str, pointer: str, value, *, memory: bool = False) -> None:
        fact = {
            "source_id": source["source_id"],
            "target_kind": "MemorySnapshot" if memory else "EvidenceRevision",
            "target_id": snapshot["snapshot_id"] if memory else evidence["evidence_revision"],
            "category": category,
            "locator": pointer,
            "value": copy.deepcopy(value),
            "value_sha256": hash_json(value),
            "qualification": evidence["qualification"],
            "failure_attribution": evidence["unknowns"]["failure_attribution"],
        }
        fact["fact_id"] = hash_json(fact)
        facts.append(fact)

    add("memory", "/memory", snapshot["memory"], memory=True)
    add("contract", "/case/contract", evidence["case"]["contract"])
    for i, check in enumerate(evidence["preflight"]["variants"]["initial"]["checks"]):
        add("initial_check", f"/preflight/variants/initial/checks/{i}", check)
    for i, check in enumerate(evidence["final"]["checks"]):
        add("final_check", f"/final/checks/{i}", check)
    for field in ("files", "text_diffs"):
        for i, change in enumerate(evidence["changes"][field]):
            # A truncated diff cannot stand in for the missing code. Full input
            # snapshots remain in the trusted evidence, outside the model view.
            if not change.get("truncated", False):
                add("change", f"/changes/{field}/{i}", change)
    add("environment", "/environment", evidence["environment"])
    for key, value in evidence["unknowns"].items():
        add("unknown", "/unknowns/" + key.replace("~", "~0").replace("/", "~1"), value)
    return facts


def collect_packet(bridge, plan: dict) -> dict:
    """Consume every specified registered Memory through a fresh scoped GET.

    P1b's convenience list can omit unavailable records. This path instead
    binds an explicit set and fails the whole batch if any member is missing.
    The private bridge helpers are reused locally to preserve its exact ledger
    and snapshot checks without changing the implementation pinned by Evidence.
    """
    validate_plan(plan, bridge)
    sources, lineages = [], set()
    successful = False
    for item in plan["sources"]:
        path = Path(item["evidence_path"]).resolve()
        evidence = verify_evidence(path)
        if evidence["evidence_revision"] != item["evidence_revision"]:
            raise ValueError("source_evidence_mismatch")
        if evidence["case"]["split"] != "source" or evidence["qualification"] not in {
            "qualified_success",
            "qualified_failure",
        }:
            raise ValueError("source_ineligible")
        operation = hash_json(
            [
                bridge.client.service_identity,
                bridge.cube_id,
                bridge.user_id,
                bridge.namespace,
                item["evidence_revision"],
            ]
        )
        # Never allow P2 to create a Memory merely because a caller supplied a
        # local Evidence file. Only an already registered concrete ID set is read.
        try:
            row = bridge._row(operation)
        except ValueError:
            raise ValueError("source_not_registered") from None
        if sorted(json.loads(row["memory_ids"])) != sorted(item["memory_ids"]):
            raise ValueError("source_memory_identity_mismatch")
        result = bridge.ingest(path)
        if result["status"] != "completed":
            raise ValueError("source_readback_unavailable")
        if sorted(result["memory_ids"]) != sorted(item["memory_ids"]) or len(
            result["snapshots"]
        ) != len(item["memory_ids"]):
            raise ValueError("source_memory_identity_mismatch")
        seen = set()
        for snapshot_path in result["snapshots"]:
            snapshot = bridge._read_snapshot(Path(snapshot_path))
            if (
                any(snapshot[k] != v for k, v in plan["scope"].items())
                or snapshot["evidence_revision"] != evidence["evidence_revision"]
                or snapshot["memory_id"] not in item["memory_ids"]
                or snapshot["memory_id"] in seen
                or snapshot["qualification"] != evidence["qualification"]
                or snapshot["lineage_id"] != evidence["case"]["lineage_id"]
                or snapshot["case_id"] != evidence["case"]["case_id"]
                or snapshot["split"] != "source"
                or not isinstance(snapshot["memory"], str)
                or not snapshot["memory"].strip()
                or len(snapshot["memory"]) > 18000
            ):
                raise ValueError("source_snapshot_mismatch")
            seen.add(snapshot["memory_id"])
            source_id = hash_json(
                [
                    snapshot["service_identity"],
                    snapshot["cube_id"],
                    snapshot["memory_id"],
                    snapshot["content_sha256"],
                    snapshot["evidence_revision"],
                ]
            )
            sources.append({"source_id": source_id, "snapshot": snapshot, "evidence": evidence})
        lineages.add(evidence["case"]["lineage_id"])
        successful |= evidence["qualification"] == "qualified_success"
    if len(lineages) < 2 or not successful:
        raise ValueError("insufficient_independent_sources")
    sources.sort(key=lambda s: s["source_id"])
    packet = {
        "schema_version": 1,
        "project_id": plan["project_id"],
        "scope": _scope(bridge),
        "plan": copy.deepcopy(plan),
        "plan_sha256": hash_json(plan),
        "sources": sources,
        "facts": [fact for source in sources for fact in _facts(source)],
        "watermark": [source["source_id"] for source in sources],
        "independent_lineages": sorted(lineages),
    }
    packet["packet_id"] = hash_json(packet)
    validate_packet(packet)
    return packet


def recheck_packet(bridge, packet: dict) -> None:
    """Observe the same source set again before sealing/importing a candidate."""
    validate_packet(packet)
    current = collect_packet(bridge, packet["plan"])
    if current["watermark"] != packet["watermark"]:
        raise ValueError("source_watermark_changed")
