import hashlib
import json
import os
import shutil
import stat

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memos.skill_evaluation.snapshots import (
    SnapshotIntegrityError,
    SnapshotStore,
    snapshot_fingerprint,
)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("answer = 42\n")
    return source


def test_capture_restore_preserves_all_supported_inputs_without_source(tmp_path):
    source = _source(tmp_path)
    (source / ".hidden").write_bytes(b"\x00\xff")
    (source / "empty").mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "nested" / "run.sh").chmod(0o751)
    (source / "nested" / "relative").symlink_to("../main.py")
    (source / "dangling").symlink_to("not-yet-created")
    store = SnapshotStore(tmp_path / "store")

    captured = store.capture(source)
    assert captured.snapshot_id == snapshot_fingerprint(source)
    assert store.verify(captured.snapshot_id) == captured
    shutil.rmtree(source)
    restored = store.restore(captured.snapshot_id, tmp_path / "restored")

    assert restored == tmp_path / "restored"
    assert snapshot_fingerprint(restored) == captured.snapshot_id
    assert (restored / ".hidden").read_bytes() == b"\x00\xff"
    assert (restored / "empty").is_dir()
    assert list((restored / "empty").iterdir()) == []
    assert stat.S_IMODE((restored / "nested" / "run.sh").stat().st_mode) & 0o111 == 0o111
    assert os.readlink(restored / "nested" / "relative") == "../main.py"
    assert os.readlink(restored / "dangling") == "not-yet-created"


def test_identity_is_content_addressed_and_ignores_read_write_permissions(tmp_path):
    source = _source(tmp_path)
    (source / "empty").mkdir()
    (source / "main.py").chmod(0o744)
    other = tmp_path / "other"
    shutil.copytree(source, other)
    store = SnapshotStore(tmp_path / "store")
    first = store.capture(source)
    (source / "main.py").chmod(0o500)
    (source / "empty").chmod((source / "empty").stat().st_mode & ~0o222)
    source.chmod(source.stat().st_mode & ~0o222)

    assert snapshot_fingerprint(source) == first.snapshot_id
    assert store.capture(source) == first
    assert store.capture(other) == first


@pytest.mark.parametrize(
    "change", ["content", "add", "delete", "rename", "execute", "kind", "empty"]
)
def test_identity_changes_for_changed_input(tmp_path, change):
    source = _source(tmp_path)
    before = snapshot_fingerprint(source)
    target = source / "main.py"
    if change == "content":
        target.write_text("answer = 43\n")
    elif change == "add":
        (source / ".added").write_text("new")
    elif change == "delete":
        target.unlink()
    elif change == "rename":
        target.rename(source / "renamed.py")
    elif change == "execute":
        target.chmod(target.stat().st_mode ^ stat.S_IXUSR)
    elif change == "kind":
        target.unlink()
        target.mkdir()
    else:
        (source / "empty").mkdir()

    assert snapshot_fingerprint(source) != before


def test_identity_records_symlink_targets_and_directory_execute_bits(tmp_path):
    source = _source(tmp_path)
    (source / "alias").symlink_to("main.py")
    before = snapshot_fingerprint(source)
    (source / "alias").unlink()
    (source / "alias").symlink_to("./main.py")
    assert snapshot_fingerprint(source) != before
    before = snapshot_fingerprint(source)
    source.chmod(source.stat().st_mode ^ stat.S_IXGRP)
    assert snapshot_fingerprint(source) != before


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside", "nested/../../outside"])
def test_capture_rejects_absolute_and_escaping_links(tmp_path, target):
    source = _source(tmp_path)
    (source / "bad").symlink_to(target)
    with pytest.raises(SnapshotIntegrityError):
        SnapshotStore(tmp_path / "store").capture(source)
    with pytest.raises(SnapshotIntegrityError):
        snapshot_fingerprint(source)


def test_rejects_symlink_cycles_and_special_files(tmp_path):
    source = _source(tmp_path)
    (source / "loop").symlink_to("loop")
    with pytest.raises(SnapshotIntegrityError):
        snapshot_fingerprint(source)
    (source / "loop").unlink()
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is not available on this platform")
    os.mkfifo(source / "pipe")
    with pytest.raises(SnapshotIntegrityError):
        SnapshotStore(tmp_path / "store").capture(source)


@pytest.mark.parametrize("relative", [".", "artifacts", "nested/artifacts"])
def test_rejects_store_inside_source(tmp_path, relative):
    source = _source(tmp_path)
    with pytest.raises(SnapshotIntegrityError):
        SnapshotStore(source / relative).capture(source)


@pytest.mark.parametrize("kind", ["directory", "file", "dangling_link"])
def test_restore_never_overwrites_existing_destination(tmp_path, kind):
    store = SnapshotStore(tmp_path / "store")
    captured = store.capture(_source(tmp_path))
    destination = tmp_path / "existing"
    if kind == "directory":
        destination.mkdir()
    elif kind == "file":
        destination.write_text("keep me")
    else:
        destination.symlink_to("missing")

    with pytest.raises(SnapshotIntegrityError):
        store.restore(captured.snapshot_id, destination)
    assert os.path.lexists(destination)
    if kind == "file":
        assert destination.read_text() == "keep me"


