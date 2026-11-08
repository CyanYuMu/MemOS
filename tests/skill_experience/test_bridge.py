"""Durable delivery and source readback contracts, using synthetic HTTP outcomes."""

import copy
import hashlib
import json
import threading

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memos.skill_experience import bridge as module
from memos.skill_experience.bridge import MemoryBridge
from memos.skill_experience.client import MemoryClientError


class Product:
    service_identity = "synthetic-service"

    def __init__(self):
        self.add_calls = 0
        self.records = {}
        self.filters = []
        self.lose_response = False
        self.accept_only = False
        self.query_empty = False
        self.entered = None
        self.release = None
        self.chunks = None

    def add(self, payload):
        self.add_calls += 1
        self.payload = payload
        if self.entered:
            self.entered.set()
            self.release.wait(3)
        summary = payload["messages"][0]["content"]
        for index, part in enumerate(self.chunks or [summary]):
            memory_id = f"memory-{index}"
            self.records[memory_id] = {
                "id": memory_id,
                "memory": f"user: [time]: {part}\n",
                "metadata": {
                    **payload["info"],
                    "user_id": payload["user_id"],
                    "sources": [{"type": "chat", "role": "user", "content": part}],
                },
            }
        if self.lose_response:
            raise MemoryClientError("transport_outcome_unknown")
        if self.accept_only:
            return {"code": 200, "data": {"task_id": "accepted"}}
        return {
            "code": 200,
            "data": [
                {"memory_id": key, "cube_id": payload["writable_cube_ids"][0]}
                for key in self.records
            ],
        }

    def get(self, memory_id, cube_id):
        return {"code": 200, "data": copy.deepcopy(self.records.get(memory_id))}

    def find(self, cube_id, user_id, filters):
        self.filters.append(filters)
        values = [] if self.query_empty else list(self.records.values())
        return {
            "code": 200,
            "data": {
                "text_mem": [
                    {
                        "cube_id": cube_id,
                        "memories": copy.deepcopy(values),
                        "total_nodes": len(values),
                    }
                ]
            },
        }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("synthetic trusted collector artifact")
    summary = {
        "content": "Observed mock scope failure. Repaired fixture isolation. Tests passed.",
        "info": {"skill_evidence_uri": str(evidence), "skill_final_receipt_sha256": "receipt"},
        "evidence_revision": "evidence-1",
        "qualification": "qualified_success",
        "case_id": "source-case",
        "lineage_id": "lineage-1",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "split": "source",
    }
    monkeypatch.setattr(module, "verify_evidence", lambda path: {"verified": True})
    monkeypatch.setattr(module, "load_source_summary", lambda path: copy.deepcopy(summary))
    client = Product()
    ledger = tmp_path / "ledger.sqlite"
    snapshots = tmp_path / "snapshots"

    def make():
        return MemoryBridge(client, ledger, snapshots, cube_id="source-cube", user_id="owner")

    return make, client, evidence, summary


def test_completed_delivery_reads_actual_body_and_never_posts_again(setup):
    make, client, evidence, _ = setup
    result = make().ingest(evidence)
    assert result["status"] == "completed"
    assert result["memory_ids"] == ["memory-0"]
    snapshot = json.loads(Path(result["snapshots"][0]).read_text())
    assert snapshot["memory"] == client.records["memory-0"]["memory"]
    assert snapshot["sources"]
    assert snapshot["evidence_revision"] == "evidence-1"
    assert make().ingest(evidence)["status"] == "completed"
    assert client.add_calls == 1
    assert client.payload["async_mode"] == "sync"
    assert client.payload["mode"] == "fast"
    assert client.payload["allow_memory_view"] == ["detail_factual"]
    assert len(make().list_sources()) == 1


def test_lost_post_response_recovers_by_exact_scope_after_restart(setup):
    make, client, evidence, _ = setup
    client.lose_response = True
    assert make().ingest(evidence)["status"] == "pending_reconcile"
    result = make().ingest(evidence)
    assert result["status"] == "completed"
    assert client.add_calls == 1
    filters = client.filters[-1]
    assert filters["user_id"] == "owner"
    assert filters["skill_operation_id"] == result["operation_id"]
    assert filters["skill_evidence_revision"] == "evidence-1"
    assert filters["skill_split"] == "source"


def test_interrupted_sender_leaves_durable_claim_and_recovers_without_replay(setup, monkeypatch):
    make, client, evidence, _ = setup
    original = client.add

    def crash_after_commit(payload):
        original(payload)
        raise SystemExit("simulated process termination")

    monkeypatch.setattr(client, "add", crash_after_commit)
    with pytest.raises(SystemExit):
        make().ingest(evidence)
    assert make().ingest(evidence)["status"] == "completed"
    assert client.add_calls == 1


