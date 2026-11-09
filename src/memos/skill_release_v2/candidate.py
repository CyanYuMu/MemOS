"""Schema 2 candidates separate Memory evidence, authority and transfer claims."""

from __future__ import annotations

import copy
import re

from memos.skill_drafting.domain import (
    _text as validate_literal_text,
)
from memos.skill_drafting.domain import (
    _validate_proposal as validate_legacy_proposal,
)
from memos.skill_drafting.domain import hash_json, validate_packet
from memos.skill_drafting.domain import proposal_schema as legacy_schema
from memos.skill_release_v2.inputs import (
    _bounded,
    _checked,
    _keys,
    _require,
    _same,
    validate_contexts,
    validate_generation,
    validate_input_manifest,
    validate_parent,
)


DOMAIN_VERSION = "2"
PRODUCER = "memos-skill-release-v2-candidate"
_HOST_LIMITS = [
    "Structural and reference checks do not establish semantic entailment.",
    "No independent Skill task validation or causal benefit evaluation has run.",
    "This candidate is not authorized for ordinary Agent use or publication.",
    "Project interface context is normative authority, not empirical Memory evidence.",
    "Transfer beyond the observed source remains an unverified hypothesis.",
]


def proposal_schema() -> dict:
    value = copy.deepcopy(legacy_schema())
    rules = value["properties"]["rules"]
    rules.update(minItems=2, maxItems=2)
    rule = rules["items"]
    rule["required"] = sorted([*rule["required"], "contract_refs"])
    rule["properties"]["basis"] = {"enum": ["normative", "hypothesis"]}
    rule["properties"]["branches"].update(minItems=3, maxItems=3)
    text = {"type": "string", "minLength": 1, "maxLength": 4000}
    rule["properties"]["contract_refs"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["context_id", "locator", "value_sha256", "kind", "claim"],
            "properties": {
                "context_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "locator": {"const": "/excerpt"},
                "value_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "kind": {"enum": ["precondition", "normative_support"]},
                "claim": text,
            },
        },
    }
    return value


@_checked
def validate_proposal(proposal: dict, packet: dict, *, contexts: list) -> None:
    _bounded(proposal, 131072)
    validate_packet(packet)
    _require(
        len(packet["sources"]) == 3 and len(packet["independent_lineages"]) == 2, "source_scope"
    )
    validate_contexts(contexts, packet)
    schema = proposal_schema()
    _keys(proposal, set(schema["required"]), "proposal_fields")
    rules = proposal["rules"]
    _require(type(rules) is list and len(rules) == 2, "rule_count")
    rule_keys = set(schema["properties"]["rules"]["items"]["required"])
    context_index = {context["context_id"]: context for context in contexts}
    for rule in rules:
        _keys(rule, rule_keys, "rule_fields")
        refs = rule["contract_refs"]
        _require(type(refs) is list and 1 <= len(refs) <= 20, "contract_refs")
        seen = set()
        for ref in refs:
            _keys(
                ref,
                {"context_id", "locator", "value_sha256", "kind", "claim"},
                "contract_ref_fields",
            )
            _require(
                ref["context_id"] in context_index
                and ref["locator"] == "/excerpt"
                and ref["kind"] in {"precondition", "normative_support"},
                "contract_reference",
            )
            _require(
                ref["value_sha256"] == hash_json(context_index[ref["context_id"]]["excerpt"]),
                "contract_value",
            )
            validate_literal_text(ref["claim"])
            identity = hash_json(ref)
            _require(identity not in seen, "duplicate_contract_reference")
            seen.add(identity)
    # Reuse every original Memory/source/flow check. Normative support is now
    # supplied independently by the validated context references, so the legacy
    # projection asks for no additional old-source contract justification.
    projection = copy.deepcopy(proposal)
    for rule in projection["rules"]:
        del rule["contract_refs"]
        rule["basis"] = "hypothesis"
    validate_legacy_proposal(projection, packet)
    entry = next(rule for rule in rules if rule["rule_id"] == proposal["entry_rule"])
    handling = next(rule for rule in rules if rule is not entry)
    _require(entry["basis"] == "normative" and handling["basis"] == "hypothesis", "rule_basis")
    for rule, targets in (
        (entry, {handling["rule_id"], "exit:not_applicable", "exit:needs_evidence"}),
        (handling, {"exit:success", "exit:needs_evidence", "exit:unresolved"}),
    ):
        _require(
            len(rule["branches"]) == 3
            and {branch["target"] for branch in rule["branches"]} == targets,
            "rule_topology",
        )


