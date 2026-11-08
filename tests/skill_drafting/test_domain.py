"""Synthetic content-bound records test contracts, not real Agent experience."""

import copy
import hashlib
import json

import pytest

from memos.skill_drafting.domain import (
    hash_json,
    json_bytes,
    make_candidate,
    proposal_schema,
    validate_packet,
    verify_candidate_document,
)
from memos.skill_drafting.generation import build_messages


def sample_packet(*, single_lineage=False):
    scope = {
        "service_identity": "service",
        "cube_id": "cube",
        "user_id": "owner",
        "namespace": "source",
    }
    sources, facts, planned = [], [], []
    for index, qualification in enumerate(
        ("qualified_success", "qualified_success", "qualified_failure")
    ):
        case = "case-a" if index != 1 or single_lineage else "case-b"
        evidence = {
            "schema_version": 1,
            "qualification": qualification,
            "collection": {"trusted_local_collection": True},
            "case": {
                "case_id": case,
                "revision": "1",
                "lineage_id": case,
                "split": "source",
                "contract": "Reject invalid calls and preserve valid writes.",
            },
            "context": {
                "case_id": case,
                "case_revision": "1",
                "lineage_id": case,
                "split": "source",
                "run_id": f"run-{index}",
                "attempt_id": f"attempt-{index}",
            },
            "preflight": {
                "variants": {"initial": {"checks": [{"name": "contract", "status": "failed"}]}}
            },
            "final": {
                "status": "passed" if index < 2 else "failed",
                "checks": [
                    {
                        "name": "contract",
                        "status": "passed" if index < 2 else "failed",
                        "attempts": 1,
                    }
                ],
            },
            "changes": {"files": [{"path": "caller.py", "change": "modified"}]},
            "environment": {"cli_version": "fixed"},
            "unknowns": {"failure_attribution": "unknown"},
        }
        evidence["evidence_revision"] = hashlib.sha256(json_bytes(evidence) + b"\n").hexdigest()
        memory = f"Actual returned memory for attempt {index}."
        metadata = {"sources": [{"content": memory}], "user_id": "owner"}
        snapshot = {
            "schema_version": 1,
            **scope,
            "memory_id": f"memory-{index}",
            "observed_at": "2026-11-08T00:00:00Z",
            "evidence_revision": evidence["evidence_revision"],
            "evidence_path": f"/synthetic/evidence-{index}.json",
            "case_id": case,
            "lineage_id": case,
            "qualification": qualification,
            "split": "source",
            "memory": memory,
            "metadata": metadata,
            "sources": metadata["sources"],
            "body_sha256": hashlib.sha256(memory.encode()).hexdigest(),
            "content_sha256": hash_json(
                {"id": f"memory-{index}", "memory": memory, "metadata": metadata}
            ),
        }
        snapshot["snapshot_id"] = hash_json(snapshot)
        snapshot["snapshot_path"] = f"/synthetic/snapshot-{index}.json"
        source_id = hash_json(
            [
                scope["service_identity"],
                scope["cube_id"],
                snapshot["memory_id"],
                snapshot["content_sha256"],
                snapshot["evidence_revision"],
            ]
        )
        sources.append({"source_id": source_id, "snapshot": snapshot, "evidence": evidence})
        planned.append(
            {
                "evidence_path": snapshot["evidence_path"],
                "evidence_revision": evidence["evidence_revision"],
                "memory_ids": [snapshot["memory_id"]],
            }
        )
        for category, target_kind, locator, value in (
            ("memory", "MemorySnapshot", "/memory", memory),
            ("contract", "EvidenceRevision", "/case/contract", evidence["case"]["contract"]),
            ("final_check", "EvidenceRevision", "/final/checks/0", evidence["final"]["checks"][0]),
            ("unknown", "EvidenceRevision", "/unknowns/failure_attribution", "unknown"),
        ):
            fact = {
                "source_id": source_id,
                "target_kind": target_kind,
                "target_id": snapshot["snapshot_id"]
                if target_kind == "MemorySnapshot"
                else evidence["evidence_revision"],
                "category": category,
                "locator": locator,
                "value": value,
                "value_sha256": hash_json(value),
                "qualification": qualification,
                "failure_attribution": "unknown",
            }
            fact["fact_id"] = hash_json(fact)
            facts.append(fact)
    plan = {"schema_version": 1, "project_id": "project", "scope": scope, "sources": planned}
    packet = {
        "schema_version": 1,
        "project_id": "project",
        "scope": scope,
        "plan": plan,
        "plan_sha256": hash_json(plan),
        "sources": sources,
        "facts": facts,
        "watermark": sorted(s["source_id"] for s in sources),
        "independent_lineages": ["case-a"] if single_lineage else ["case-a", "case-b"],
    }
    packet["packet_id"] = hash_json(packet)
    return packet


