"""A developer patch has explicit provenance and cannot widen a source candidate."""

import copy
import json

import pytest

from test_domain import sample_generation, sample_packet, sample_proposal

from memos.skill_drafting.artifacts import load_candidate, write_candidate
from memos.skill_drafting.domain import hash_json, make_candidate
from memos.skill_drafting.revision import build_revision_context, load_revision
from memos.skill_drafting.source_patch import apply_source_revision, source_patch_generation


@pytest.fixture
def parent(tmp_path):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    candidate = make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)
    directory = tmp_path / "parent"
    write_candidate(directory, packet, proposal, candidate, generation)
    loaded = load_candidate(directory)
    feedback = [
        {
            "issue_id": "missing-check",
            "reason": "Contract requires preserving validation.",
            "required_change": "Keep every required check.",
            "fact_ids": [packet["facts"][0]["fact_id"]],
        }
    ]
    context = build_revision_context(
        loaded, feedback=feedback, host_policy={"max_repair_cycles": 2}
    )
    patches = [
        {
            "issue_id": "missing-check",
            "fact_ids": feedback[0]["fact_ids"],
            "target": "/limitations",
            "value": [*proposal["limitations"], "Cycle count is not automatically enforced."],
        }
    ]
    return directory, loaded, context, patches


def test_patch_proof_is_explicit_and_roundtrips(parent, tmp_path, monkeypatch):
    from memos.skill_drafting import revision

    directory, loaded, context, patches = parent
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    calls = []
    monkeypatch.setattr(
        revision, "recheck_packet", lambda bridge, packet: calls.append(packet["packet_id"])
    )
    job_path = apply_source_revision(
        directory,
        tmp_path / "patched",
        bridge=object(),
        feedback=context["feedback"],
        host_policy=context["host_policy"],
        patches=patches,
    )
    job = json.loads(job_path.read_text())
    assert job["status"] == "completed"
    result = load_revision(job_path.parent, parent_directory=directory)
    generated = result["candidate"]["generation"]
    assert generated["provider"] == "source_review_patch_v1"
    assert generated["actor_kind"] == "developer_agent" and generated["human_approved"] is False
    assert generated["patches"] == patches
    assert result["record"]["actor_kind"] == "developer_agent"
    assert result["record"]["revision_method"] == "source_review_patch_v1"
    assert len(calls) == 2
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    assert result["candidate"]["candidate"]["revision"] != loaded["candidate"]["revision"]


@pytest.mark.parametrize(
    "target",
    [
        "/applicability",
        "/title",
        "/goal",
        "/exclusions",
        "/rules/new/action",
        "/rules/confirm_contract/basis",
        "/requires",
        "/source_refs",
    ],
)
def test_scope_identity_and_basis_targets_rejected(parent, target):
    _, loaded, context, patches = parent
    patches[0]["target"] = target
    with pytest.raises(ValueError, match="source_patch"):
        source_patch_generation(loaded, context, patches)


@pytest.mark.parametrize(
    "mutation",
    ["unknown_issue", "unknown_fact", "duplicate_target", "approval", "parent", "result"],
)
def test_tampered_patch_provenance_rejected(parent, mutation):
    _, loaded, context, patches = parent
    if mutation == "unknown_issue":
        patches[0]["issue_id"] = "unknown"
    elif mutation == "unknown_fact":
        patches[0]["fact_ids"] = ["f" * 64]
    elif mutation == "duplicate_target":
        patches.append(copy.deepcopy(patches[0]))
    if mutation in {"unknown_issue", "unknown_fact", "duplicate_target"}:
        with pytest.raises(ValueError, match="source_patch"):
            source_patch_generation(loaded, context, patches)
        return
    proposal, generation = source_patch_generation(loaded, context, patches)
    if mutation == "approval":
        generation["human_approved"] = True
    elif mutation == "parent":
        generation["parent"]["proposal"]["goal"] = "Unbound parent change"
    else:
        proposal["goal"] = "Widened child"
    generation["operation_sha256"] = hash_json(
        {k: v for k, v in generation.items() if k != "operation_sha256"}
    )
    with pytest.raises(ValueError, match="source_patch|draft_"):
        make_candidate(
            proposal, loaded["packet"], skill_id="contract-repair", generation=generation
        )


def test_original_rule_and_reference_validation_stays_strict(parent):
    _, loaded, context, patches = parent
    rule = loaded["proposal"]["rules"][0]
    patches[0].update(
        target="/rules/" + rule["rule_id"] + "/branches",
        value=[{"when": "Always", "target": rule["rule_id"]}],
    )
    proposal, generation = source_patch_generation(loaded, context, patches)
    with pytest.raises(ValueError, match="rule_cycle"):
        make_candidate(
            proposal, loaded["packet"], skill_id="contract-repair", generation=generation
        )


def test_patch_parent_is_not_recursively_accepted(parent):
    _, loaded, context, patches = parent
    proposal, generation = source_patch_generation(loaded, context, patches)
    candidate = make_candidate(
        proposal, loaded["packet"], skill_id="contract-repair", generation=generation
    )
    child = {**loaded, "proposal": proposal, "generation": generation, "candidate": candidate}
    with pytest.raises(ValueError, match="source_patch_parent_provider"):
        source_patch_generation(child, context, patches)


def test_patch_cannot_be_rebound_to_a_different_skill(parent):
    _, loaded, context, patches = parent
    proposal, generation = source_patch_generation(loaded, context, patches)
    with pytest.raises(ValueError, match="source_patch_skill_mismatch"):
        make_candidate(proposal, loaded["packet"], skill_id="another-skill", generation=generation)