@_checked
def make_candidate(
    proposal: dict,
    packet: dict,
    *,
    skill_id: str,
    contexts: list,
    parent: dict,
    input_manifest: dict,
    exposure_manifest: dict,
    generation: dict,
) -> dict:
    validate_proposal(proposal, packet, contexts=contexts)
    binding = validate_parent(parent, packet)
    _require(
        type(skill_id) is str
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", skill_id)
        and skill_id == parent["candidate"]["skill_id"],
        "skill_identity",
    )
    validate_input_manifest(
        input_manifest,
        packet,
        contexts=contexts,
        parent=parent,
        exposure_manifest=exposure_manifest,
        output_schema=proposal_schema(),
    )
    validate_generation(generation, proposal, input_manifest=input_manifest)
    result = {
        **copy.deepcopy(proposal),
        "schema_version": 2,
        "domain_version": DOMAIN_VERSION,
        "producer": PRODUCER,
        "project_id": packet["project_id"],
        "skill_id": skill_id,
        "type": "procedure",
        "packet_id": packet["packet_id"],
        "generation_sha256": hash_json(generation),
        "status": "candidate",
        "semantic_review": "pending",
        "independent_validation": "not_run",
        "release_eligibility": "not_evaluated",
        "requires": [],
        "host_limits": list(_HOST_LIMITS),
        "parent": binding,
        "contract_context_ids": sorted(c["context_id"] for c in contexts),
        "input_manifest_id": input_manifest["input_manifest_id"],
        "exposure_manifest_id": exposure_manifest["exposure_manifest_id"],
        "source_refs": [
            {
                "source_id": source["source_id"],
                "snapshot_id": source["snapshot"]["snapshot_id"],
                "evidence_revision": source["evidence"]["evidence_revision"],
                "case_id": source["evidence"]["case"]["case_id"],
                "lineage_id": source["evidence"]["case"]["lineage_id"],
                "qualification": source["evidence"]["qualification"],
            }
            for source in sorted(packet["sources"], key=lambda s: s["source_id"])
        ],
    }
    result["revision"] = hash_json(result)
    facts = {fact["fact_id"]: fact for fact in packet["facts"]}
    for rule in result["rules"]:
        rule["validation_status"] = "needs_evidence" if rule["basis"] == "hypothesis" else "not_run"
        common = {
            "producer": PRODUCER,
            "semantic_review": "pending",
            "skill_id": skill_id,
            "revision": result["revision"],
            "rule_id": rule["rule_id"],
        }
        links = []
        for reference in rule["evidence"]:
            fact = facts[reference["fact_id"]]
            link = {
                **reference,
                **{
                    key: fact[key]
                    for key in ("source_id", "target_kind", "target_id", "locator", "value_sha256")
                },
                **common,
            }
            link["link_id"] = hash_json(link)
            links.append(link)
        rule["evidence_links"] = links
        rule["contract_links"] = []
        for reference in rule["contract_refs"]:
            link = {**reference, **common}
            link["link_id"] = hash_json(link)
            rule["contract_links"].append(link)
    result["content_hash"] = hash_json(result)
    return result


@_checked
def verify_candidate_document(
    candidate: dict,
    packet: dict,
    *,
    contexts: list,
    parent: dict,
    input_manifest: dict,
    exposure_manifest: dict,
    generation: dict,
) -> None:
    _bounded(candidate, 4 * 1024 * 1024)
    schema = proposal_schema()
    proposal = {key: copy.deepcopy(candidate[key]) for key in schema["required"]}
    rule_keys = schema["properties"]["rules"]["items"]["required"]
    proposal["rules"] = [{key: rule[key] for key in rule_keys} for rule in candidate["rules"]]
    expected = make_candidate(
        proposal,
        packet,
        skill_id=candidate["skill_id"],
        contexts=contexts,
        parent=parent,
        input_manifest=input_manifest,
        exposure_manifest=exposure_manifest,
        generation=generation,
    )
    _same(candidate, expected, "candidate_content_changed")
