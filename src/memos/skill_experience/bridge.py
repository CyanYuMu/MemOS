"""Durable source delivery and immutable observations of actual product Memories.

The SQLite claim prevents duplicate POSTs among callers sharing this ledger.
It does not promise remote exactly-once delivery or coordinate other ledgers.
An uncertain POST outcome is reconciled, never blindly replayed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from memos.skill_adapter.events import sanitize_public
from memos.skill_experience.client import MemoryClientError, ProductMemoryClient


def verify_evidence(path: Path) -> dict:
    # Evidence owns qualification; HTTP metadata is never a qualification input.
    from memos.skill_experience.evidence import verify_evidence as verify

    return verify(path)


def load_source_summary(path: Path) -> dict:
    from memos.skill_experience.evidence import load_source_summary as load

    return load(path)


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ReadbackError(ValueError):
    def __init__(self, status: str, reason: str):
        self.status = status
        super().__init__(reason)


class MemoryBridge:
    """Bind trusted Evidence to verified, scoped Memory observations."""

    def __init__(
        self,
        client: ProductMemoryClient,
        ledger_path: Path,
        snapshot_root: Path,
        *,
        cube_id: str,
        user_id: str,
        namespace: str = "memos-skill-source-v1",
    ):
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (client.service_identity, cube_id, user_id, namespace)
        ):
            raise ValueError("invalid_memory_scope")
        self.client = client
        self.ledger_path = Path(ledger_path).resolve()
        self.snapshot_root = Path(snapshot_root).resolve()
        self.cube_id, self.user_id, self.namespace = cube_id, user_id, namespace
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.snapshot_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS source_operations (
                operation_id TEXT PRIMARY KEY,
                service_identity TEXT NOT NULL, cube_id TEXT NOT NULL,
                user_id TEXT NOT NULL, namespace TEXT NOT NULL,
                evidence_revision TEXT NOT NULL, evidence_path TEXT NOT NULL,
                summary_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL,
                memory_ids TEXT NOT NULL, snapshots TEXT NOT NULL,
                baseline_snapshots TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(service_identity, cube_id, user_id, namespace, evidence_revision)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS source_observations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL, snapshots TEXT NOT NULL
            )""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.ledger_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _row(self, operation_id: str) -> dict:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM source_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise ValueError("unknown_source_operation")
        return dict(row)

    def _summary(self, evidence_path: Path) -> dict:
        verify_evidence(evidence_path)
        summary = load_source_summary(evidence_path)
        required = ("evidence_revision", "case_id", "lineage_id", "run_id", "attempt_id", "content")
        if (
            not isinstance(summary, dict)
            or any(
                not isinstance(summary.get(key), str) or not summary[key].strip()
                for key in required
            )
            or summary.get("split") != "source"
            or summary.get("qualification") not in {"qualified_success", "qualified_failure"}
            or len(summary["content"]) > 16000
            or not isinstance(summary.get("info"), dict)
            or not summary["info"].get("skill_evidence_uri")
            or any(not key.startswith("skill_") for key in summary["info"])
        ):
            raise ValueError("ineligible_source")
        return summary

    def _info(self, summary: dict, operation_id: str) -> dict:
        return {
            **summary["info"],
            "skill_namespace": self.namespace,
            "skill_operation_id": operation_id,
            "skill_evidence_revision": summary["evidence_revision"],
            "skill_case_id": summary["case_id"],
            "skill_lineage_id": summary["lineage_id"],
            "skill_run_id": summary["run_id"],
            "skill_attempt_id": summary["attempt_id"],
            "skill_split": "source",
            "skill_owner": self.user_id,
            "skill_summary_sha256": hashlib.sha256(summary["content"].encode()).hexdigest(),
        }

    def _payload(self, summary: dict, operation_id: str) -> dict:
        return {
            "user_id": self.user_id,
            "writable_cube_ids": [self.cube_id],
            "session_id": summary["run_id"],
            "task_id": operation_id,
            "async_mode": "sync",
            "mode": "fast",
            "allow_memory_view": ["detail_factual"],
            "messages": [{"role": "user", "content": summary["content"]}],
            "info": self._info(summary, operation_id),
        }

    def _claim(self, evidence_path: Path, summary: dict) -> tuple[dict, bool]:
        operation_id = _digest(
            [
                self.client.service_identity,
                self.cube_id,
                self.user_id,
                self.namespace,
                summary["evidence_revision"],
            ]
        )
        payload_hash = _digest(self._payload(summary, operation_id))
        # Commit the claim BEFORE HTTP, and release SQLite's lock before waiting
        # on the network. Even a crash between these steps cannot cause a replay.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """INSERT OR IGNORE INTO source_operations VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sending', '', '[]', '[]', '[]', ?)""",
                (
                    operation_id,
                    self.client.service_identity,
                    self.cube_id,
                    self.user_id,
                    self.namespace,
                    summary["evidence_revision"],
                    str(evidence_path),
                    _canonical(summary).decode(),
                    payload_hash,
                    _now(),
                ),
            )
            claimed = cursor.rowcount == 1
        row = self._row(operation_id)
        if row["payload_hash"] != payload_hash:
            raise ValueError("evidence_payload_conflict")
        return row, claimed

    def _finish(self, operation_id: str, status: str, reason: str = "", *, snapshots=None) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = dict(
                db.execute(
                    "SELECT * FROM source_operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
            )
            # A second caller can be reconciling while the first POST finishes.
            # Its stale empty query must not downgrade a completed observation.
            if status == "pending_reconcile" and row["status"] == "completed":
                return self._result(row)
            paths = row["snapshots"] if snapshots is None else json.dumps(snapshots)
            baseline = row["baseline_snapshots"]
            if status == "completed" and baseline == "[]":
                baseline = paths
            db.execute(
                """UPDATE source_operations SET status=?, reason=?, snapshots=?,
                baseline_snapshots=?, updated_at=? WHERE operation_id=?""",
                (status, reason, paths, baseline, _now(), operation_id),
            )
            db.execute(
                "INSERT INTO source_observations VALUES (NULL, ?, ?, ?, ?, ?)",
                (operation_id, _now(), status, reason, paths),
            )
        return self._result(self._row(operation_id))

    @staticmethod
    def _result(row: dict) -> dict:
        return {
            "status": row["status"],
            "operation_id": row["operation_id"],
            "reason": row["reason"],
            "memory_ids": json.loads(row["memory_ids"]),
            "snapshots": json.loads(row["snapshots"]),
        }

    @staticmethod
    def _validated_ids(ids: list[str]) -> list[str]:
        if not ids or any(not isinstance(item, str) or not item for item in ids):
            raise _ReadbackError("pending_reconcile", "actual_memory_ids_missing")
        if len(set(ids)) != len(ids):
            raise _ReadbackError("conflict", "duplicate_memory_ids")
        return sorted(ids)

    def _ids(self, operation_id: str, ids: list[str]) -> None:
        ids = self._validated_ids(ids)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(
                db.execute(
                    "SELECT memory_ids FROM source_operations WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
            )
            if current and current != ids:
                raise _ReadbackError("conflict", "memory_id_set_changed")
            db.execute(
                "UPDATE source_operations SET memory_ids=? WHERE operation_id=?",
                (json.dumps(ids), operation_id),
            )

    def ingest(self, evidence_path: Path) -> dict:
        evidence_path = Path(evidence_path).resolve()
        summary = self._summary(evidence_path)
        row, claimed = self._claim(evidence_path, summary)
        operation_id = row["operation_id"]
        recovered_ids = None
        try:
            if claimed:
                response = self.client.add(self._payload(summary, operation_id))
                items = response.get("data")
                if not isinstance(items, list) or not items:
                    raise _ReadbackError("pending_reconcile", "actual_memory_ids_missing")
                if any(
                    not isinstance(item, dict) or item.get("cube_id") != self.cube_id
                    for item in items
                ):
                    raise _ReadbackError("invalid", "add_cube_mismatch")
                self._ids(operation_id, [item.get("memory_id") for item in items])
            elif not json.loads(row["memory_ids"]):
                recovered_ids = self._recover(row, summary)
        except MemoryClientError:
            return self._finish(operation_id, "pending_reconcile", "delivery_outcome_unknown")
        except _ReadbackError as error:
            return self._finish(operation_id, error.status, str(error))
        return self._observe(self._row(operation_id), summary, recovered_ids=recovered_ids)

    def _recover(self, row: dict, summary: dict) -> list[str]:
        # Product's user_id parameter alone does not restrict this listing route.
        # Both exact metadata filters AND readback validation enforce ownership.
        filters = {**self._info(summary, row["operation_id"]), "user_id": self.user_id}
        response = self.client.find(self.cube_id, self.user_id, filters)
        data = response.get("data")
        groups = data.get("text_mem") if isinstance(data, dict) else None
        if not isinstance(groups, list) or not groups:
            raise _ReadbackError("pending_reconcile", "recovery_not_visible")
        ids = []
        for group in groups:
            if not isinstance(group, dict) or group.get("cube_id") != self.cube_id:
                raise _ReadbackError("invalid", "recovery_cube_mismatch")
            records = group.get("memories")
            if not isinstance(records, list) or group.get("total_nodes") != len(records):
                raise _ReadbackError("pending_reconcile", "recovery_incomplete")
            for record in records:
                self._validate_record(record, summary, row["operation_id"])
                ids.append(record["id"])
        # An empty successful query does not prove a previous POST did not commit.
        # Listing may observe only some chunks before the entire write is
        # visible. Do not pin this set until GET and full coverage both succeed.
        return self._validated_ids(ids)

    def _validate_record(self, record, summary: dict, operation_id: str) -> list[str]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("id"), str)
            or not record["id"]
        ):
            raise _ReadbackError("invalid", "memory_identity_missing")
        metadata, body = record.get("metadata"), record.get("memory")
        if not isinstance(metadata, dict) or not isinstance(body, str) or not body.strip():
            raise _ReadbackError("invalid", "memory_content_missing")
        expected = {**self._info(summary, operation_id), "user_id": self.user_id}
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise _ReadbackError("invalid", "memory_provenance_mismatch")
        if any(
            key in metadata and metadata[key] != expected_value
            for key, expected_value in (
                ("owner_id", self.user_id),
                ("owner_type", "user"),
                ("cube_id", self.cube_id),
            )
        ):
            raise _ReadbackError("invalid", "memory_owner_mismatch")
        sources = metadata.get("sources")
        if not isinstance(sources, list) or not sources:
            raise _ReadbackError("invalid", "memory_sources_missing")
        fragments = []
        for source in sources:
            fragment = source.get("content") if isinstance(source, dict) else None
            # P1b supports short factual summaries with an explicit body/source
            # correspondence. The real reader can also keep a FULL source on
            # each PARTIAL chunk; that shape needs a future chunk contract and
            # fails closed here rather than guessing a source-to-body mapping.
            if (
                not isinstance(fragment, str)
                or not fragment.strip()
                or fragment not in summary["content"]
                or fragment not in body
            ):
                raise _ReadbackError("invalid", "memory_source_content_mismatch")
            fragments.append(fragment)
        return fragments

    def _observe(self, row: dict, summary: dict, *, recovered_ids=None) -> dict:
        operation_id = row["operation_id"]
        try:
            baseline = [self._read_snapshot(path) for path in json.loads(row["baseline_snapshots"])]
            for path in json.loads(row["snapshots"]):
                self._read_snapshot(path)
            responses, fragment_groups = [], []
            for memory_id in recovered_ids or json.loads(row["memory_ids"]):
                response = self.client.get(memory_id, self.cube_id)
                record = response.get("data")
                if record is None:
                    raise _ReadbackError("unavailable", "memory_not_available")
                fragments = self._validate_record(record, summary, operation_id)
                if record["id"] != memory_id:
                    raise _ReadbackError("invalid", "memory_id_mismatch")
                responses.append(response)
                fragment_groups.append(fragments)
            # Different returned IDs may represent distinct chunks. Identical
            # sources under multiple IDs are ambiguous duplicate delivery.
            signatures = [_digest(sorted(parts)) for parts in fragment_groups]
            if len(signatures) != len(set(signatures)):
                raise _ReadbackError("conflict", "duplicate_source_mapping")
            covered = [False] * len(summary["content"])
            for fragment in (part for group in fragment_groups for part in group):
                start = summary["content"].find(fragment)
                while start >= 0:
                    covered[start : start + len(fragment)] = [True] * len(fragment)
                    start = summary["content"].find(fragment, start + 1)
            if not all(covered):
                raise _ReadbackError(
                    "pending_reconcile" if recovered_ids is not None else "invalid",
                    "source_summary_coverage_incomplete",
                )
            if recovered_ids is not None:
                self._ids(operation_id, recovered_ids)
            snapshots = [self._snapshot(row, response, summary) for response in responses]
            old = {item["memory_id"]: item["content_sha256"] for item in baseline}
            new = {item["memory_id"]: item["content_sha256"] for item in snapshots}
            status = "changed" if old and old != new else "completed"
            return self._finish(
                operation_id,
                status,
                "memory_changed_since_baseline" if status == "changed" else "",
                snapshots=[item["snapshot_path"] for item in snapshots],
            )
        except MemoryClientError:
            return self._finish(operation_id, "unavailable", "memory_read_unavailable")
        except _ReadbackError as error:
            return self._finish(operation_id, error.status, str(error))
        except (OSError, ValueError, KeyError, TypeError):
            return self._finish(operation_id, "invalid", "snapshot_integrity_failure")

    def _atomic(self, directory: Path, name: str, data: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / name
        fd, temporary = tempfile.mkstemp(prefix=".observation-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # A hard link publishes complete bytes atomically without ever
                # replacing an existing immutable observation.
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != data:
                    raise ValueError("snapshot_collision") from None
        finally:
            os.unlink(temporary)
        return target

    def _snapshot(self, row: dict, response: dict, summary: dict) -> dict:
        # Credentials or private reasoning returned by a backend are not stored.
        public_response = sanitize_public(response)
        record = public_response["data"]
        if record["memory"] != response["data"]["memory"]:
            raise _ReadbackError("invalid", "private_memory_content")
        self._validate_record(record, summary, row["operation_id"])
        raw = _canonical(public_response)
        response_hash = hashlib.sha256(raw).hexdigest()
        response_path = self._atomic(self.snapshot_root / "responses", response_hash + ".json", raw)
        value = {
            "schema_version": 1,
            "backend": "memos_product_api",
            "service_identity": self.client.service_identity,
            "cube_id": self.cube_id,
            "user_id": self.user_id,
            "namespace": self.namespace,
            "operation_id": row["operation_id"],
            "memory_id": record["id"],
            "observed_at": _now(),
            "evidence_revision": row["evidence_revision"],
            "evidence_path": row["evidence_path"],
            "case_id": summary["case_id"],
            "lineage_id": summary["lineage_id"],
            "qualification": summary["qualification"],
            "split": "source",
            "memory": record["memory"],
            "metadata": record["metadata"],
            "sources": record["metadata"]["sources"],
            "body_sha256": hashlib.sha256(record["memory"].encode()).hexdigest(),
            "content_sha256": _digest(record),
            "response_sha256": response_hash,
            "response_ref": str(response_path.relative_to(self.snapshot_root)),
            "response_projection": "public",
        }
        value["snapshot_id"] = _digest(value)
        path = self._atomic(
            self.snapshot_root / "snapshots", value["snapshot_id"] + ".json", _canonical(value)
        )
        return {**value, "snapshot_path": str(path)}

    def _confined(self, path: Path) -> Path:
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.snapshot_root):
            raise ValueError("snapshot_path_invalid")
        return path.resolve()

    def _read_snapshot(self, path: Path) -> dict:
        path = self._confined(path)
        value = json.loads(path.read_bytes())
        identifier = value.pop("snapshot_id")
        if identifier != _digest(value) or path.stem != identifier:
            raise ValueError("snapshot_hash_mismatch")
        if any(
            value.get(key) != expected
            for key, expected in (
                ("service_identity", self.client.service_identity),
                ("cube_id", self.cube_id),
                ("user_id", self.user_id),
                ("namespace", self.namespace),
            )
        ):
            raise ValueError("snapshot_scope_mismatch")
        response_path = self._confined(self.snapshot_root / value["response_ref"])
        raw = response_path.read_bytes()
        response = json.loads(raw)
        record = response["data"]
        if (
            hashlib.sha256(raw).hexdigest() != value["response_sha256"]
            or _digest(record) != value["content_sha256"]
            or record["memory"] != value["memory"]
            or record["metadata"] != value["metadata"]
            or record["metadata"]["sources"] != value["sources"]
            or hashlib.sha256(value["memory"].encode()).hexdigest() != value["body_sha256"]
        ):
            raise ValueError("snapshot_response_mismatch")
        return {**value, "snapshot_id": identifier, "snapshot_path": str(path)}

    def refresh(self, snapshot_path: Path) -> dict:
        snapshot = self._read_snapshot(snapshot_path)
        row = self._row(snapshot["operation_id"])
        summary = self._summary(Path(row["evidence_path"]))
        if row["payload_hash"] != _digest(self._payload(summary, row["operation_id"])):
            raise ValueError("evidence_payload_conflict")
        result = self._observe(row, summary)
        if result["status"] == "completed":
            result = {**result, "status": "unchanged"}
        return result

    def list_sources(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                """SELECT operation_id FROM source_operations WHERE service_identity=?
                AND cube_id=? AND user_id=? AND namespace=? AND status='completed'
                ORDER BY operation_id""",
                (self.client.service_identity, self.cube_id, self.user_id, self.namespace),
            ).fetchall()
        result = []
        for row in rows:
            try:
                operation = self._row(row["operation_id"])
                verified = self.ingest(Path(operation["evidence_path"]))
                if verified["status"] == "completed":
                    result.extend(self._read_snapshot(path) for path in verified["snapshots"])
            except (OSError, ValueError, KeyError, TypeError):
                # An old qualification or a Memory label cannot bypass a fresh
                # Evidence check when supplying extraction inputs.
                continue
        return result
