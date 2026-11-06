"""Maintenance scripts must keep Qdrant in sync with their raw Cypher writes."""

import importlib.util

from pathlib import Path


def _load(name: str):
    path = Path(__file__).resolve().parents[2] / "scripts" / "maintenance" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRollbackRunner:
    def __init__(self, ids):
        self.ids = list(ids)

    def __call__(self, query, params):
        if "DETACH DELETE" in query:
            wanted = set(params["ids"])
            self.ids = [node_id for node_id in self.ids if node_id not in wanted]
            return []
        if "MATCH (n:Memory)" in query:
            return [{"id": node_id} for node_id in self.ids]
        raise AssertionError(query)


def test_rollback_removes_vectors_for_deleted_coarse_nodes():
    module = _load("rollback_two_stage")
    runner = FakeRollbackRunner(["c1", "c2"])
    cleaned = []

    stats = module.rollback(
        runner,
        task_id="task-1",
        apply=True,
        vector_cleanup=cleaned.extend,
    )

    assert stats == {"matched": 2, "deleted": 2}
    assert cleaned == ["c1", "c2"]


def test_rollback_dry_run_does_not_touch_vectors():
    module = _load("rollback_two_stage")
    runner = FakeRollbackRunner(["c1"])
    cleaned = []

    module.rollback(runner, task_id="task-1", apply=False, vector_cleanup=cleaned.extend)

    assert cleaned == []
    assert runner.ids == ["c1"]


class FakeExpireRunner:
    def __init__(self, ids):
        self.ids = list(ids)
        self.updated = []

    def __call__(self, query, params):
        if "SET n.status = 'expired'" in query:
            wanted = set(params["ids"])
            self.updated.extend(params["ids"])
            self.ids = [node_id for node_id in self.ids if node_id not in wanted]
            return []
        if "MATCH (n:Memory)" in query:
            rows = [
                {"id": node_id}
                for node_id in self.ids
                if params.get("last_id") is None or node_id > params["last_id"]
            ]
            return rows[: params["limit"]]
        raise AssertionError(query)


def test_expire_syncs_the_vector_payload():
    module = _load("expire_memories")
    runner = FakeExpireRunner(["m1", "m2"])
    synced = []

    stats = module.expire_batch(
        runner,
        apply=True,
        now="2026-10-21T00:00:00+00:00",
        vector_sync=lambda ids, cutoff: synced.append((ids, cutoff)),
    )

    assert stats == {"matched": 2, "expired": 2}
    assert synced == [(["m1", "m2"], "2026-10-21T00:00:00+00:00")]
    assert runner.updated == ["m1", "m2"]


def test_expire_dry_run_does_not_write_or_sync():
    module = _load("expire_memories")
    runner = FakeExpireRunner(["m1"])
    synced = []

    stats = module.expire_batch(
        runner,
        apply=False,
        vector_sync=lambda ids, cutoff: synced.append(ids),
    )

    assert stats == {"matched": 1, "expired": 0}
    assert runner.updated == []
    assert synced == []
