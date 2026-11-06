"""T2.3 add status: task status plus the cloud ``memory_views`` summary."""

from types import SimpleNamespace

from memos.api.handlers.cloud_handler import handle_add_status
from memos.api.product_models import AddStatusRequestV2


class FakeTracker:
    def __init__(self, by_business=None, by_task=None):
        self.by_business = by_business
        self.by_task = by_task
        self.calls = []

    def get_task_status_by_business_id(self, task_id, user_id):
        self.calls.append(("business", task_id, user_id))
        return self.by_business

    def get_task_status(self, task_id, user_id):
        self.calls.append(("task", task_id, user_id))
        return self.by_task


class FakeTextMem:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def get_all(self, **kwargs):
        self.calls.append(kwargs)
        return {"nodes": list(self.nodes), "total_nodes": len(self.nodes)}


def _node(node_id, memory_type):
    return {"id": node_id, "memory": "x", "metadata": {"memory_type": memory_type}}


def _cube(nodes):
    return SimpleNamespace(text_mem=FakeTextMem(nodes))


def test_status_is_mapped_and_views_grouped_by_memory_type():
    tracker = FakeTracker(by_business={"status": "completed"})
    cube = _cube(
        [
            _node("a", "LongTermMemory"),
            _node("b", "LongTermMemory"),
            _node("c", "PreferenceMemory"),
            _node("d", "EventMemory"),
        ]
    )

    resp = handle_add_status(
        AddStatusRequestV2(user_id="u1", task_id="t-1"), tracker, cube
    )

    assert resp.code == 200
    assert resp.data["status"] == "completed"
    views = resp.data["memory_views"]
    assert views["added"] == 4
    assert views["detail_factual"]["memory_ids"] == ["a", "b"]
    assert views["preference"]["memory_ids"] == ["c"]
    assert views["event"]["memory_ids"] == ["d"]
    assert cube.text_mem.calls[0]["user_name"] == "u1"
    assert cube.text_mem.calls[0]["filter"] == {"task_id": "t-1"}


def test_running_task_reports_partial_views():
    """Views are collected live so two-stage coarse results are observable."""
    tracker = FakeTracker(by_business={"status": "running"})
    cube = _cube([_node("a", "LongTermMemory")])

    resp = handle_add_status(AddStatusRequestV2(user_id="u1", task_id="t-1"), tracker, cube)

    assert resp.data["status"] == "running"
    assert resp.data["memory_views"]["detail_factual"]["memory_ids"] == ["a"]


def test_failed_task_exposes_errors():
    tracker = FakeTracker(by_business={"status": "failed", "errors": ["boom"]})

    resp = handle_add_status(
        AddStatusRequestV2(user_id="u1", task_id="t-1"), tracker, _cube([])
    )

    assert resp.data["status"] == "failed"
    assert resp.data["errors"] == ["boom"]


def test_unknown_task_is_not_found():
    tracker = FakeTracker()

    resp = handle_add_status(
        AddStatusRequestV2(user_id="u1", task_id="missing"), tracker, _cube([])
    )

    assert resp.code == 404
    assert resp.data["status"] == "not_found"
    assert resp.data["memory_views"] == {"added": 0, "updated": 0, "deleted": 0}


def test_business_id_lookup_is_preferred_over_raw_task_id():
    tracker = FakeTracker(by_business={"status": "completed"}, by_task={"status": "failed"})

    resp = handle_add_status(AddStatusRequestV2(user_id="u1", task_id="t-1"), tracker, _cube([]))

    assert resp.data["status"] == "completed"
    assert tracker.calls[0][0] == "business"
    assert len(tracker.calls) == 1


def test_falls_back_to_task_id_lookup():
    tracker = FakeTracker(by_business=None, by_task={"status": "completed"})

    resp = handle_add_status(AddStatusRequestV2(user_id="u1", task_id="t-1"), tracker, _cube([]))

    assert resp.data["status"] == "completed"
    assert [call[0] for call in tracker.calls] == ["business", "task"]


def test_mem_cube_id_wins_as_tenant_scope():
    cube = _cube([])
    handle_add_status(
        AddStatusRequestV2(user_id="u1", task_id="t-1", mem_cube_id="cube-9"),
        FakeTracker(by_business={"status": "completed"}),
        cube,
    )

    assert cube.text_mem.calls[0]["user_name"] == "cube-9"
