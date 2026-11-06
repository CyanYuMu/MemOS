"""Deleting an agent's independent memories (cloud "delete all memory of an agent")."""

from types import SimpleNamespace

import pytest

from fastapi import HTTPException
from pydantic import ValidationError

from memos.api.handlers.memory_handler import handle_delete_memories
from memos.api.product_models import DeleteMemoryRequest


def _matches(node, condition):
    for key, expected in condition.items():
        actual = node.get(key)
        if isinstance(expected, dict) and "in" in expected:
            if actual not in expected["in"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeTextMem:
    """Graph store stand-in that honours the AND-of-keys filter semantics."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.deleted_ids = []

    def get_all(self, user_name=None, filter=None, **kwargs):
        conditions = (filter or {}).get("and", [])
        matched = [
            node for node in self.nodes.values() if all(_matches(node, c) for c in conditions)
        ]
        return {"nodes": matched, "total_nodes": len(matched)}

    def delete_by_memory_ids(self, memory_ids):
        self.deleted_ids.extend(memory_ids)
        for memory_id in memory_ids:
            self.nodes.pop(memory_id, None)

    def delete_by_filter(self, **kwargs):  # pragma: no cover - guard path must be used
        raise AssertionError("agent delete must go through the frozen-aware path")


class FakePrefMem:
    def __init__(self):
        self.calls = []

    def delete_by_filter(self, filter):
        self.calls.append(filter)


def _nodes():
    return {
        "agent-a1": {
            "id": "agent-a1",
            "user_name": "tenant-1",
            "owner_type": "agent",
            "owner_id": "a1",
            "agent_id": "a1",
            "status": "activated",
        },
        "agent-a1-frozen": {
            "id": "agent-a1-frozen",
            "user_name": "tenant-1",
            "owner_type": "agent",
            "owner_id": "a1",
            "status": "frozen",
        },
        # A user's own memory that merely carries the agent id.
        "user-u1": {
            "id": "user-u1",
            "user_name": "tenant-1",
            "owner_type": "user",
            "owner_id": "u1",
            "agent_id": "a1",
            "status": "activated",
        },
        "agent-a2": {
            "id": "agent-a2",
            "user_name": "tenant-1",
            "owner_type": "agent",
            "owner_id": "a2",
            "status": "activated",
        },
        "other-tenant": {
            "id": "other-tenant",
            "user_name": "tenant-2",
            "owner_type": "agent",
            "owner_id": "a1",
            "status": "activated",
        },
    }


def _cube(nodes, pref_mem=None):
    return SimpleNamespace(text_mem=FakeTextMem(nodes), pref_mem=pref_mem)


def test_agent_delete_removes_only_agent_owned_nodes_in_the_tenant():
    nodes = _nodes()
    cube = _cube(nodes)

    resp = handle_delete_memories(
        DeleteMemoryRequest(agent_id="a1", writable_cube_ids=["tenant-1"]), cube
    )

    assert resp.data["status"] == "success"
    assert cube.text_mem.deleted_ids == ["agent-a1"]
    assert set(nodes) == {"agent-a1-frozen", "user-u1", "agent-a2", "other-tenant"}


def test_agent_delete_keeps_frozen_nodes():
    nodes = _nodes()
    cube = _cube(nodes)

    handle_delete_memories(DeleteMemoryRequest(agent_id="a1", writable_cube_ids=["tenant-1"]), cube)

    assert "agent-a1-frozen" in nodes


def test_agent_delete_accepts_user_id_as_the_tenant():
    nodes = _nodes()
    cube = _cube(nodes)

    resp = handle_delete_memories(DeleteMemoryRequest(agent_id="a1", user_id="tenant-1"), cube)

    assert resp.data["status"] == "success"
    assert cube.text_mem.deleted_ids == ["agent-a1"]


def test_agent_delete_without_a_tenant_is_rejected():
    nodes = _nodes()
    cube = _cube(nodes)

    with pytest.raises(HTTPException) as exc:
        handle_delete_memories(DeleteMemoryRequest(agent_id="a1"), cube)

    assert exc.value.status_code == 400
    assert "tenant" in exc.value.detail
    assert cube.text_mem.deleted_ids == []
    assert len(nodes) == 5


def test_agent_delete_never_touches_user_preference_vectors():
    pref = FakePrefMem()
    cube = _cube(_nodes(), pref_mem=pref)

    handle_delete_memories(DeleteMemoryRequest(agent_id="a1", user_id="tenant-1"), cube)

    assert pref.calls == []


def test_agent_delete_cannot_be_combined_with_memory_ids():
    cube = _cube(_nodes())

    with pytest.raises(HTTPException) as exc:
        handle_delete_memories(
            DeleteMemoryRequest(agent_id="a1", user_id="tenant-1", memory_ids=["agent-a1"]), cube
        )

    assert exc.value.status_code == 400
    assert cube.text_mem.deleted_ids == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_agent_id_is_rejected_by_the_model(blank):
    with pytest.raises(ValidationError):
        DeleteMemoryRequest(agent_id=blank, user_id="tenant-1")