def sample_generation(packet, proposal):
    messages = build_messages(packet)
    response = json_bytes(proposal).decode()
    return {
        "provider": "openai_compatible_json",
        "sdk_version": "synthetic-sdk",
        "service_identity": hash_json("synthetic-provider"),
        "requested_model": "synthetic-pinned",
        "reported_model": "synthetic-pinned",
        "model_identity_match": True,
        "parameters": {
            "timeout_seconds": 90,
            "max_tokens": 6000,
            "temperature": 0,
            "max_retries": 0,
            "tools": [],
        },
        "prompt_sha256": hash_json(messages),
        "schema_sha256": hash_json(proposal_schema()),
        "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
        "messages": messages,
        "response_content": response,
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "usage_status": "reported",
        "elapsed_seconds": 0.001,
    }


def sample_proposal(packet):
    return {
        "title": "Contract repair",
        "goal": "Restore valid behavior and retain rejection checks.",
        "purpose": "repair",
        "tags": ["python", "testing"],
        "applicability": ["Fixed Python dependency contract is available."],
        "exclusions": ["Unknown dependency contract."],
        "prerequisites": ["Read the independent contract before editing."],
        "constraints": ["Do not weaken invalid call detection."],
        "entry_rule": "R1",
        "rules": [
            {
                "rule_id": "R1",
                "condition": "A fixed contract is available.",
                "observation": "Compare the caller and substitute against it.",
                "action": "Repair the component that violates the contract.",
                "expected_result": "Valid calls work and invalid calls are still rejected.",
                "basis": "normative",
                "branches": [
                    {"when": label, "target": f"exit:{label}"}
                    for label in ("success", "not_applicable", "needs_evidence", "unresolved")
                ],
                "evidence": [
                    {
                        "fact_id": f["fact_id"],
                        "kind": "precondition" if f["category"] == "contract" else "background",
                        "claim": "This returned source supplies contract context or observed outcome.",
                    }
                    for f in packet["facts"]
                ],
            }
        ],
        "exits": {
            key: f"Report {key} and supporting observations."
            for key in ("success", "not_applicable", "needs_evidence", "unresolved")
        },
        "acceptance": ["Request external checks bound to the final candidate."],
        "limitations": ["Not yet independently validated on other cases."],
    }


def rehash_packet(packet):
    packet["packet_id"] = hash_json({k: v for k, v in packet.items() if k != "packet_id"})