def test_unknown_or_empty_recovery_never_reposts(setup):
    make, client, evidence, _ = setup
    client.lose_response = client.query_empty = True
    assert make().ingest(evidence)["status"] == "pending_reconcile"
    assert make().ingest(evidence)["status"] == "pending_reconcile"
    assert client.add_calls == 1
    assert make().list_sources() == []


def test_task_id_receipt_waits_for_explicit_readback(setup):
    make, client, evidence, _ = setup
    client.accept_only = True
    assert make().ingest(evidence)["status"] == "pending_reconcile"
    assert make().list_sources() == []
    assert make().ingest(evidence)["status"] == "completed"
    assert client.add_calls == 1


def test_recovery_wrong_cube_cannot_be_relabelled(setup, monkeypatch):
    make, client, evidence, _ = setup
    client.lose_response = True
    make().ingest(evidence)
    original = client.find

    def wrong_cube(*args):
        result = original(*args)
        result["data"]["text_mem"][0]["cube_id"] = "foreign-cube"
        return result

    monkeypatch.setattr(client, "find", wrong_cube)
    assert make().ingest(evidence)["status"] == "invalid"
    assert make().list_sources() == []


def test_partial_listing_does_not_appear_complete(setup, monkeypatch):
    make, client, evidence, _ = setup
    client.lose_response = True
    make().ingest(evidence)
    original = client.find

    def incomplete(*args):
        result = original(*args)
        result["data"]["text_mem"][0]["total_nodes"] += 1
        return result

    monkeypatch.setattr(client, "find", incomplete)
    assert make().ingest(evidence)["status"] == "pending_reconcile"
    assert make().list_sources() == []


def test_recovery_waits_for_complete_chunk_coverage_before_fixing_ids(setup):
    make, client, evidence, summary = setup
    text = summary["content"]
    client.chunks = [text[:30], text[30:]]
    client.lose_response = True
    assert make().ingest(evidence)["status"] == "pending_reconcile"
    later_chunk = client.records.pop("memory-1")
    incomplete = make().ingest(evidence)
    assert incomplete["status"] == "pending_reconcile"
    assert incomplete["memory_ids"] == []
    client.records["memory-1"] = later_chunk
    assert make().ingest(evidence)["status"] == "completed"
    assert client.add_calls == 1


def test_same_ledger_concurrent_claim_does_not_replay_http(setup):
    make, client, evidence, _ = setup
    first, second = make(), make()
    client.entered, client.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(first.ingest, evidence)
        assert client.entered.wait(2)
        assert second.ingest(evidence)["status"] == "pending_reconcile"
        client.release.set()
        assert pending.result(timeout=3)["status"] == "completed"
    assert client.add_calls == 1


@pytest.mark.parametrize(
    "split,qualification",
    [
        ("holdout", "qualified_success"),
        ("dev", "qualified_success"),
        ("source", "needs_evidence"),
        ("source", "excluded"),
    ],
)
def test_ineligible_evidence_never_reaches_memory(setup, split, qualification):
    make, client, evidence, summary = setup
    summary.update(split=split, qualification=qualification)
    with pytest.raises(ValueError, match="ineligible_source"):
        make().ingest(evidence)
    assert client.add_calls == 0


def test_evidence_reverified_at_ingest_and_source_export(setup, monkeypatch):
    make, client, evidence, _ = setup
    assert make().ingest(evidence)["status"] == "completed"

    def invalid(_):
        raise ValueError("evidence_corrupt")

    monkeypatch.setattr(module, "verify_evidence", invalid)
    with pytest.raises(ValueError, match="evidence_corrupt"):
        make().ingest(evidence)
    assert make().list_sources() == []
    assert client.add_calls == 1


@pytest.mark.parametrize(
    "field",
    [
        "skill_operation_id",
        "skill_evidence_revision",
        "skill_namespace",
        "skill_case_id",
        "skill_lineage_id",
        "skill_run_id",
        "skill_attempt_id",
        "skill_split",
        "skill_summary_sha256",
        "skill_evidence_uri",
        "skill_owner",
        "user_id",
    ],
)
def test_readback_missing_or_wrong_scope_cannot_export(setup, field):
    make, client, evidence, _ = setup
    result = make().ingest(evidence)
    client.records["memory-0"]["metadata"].pop(field)
    assert make().refresh(result["snapshots"][0])["status"] == "invalid"
    assert make().list_sources() == []


