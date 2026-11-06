from types import SimpleNamespace

import pytest

from memos.api.handlers.add_handler import AddHandler
from memos.api.handlers.cloud_handler import handle_get_message
from memos.api.handlers.memory_handler import handle_delete_memories
from memos.api.message_store import JsonlMessageStore, Neo4jMessageStore, normalize_messages
from memos.api.product_models import APIADDRequest, DeleteMemoryRequest, GetMessageRequest


def _text_mem():
    return SimpleNamespace(
        graph_store=None,
        get_all=lambda **kwargs: {"nodes": [], "total_nodes": 0},
        get_by_ids=lambda **kwargs: [],
        delete_by_filter=lambda **kwargs: None,
        delete_by_memory_ids=lambda ids: None,
    )


def _add(store, tenant, user_ids, conversation, texts):
    store.add(
        user_name=tenant,
        user_ids=user_ids,
        conversation_id=conversation,
        messages=[{"role": "user", "content": text} for text in texts],
    )


def test_normalize_keeps_text_and_drops_empty_parts():
    messages = normalize_messages(
        [
            {"role": "user", "content": "hi", "chat_time": "2026-10-21 10:00"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            {"role": "user", "content": "  "},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        ]
    )

    assert [m["content"] for m in messages] == ["hi", "hello"]
    assert messages[0]["chat_time"] == "2026-10-21 10:00"
    assert normalize_messages("plain")[0]["role"] == "user"


def test_store_returns_latest_window_oldest_first_and_scopes_by_user(tmp_path):
    store = JsonlMessageStore(tmp_path)
    _add(store, "t1", ["alice", "bob"], "c1", ["one", "two"])
    _add(store, "t1", ["alice"], "c1", ["three"])
    _add(store, "t1", ["alice"], "c2", ["other conversation"])
    _add(store, "t2", ["alice"], "c1", ["other tenant"])

    alice = store.list(user_name="t1", user_id="alice", conversation_id="c1", limit=6)
    first = store.list(user_name="t1", user_id="alice", conversation_id="c1", limit=1)
    window = store.list(user_name="t1", user_id="alice", conversation_id="c1", limit=2)
    bob = store.list(user_name="t1", user_id="bob", conversation_id="c1", limit=6)

    assert [m["content"] for m in alice] == ["one", "two", "three"]
    assert [m["content"] for m in first] == ["three"]
    assert [m["content"] for m in window] == ["two", "three"]
    assert [m["content"] for m in bob] == ["one", "two"]
    assert all(isinstance(m["create_time"], int) for m in alice)


def test_store_delete_by_conversation_or_tenant(tmp_path):
    store = JsonlMessageStore(tmp_path)
    _add(store, "t1", ["alice"], "c1", ["a", "b"])
    _add(store, "t1", ["alice"], "c2", ["c"])
    _add(store, "t2", ["alice"], "c1", ["d"])

    assert store.delete(user_names=["t1"], conversation_id="c1") == 2
    assert store.list(user_name="t1", user_id="alice", conversation_id="c2", limit=6)
    assert store.delete(user_names=["t1"]) == 1
    assert not store.list(user_name="t1", user_id="alice", conversation_id="c2", limit=6)
    assert store.list(user_name="t2", user_id="alice", conversation_id="c1", limit=6)
    assert store.delete(user_names=None, conversation_id="c1") == 1
    assert not store.list(user_name="t2", user_id="alice", conversation_id="c1", limit=6)
    with pytest.raises(ValueError, match="user_names or conversation_id"):
        store.delete()


def test_neo4j_store_reads_latest_window_and_deletes_by_scope():
    calls = []

    class Result:
        def __init__(self, records):
            self.records = records

        def __iter__(self):
            return iter(self.records)

        def consume(self):
            return SimpleNamespace(counters=SimpleNamespace(nodes_deleted=2))

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            newest_first = [
                {"m": {"message_id": "2", "content": "b"}},
                {"m": {"message_id": "1", "content": "a"}},
            ]
            return Result(newest_first)

    store = Neo4jMessageStore(SimpleNamespace(session=lambda: Session()))

    window = store.list(user_name="t1", user_id="alice", conversation_id="c1", limit=2)
    removed = store.delete(user_names=None, conversation_id="c1")

    assert [m["content"] for m in window] == ["a", "b"]
    list_query = calls[0][0]
    assert "$user_id IN m.user_ids" in list_query
    assert "ORDER BY m.order DESC, m.seq DESC" in list_query
    assert removed == 2
    assert "user_name" not in calls[1][0]
    assert calls[1][1] == {"conversation_id": "c1"}


def test_get_message_defaults_the_tenant_to_user_id(tmp_path):
    store = JsonlMessageStore(tmp_path)
    _add(store, "alice", ["alice"], "c1", ["hello"])

    resp = handle_get_message(GetMessageRequest(user_id="alice", session_id="c1"), store)

    assert resp.code == 0
    assert [m["content"] for m in resp.data["message_detail_list"]] == ["hello"]
    with pytest.raises(ValueError, match="conversation_id"):
        GetMessageRequest(user_id="alice")
    with pytest.raises(ValueError, match="blank"):
        GetMessageRequest(user_id=" ", session_id="c1")
    assert GetMessageRequest(user_id="alice", session_id="c1", mem_cube_id=" ").mem_cube_id is None


def test_add_stores_messages_unless_switched_off(tmp_path, monkeypatch):
    handler = object.__new__(AddHandler)
    handler.logger = SimpleNamespace(warning=lambda *a, **k: None)
    handler._message_store = JsonlMessageStore(tmp_path)
    req = APIADDRequest(
        user_id="alice",
        session_id="c1",
        messages=[{"role": "user", "content": "remember this"}],
    )

    handler._store_raw_messages(
        APIADDRequest(user_id="alice", messages=[{"role": "user", "content": "no session"}])
    )
    assert not list(tmp_path.rglob("*.jsonl"))

    handler._store_raw_messages(req)
    stored = handler._message_store.list(
        user_name="alice", user_id="alice", conversation_id="c1", limit=6
    )
    monkeypatch.setenv("MEMOS_STORE_RAW_MESSAGES", "false")
    handler._store_raw_messages(req)

    assert [m["content"] for m in stored] == ["remember this"]
    assert (
        len(
            handler._message_store.list(
                user_name="alice", user_id="alice", conversation_id="c1", limit=6
            )
        )
        == 1
    )


def test_delete_user_or_session_takes_raw_messages_along(tmp_path):
    store = JsonlMessageStore(tmp_path)
    _add(store, "alice", ["alice"], "c1", ["a"])
    _add(store, "alice", ["alice"], "c2", ["b"])
    cube = SimpleNamespace(text_mem=_text_mem(), pref_mem=None)

    handle_delete_memories(
        DeleteMemoryRequest(user_id="alice", session_id="c1"), cube, message_store=store
    )
    assert not store.list(user_name="alice", user_id="alice", conversation_id="c1", limit=6)
    assert store.list(user_name="alice", user_id="alice", conversation_id="c2", limit=6)

    handle_delete_memories(DeleteMemoryRequest(user_id="alice"), cube, message_store=store)
    assert not store.list(user_name="alice", user_id="alice", conversation_id="c2", limit=6)

    _add(store, "alice", ["alice"], "c3", ["x"])
    _add(store, "bob", ["bob"], "c3", ["y"])
    handle_delete_memories(DeleteMemoryRequest(session_id="c3"), cube, message_store=store)
    assert not store.list(user_name="alice", user_id="alice", conversation_id="c3", limit=6)
    assert not store.list(user_name="bob", user_id="bob", conversation_id="c3", limit=6)


def test_delete_by_memory_ids_keeps_raw_messages(tmp_path):
    store = JsonlMessageStore(tmp_path)
    _add(store, "alice", ["alice"], "c1", ["a"])
    cube = SimpleNamespace(text_mem=_text_mem(), pref_mem=None)

    handle_delete_memories(DeleteMemoryRequest(memory_ids=["m1"]), cube, message_store=store)

    assert store.list(user_name="alice", user_id="alice", conversation_id="c1", limit=6)
