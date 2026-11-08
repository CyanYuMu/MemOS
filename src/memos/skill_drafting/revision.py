"""Source-only revision requests and immutable, unapproved parent/child records."""

from __future__ import annotations

import copy
import json
import re

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate, write_candidate
from memos.skill_drafting.domain import (
    hash_json,
    json_bytes,
    make_candidate,
    verify_candidate_document,
)
from memos.skill_drafting.generation import GenerationError, assert_public_json
from memos.skill_drafting.sources import recheck_packet


def _require(condition, code="invalid_context"):
    if not condition:
        raise ValueError("revision_" + code)


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _text(value, maximum=2000):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def validate_revision_context(value: dict, packet: dict) -> dict:
    """Bind feedback to the same source packet and a self-consistent parent.

    The host verifies the sealed parent's full generation separately. This
    projection contains no filesystem references or claim of human approval.
    """
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "schema_version",
            "review_kind",
            "packet_id",
            "parent_candidate",
            "parent_manifest_id",
            "feedback",
            "host_policy",
        }
    )
    _require(value["schema_version"] == 1 and value["review_kind"] == "developer_source_review")
    _require(value["packet_id"] == packet["packet_id"])
    _require(_digest(value["parent_manifest_id"]))
    parent = value["parent_candidate"]
    verify_candidate_document(parent, packet)
    _require(parent["packet_id"] == packet["packet_id"] and parent["status"] == "candidate")
    feedback = value["feedback"]
    _require(isinstance(feedback, list) and 0 < len(feedback) <= 16)
    ids = set()
    facts = {fact["fact_id"] for fact in packet["facts"]}
    for item in feedback:
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "issue_id",
                "reason",
                "required_change",
                "fact_ids",
            }
        )
        issue = item["issue_id"]
        _require(_text(issue, 100) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", issue))
        _require(issue not in ids)
        ids.add(issue)
        _require(_text(item["reason"]) and _text(item["required_change"]))
        refs = item["fact_ids"]
        _require(isinstance(refs, list) and 0 < len(refs) <= 64)
        _require(all(isinstance(ref, str) for ref in refs))
        _require(len(set(refs)) == len(refs) and set(refs) <= facts)
    policy = value["host_policy"]
    _require(isinstance(policy, dict) and set(policy) == {"max_repair_cycles"})
    _require(type(policy["max_repair_cycles"]) is int and 1 <= policy["max_repair_cycles"] <= 10)
    assert_public_json(value)
    _require(len(json_bytes(value)) <= 512_000)
    return copy.deepcopy(value)


def build_revision_context(loaded: dict, *, feedback: list, host_policy: dict) -> dict:
    return validate_revision_context(
        {
            "schema_version": 1,
            "review_kind": "developer_source_review",
            "packet_id": loaded["packet"]["packet_id"],
            "parent_candidate": loaded["candidate"],
            "parent_manifest_id": loaded["manifest"]["manifest_id"],
            "feedback": feedback,
            "host_policy": host_policy,
        },
        loaded["packet"],
    )


def _history(value):
    _require(isinstance(value, list) and len(value) <= 1000, "invalid_history")
    seen = set()
    for item in value:
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "jobId",
                "exportHash",
                "status",
                "attribution",
                "disposition",
            },
            "invalid_history",
        )
        _require(_text(item["jobId"], 128) and item["jobId"] not in seen, "invalid_history")
        seen.add(item["jobId"])
        _require(_digest(item["exportHash"]), "invalid_history")
        _require(item["status"] in {"unknown", "failed", "passed"}, "invalid_history")
        _require(_text(item["attribution"]), "invalid_history")
        _require(item["disposition"] == "retained_" + item["status"], "invalid_history")
    assert_public_json(value)
    return copy.deepcopy(value)


def _write(path: Path, value):
    assert_public_json(value)
    with path.open("xb") as stream:
        stream.write(json_bytes(value) + b"\n")


