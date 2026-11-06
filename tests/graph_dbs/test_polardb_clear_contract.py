import pytest

from memos.graph_dbs.polardb import PolarDBGraphDB


class _FailingCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, *args, **kwargs):
        raise RuntimeError("db down")


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def cursor(self):
        return _FailingCursor()


def test_clear_propagates_database_errors():
    """delete_all must not report success when the store failed to clear.

    Neo4j's clear() already re-raises; PolarDB used to log and return, which
    turned a failed tenant wipe into a successful API response.
    """
    db = object.__new__(PolarDBGraphDB)
    db.db_name = "memos"
    db._get_connection = lambda: _Connection()

    with pytest.raises(RuntimeError, match="db down"):
        db.clear(user_name="alice")