@pytest.mark.parametrize("damage", ["content", "manifest", "extra_file", "missing_file", "mode"])
def test_verify_and_recapture_reject_corruption_without_repair(tmp_path, damage):
    source = _source(tmp_path)
    store = SnapshotStore(tmp_path / "store")
    captured = store.capture(source)
    object_dir = tmp_path / "store" / captured.snapshot_id
    target = object_dir / "tree" / "main.py"
    if damage == "content":
        target.write_text("corrupted")
    elif damage == "manifest":
        (object_dir / "manifest.json").write_text("{}")
    elif damage == "extra_file":
        (object_dir / "unexpected").write_text("unexpected")
    elif damage == "missing_file":
        target.unlink()
    else:
        target.chmod(target.stat().st_mode ^ stat.S_IXUSR)

    with pytest.raises(SnapshotIntegrityError):
        store.verify(captured.snapshot_id)
    with pytest.raises(SnapshotIntegrityError):
        store.capture(source)
    with pytest.raises(SnapshotIntegrityError):
        store.restore(captured.snapshot_id, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_existing_empty_object_is_not_silently_replaced(tmp_path):
    source = _source(tmp_path)
    snapshot_id = snapshot_fingerprint(source)
    object_dir = tmp_path / "store" / snapshot_id
    object_dir.mkdir(parents=True)
    with pytest.raises(SnapshotIntegrityError):
        SnapshotStore(tmp_path / "store").capture(source)
    assert list(object_dir.iterdir()) == []


@pytest.mark.parametrize("snapshot_id", ["../escape", "/tmp/escape", ".", "a" * 63, "g" * 64])
def test_verify_rejects_noncanonical_snapshot_identity(tmp_path, snapshot_id):
    with pytest.raises(SnapshotIntegrityError):
        SnapshotStore(tmp_path / "store").verify(snapshot_id)


def test_readdressing_manifest_cannot_enable_path_traversal(tmp_path):
    store = SnapshotStore(tmp_path / "store")
    captured = store.capture(_source(tmp_path))
    object_dir = tmp_path / "store" / captured.snapshot_id
    manifest = json.loads((object_dir / "manifest.json").read_text())
    manifest["entries"][0]["path"] = "../escaped.py"
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    malicious_id = hashlib.sha256(encoded).hexdigest()
    (object_dir / "manifest.json").write_bytes(encoded)
    object_dir.rename(tmp_path / "store" / malicious_id)

    with pytest.raises(SnapshotIntegrityError):
        store.restore(malicious_id, tmp_path / "restored")
    assert not (tmp_path / "escaped.py").exists()
    assert not (tmp_path / "restored").exists()


def test_parallel_capture_publishes_one_complete_object(tmp_path):
    source = _source(tmp_path)
    store = SnapshotStore(tmp_path / "store")
    with ThreadPoolExecutor(max_workers=2) as executor:
        captured = list(executor.map(store.capture, [source, source]))
    assert captured[0] == captured[1] == store.verify(captured[0].snapshot_id)
    assert [path.name for path in store.root.iterdir()] == [captured[0].snapshot_id]


def test_failed_publish_does_not_leave_a_partial_object(tmp_path, monkeypatch):
    source = _source(tmp_path)
    store = SnapshotStore(tmp_path / "store")
    before = snapshot_fingerprint(source)

    def fail_publish(_self, _target):
        raise OSError("injected publication failure")

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(SnapshotIntegrityError):
        store.capture(source)
    assert list(store.root.iterdir()) == []
    assert snapshot_fingerprint(source) == before


@pytest.mark.parametrize("part", ["object", "tree", "manifest"])
def test_verify_rejects_symlink_substitution_of_object_parts(tmp_path, part):
    source = _source(tmp_path)
    store = SnapshotStore(tmp_path / "store")
    captured = store.capture(source)
    object_dir = store.root / captured.snapshot_id
    target = {
        "object": object_dir,
        "tree": object_dir / "tree",
        "manifest": object_dir / "manifest.json",
    }[part]
    saved = tmp_path / "saved"
    target.rename(saved)
    target.symlink_to(saved)
    with pytest.raises(SnapshotIntegrityError):
        store.verify(captured.snapshot_id)


def test_duplicate_manifest_fields_are_not_accepted(tmp_path):
    store = SnapshotStore(tmp_path / "store")
    captured = store.capture(_source(tmp_path))
    metadata = store.root / captured.snapshot_id / "manifest.json"
    original = metadata.read_text()
    metadata.write_text(
        original.replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    )
    with pytest.raises(SnapshotIntegrityError):
        store.verify(captured.snapshot_id)


@pytest.mark.parametrize("part,extra", [("main.py", stat.S_ISUID), (".", stat.S_ISVTX)])
def test_rejects_special_permissions_not_represented_by_snapshot_identity(tmp_path, part, extra):
    source = _source(tmp_path)
    path = source / part
    path.chmod(path.stat().st_mode | extra)
    with pytest.raises(SnapshotIntegrityError):
        snapshot_fingerprint(source)
