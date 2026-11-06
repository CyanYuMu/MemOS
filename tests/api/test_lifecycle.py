"""T2.5 lifecycle: expired / frozen guards, cleanup and recall exclusion."""

import pytest

from memos.api.handlers.search_handler import SearchHandler
from memos.api.lifecycle_rules import expire_memories, lifecycle_guard


class FakeGraph:
    def __init__(self, nodes):
        self.nodes = {node["id"]: dict(node) for node in nodes}
        self.updates = []

    def export_graph(self, **kwargs):
        statuses = kwargs.get("status")
        cutoff = (kwargs.get("filter") or {}).get("expires_at", {}).get("lte")
        nodes = []
        for node in self.nodes.values():
            if statuses and node.get("status") not in statuses:
                continue
            if cutoff is not None and (node.get("expires_at") or "9999") > cutoff:
                continue
            nodes.append(dict(node))
        return {"nodes": nodes, "total_nodes": len(nodes)}

    def update_node(self, node_id, fields, user_name=None):
        self.updates.append((node_id, dict(fields)))
        self.nodes[node_id].update(fields)


def _node(node_id, status="activated", expires_at=None):
    return {
        "id": node_id,
        "user_name": "tenant-1",
        "status": status,
        "expires_at": expires_at,
        "metadata": {"status": status},
    }


@pytest.mark.parametrize("action", ["update", "delete"])
def test_frozen_rejects_update_and_delete(action):
    reason = lifecycle_guard("frozen", action)

    assert reason is not None
    assert "frozen" in reason


def test_expired_is_read_only_but_deletable():
    assert "expired" in lifecycle_guard("expired", "update")
    assert lifecycle_guard("expired", "delete") is None


def test_allowed_transitions():
    assert lifecycle_guard("activated", "update", "archived") is None
    assert lifecycle_guard("activated", "update", "frozen") is None
    assert lifecycle_guard("archived", "update", "activated") is None
    assert lifecycle_guard("archived", "update", "expired") is None


def test_disallowed_transitions():
    assert "not allowed" in lifecycle_guard("activated", "update", "resolving")
    assert "not allowed" in lifecycle_guard("archived", "update", "deleted")
    assert "read-only" in lifecycle_guard("expired", "update", "activated")
    assert "frozen" in lifecycle_guard("frozen", "update", "activated")


def test_unknown_target_status_is_rejected():
    assert "unsupported" in lifecycle_guard("activated", "update", "banana")


def test_missing_status_is_treated_as_activated():
    assert lifecycle_guard(None, "update", "archived") is None


def test_expire_marks_only_past_due_nodes():
    graph = FakeGraph(
        [
            _node("due", expires_at="2026-01-01T00:00:00+00:00"),
            _node("future", expires_at="2099-01-01T00:00:00+00:00"),
            _node("frozen", status="frozen", expires_at="2026-01-01T00:00:00+00:00"),
            _node("already", status="expired", expires_at="2026-01-01T00:00:00+00:00"),
        ]
    )

    expired = expire_memories(graph, now="2026-10-21T00:00:00+00:00")

    assert expired == ["due"]
    assert graph.nodes["due"]["status"] == "expired"
    assert graph.nodes["future"]["status"] == "activated"
    assert graph.nodes["frozen"]["status"] == "frozen"
    assert graph.nodes["already"]["status"] == "expired"


def test_expire_is_idempotent():
    graph = FakeGraph([_node("due", expires_at="2026-01-01T00:00:00+00:00")])

    assert expire_memories(graph, now="2026-10-21T00:00:00+00:00") == ["due"]
    assert expire_memories(graph, now="2026-10-21T00:00:00+00:00") == []
    assert len(graph.updates) == 1


def test_expire_survives_graph_errors():
    class BrokenGraph:
        def export_graph(self, **kwargs):
            raise RuntimeError("neo4j down")

    assert expire_memories(BrokenGraph(), now="2026-10-21T00:00:00+00:00") == []


def _results(*statuses):
    return {
        "text_mem": [
            {
                "cube_id": "c1",
                "memories": [
                    {
                        "id": f"m{i}",
                        "metadata": {
                            "status": status,
                            "relativity": 1.0 if status == "resolving" else 0.5,
                        },
                    }
                    for i, status in enumerate(statuses)
                ],
            }
        ]
    }


def _ids(results):
    return [mem["id"] for mem in results["text_mem"][0]["memories"]]


def test_recall_excludes_expired_frozen_and_unproven(monkeypatch):
    monkeypatch.delenv("TWO_STAGE_ADD", raising=False)

    results = SearchHandler._drop_inactive(
        _results("activated", "expired", "frozen", "archived", "resolving", None)
    )

    assert _ids(results) == ["m0"]


def test_recall_keeps_two_stage_coarse_results_when_enabled(monkeypatch):
    monkeypatch.setenv("TWO_STAGE_ADD", "true")

    results = SearchHandler._drop_inactive(_results("activated", "resolving"))

    assert _ids(results) == ["m0", "m1"]
    coarse = results["text_mem"][0]["memories"][1]
    assert coarse["metadata"]["relativity"] == pytest.approx(0.8)