def _record(loaded: dict, history: list) -> dict:
    candidate = loaded["candidate"]
    context = loaded["generation"]["revision_context"]
    parent = context["parent_candidate"]
    value = {
        "schema_version": 1,
        "kind": "source_bound_revision",
        "review_kind": "developer_source_review",
        "skill_id": candidate["skill_id"],
        "packet_id": candidate["packet_id"],
        "parent_revision": parent["revision"],
        "parent_content_hash": parent["content_hash"],
        "parent_manifest_id": context["parent_manifest_id"],
        "new_revision": candidate["revision"],
        "new_content_hash": candidate["content_hash"],
        "new_manifest_id": loaded["manifest"]["manifest_id"],
        "generation_sha256": candidate["generation_sha256"],
        "revision_context_sha256": hash_json(context),
        "feedback": copy.deepcopy(context["feedback"]),
        "history": _history(history),
        "history_sha256": hash_json(history),
        "historical_results_changed": False,
        "semantic_review": "pending",
        "human_approved": False,
        "independent_validation": "not_run",
        "remaining_risks": [
            "Source checks bind the request, not the semantic correctness of the response.",
            "Health/no-change behavior and transfer beyond the source contracts remain unverified.",
            "The repair-cycle ceiling is host policy, not observed optimality.",
            "Historical unknown and failed reports remain unchanged on the parent revision.",
        ],
    }
    value["record_id"] = hash_json(value)
    return value


def revise_candidate(
    parent_directory: Path,
    output: Path,
    generator,
    *,
    bridge,
    feedback: list,
    host_policy: dict,
    history: list | None = None,
) -> Path:
    """Make one source-rechecked request; preserve failure evidence without retry."""
    output = Path(output).absolute()
    _require(not output.exists() and not output.is_symlink(), "output_exists")
    parent = load_candidate(parent_directory)
    context = build_revision_context(parent, feedback=feedback, host_policy=host_policy)
    history = _history([] if history is None else history)
    output.mkdir(parents=True, mode=0o700)
    _write(output / "revision-request.json", context)
    _write(output / "history.json", history)
    job = {"status": "failed", "failure_stage": None, "qualification": "unpublished_candidate"}
    stage = "source_precheck"
    try:
        recheck_packet(bridge, parent["packet"])
        stage = "generate"
        proposal, generation = generator.generate(parent["packet"], revision_context=context)
        assert_public_json(proposal)
        assert_public_json(generation)
        _write(output / "proposal.json", proposal)
        _write(output / "generation.json", generation)
        stage = "validate"
        _require(generation.get("revision_context") == context, "context_mismatch")
        candidate = make_candidate(
            proposal,
            parent["packet"],
            skill_id=parent["candidate"]["skill_id"],
            generation=generation,
        )
        _require(candidate["revision"] != parent["candidate"]["revision"], "unchanged_identity")
        stage = "recheck"
        recheck_packet(bridge, parent["packet"])
        stage = "parent_recheck"
        _require(load_candidate(parent_directory) == parent, "parent_changed")
        stage = "write"
        write_candidate(output / "candidate", parent["packet"], proposal, candidate, generation)
        record = _record(load_candidate(output / "candidate"), history)
        _write(output / "revision-record.json", record)
        job.update(
            status="completed",
            candidate_directory=str(output / "candidate"),
            revision_record_id=record["record_id"],
            revision=candidate["revision"],
        )
    except Exception as error:
        job["failure_stage"] = stage
        job["error_type"] = type(error).__name__
        # Domain failures are fixed categories; never persist arbitrary source
        # or provider exception text while retaining useful structural reasons.
        category = str(error)
        if re.fullmatch(r"(?:draft|revision|generation)_[a-z0-9_]{1,100}", category):
            job["error_code"] = category
        if isinstance(error, GenerationError) and error.diagnostic is not None:
            job["generation_diagnostic"] = error.diagnostic
    _write(output / "job.json", job)
    return output / "job.json"


def load_revision(directory: Path, *, parent_directory: Path | None = None) -> dict:
    """Recompute parent/child, generation, request and historical summary bindings."""
    directory = Path(directory)
    _require(directory.is_dir() and not directory.is_symlink(), "invalid_directory")

    def read(name):
        path = directory / name
        _require(
            path.is_file() and not path.is_symlink() and path.stat().st_size <= 8_000_000,
            "invalid_file",
        )
        return json.loads(path.read_text())

    loaded = load_candidate(directory / "candidate")
    context = loaded["generation"].get("revision_context")
    _require(context is not None and read("revision-request.json") == context, "context_mismatch")
    record = read("revision-record.json")
    _require(record == _record(loaded, read("history.json")), "record_mismatch")
    job = read("job.json")
    _require(
        job["status"] == "completed"
        and job["revision_record_id"] == record["record_id"]
        and job["revision"] == record["new_revision"],
        "job_mismatch",
    )
    if parent_directory is not None:
        parent = load_candidate(parent_directory)
        _require(
            parent["candidate"] == context["parent_candidate"]
            and parent["manifest"]["manifest_id"] == context["parent_manifest_id"],
            "parent_changed",
        )
    return {"candidate": loaded, "record": record, "job": job}
