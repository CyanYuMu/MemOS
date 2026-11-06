"""G8: one schema initialisation per database, connections always closed.

The store opens a fresh SQLite connection per call. Running ``CREATE TABLE`` and
the legacy migration inside a write transaction on every one of those made pure
reads take the write lock, leaked the connection when migration raised, and
turned "database is locked" into a 500.
"""

import sqlite3
import threading

import pytest

from memos.exceptions import PromptBusyError
from memos.mem_reader import prompt_store


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "connect.db"))


def test_schema_and_migration_run_once_per_database(monkeypatch):
    calls = []
    monkeypatch.setattr(prompt_store, "_migrate_legacy", lambda conn: calls.append(1))

    for _ in range(5):
        prompt_store._connect().close()

    assert len(calls) == 1


def test_the_first_connection_initialises_only_once_under_concurrency(monkeypatch, tmp_path):
    path = tmp_path / "concurrent.db"
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(path))
    calls = []
    monkeypatch.setattr(prompt_store, "_migrate_legacy", lambda conn: calls.append(1))

    threads = [threading.Thread(target=lambda: prompt_store._connect().close()) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(calls) == 1


def test_a_failed_migration_closes_the_connection(monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(prompt_store.sqlite3, "connect", tracking_connect)

    def boom(_conn):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(prompt_store, "_migrate_legacy", boom)
    with pytest.raises(RuntimeError):
        prompt_store._connect()

    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("SELECT 1")


def test_a_locked_database_is_reported_as_busy(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMOS_PROMPT_DB_TIMEOUT", "0.2")
    prompt_store.save_slot("cube-lock", "extract.chat", "zh", body="甲 ${conversation}")

    blocker = sqlite3.connect(prompt_store._db_path(), timeout=5)
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(PromptBusyError) as caught:
            prompt_store.save_slot("cube-lock", "extract.chat", "zh", body="乙 ${conversation}")
        assert caught.value.retry_after_sec >= 1
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
