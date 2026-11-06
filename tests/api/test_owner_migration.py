"""T3.4 owner migration (dry-run, idempotent) and read-scope isolation."""

import importlib.util

from pathlib import Path
from types import SimpleNamespace

import memos.api.handlers.add_handler  # noqa: F401  (breaks the import cycle)

from memos.multi_mem_cube.single_cube import SingleCubeView


def _load_script():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "maintenance"
        / "backfill_owner_fields.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_owner_fields", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    """Applies the documented Neo4j queries to an in-memory node list."""

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.updates = 0

    def __call__(self, query, params):
        if "SET n.owner_type" in query:
            wanted = {row["id"]: row for row in params["rows"]}
            for row in self.rows:
                if row["id"] in wanted and row.get("owner_type") is None:
                    row.update(wanted[row["id"]])
            self.updates += 1
            return []
        if "MATCH (n:Memory)" in query:
            rows = [row for row in self.rows if row.get("owner_type") is None]
            tenant = params.get("tenant")
            if tenant is not None:
                rows = [row for row in rows if row.get("user_name") == tenant]
            rows.sort(key=lambda row: row["id"])
            return rows[: params["limit"]]
        raise AssertionError(f"unexpected query: {query}")


def test_backfill_dry_run_writes_nothing():
    module = _load_script()
    runner = FakeRunner(
        [
            {"id": "n1", "user_name": "tenant-1", "user_id": "u1", "owner_type": None},
            {"id": "n2", "user_name": "tenant-1", "user_id": "u2", "owner_type": None},
        ]
    )

    stats = module.backfill_neo4j(runner, apply=False)

    assert stats == {"scanned": 2, "planned": 2, "written": 0}
    assert all(row["owner_type"] is None for row in runner.rows)
    assert runner.updates == 0


def test_backfill_apply_sets_user_owner_from_user_id():
    module = _load_script()
    runner = FakeRunner(
        [{"id": "n1", "user_name": "tenant-1", "user_id": "u1", "owner_type": None}]
    )

    stats = module.backfill_neo4j(runner, apply=True)

    assert stats == {"scanned": 1, "planned": 1, "written": 1}
    assert runner.rows[0]["owner_type"] == "user"
    assert runner.rows[0]["owner_id"] == "u1"


def test_backfill_ownerless_nodes_become_public():
    module = _load_script()
    runner = FakeRunner(
        [{"id": "n1", "user_name": "tenant-1", "user_id": None, "owner_type": None}]
    )

    module.backfill_neo4j(runner, apply=True)

    assert runner.rows[0]["owner_type"] == "public"
    assert runner.rows[0]["owner_id"] == "tenant-1"


def test_backfill_is_idempotent():
    module = _load_script()
    runner = FakeRunner(
        [{"id": "n1", "user_name": "tenant-1", "user_id": "u1", "owner_type": None}]
    )

    first = module.backfill_neo4j(runner, apply=True)
    second = module.backfill_neo4j(runner, apply=True)

    assert first["written"] == 1
    assert second == {"scanned": 0, "planned": 0, "written": 0}
    assert runner.updates == 1


def test_backfill_scopes_to_a_tenant():
    module = _load_script()
    runner = FakeRunner(
        [
            {"id": "n1", "user_name": "tenant-1", "owner_type": None},
            {"id": "n2", "user_name": "tenant-2", "owner_type": None},
        ]
    )

    stats = module.backfill_neo4j(runner, apply=True, tenant="tenant-1")

    assert stats["written"] == 1
    by_id = {row["id"]: row for row in runner.rows}
    assert by_id["n1"]["owner_type"] == "public"
    assert by_id["n2"]["owner_type"] is None


def test_plan_prefers_user_id_and_skips_rows_without_any_owner():
    module = _load_script()

    planned = module.plan_neo4j_rows(
        [
            {"id": "n1", "user_name": None, "user_id": "u1"},
            {"id": "n2", "user_name": None, "user_id": None},
        ]
    )

    assert planned == [{"id": "n1", "owner_type": "user", "owner_id": "u1"}]


def test_plan_falls_back_to_tenant_public_owner():
    module = _load_script()

    planned = module.plan_neo4j_rows([{"id": "n1", "user_name": "tenant-1", "user_id": None}])

    assert planned == [{"id": "n1", "owner_type": "public", "owner_id": "tenant-1"}]


# ── read-side isolation ──────────────────────────────────────────────────────


def _scope(monkeypatch, **request):
    monkeypatch.delenv("MEMOS_OWNER_SCOPE_ENFORCED", raising=False)
    cube = object.__new__(SingleCubeView)
    cube.cube_id = "tenant-1"
    return cube._with_owner_scope(SimpleNamespace(**request), None)


def _branches(scope):
    return {(b.get("owner_type"), b.get("owner_id"), b.get("user_id")) for b in scope["or"]}


def test_private_user_scope_excludes_other_users(monkeypatch):
    alice = _scope(monkeypatch, user_id="u1", agent_id=None, group_id=None)
    bob = _scope(monkeypatch, user_id="u2", agent_id=None, group_id=None)

    assert ("user", "u1", None) in _branches(alice)
    assert ("user", "u1", None) not in _branches(bob)
    assert ("user", None, "u1") not in _branches(bob)
    assert ("public", "tenant-1", None) in _branches(alice) & _branches(bob)


def test_agent_scope_only_sees_its_own_private_memory(monkeypatch):
    agent_a = _scope(monkeypatch, user_id="u1", agent_id="a1", group_id=None)
    agent_b = _scope(monkeypatch, user_id="u1", agent_id="a2", group_id=None)

    assert ("agent", "a1", None) in _branches(agent_a)
    assert ("agent", "a1", None) not in _branches(agent_b)


def test_group_scope_is_only_added_when_requested(monkeypatch):
    without_group = _scope(monkeypatch, user_id="u1", agent_id=None, group_id=None)
    with_group = _scope(monkeypatch, user_id="u1", agent_id=None, group_id="g-1")

    assert ("group", "g-1", None) not in _branches(without_group)
    assert ("group", "g-1", None) in _branches(with_group)


def test_scope_is_merged_with_the_client_filter(monkeypatch):
    monkeypatch.delenv("MEMOS_OWNER_SCOPE_ENFORCED", raising=False)
    cube = object.__new__(SingleCubeView)
    cube.cube_id = "tenant-1"
    client_filter = {"and": [{"session_id": "s1"}]}

    merged = cube._with_owner_scope(
        SimpleNamespace(user_id="u1", agent_id=None, group_id=None), client_filter
    )

    assert merged["and"][0] == client_filter


def test_scope_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MEMOS_OWNER_SCOPE_ENFORCED", "false")
    cube = object.__new__(SingleCubeView)
    cube.cube_id = "tenant-1"

    assert cube._with_owner_scope(
        SimpleNamespace(user_id="u1", agent_id=None, group_id=None), {"x": 1}
    ) == {"x": 1}


def test_empty_subject_fails_closed(monkeypatch):
    monkeypatch.delenv("MEMOS_OWNER_SCOPE_ENFORCED", raising=False)
    cube = object.__new__(SingleCubeView)
    cube.cube_id = "tenant-1"

    scope = cube._with_owner_scope(
        SimpleNamespace(user_id="   ", agent_id=None, group_id=None), None
    )

    assert scope == {"owner_type": "__none__"}


def test_legacy_ownerless_summaries_stay_readable(monkeypatch):
    scope = _scope(monkeypatch, user_id="u1", agent_id=None, group_id=None)

    assert (None, None, None) in _branches(scope)
