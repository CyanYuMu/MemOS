"""V2 artifacts close all bindings and fail atomically on authority/source drift."""

import hashlib
import json

from concurrent.futures import ThreadPoolExecutor

import pytest

from test_candidate import candidate_args, make
from test_inputs import bundle, generated

from memos.skill_drafting.domain import hash_json
from memos.skill_release_v2 import artifacts
from memos.skill_release_v2.artifacts import load_candidate, write_candidate
from memos.skill_release_v2.authority import CONTRACT_PATH


def publish(tmp_path, *, source_recheck=lambda _: None):
    item = bundle(tmp_path)
    path = tmp_path / "candidate"
    write_candidate(
        path,
        item["packet"],
        item["proposal"],
        make(item),
        generated(item),
        **candidate_args(item),
        authority_bindings=item["authority_bindings"],
        source_recheck=source_recheck,
    )
    return item, path


def test_roundtrip_rederives_all_documents_and_counts_contracts_separately(tmp_path):
    calls = []
    item, path = publish(tmp_path, source_recheck=lambda packet: calls.append(packet["packet_id"]))
    assert calls == [item["packet"]["packet_id"]] * 2
    loaded = load_candidate(path, authority_bindings=item["authority_bindings"])
    assert loaded["candidate"] == make(item)
    assert loaded["validation"]["source_count"] == 3
    assert loaded["validation"]["contract_context_count"] == 1
    assert loaded["generation"]["model_identity_match"] is False
    assert len(list(path.iterdir())) == 11
    assert "SKILL.md" not in [p.name for p in path.iterdir()]


@pytest.mark.parametrize(
    "filename",
    [
        "candidate.json",
        "proposal.json",
        "parent.json",
        "contexts.json",
        "input-manifest.json",
        "exposure-manifest.json",
        "generation.json",
        "validation.json",
        "REVIEW.md",
    ],
)
def test_rehashing_manifest_does_not_bless_changed_artifacts(tmp_path, filename):
    item, path = publish(tmp_path)
    (path / filename).write_text("{}\n" if filename.endswith("json") else "Approved\n")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["files"][filename] = hashlib.sha256((path / filename).read_bytes()).hexdigest()
    manifest["manifest_id"] = hash_json({k: v for k, v in manifest.items() if k != "manifest_id"})
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_candidate(path, authority_bindings=item["authority_bindings"])


def test_authority_drift_blocks_loading_without_rewriting_baseline(tmp_path):
    item, path = publish(tmp_path)
    source = next(iter(item["authority_bindings"].values())) / CONTRACT_PATH
    frozen = source.read_bytes()
    source.write_bytes(frozen + b"\n# unrelated file change\n")
    with pytest.raises(ValueError):
        load_candidate(path, authority_bindings=item["authority_bindings"])
    source.write_bytes(frozen)
    assert (
        load_candidate(path, authority_bindings=item["authority_bindings"])["candidate"]["status"]
        == "candidate"
    )


def test_second_source_check_failure_never_publishes_a_candidate(tmp_path):
    calls = []

    def recheck(_):
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("synthetic_source_changed")

    with pytest.raises(ValueError, match="synthetic_source_changed"):
        publish(tmp_path, source_recheck=recheck)
    assert not (tmp_path / "candidate").exists()
    assert not list(tmp_path.glob(".candidate*"))


def test_disk_failure_and_symlinks_cannot_create_loadable_partial_artifacts(tmp_path, monkeypatch):
    item = bundle(tmp_path)

    def fail(_):
        raise OSError("synthetic_disk_failure")

    monkeypatch.setattr(artifacts.os, "fsync", fail)
    with pytest.raises(OSError, match="synthetic_disk_failure"):
        write_candidate(
            tmp_path / "candidate",
            item["packet"],
            item["proposal"],
            make(item),
            generated(item),
            **candidate_args(item),
            authority_bindings=item["authority_bindings"],
            source_recheck=lambda _: None,
        )
    assert not (tmp_path / "candidate").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "extra", "missing", "duplicate_json"])
def test_reader_rejects_unsafe_or_incomplete_artifacts(tmp_path, kind):
    item, path = publish(tmp_path)
    target = path / "proposal.json"
    raw = target.read_bytes()
    if kind in {"symlink", "hardlink"}:
        outside = tmp_path / "outside.json"
        outside.write_bytes(raw)
        target.unlink()
        if kind == "symlink":
            target.symlink_to(outside)
        else:
            target.hardlink_to(outside)
    elif kind == "extra":
        (path / "unexpected.json").write_text("{}")
    elif kind == "missing":
        target.unlink()
    else:
        target.write_text('{"title":"first","title":"second"}')
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["files"]["proposal.json"] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest["manifest_id"] = hash_json(
            {k: v for k, v in manifest.items() if k != "manifest_id"}
        )
        (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_candidate(path, authority_bindings=item["authority_bindings"])


def test_authority_drift_during_second_source_check_blocks_publication(tmp_path):
    item = bundle(tmp_path)
    source = next(iter(item["authority_bindings"].values())) / CONTRACT_PATH
    checks = []

    def recheck(_):
        checks.append(True)
        if len(checks) == 2:
            source.write_bytes(source.read_bytes() + b"\n# changed during validation\n")

    with pytest.raises(ValueError):
        write_candidate(
            tmp_path / "candidate",
            item["packet"],
            item["proposal"],
            make(item),
            generated(item),
            **candidate_args(item),
            authority_bindings=item["authority_bindings"],
            source_recheck=recheck,
        )
    assert not (tmp_path / "candidate").exists()


def test_concurrent_writers_cannot_replace_a_sealed_v2_candidate(tmp_path):
    item = bundle(tmp_path)
    candidate = make(item)

    def publish_once():
        try:
            write_candidate(
                tmp_path / "candidate",
                item["packet"],
                item["proposal"],
                candidate,
                generated(item),
                **candidate_args(item),
                authority_bindings=item["authority_bindings"],
                source_recheck=lambda _: None,
            )
            return "written"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish_once(), range(2)))
    assert sorted(results) == ["rejected", "written"]
    assert (
        load_candidate(tmp_path / "candidate", authority_bindings=item["authority_bindings"])[
            "candidate"
        ]
        == candidate
    )
