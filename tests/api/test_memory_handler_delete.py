from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from memos.api.handlers.memory_handler import handle_delete_memories
from memos.api.product_models import DeleteMemoryRequest


def _build_naive_mem_cube() -> Mock:
    naive_mem_cube = Mock()
    naive_mem_cube.text_mem = Mock()
    naive_mem_cube.pref_mem = Mock()
    return naive_mem_cube


def test_delete_memories_quick_by_user_id():
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(user_id="u_1")

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_filter.assert_called_once_with(
        writable_cube_ids=None,
        filter={"and": [{"user_name": "u_1"}]},
    )
    naive_mem_cube.pref_mem.delete_by_filter.assert_called_once_with(
        filter={"and": [{"user_id": "u_1"}]}
    )


def test_delete_memories_quick_by_conversation_alias():
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(conversation_id="conv_1")

    assert req.session_id == "conv_1"

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_filter.assert_called_once_with(
        writable_cube_ids=None,
        filter={"and": [{"session_id": "conv_1"}]},
    )
    naive_mem_cube.pref_mem.delete_by_filter.assert_called_once_with(
        filter={"and": [{"session_id": "conv_1"}]}
    )


def test_delete_memories_filter_and_quick_conditions():
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(
        filter={"and": [{"memory_type": "WorkingMemory"}]},
        user_id="u_1",
        session_id="s_1",
    )

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_filter.assert_called_once_with(
        writable_cube_ids=None,
        filter={
            "and": [
                {"memory_type": "WorkingMemory"},
                {"user_name": "u_1", "session_id": "s_1"},
            ]
        },
    )
    naive_mem_cube.pref_mem.delete_by_filter.assert_called_once_with(
        filter={
            "and": [
                {"memory_type": "WorkingMemory"},
                {"user_id": "u_1", "session_id": "s_1"},
            ]
        }
    )


def test_delete_memories_filter_or_with_distribution():
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(
        filter={"or": [{"memory_type": "WorkingMemory"}, {"memory_type": "UserMemory"}]},
        user_id="u_1",
    )

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_filter.assert_called_once_with(
        writable_cube_ids=None,
        filter={
            "or": [
                {"memory_type": "WorkingMemory", "user_name": "u_1"},
                {"memory_type": "UserMemory", "user_name": "u_1"},
            ]
        },
    )
    naive_mem_cube.pref_mem.delete_by_filter.assert_called_once_with(
        filter={
            "or": [
                {"memory_type": "WorkingMemory", "user_id": "u_1"},
                {"memory_type": "UserMemory", "user_id": "u_1"},
            ]
        }
    )


def test_delete_memories_reject_multiple_modes():
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(memory_ids=["m_1"], user_id="u_1")

    with pytest.raises(HTTPException) as exc:
        handle_delete_memories(req, naive_mem_cube)

    assert exc.value.status_code == 400
    assert "Exactly one delete mode must be provided" in exc.value.detail
    naive_mem_cube.text_mem.delete_by_filter.assert_not_called()
    naive_mem_cube.text_mem.delete_by_memory_ids.assert_not_called()


def test_delete_memories_reject_empty_filter():
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(filter={})

    with pytest.raises(HTTPException) as exc:
        handle_delete_memories(req, naive_mem_cube)

    assert exc.value.status_code == 400
    assert "filter cannot be empty" in exc.value.detail
    naive_mem_cube.text_mem.delete_by_filter.assert_not_called()
    naive_mem_cube.pref_mem.delete_by_filter.assert_not_called()


def test_delete_memories_with_pref_mem_disabled():
    naive_mem_cube = _build_naive_mem_cube()
    naive_mem_cube.pref_mem = None
    req = DeleteMemoryRequest(user_id="u_1")

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_filter.assert_called_once_with(
        writable_cube_ids=None,
        filter={"and": [{"user_name": "u_1"}]},
    )


def test_delete_memories_by_ids_uses_request_scope():
    naive_mem_cube = _build_naive_mem_cube()
    naive_mem_cube.text_mem.config.cube_id = "cube_fallback"
    req = DeleteMemoryRequest(memory_ids=["m_1"], writable_cube_ids=["cube_a", "cube_b"])

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_memory_ids.assert_called_once_with(
        ["m_1"], writable_cube_ids=["cube_a", "cube_b"]
    )


def test_delete_memories_by_ids_falls_back_to_cube_id():
    naive_mem_cube = _build_naive_mem_cube()
    naive_mem_cube.text_mem.config.cube_id = "cube_fallback"
    req = DeleteMemoryRequest(memory_ids=["m_1"])

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    naive_mem_cube.text_mem.delete_by_memory_ids.assert_called_once_with(
        ["m_1"], writable_cube_ids=["cube_fallback"]
    )


def test_delete_memories_by_ids_refuses_without_scope():
    naive_mem_cube = _build_naive_mem_cube()
    naive_mem_cube.text_mem.config.cube_id = None
    req = DeleteMemoryRequest(memory_ids=["m_1"])

    resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "failure"
    assert "writable_cube_ids is required" in resp.message
    naive_mem_cube.text_mem.delete_by_memory_ids.assert_not_called()


def test_delete_request_log_arguments_match_their_placeholders(caplog):
    naive_mem_cube = _build_naive_mem_cube()
    req = DeleteMemoryRequest(
        filter={"and": [{"memory_type": "LongTermMemory"}]},
        user_id="u_1",
        session_id="s_1",
    )

    with caplog.at_level("INFO"):
        resp = handle_delete_memories(req, naive_mem_cube)

    assert resp.data["status"] == "success"
    messages = [
        record.getMessage()
        for record in caplog.records
        if "Delete memory request" in record.getMessage()
    ]
    assert messages
    # Each placeholder must show its own value; a misordered argument list used
    # to shift has_filter/user_id/session_id into the wrong fields.
    assert "writable_cube_ids: None" in messages[-1]
    assert "memory_ids: None" in messages[-1]
    assert "file_ids: None" in messages[-1]
    assert "auto_cleanup_working: False" in messages[-1]
    assert "has_filter: True" in messages[-1]
    assert "user_id: u_1" in messages[-1]
    assert "agent_id: None" in messages[-1]
    assert "session_id: s_1" in messages[-1]