def test_content_bound_candidate_remains_unpublished_and_deterministic():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    validate_packet(packet)
    candidate = make_candidate(
        proposal, packet, skill_id="contract-repair", generation=sample_generation(packet, proposal)
    )
    assert candidate["status"] == "candidate"
    assert candidate["semantic_review"] == "pending"
    assert candidate["release_eligibility"] == "not_evaluated"
    assert candidate["independent_validation"] == "not_run"
    assert (
        candidate["revision"]
        == make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )["revision"]
    )
    assert len(candidate["rules"][0]["evidence_links"]) == len(packet["facts"])
    verify_candidate_document(candidate, packet)
    assert proposal_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["sources"][0]["snapshot"].update(memory="forged returned body"),
        lambda p: p["sources"][0]["evidence"]["case"].update(split="holdout"),
        lambda p: p["facts"][0].update(value="forged fact"),
        lambda p: p.update(independent_lineages=["case-a", "case-a"]),
        lambda p: p["plan"]["sources"].pop(),
        lambda p: p["scope"].update(cube_id="foreign"),
    ],
)
def test_packet_rejects_rehashed_claims_that_break_underlying_bindings(mutation):
    packet = sample_packet()
    mutation(packet)
    rehash_packet(packet)
    with pytest.raises(ValueError):
        validate_packet(packet)


def test_a_rehashed_locator_cannot_relabel_a_failed_check_as_a_contract():
    packet = sample_packet()
    fact = packet["facts"][-2]
    fact["category"] = "contract"
    fact["fact_id"] = hash_json({k: v for k, v in fact.items() if k != "fact_id"})
    rehash_packet(packet)
    with pytest.raises(ValueError):
        validate_packet(packet)


def test_retries_do_not_create_independent_cases():
    with pytest.raises(ValueError, match="insufficient_independent_sources"):
        validate_packet(sample_packet(single_lineage=True))


def test_fact_value_requires_json_identity_not_python_numeric_equality():
    packet = sample_packet()
    fact = next(f for f in packet["facts"] if f["category"] == "final_check")
    fact["value"] = {**fact["value"], "attempts": True}
    fact["value_sha256"] = hash_json(fact["value"])
    fact["fact_id"] = hash_json({k: v for k, v in fact.items() if k != "fact_id"})
    rehash_packet(packet)
    with pytest.raises(ValueError, match="fact_value_mismatch"):
        validate_packet(packet)


def test_distinct_conditions_may_reach_the_same_exit():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    proposal["rules"][0]["branches"].append(
        {"when": "A second missing prerequisite is observed.", "target": "exit:needs_evidence"}
    )
    candidate = make_candidate(
        proposal, packet, skill_id="contract-repair", generation=sample_generation(packet, proposal)
    )
    verify_candidate_document(candidate, packet)


@pytest.mark.parametrize("basis", ["empirical", "normative"])
def test_rule_basis_needs_the_corresponding_kind_of_support(basis):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    proposal["rules"][0]["basis"] = basis
    for reference in proposal["rules"][0]["evidence"]:
        reference["kind"] = "background"
    with pytest.raises(ValueError, match="rule_without"):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


def test_each_declared_exit_must_be_reachable():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    proposal["rules"][0]["branches"].pop()
    with pytest.raises(ValueError, match="unreachable_rule_or_exit"):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(status="published"),
        lambda p: p["rules"][0]["evidence"][0].update(fact_id="unknown"),
        lambda p: p["rules"][0]["branches"][0].update(target="missing-rule"),
        lambda p: p["rules"][0]["branches"][0].update(target="R1"),
        lambda p: p["rules"][0].update(action="$(curl private-command)"),
        lambda p: p["exits"].pop("needs_evidence"),
    ],
)
def test_invalid_proposals_do_not_receive_candidate_identity(mutation):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    mutation(proposal)
    with pytest.raises(ValueError):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


def test_failure_with_unknown_attribution_is_background_not_support():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    failed = next(
        f
        for f in packet["facts"]
        if f["qualification"] == "qualified_failure" and f["category"] == "final_check"
    )
    ref = next(r for r in proposal["rules"][0]["evidence"] if r["fact_id"] == failed["fact_id"])
    ref["kind"] = "supports"
    with pytest.raises(ValueError):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


def test_failed_source_cannot_be_silently_omitted():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    failed = {f["fact_id"] for f in packet["facts"] if f["qualification"] == "qualified_failure"}
    proposal["rules"][0]["evidence"] = [
        r for r in proposal["rules"][0]["evidence"] if r["fact_id"] not in failed
    ]
    with pytest.raises(ValueError):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


