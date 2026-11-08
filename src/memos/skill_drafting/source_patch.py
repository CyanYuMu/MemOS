"""Bounded developer-Agent edits with provenance distinct from model generation.

This is an explicit fallback for source review, never a synthetic provider
response or a human approval. Parent LLM proof and all original evidence remain
verifiable; the normal candidate graph and citation checks still apply.
"""

from __future__ import annotations

import copy

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json, json_bytes, make_candidate, proposal_schema
from memos.skill_drafting.generation import assert_public_json
from memos.skill_drafting.revision import revise_candidate, validate_revision_context


PROVIDER = "source_review_patch_v1"
_TOP_FIELDS = {"acceptance", "limitations", "constraints"}
_RULE_FIELDS = {"condition", "observation", "action", "expected_result", "branches", "evidence"}


def _require(condition, code):
    if not condition:
        raise ValueError("source_patch_" + code)


def _parent(parent, packet, context):
    _require(
        isinstance(parent, dict)
        and set(parent) == {"proposal", "generation", "candidate", "manifest_id"},
        "parent_fields",
    )
    _require(parent["generation"].get("provider") == "openai_compatible_json", "parent_provider")
    expected = make_candidate(
        parent["proposal"],
        packet,
        skill_id=parent["candidate"]["skill_id"],
        generation=parent["generation"],
    )
    _require(expected == parent["candidate"], "parent_mismatch")
    validate_revision_context(context, packet)
    _require(
        context["parent_candidate"] == expected
        and context["parent_manifest_id"] == parent["manifest_id"],
        "context_mismatch",
    )


def _apply(parent_proposal, context, packet, patches):
    _require(isinstance(patches, list) and 0 < len(patches) <= 32, "invalid_patches")
    issues = {item["issue_id"]: item for item in context["feedback"]}
    facts = {fact["fact_id"] for fact in packet["facts"]}
    proposal = copy.deepcopy(parent_proposal)
    rules = {rule["rule_id"]: rule for rule in proposal["rules"]}
    seen = set()
    for patch in patches:
        _require(
            isinstance(patch, dict) and set(patch) == {"issue_id", "fact_ids", "target", "value"},
            "patch_fields",
        )
        issue = patch["issue_id"]
        _require(isinstance(issue, str) and issue in issues, "unknown_issue")
        refs = patch["fact_ids"]
        _require(
            isinstance(refs, list) and refs and all(isinstance(x, str) for x in refs),
            "invalid_refs",
        )
        _require(
            len(set(refs)) == len(refs)
            and set(refs) <= facts
            and set(refs) <= set(issues[issue]["fact_ids"]),
            "unbound_refs",
        )
        target = patch["target"]
        _require(
            isinstance(target, str) and target.startswith("/") and target not in seen,
            "invalid_target",
        )
        seen.add(target)
        parts = target.split("/")[1:]
        if len(parts) == 1 and parts[0] in _TOP_FIELDS:
            proposal[parts[0]] = copy.deepcopy(patch["value"])
        elif (
            len(parts) == 3
            and parts[0] == "rules"
            and parts[1] in rules
            and parts[2] in _RULE_FIELDS
        ):
            rules[parts[1]][parts[2]] = copy.deepcopy(patch["value"])
        else:
            raise ValueError("source_patch_forbidden_target")
    assert_public_json(proposal)
    _require(proposal != parent_proposal, "no_change")
    return proposal


def source_patch_generation(loaded: dict, context: dict, patches: list) -> tuple[dict, dict]:
    parent = {key: copy.deepcopy(loaded[key]) for key in ("proposal", "generation", "candidate")}
    parent["manifest_id"] = loaded["manifest"]["manifest_id"]
    _parent(parent, loaded["packet"], context)
    proposal = _apply(parent["proposal"], context, loaded["packet"], patches)
    generation = {
        "provider": PROVIDER,
        "schema_version": 1,
        "actor_kind": "developer_agent",
        "human_approved": False,
        "parent": parent,
        "packet_id": loaded["packet"]["packet_id"],
        "revision_context": copy.deepcopy(context),
        "patches": copy.deepcopy(patches),
        "result_proposal_sha256": hash_json(proposal),
        "schema_sha256": hash_json(proposal_schema()),
    }
    generation["operation_sha256"] = hash_json(generation)
    validate_source_patch_generation(generation, loaded["packet"], proposal)
    return proposal, generation


def validate_source_patch_generation(generation: dict, packet: dict, proposal: dict) -> None:
    _require(
        isinstance(generation, dict)
        and set(generation)
        == {
            "provider",
            "schema_version",
            "actor_kind",
            "human_approved",
            "parent",
            "packet_id",
            "revision_context",
            "patches",
            "result_proposal_sha256",
            "schema_sha256",
            "operation_sha256",
        },
        "generation_fields",
    )
    _require(
        generation["provider"] == PROVIDER and generation["schema_version"] == 1,
        "unsupported_generation",
    )
    _require(
        generation["actor_kind"] == "developer_agent" and generation["human_approved"] is False,
        "invalid_authority",
    )
    _require(
        generation["packet_id"] == packet["packet_id"]
        and generation["schema_sha256"] == hash_json(proposal_schema()),
        "packet_or_schema_mismatch",
    )
    _require(
        generation["operation_sha256"]
        == hash_json({k: v for k, v in generation.items() if k != "operation_sha256"}),
        "operation_mismatch",
    )
    context = generation["revision_context"]
    _parent(generation["parent"], packet, context)
    expected = _apply(generation["parent"]["proposal"], context, packet, generation["patches"])
    _require(
        proposal == expected and generation["result_proposal_sha256"] == hash_json(expected),
        "proposal_mismatch",
    )
    assert_public_json(generation)
    _require(len(json_bytes(generation)) <= 8 * 1024 * 1024, "generation_too_large")


def apply_source_revision(
    parent_directory: Path,
    output: Path,
    *,
    bridge,
    feedback: list,
    host_policy: dict,
    patches: list,
    history: list | None = None,
) -> Path:
    """Reuse source rechecks and immutable publication, without any model call."""
    parent_directory = Path(parent_directory)
    loaded = load_candidate(parent_directory)
    _require(loaded["generation"]["provider"] == "openai_compatible_json", "parent_provider")

    class SourcePatch:
        def generate(self, packet, *, revision_context):
            _require(packet == loaded["packet"], "packet_mismatch")
            return source_patch_generation(loaded, revision_context, patches)

    return revise_candidate(
        parent_directory,
        output,
        SourcePatch(),
        bridge=bridge,
        feedback=feedback,
        host_policy=host_policy,
        history=history,
    )