@pytest.mark.parametrize("mutation", ["no_sources", "empty_body", "unrelated_body", "wrong_source"])
def test_readback_must_include_corresponding_body_and_sources(setup, mutation):
    make, client, evidence, _ = setup
    result = make().ingest(evidence)
    record = client.records["memory-0"]
    if mutation == "no_sources":
        record["metadata"]["sources"] = []
    elif mutation == "empty_body":
        record["memory"] = ""
    elif mutation == "unrelated_body":
        record["memory"] = "unrelated replacement text"
    else:
        record["metadata"]["sources"][0]["content"] = "not this evidence"
    assert make().refresh(result["snapshots"][0])["status"] == "invalid"


def test_changed_and_missing_memory_preserve_old_snapshot(setup):
    make, client, evidence, _ = setup
    result = make().ingest(evidence)
    old = Path(result["snapshots"][0])
    old_bytes = old.read_bytes()
    client.records["memory-0"]["metadata"]["review_note"] = "modified"
    changed = make().refresh(old)
    assert changed["status"] == "changed"
    assert Path(changed["snapshots"][0]) != old
    assert old.read_bytes() == old_bytes
    assert make().list_sources() == []
    assert make().ingest(evidence)["status"] == "changed"
    assert make().list_sources() == []
    client.records.clear()
    assert make().refresh(old)["status"] == "unavailable"
    assert old.read_bytes() == old_bytes


def test_multiple_distinct_chunks_supported_but_duplicate_mapping_rejected(setup):
    make, client, evidence, summary = setup
    text = summary["content"]
    client.chunks = [text[:30], text[30:]]
    result = make().ingest(evidence)
    assert result["status"] == "completed"
    assert len(result["snapshots"]) == 2
    assert len(make().list_sources()) == 2


def test_product_full_source_with_partial_chunk_body_fails_closed(setup):
    make, client, evidence, summary = setup
    result = make().ingest(evidence)
    # The real reader may retain the full original SourceMessage on every
    # chunk. P1b does not infer a chunk mapping from these repeated sources.
    client.records["memory-0"]["memory"] = summary["content"][:30]
    rejected = make().refresh(result["snapshots"][0])
    assert rejected["status"] == "invalid"
    assert rejected["reason"] == "memory_source_content_mismatch"
    assert make().list_sources() == []


def test_recovery_does_not_accept_duplicate_records_or_wrong_cube(setup):
    make, client, evidence, _ = setup
    client.lose_response = True
    make().ingest(evidence)
    duplicate = copy.deepcopy(client.records["memory-0"])
    duplicate["id"] = "memory-duplicate"
    client.records["memory-duplicate"] = duplicate
    assert make().ingest(evidence)["status"] == "conflict"
    assert make().list_sources() == []


def test_snapshot_corruption_is_not_exported(setup):
    make, _, evidence, _ = setup
    result = make().ingest(evidence)
    path = Path(result["snapshots"][0])
    value = json.loads(path.read_text())
    value["memory"] = "forged"
    path.write_text(json.dumps(value))
    assert make().list_sources() == []
    with pytest.raises(ValueError, match="snapshot"):
        make().refresh(path)


def test_changed_payload_cannot_reuse_operation_identity(setup):
    make, client, evidence, summary = setup
    make().ingest(evidence)
    summary["content"] += " changed under same revision"
    with pytest.raises(ValueError, match="evidence_payload_conflict"):
        make().ingest(evidence)
    assert client.add_calls == 1


def test_snapshot_artifacts_are_atomic_and_content_addressed(setup):
    make, _, evidence, _ = setup
    result = make().ingest(evidence)
    path = Path(result["snapshots"][0])
    value = json.loads(path.read_text())
    identifier = value.pop("snapshot_id")
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    assert identifier == hashlib.sha256(canonical).hexdigest() == path.stem
    assert not list(path.parent.glob("*.tmp"))


def test_snapshot_publish_failure_cannot_leave_partial_artifact_or_replay_post(setup, monkeypatch):
    make, client, evidence, _ = setup
    instance = make()
    original = module.os.link

    def disk_failure(*_):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(module.os, "link", disk_failure)
    assert instance.ingest(evidence)["status"] == "invalid"
    assert not list(instance.snapshot_root.rglob("*.json"))
    assert not list(instance.snapshot_root.rglob("*.tmp"))
    monkeypatch.setattr(module.os, "link", original)
    assert make().ingest(evidence)["status"] == "completed"
    assert client.add_calls == 1


def test_snapshots_do_not_serialize_private_backend_fields(setup):
    make, client, evidence, _ = setup
    initial = make().ingest(evidence)
    client.records["memory-0"]["metadata"]["authorization"] = "Bearer synthetic-private"
    result = make().refresh(initial["snapshots"][0])
    assert result["status"] == "changed"
    snapshot = Path(result["snapshots"][0]).read_text()
    assert "synthetic-private" not in snapshot
    assert "[REDACTED]" in snapshot
