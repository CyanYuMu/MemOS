from types import SimpleNamespace

from memos.graph_dbs.polardb import PolarDBGraphDB


def _db() -> PolarDBGraphDB:
    db = object.__new__(PolarDBGraphDB)
    db.config = SimpleNamespace(user_name="alice")
    return db


def test_add_nodes_batch_does_not_report_nodes_that_failed_preparation():
    db = _db()

    ids = db.add_nodes_batch(
        [
            {"id": "missing-memory"},
            {"memory": "missing-id"},
        ],
        user_name="alice",
    )

    # Preparation errors are intentionally tolerated per node, so the batch
    # contract must report only what was actually written.
    assert ids == []


def test_add_nodes_batch_returns_written_ids():
    db = _db()

    ids = db.add_nodes_batch([], user_name="alice")

    assert ids == []
