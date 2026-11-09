"""Project contracts remain separate from empirical Memory qualifications."""

import copy

import pytest

from test_inputs import bundle, generated

from memos.skill_release_v2.candidate import (
    make_candidate,
    proposal_schema,
    validate_proposal,
    verify_candidate_document,
)


def candidate_args(item):
    return {key: item[key] for key in ("contexts", "parent", "input_manifest", "exposure_manifest")}


def make(item):
    return make_candidate(
        item["proposal"],
        item["packet"],
        skill_id="contract-repair",
        generation=generated(item),
        **candidate_args(item),
    )


def test_candidate_two_reference_kinds_have_distinct_ids_and_counts(tmp_path):
    item = bundle(tmp_path)
    result = make(item)
    assert result["schema_version"] == 2 and result["domain_version"] == "2"
    assert len(result["source_refs"]) == 3
    assert result["status"] == "candidate"
    assert result["semantic_review"] == "pending"
    assert result["release_eligibility"] == "not_evaluated"
    assert result["parent"]["revision"] == item["parent"]["candidate"]["revision"]
    assert result["revision"] != result["parent"]["revision"]
    assert result == make(item)
    for rule in result["rules"]:
        assert rule["contract_links"] and rule["evidence_links"]
        assert all(
            "source_id" not in ref and "qualification" not in ref for ref in rule["contract_links"]
        )
    verify_candidate_document(
        result, item["packet"], generation=generated(item), **candidate_args(item)
    )
    assert proposal_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["rules"][0].update(basis="empirical"),
        lambda p: p["rules"][1].update(basis="normative"),
        lambda p: p["rules"][0].update(contract_refs=[]),
        lambda p: p["rules"][0]["contract_refs"][0].update(kind="supports"),
        lambda p: p["rules"][0]["contract_refs"][0].update(locator="/file_sha256"),
        lambda p: p["rules"][0]["contract_refs"][0].update(context_id="a" * 64),
        lambda p: p["rules"][0]["contract_refs"][0].update(value_sha256="a" * 64),
        lambda p: p["rules"][0]["contract_refs"][0].update(source_id="fake-memory"),
        lambda p: p["rules"][1]["branches"][0].update(target="R1"),
        lambda p: p["rules"][0]["branches"].append({"when": "extra", "target": "exit:success"}),
        lambda p: p.update(status="published"),
    ],
)
def test_proposal_rejects_contract_as_experience_and_wrong_graph(tmp_path, change):
    item = bundle(tmp_path)
    change(item["proposal"])
    with pytest.raises(ValueError):
        validate_proposal(item["proposal"], item["packet"], contexts=item["contexts"])


def test_original_sources_and_unknown_failure_remain_mandatory(tmp_path):
    item = bundle(tmp_path)
    failure = next(
        f
        for f in item["packet"]["facts"]
        if f["qualification"] == "qualified_failure" and f["category"] == "memory"
    )
    proposal = copy.deepcopy(item["proposal"])
    for rule in proposal["rules"]:
        rule["evidence"] = [r for r in rule["evidence"] if r["fact_id"] != failure["fact_id"]]
    omitted_source = failure["source_id"]
    omitted_facts = {
        f["fact_id"] for f in item["packet"]["facts"] if f["source_id"] == omitted_source
    }
    for rule in proposal["rules"]:
        rule["evidence"] = [r for r in rule["evidence"] if r["fact_id"] not in omitted_facts]
    with pytest.raises(ValueError, match="omitted_source"):
        validate_proposal(proposal, item["packet"], contexts=item["contexts"])
    for ref in item["proposal"]["rules"][0]["evidence"]:
        if ref["fact_id"] == failure["fact_id"]:
            ref["kind"] = "supports"
    with pytest.raises(ValueError, match="unattributed_failure"):
        validate_proposal(item["proposal"], item["packet"], contexts=item["contexts"])


def test_candidate_reconstruction_rejects_host_state_and_link_changes(tmp_path):
    item = bundle(tmp_path)
    result = make(item)
    result["rules"][0]["contract_links"][0]["semantic_review"] = "approved"
    with pytest.raises(ValueError):
        verify_candidate_document(
            result, item["packet"], generation=generated(item), **candidate_args(item)
        )


def test_contract_claims_cannot_introduce_dynamic_interpolation(tmp_path):
    item = bundle(tmp_path)
    item["proposal"]["rules"][0]["contract_refs"][0]["claim"] = "$(execute-something)"
    with pytest.raises(ValueError, match="dynamic_content"):
        validate_proposal(item["proposal"], item["packet"], contexts=item["contexts"])
