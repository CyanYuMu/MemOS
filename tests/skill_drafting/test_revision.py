"""Source-bound revision requests never rewrite parents or approve their output."""

import copy
import json

from types import SimpleNamespace

import pytest

from test_domain import sample_generation, sample_packet, sample_proposal
from test_generation import Client, Response, make

from memos.skill_drafting.artifacts import load_candidate, write_candidate
from memos.skill_drafting.domain import hash_json, make_candidate
from memos.skill_drafting.generation import (
    GenerationError,
    build_messages,
    validate_failure_diagnostic,
)
from memos.skill_drafting.revision import (
    build_revision_context,
    load_revision,
    revise_candidate,
    validate_revision_context,
)


@pytest.fixture
def parent(tmp_path):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    candidate = make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)
    directory = tmp_path / "parent"
    write_candidate(directory, packet, proposal, candidate, generation)
    return directory, load_candidate(directory)


def feedback(packet):
    return [
        {
            "issue_id": "acceptance-gap",
            "reason": "A source check was omitted.",
            "required_change": "Preserve all scoped acceptance checks.",
            "fact_ids": [packet["facts"][0]["fact_id"]],
        }
    ]


def context(loaded):
    return build_revision_context(
        loaded, feedback=feedback(loaded["packet"]), host_policy={"max_repair_cycles": 2}
    )


def test_legacy_messages_and_generation_still_validate(parent):
    _, loaded = parent
    assert build_messages(loaded["packet"]) == loaded["generation"]["messages"]
    assert "revision_context" not in loaded["generation"]


def test_context_binds_parent_packet_feedback_and_host_policy(parent):
    _, loaded = parent
    value = context(loaded)
    assert value["parent_candidate"] == loaded["candidate"]
    assert value["parent_manifest_id"] == loaded["manifest"]["manifest_id"]
    assert value["review_kind"] == "developer_source_review"
    assert validate_revision_context(value, loaded["packet"]) == value
    messages = build_messages(loaded["packet"], revision_context=value)
    assert messages != loaded["generation"]["messages"]
    assert "host policy" in messages[0]["content"]
    assert "not proof" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["revision_request"] == value


@pytest.mark.parametrize("mutation", ["parent", "packet", "fact", "budget", "approval"])
def test_invalid_or_unbound_revision_context_rejected(parent, mutation):
    _, loaded = parent
    value = context(loaded)
    if mutation == "parent":
        value["parent_candidate"]["goal"] = "Forged parent"
    elif mutation == "packet":
        value["packet_id"] = "f" * 64
    elif mutation == "fact":
        value["feedback"][0]["fact_ids"] = ["f" * 64]
    elif mutation == "budget":
        value["host_policy"]["max_repair_cycles"] = True
    else:
        value["human_approved"] = True
    with pytest.raises(ValueError):
        validate_revision_context(value, loaded["packet"])


def test_generated_metadata_binds_revision_and_roundtrips(parent, tmp_path):
    _, loaded = parent
    value = context(loaded)
    proposal = copy.deepcopy(loaded["proposal"])
    proposal["limitations"].append("Host budget is policy, not observed optimality.")
    client = Client(Response(json.dumps(proposal)))
    generated, metadata = make(client).generate(loaded["packet"], revision_context=value)
    assert metadata["revision_context"] == value
    assert metadata["prompt_sha256"] == hash_json(client.calls[0]["messages"])
    candidate = make_candidate(
        generated, loaded["packet"], skill_id="contract-repair", generation=metadata
    )
    assert candidate["revision"] != loaded["candidate"]["revision"]
    write_candidate(tmp_path / "revised", loaded["packet"], generated, candidate, metadata)
    assert load_candidate(tmp_path / "revised")["candidate"] == candidate
    metadata["revision_context"]["host_policy"]["max_repair_cycles"] = 3
    with pytest.raises(ValueError, match="generation_prompt_mismatch"):
        make_candidate(generated, loaded["packet"], skill_id="contract-repair", generation=metadata)


