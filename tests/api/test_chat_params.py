"""Tests for cloud-compatible chat request fields."""

from memos.api.handlers.chat_handler import ChatHandler
from memos.api.product_models import APIChatCompleteRequest, ChatRequest


def test_memory_limit_number_overrides_top_k():
    req = ChatRequest(user_id="u", query="q", memory_limit_number=5)

    assert req.top_k == 5


def test_memory_limit_number_is_capped_at_25():
    try:
        ChatRequest(user_id="u", query="q", memory_limit_number=26)
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected validation error")


def test_chat_complete_request_supports_memory_limit_number():
    req = APIChatCompleteRequest(user_id="u", query="q", memory_limit_number=7)

    assert req.top_k == 7


def test_writeback_meta_merges_agent_and_app_ids():
    req = ChatRequest(
        user_id="u",
        query="q",
        tags=["t1"],
        info={"scene": "chat"},
        agent_id="agent-1",
        app_id="app-1",
    )

    tags, info = ChatHandler._writeback_meta(req)

    assert tags == ["t1"]
    assert info == {"scene": "chat", "agent_id": "agent-1", "app_id": "app-1"}


def test_writeback_meta_empty_info_is_none():
    req = ChatRequest(user_id="u", query="q")

    tags, info = ChatHandler._writeback_meta(req)

    assert tags is None
    assert info is None


def _writeback(**kwargs):
    import asyncio

    from types import SimpleNamespace

    captured = []
    handler = object.__new__(ChatHandler)
    handler.add_handler = SimpleNamespace(handle_add_memories=captured.append)
    asyncio.run(
        handler._add_conversation_to_memory(
            user_id="u",
            writable_cube_ids=["c"],
            session_id="s",
            query="q",
            clean_response="a",
            **kwargs,
        )
    )
    return captured[0]


def test_chat_request_allow_public_defaults_to_false():
    assert ChatRequest(user_id="u", query="q").allow_public is False
    assert APIChatCompleteRequest(user_id="u", query="q").allow_public is False


def test_chat_writeback_forwards_allow_public():
    assert _writeback(allow_public=True).allow_public is True


def test_chat_writeback_is_private_by_default():
    assert _writeback().allow_public is False