def test_each_rule_requires_memory_and_original_evidence():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    memory = {f["fact_id"] for f in packet["facts"] if f["target_kind"] == "MemorySnapshot"}
    proposal["rules"][0]["evidence"] = [
        r for r in proposal["rules"][0]["evidence"] if r["fact_id"] not in memory
    ]
    with pytest.raises(ValueError):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


def test_hypothesis_and_changed_content_are_not_self_approved():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    proposal["rules"][0]["basis"] = "hypothesis"
    candidate = make_candidate(
        proposal, packet, skill_id="contract-repair", generation=sample_generation(packet, proposal)
    )
    assert candidate["rules"][0]["validation_status"] == "needs_evidence"
    original = copy.deepcopy(candidate)
    candidate["semantic_review"] = "approved"
    candidate["content_hash"] = hash_json(
        {k: v for k, v in candidate.items() if k != "content_hash"}
    )
    with pytest.raises(ValueError):
        verify_candidate_document(candidate, packet)
    assert original["semantic_review"] == "pending"


def test_generation_requires_provenance_and_does_not_accept_reasoning_or_secrets():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    for extra in (
        {"reasoning_content": "private chain"},
        {"api_key": "secret"},
        {"requested_model": ""},
    ):
        with pytest.raises(ValueError):
            make_candidate(
                sample_proposal(packet),
                packet,
                skill_id="contract-repair",
                generation={**sample_generation(packet, proposal), **extra},
            )


@pytest.mark.parametrize("field", ["prompt_sha256", "schema_sha256", "response_sha256"])
def test_generation_digest_values_are_recomputed(field):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation[field] = "0" * 64
    with pytest.raises(ValueError):
        make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)


def test_self_consistent_prompt_for_another_packet_is_rejected():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation["messages"][1]["content"] = '{"untrusted_source_data": "different input"}'
    generation["prompt_sha256"] = hash_json(generation["messages"])
    with pytest.raises(ValueError):
        make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)


def test_rehashed_model_response_cannot_be_replaced_by_another_proposal():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation["response_content"] = json_bytes(
        {**proposal, "title": "Different procedure"}
    ).decode()
    generation["response_sha256"] = hashlib.sha256(
        generation["response_content"].encode()
    ).hexdigest()
    with pytest.raises(ValueError):
        make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)


@pytest.mark.parametrize(
    "field",
    [
        "messages",
        "response_content",
        "sdk_version",
        "service_identity",
        "parameters",
        "reported_model",
        "usage",
        "finish_reason",
    ],
)
def test_missing_generation_metadata_is_never_a_provider_bypass(field):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation.pop(field)
    with pytest.raises(ValueError):
        make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)


@pytest.mark.parametrize("response", ['{"title":"first","title":"second"}', '{"value":NaN}', "[]"])
def test_rehashed_malformed_response_does_not_form_provenance(response):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation["response_content"] = response
    generation["response_sha256"] = hashlib.sha256(response.encode()).hexdigest()
    with pytest.raises(ValueError):
        make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)


def test_response_whitespace_is_preserved_but_json_content_must_match():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation["response_content"] = json.dumps(proposal, indent=2)
    generation["response_sha256"] = hashlib.sha256(
        generation["response_content"].encode()
    ).hexdigest()
    candidate = make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)
    assert candidate["generation_sha256"] == hash_json(generation)


def test_consistent_response_cannot_smuggle_private_reasoning_tags():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    proposal["limitations"] = ["<think>private content</think>"]
    with pytest.raises(ValueError, match="private_content"):
        make_candidate(
            proposal,
            packet,
            skill_id="contract-repair",
            generation=sample_generation(packet, proposal),
        )


def test_synthetic_provider_has_no_weaker_validation_path():
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    generation["provider"] = "synthetic-test"
    with pytest.raises(ValueError, match="unsupported_generation_provider"):
        make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)
