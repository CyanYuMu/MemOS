"""T3.5 two-stage add: default off, coarse visibility, idempotency, rollback."""

import importlib.util

from pathlib import Path
from types import SimpleNamespace

# Import the handlers package first: it breaks the cycle between
# ``memos.api.handlers`` and ``memos.multi_mem_cube.single_cube``.
import memos.api.handlers.add_handler  # noqa: F401

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.two_stage import (
    archive_coarse_nodes,
    build_coarse_items,
    coarse_node_id,
    content_hash,
    rollback_coarse_nodes,
    two_stage_enabled,
)
from memos.multi_mem_cube.single_cube import SingleCubeView


def _item(text, memory_type="LongTermMemory"):
    return TextualMemoryItem(
        memory=text,
        metadata=TreeNodeTextualMemoryMetadata(memory_type=memory_type, status="activated"),
    )


class FakeGraph:
    def __init__(self, nodes=None):
        self.nodes = {node["id"]: dict(node) for node in nodes or []}
        self.edges = []
        self.deleted = []

    def export_graph(self, **kwargs):
        filters = kwargs.get("filter") or {}

        def flat(node):
            merged = dict(node.get("metadata") or {})
            merged.update({k: v for k, v in node.items() if k != "metadata"})
            return merged

        nodes = [
            dict(node)
            for node in self.nodes.values()
            if all(flat(node).get(key) == value for key, value in filters.items())
        ]
        return {"nodes": nodes, "total_nodes": len(nodes)}

    def update_node(self, node_id, fields, user_name=None):
        self.nodes[node_id].update(fields)

    def add_edge(self, source, target, edge_type, user_name=None):
        self.edges.append((source, target, edge_type))

    def delete_node(self, node_id, user_name=None):
        self.deleted.append(node_id)
        self.nodes.pop(node_id, None)


def test_two_stage_is_off_by_default(monkeypatch):
    monkeypatch.delenv("TWO_STAGE_ADD", raising=False)
    assert two_stage_enabled() is False

    monkeypatch.setenv("TWO_STAGE_ADD", "false")
    assert two_stage_enabled() is False

    monkeypatch.setenv("TWO_STAGE_ADD", "true")
    assert two_stage_enabled() is True


def test_coarse_id_is_deterministic_per_task_and_content():
    first = coarse_node_id("task-1", "hello")
    second = coarse_node_id("task-1", "hello")

    assert first == second
    assert first != coarse_node_id("task-2", "hello")
    assert first != coarse_node_id("task-1", "hello!")
    # The same task/content under another tenant or owner must not collide.
    assert first != coarse_node_id("task-1", "hello", "tenant-2:user:u1")
    assert first != coarse_node_id("task-1", "hello", "tenant-1:user:u2")


def test_coarse_items_are_copies_with_coarse_metadata():
    original = _item("hello")
    items = build_coarse_items([original], task_id="task-1")

    assert len(items) == 1
    coarse = items[0]
    assert coarse.id == coarse_node_id("task-1", "hello", ":unowned")
    assert coarse.memory == "hello"
    assert coarse.metadata.status == "resolving"
    assert coarse.metadata.stage == "coarse"
    assert coarse.metadata.task_id == "task-1"
    assert coarse.metadata.is_fast is True
    # The fine item is not mutated.
    assert original.metadata.status == "activated"
    assert original.metadata.stage is None


def test_archive_marks_coarse_and_links_to_fine_result():
    graph = FakeGraph(
        [
            {
                "id": "coarse-1",
                "metadata": {
                    "task_id": "task-1",
                    "stage": "coarse",
                    "status": "resolving",
                    "content_hash": content_hash("hello"),
                },
            }
        ]
    )
    fine = _item("hello")
    fine.id = "fine-1"

    archived = archive_coarse_nodes(
        graph, user_name="tenant-1", task_id="task-1", fine_memories=[fine]
    )
    assert archived == 1
    assert graph.nodes["coarse-1"]["status"] == "archived"
    assert graph.edges == [("coarse-1", "fine-1", "MERGED_TO")]


def test_archive_is_idempotent():
    graph = FakeGraph(
        [
            {
                "id": "coarse-1",
                "metadata": {"task_id": "task-1", "stage": "coarse", "status": "archived"},
            }
        ]
    )

    assert (
        archive_coarse_nodes(graph, user_name="tenant-1", task_id="task-1", fine_memories=[]) == 0
    )


def test_archive_ignores_other_tasks():
    graph = FakeGraph(
        [
            {
                "id": "coarse-1",
                "metadata": {"task_id": "task-2", "stage": "coarse", "status": "resolving"},
            }
        ]
    )

    assert (
        archive_coarse_nodes(graph, user_name="tenant-1", task_id="task-1", fine_memories=[]) == 0
    )
    assert graph.nodes["coarse-1"]["metadata"]["status"] == "resolving"