def test_revision_rechecks_sources_and_preserves_history(parent, tmp_path, monkeypatch):
    from memos.skill_drafting import revision

    directory, loaded = parent
    original = {p.name: p.read_bytes() for p in directory.iterdir()}
    calls = []
    monkeypatch.setattr(revision, "recheck_packet", lambda bridge, packet: calls.append("recheck"))
    history = [
        {
            "jobId": "old-job",
            "exportHash": "a" * 64,
            "status": "unknown",
            "attribution": "invalid_rule_declaration",
            "disposition": "retained_unknown",
        }
    ]
    report = revise_candidate(
        directory,
        tmp_path / "job",
        make(Client(Response(json.dumps(loaded["proposal"])))),
        bridge=object(),
        feedback=feedback(loaded["packet"]),
        host_policy={"max_repair_cycles": 2},
        history=history,
    )
    result = json.loads(report.read_text())
    assert result["status"] == "completed"
    assert calls == ["recheck", "recheck"]
    assert original == {p.name: p.read_bytes() for p in directory.iterdir()}
    record = json.loads((report.parent / "revision-record.json").read_text())
    assert record["history"] == history and record["human_approved"] is False
    assert record["parent_revision"] == loaded["candidate"]["revision"]
    assert record["new_revision"] != record["parent_revision"]
    assert load_revision(report.parent, parent_directory=directory)["record"] == record
    original_record = (report.parent / "revision-record.json").read_bytes()
    record["history"][0]["status"] = "passed"
    (report.parent / "revision-record.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="record_mismatch"):
        load_revision(report.parent)
    (report.parent / "revision-record.json").write_bytes(original_record)
    changed_request = json.loads((report.parent / "revision-request.json").read_text())
    changed_request["host_policy"]["max_repair_cycles"] = 4
    (report.parent / "revision-request.json").write_text(json.dumps(changed_request))
    with pytest.raises(ValueError, match="context_mismatch"):
        load_revision(report.parent)
    with pytest.raises(ValueError, match="revision_output_exists"):
        revise_candidate(
            directory,
            tmp_path / "job",
            object(),
            bridge=object(),
            feedback=feedback(loaded["packet"]),
            host_policy={"max_repair_cycles": 2},
        )


def test_source_change_after_generation_retains_failed_attempt(parent, tmp_path, monkeypatch):
    from memos.skill_drafting import revision

    directory, loaded = parent
    calls = []

    def changed(bridge, packet):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("private source details")

    monkeypatch.setattr(revision, "recheck_packet", changed)
    report = revise_candidate(
        directory,
        tmp_path / "job",
        make(Client(Response(json.dumps(loaded["proposal"])))),
        bridge=object(),
        feedback=feedback(loaded["packet"]),
        host_policy={"max_repair_cycles": 2},
    )
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["failure_stage"] == "recheck"
    assert (report.parent / "generation.json").is_file()
    assert not (report.parent / "candidate").exists()
    assert "private source details" not in report.read_text()


def test_null_optional_context_is_not_a_legacy_record(parent):
    _, loaded = parent
    generation = copy.deepcopy(loaded["generation"])
    generation["revision_context"] = None
    with pytest.raises(ValueError, match="invalid_revision_context"):
        make_candidate(
            loaded["proposal"], loaded["packet"], skill_id="contract-repair", generation=generation
        )


def test_changed_parent_after_generation_is_rejected(parent, tmp_path, monkeypatch):
    from memos.skill_drafting import revision

    directory, loaded = parent
    monkeypatch.setattr(revision, "recheck_packet", lambda bridge, packet: None)
    real = make(Client(Response(json.dumps(loaded["proposal"]))))

    class Changed:
        def generate(self, packet, **kwargs):
            value = real.generate(packet, **kwargs)
            (directory / "REVIEW.md").write_text("tampered")
            return value

    report = revise_candidate(
        directory,
        tmp_path / "job",
        Changed(),
        bridge=object(),
        feedback=feedback(loaded["packet"]),
        host_policy={"max_repair_cycles": 2},
    )
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["failure_stage"] == "parent_recheck"
    assert not (report.parent / "candidate").exists()


def test_revision_budget_profile_requires_context_before_request(parent):
    _, loaded = parent
    client = Client()
    generator = make(client, max_tokens=32000, budget_profile="revision_32k_v1")
    with pytest.raises(GenerationError, match="generation_revision_context_required"):
        generator.generate(loaded["packet"])
    assert not client.calls


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_tokens": 32000},
        {"max_tokens": 32001, "budget_profile": "revision_32k_v1"},
        {"max_tokens": True, "budget_profile": "revision_32k_v1"},
        {"max_tokens": 32000, "budget_profile": "unknown"},
    ],
)
def test_revision_profile_does_not_expand_legacy_or_accept_unknown(kwargs):
    with pytest.raises(GenerationError, match="generation_invalid_configuration"):
        make(Client(), **kwargs)


def test_revision_32k_generation_and_request_identity_are_bound(parent, tmp_path):
    _, loaded = parent
    response = Response(json.dumps(loaded["proposal"]))
    response.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=16)
    client = Client(response)
    proposal, metadata = make(client, max_tokens=32000, budget_profile="revision_32k_v1").generate(
        loaded["packet"], revision_context=context(loaded)
    )
    assert metadata["parameters"]["budget_profile"] == "revision_32k_v1"
    assert metadata["usage"]["reasoning_tokens"] == 16
    assert client.calls[0]["max_tokens"] == 32000
    assert "budget_profile" not in client.calls[0]
    candidate = make_candidate(
        proposal, loaded["packet"], skill_id="contract-repair", generation=metadata
    )
    write_candidate(tmp_path / "32k", loaded["packet"], proposal, candidate, metadata)
    assert load_candidate(tmp_path / "32k")["candidate"] == candidate
    metadata["parameters"]["max_tokens"] = 31000
    with pytest.raises(ValueError, match="generation_request_mismatch"):
        make_candidate(proposal, loaded["packet"], skill_id="contract-repair", generation=metadata)


def test_revision_32k_failure_has_safe_bound_usage_metadata(parent):
    _, loaded = parent
    response = Response(finish_reason="length")
    response.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=16)

    class Hidden:
        @property
        def content(self):
            raise AssertionError("must not read truncated or private content")

        @property
        def reasoning_content(self):
            raise AssertionError("must not read private reasoning")

    response.choices[0].message = Hidden()
    with pytest.raises(GenerationError) as caught:
        make(Client(response), max_tokens=32000, budget_profile="revision_32k_v1").generate(
            loaded["packet"], revision_context=context(loaded)
        )
    diagnostic = caught.value.diagnostic
    assert diagnostic["parameters"]["budget_profile"] == "revision_32k_v1"
    assert diagnostic["usage"]["reasoning_tokens"] == 16
    assert len(diagnostic["request_sha256"]) == 64
    changed = copy.deepcopy(diagnostic)
    changed["parameters"]["max_tokens"] = 31000
    with pytest.raises(ValueError, match="invalid_generation_diagnostic"):
        validate_failure_diagnostic(changed)


@pytest.mark.parametrize("count", [-1, True, "private", 33])
def test_invalid_reasoning_usage_never_enters_metadata(parent, count):
    _, loaded = parent
    response = Response(json.dumps(loaded["proposal"]))
    response.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=count)
    _, metadata = make(Client(response)).generate(loaded["packet"])
    assert metadata["usage"] == {} and metadata["usage_status"] == "invalid"
