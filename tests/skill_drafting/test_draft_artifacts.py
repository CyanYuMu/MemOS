"""Immutable candidate artifacts keep review material separate from authorization."""

import hashlib
import json

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_domain import sample_generation, sample_packet, sample_proposal

from memos.skill_drafting import artifacts
from memos.skill_drafting.artifacts import load_candidate, write_candidate
from memos.skill_drafting.domain import hash_json, make_candidate


def make_artifacts(tmp_path):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    candidate = make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)
    directory = tmp_path / "candidate"
    manifest = write_candidate(directory, packet, proposal, candidate, generation)
    return directory, manifest


def test_artifacts_roundtrip_and_review_are_not_a_release(tmp_path):
    directory, manifest = make_artifacts(tmp_path)
    assert manifest == directory / "manifest.json"
    loaded = load_candidate(directory)
    assert loaded["validation"]["kind"] == "structural_and_references"
    assert loaded["validation"]["semantic_review"] == "pending"
    assert loaded["candidate"]["status"] == "candidate"
    assert "SKILL.md" not in {p.name for p in directory.iterdir()}
    assert "not authorized" in (directory / "REVIEW.md").read_text()
    assert str(Path("/synthetic/evidence-0.json")) not in (directory / "REVIEW.md").read_text()
    with pytest.raises(ValueError):
        write_candidate(
            directory,
            loaded["packet"],
            loaded["proposal"],
            loaded["candidate"],
            loaded["generation"],
        )


@pytest.mark.parametrize("name", ["candidate.json", "packet.json", "REVIEW.md", "generation.json"])
def test_modified_file_cannot_be_loaded(tmp_path, name):
    directory, _ = make_artifacts(tmp_path)
    (directory / name).write_text("changed")
    with pytest.raises(ValueError):
        load_candidate(directory)


def test_extra_file_or_symlink_is_not_ignored(tmp_path):
    directory, _ = make_artifacts(tmp_path)
    (directory / "extra.txt").write_text("unexpected")
    with pytest.raises(ValueError):
        load_candidate(directory)
    (directory / "extra.txt").unlink()
    original = directory / "REVIEW.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(outside)
    with pytest.raises(ValueError):
        load_candidate(directory)


def test_rehashed_manifest_does_not_authorize_forged_review(tmp_path):
    directory, manifest_path = make_artifacts(tmp_path)
    (directory / "REVIEW.md").write_text("Approved for production")
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["REVIEW.md"] = hashlib.sha256(
        (directory / "REVIEW.md").read_bytes()
    ).hexdigest()
    manifest["manifest_id"] = hash_json({k: v for k, v in manifest.items() if k != "manifest_id"})
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_candidate(directory)


def test_interrupted_write_never_exposes_partial_candidate(tmp_path, monkeypatch):
    def disk_failure(_):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(artifacts.os, "fsync", disk_failure)
    with pytest.raises(OSError, match="synthetic disk failure"):
        make_artifacts(tmp_path)
    assert not (tmp_path / "candidate").exists()
    assert list(tmp_path.iterdir()) == []


def test_concurrent_writers_cannot_replace_a_completed_candidate(tmp_path):
    def attempt():
        try:
            make_artifacts(tmp_path)
        except ValueError:
            return "rejected"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["rejected", "written"]
    assert load_candidate(tmp_path / "candidate")["candidate"]["status"] == "candidate"


def test_generation_metadata_is_bound_to_candidate_before_writing(tmp_path):
    packet = sample_packet()
    proposal = sample_proposal(packet)
    generation = sample_generation(packet, proposal)
    candidate = make_candidate(proposal, packet, skill_id="contract-repair", generation=generation)
    generation["requested_model"] = "a-different-model"
    generation["model_identity_match"] = False
    with pytest.raises(ValueError, match="candidate_mismatch"):
        write_candidate(tmp_path / "candidate", packet, proposal, candidate, generation)
    assert not (tmp_path / "candidate").exists()