def test_rollback_deletes_only_coarse_nodes_of_the_task():
    graph = FakeGraph(
        [
            {
                "id": "coarse-1",
                "metadata": {"task_id": "task-1", "stage": "coarse", "status": "resolving"},
            },
            {
                "id": "fine-1",
                "metadata": {"task_id": "task-1", "stage": "fine", "status": "activated"},
            },
        ]
    )

    assert rollback_coarse_nodes(graph, user_name="tenant-1", task_id="task-1") == 1
    assert graph.deleted == ["coarse-1"]
    assert "fine-1" in graph.nodes


# ── add path: coarse write is gated by the flag ──────────────────────────────


class FakeTextMem:
    def __init__(self):
        self.added = []
        self.graph_store = FakeGraph()

    def add(self, items, user_name=None):
        self.added.append((list(items), user_name))
        return [f"fine-{i}" for i in range(len(items))]


def _cube(items):
    cube = object.__new__(SingleCubeView)
    cube.logger = SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None
    )
    cube.cube_id = "tenant-1"
    cube.naive_mem_cube = SimpleNamespace(text_mem=FakeTextMem())
    cube.mem_reader = SimpleNamespace(save_rawfile=False)
    cube._extract_text_mem = lambda *a, **k: (items, "fast", 0.0)
    cube._schedule_memory_tasks = lambda **kwargs: None
    return cube


def _add_req(task_id="task-1"):
    return SimpleNamespace(
        task_id=task_id,
        user_id="u1",
        session_id="s1",
        messages=[{"role": "user", "content": "hello"}],
    )


def test_add_writes_only_fine_stage_when_flag_is_off(monkeypatch):
    monkeypatch.delenv("TWO_STAGE_ADD", raising=False)
    cube = _cube([_item("hello")])

    cube._process_text_mem(_add_req(), SimpleNamespace(mem_cube_id="tenant-1"), "fast")

    assert len(cube.naive_mem_cube.text_mem.added) == 1
    fine_items, _ = cube.naive_mem_cube.text_mem.added[0]
    assert fine_items[0].metadata.status == "activated"
    assert fine_items[0].metadata.stage is None


def test_add_writes_coarse_before_fine_when_flag_is_on(monkeypatch):
    monkeypatch.setenv("TWO_STAGE_ADD", "true")
    cube = _cube([_item("hello")])

    cube._process_text_mem(_add_req(), SimpleNamespace(mem_cube_id="tenant-1"), "fast")

    assert len(cube.naive_mem_cube.text_mem.added) == 2
    coarse_items, _ = cube.naive_mem_cube.text_mem.added[0]
    fine_items, _ = cube.naive_mem_cube.text_mem.added[1]
    assert coarse_items[0].metadata.status == "resolving"
    assert coarse_items[0].metadata.stage == "coarse"
    assert fine_items[0].metadata.status == "activated"


def test_add_without_task_id_skips_the_coarse_stage(monkeypatch):
    monkeypatch.setenv("TWO_STAGE_ADD", "true")
    cube = _cube([_item("hello")])

    cube._process_text_mem(_add_req(task_id=None), SimpleNamespace(mem_cube_id="tenant-1"), "fast")

    assert len(cube.naive_mem_cube.text_mem.added) == 1


# ── rollback script ──────────────────────────────────────────────────────────


def _load_script():
    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "maintenance" / "rollback_two_stage.py"
    )
    spec = importlib.util.spec_from_file_location("rollback_two_stage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.queries = []

    def __call__(self, query, params):
        self.queries.append((query, params))
        if "DETACH DELETE" in query:
            wanted = set(params["ids"])
            self.rows = [row for row in self.rows if row["id"] not in wanted]
            return []
        return [dict(row) for row in self.rows]


def test_rollback_script_dry_run_writes_nothing():
    module = _load_script()
    runner = FakeRunner([{"id": "coarse-1"}])

    stats = module.rollback(runner, task_id="task-1", apply=False)

    assert stats == {"matched": 1, "deleted": 0}
    assert all("DETACH DELETE" not in query for query, _ in runner.queries)
    assert runner.rows == [{"id": "coarse-1"}]


def test_rollback_script_applies_and_is_idempotent():
    module = _load_script()
    runner = FakeRunner([{"id": "coarse-1"}, {"id": "coarse-2"}])

    assert module.rollback(runner, task_id="task-1", apply=True) == {
        "matched": 2,
        "deleted": 2,
    }
    assert module.rollback(runner, task_id="task-1", apply=True) == {
        "matched": 0,
        "deleted": 0,
    }
