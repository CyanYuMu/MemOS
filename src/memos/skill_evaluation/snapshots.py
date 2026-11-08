"""Content-addressed snapshots for controlled, local evaluation workspaces.

Read/write permissions and timestamps are intentionally excluded from identity.
Executable bits, empty directories and relative in-tree symlinks are retained.
This is not a filesystem sandbox or a strongly consistent live-filesystem backup.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class SnapshotIntegrityError(ValueError):
    """The input, stored snapshot or requested destination is not acceptable."""


@dataclass(frozen=True)
class Snapshot:
    """An immutable identity and its decoded, canonical manifest."""

    snapshot_id: str
    manifest: dict[str, Any]


def _encoded(manifest: Any) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _identity(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(_encoded(manifest)).hexdigest()


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotIntegrityError(f"Duplicate manifest field: {key}")
        result[key] = value
    return result


def _directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise SnapshotIntegrityError(f"Expected a directory, not a symlink or file: {path}")


def _executable(mode: int) -> int:
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise SnapshotIntegrityError("setuid, setgid and sticky permissions are not supported")
    return mode & 0o111


def _relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
        or PureWindowsPath(value).drive
    ):
        raise SnapshotIntegrityError(f"Unsupported snapshot path: {value!r}")


def _safe_link(root: Path, path: Path, target: str) -> None:
    if not target or os.path.isabs(target) or PureWindowsPath(target).drive or "\\" in target:
        raise SnapshotIntegrityError(f"Unsupported symlink target: {target!r}")
    if not (path.parent / target).resolve().is_relative_to(root):
        raise SnapshotIntegrityError(f"Symlink escapes the snapshot root: {path}")


def _manifest(source: Path) -> dict[str, Any]:
    _directory(source)
    source = source.resolve()
    root_executable = _executable(source.stat().st_mode)
    entries = []

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(source).as_posix()
            _relative_path(relative)
            mode = path.lstat().st_mode
            entry: dict[str, Any] = {"path": relative}
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                _safe_link(source, path, target)
                entry.update(kind="symlink", target=target)
            elif stat.S_ISDIR(mode):
                entry.update(kind="directory", executable=_executable(mode))
                visit(path)
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                entry.update(
                    kind="file", executable=_executable(mode), size=size, sha256=digest.hexdigest()
                )
            else:
                raise SnapshotIntegrityError(f"Unsupported special file: {path}")
            entries.append(entry)

    visit(source)
    return {
        "schema_version": 1,
        "root_executable": root_executable,
        "entries": sorted(entries, key=lambda item: item["path"]),
    }


def snapshot_fingerprint(source: Path) -> str:
    """Fingerprint supported input content and attributes without writing a store."""
    try:
        return _identity(_manifest(Path(source)))
    except (OSError, RuntimeError) as exc:
        raise SnapshotIntegrityError(f"Cannot fingerprint snapshot input: {source}") from exc


def _remove_tree(path: Path) -> None:
    """Clean an owned staging directory, including copied read-only directories."""
    if not path.exists():
        return
    for directory, _, _ in os.walk(path, followlinks=False):
        current = Path(directory)
        current.chmod(current.stat().st_mode | 0o700)
    shutil.rmtree(path)


class SnapshotStore:
    """Store complete snapshot objects under ``root/<snapshot_id>``.

    Each object contains ``manifest.json`` and ``tree/``. A short publish lock
    coordinates cooperating writers; staging directories never count as objects.
    A crashed writer may leave staging/lock files requiring explicit cleanup.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _object(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
            raise SnapshotIntegrityError("Invalid snapshot identity")
        return self.root / snapshot_id

    @contextmanager
    def _publish_lock(self, snapshot_id: str):
        lock = self.root / f".{snapshot_id}.lock"
        for attempt in range(100):
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                break
            except FileExistsError:
                if attempt == 99:
                    raise SnapshotIntegrityError(
                        "Snapshot publication is locked; retry or inspect"
                    ) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            lock.unlink()

    def capture(self, source: Path) -> Snapshot:
        """Capture and atomically publish an object; never replace a damaged one."""
        source = Path(source)
        staging = None
        try:
            _directory(source)
            source = source.resolve()
            if self.root.is_relative_to(source):
                raise SnapshotIntegrityError("The snapshot store must be outside the source")
            manifest = _manifest(source)
            snapshot_id = _identity(manifest)
            destination = self._object(snapshot_id)
            if os.path.lexists(destination):
                return self.verify(snapshot_id)
            self.root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=self.root))
            shutil.copytree(source, staging / "tree", symlinks=True)
            if _encoded(_manifest(staging / "tree")) != _encoded(manifest):
                raise SnapshotIntegrityError("Source changed while capturing the snapshot")
            with (staging / "manifest.json").open("wb") as stream:
                stream.write(_encoded(manifest))
                stream.flush()
                os.fsync(stream.fileno())
            with self._publish_lock(snapshot_id):
                if os.path.lexists(destination):
                    return self.verify(snapshot_id)
                staging.rename(destination)
            return Snapshot(snapshot_id, manifest)
        except (OSError, RuntimeError) as exc:
            raise SnapshotIntegrityError(f"Cannot capture snapshot input: {source}") from exc
        finally:
            if staging is not None:
                _remove_tree(staging)

    def verify(self, snapshot_id: str) -> Snapshot:
        """Validate manifest identity and the entire stored tree, not just files."""
        object_dir = self._object(snapshot_id)
        try:
            _directory(object_dir)
            if {path.name for path in object_dir.iterdir()} != {"manifest.json", "tree"}:
                raise SnapshotIntegrityError("Snapshot object has missing or extra entries")
            metadata = object_dir / "manifest.json"
            if not stat.S_ISREG(metadata.lstat().st_mode):
                raise SnapshotIntegrityError("Snapshot manifest must be a regular file")
            manifest = json.loads(metadata.read_bytes(), object_pairs_hook=_unique_fields)
            if not isinstance(manifest, dict) or _identity(manifest) != snapshot_id:
                raise SnapshotIntegrityError("Snapshot manifest identity mismatch")
            for entry in manifest.get("entries", []):
                _relative_path(entry["path"])
            actual = _manifest(object_dir / "tree")
            if _encoded(actual) != _encoded(manifest):
                raise SnapshotIntegrityError(
                    "Snapshot content or metadata does not match its manifest"
                )
            return Snapshot(snapshot_id, manifest)
        except SnapshotIntegrityError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise SnapshotIntegrityError(f"Cannot verify snapshot: {snapshot_id}") from exc

    def restore(self, snapshot_id: str, destination: Path) -> Path:
        """Restore into an exclusively created destination; never overwrite it."""
        self.verify(snapshot_id)
        destination = Path(destination)
        created = False
        try:
            if destination.resolve().is_relative_to(self.root):
                raise SnapshotIntegrityError("Cannot restore inside the snapshot store")
            if os.path.lexists(destination):
                raise SnapshotIntegrityError(f"Restore destination already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.mkdir()
            created = True
            shutil.copytree(
                self._object(snapshot_id) / "tree", destination, symlinks=True, dirs_exist_ok=True
            )
            if snapshot_fingerprint(destination) != snapshot_id:
                raise SnapshotIntegrityError("Restored content does not match its snapshot")
            return destination
        except (OSError, RuntimeError) as exc:
            if created:
                _remove_tree(destination)
            raise SnapshotIntegrityError(f"Cannot restore snapshot: {snapshot_id}") from exc
        except SnapshotIntegrityError:
            if created:
                _remove_tree(destination)
            raise
