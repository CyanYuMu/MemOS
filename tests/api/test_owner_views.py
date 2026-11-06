"""T2.4 / T3.3 / T3.4 owner views, scope filters and write trimming."""

from types import SimpleNamespace

import pytest

import memos.api.handlers.add_handler  # noqa: F401  (breaks the import cycle)

from memos.api.product_models import APIADDRequest
from memos.memories.textual.owner import (
    build_write_views,
    filter_by_allow_memory_view,
    inject_owner_scope,
    owner_scope_filter,
    stamp_owner,
)


def _messages():
    return [
        {"role": "user", "content": "I live in Beijing", "role_id": "u1", "role_name": "Alice"},
        {"role": "assistant", "content": "Noted", "role_id": "a1", "role_name": "Bot"},
        {
            "role": "user",
            "content": "Me too",
            "role_id": "u2",
            "role_name": "Bob",
            "chat_time": "2026-10-01T10:00:00Z",
        },
    ]


def _build(**kwargs):
    base = {
        "user_id": "u1",
        "agent_id": None,
        "group_id": None,
        "messages": _messages(),
        "info": {},
        "allow_public": False,
        "tenant": "tenant-1",
    }
    base.update(kwargs)
    return build_write_views(**base)


def test_single_user_request_keeps_historical_single_view():
    messages = _messages()
    views = _build(user_id="u1", messages=messages)

    assert len(views) == 1
    view = views[0]
    assert (view.owner.owner_type, view.owner.owner_id) == ("user", "u1")
    assert view.messages == messages  # untouched, no speaker rewriting
    assert view.messages[0]["content"] == "I live in Beijing"
    assert view.group_id is None


def test_group_chat_creates_one_view_per_participant_plus_group_view():
    views = _build(user_id=["u1", "u2"], group_id="g-1")

    owners = [(view.owner.owner_type, view.owner.owner_id) for view in views]
    assert owners == [("user", "u1"), ("user", "u2"), ("group", "g-1")]
    for view in views:
        assert view.group_id == "g-1"
    # Speaker names are carried in the text so the reader can attribute facts.
    assert views[0].messages[0]["content"] == "Alice: I live in Beijing"
    assert views[0].role_names == {"u1": "Alice", "a1": "Bot", "u2": "Bob"}


def test_group_participant_views_only_see_their_own_user_statements():
    views = _build(user_id=["u1", "u2"], group_id="g-1")
    u1_view, u2_view, group_view = views

    u1_contents = [m["content"] for m in u1_view.messages]
    u2_contents = [m["content"] for m in u2_view.messages]

    assert "Alice: I live in Beijing" in u1_contents
    assert "Bob: Me too" not in u1_contents
    assert "Bob: Me too" in u2_contents
    assert "Alice: I live in Beijing" not in u2_contents
    # The group view keeps the whole conversation (shared facts).
    assert {m["content"] for m in group_view.messages} >= {
        "Alice: I live in Beijing",
        "Bob: Me too",
    }
    # Assistant context is still visible to each participant.
    assert "Bot: Noted" in u1_contents


def test_group_view_does_not_mutate_the_request_messages():
    messages = _messages()
    _build(user_id=["u1", "u2"], group_id="g-1", messages=messages)

    assert messages[0]["content"] == "I live in Beijing"


def test_public_scope_uses_the_tenant_as_owner():
    views = _build(user_id="u1", allow_public=True)

    assert [(v.owner.owner_type, v.owner.owner_id) for v in views] == [
        ("user", "u1"),
        ("public", "tenant-1"),
    ]


def test_agent_memory_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MEMOS_AGENT_MEMORY_ENABLED", raising=False)
    views = _build(user_id="u1", agent_id="a1")

    assert [view.owner.owner_type for view in views] == ["user"]
    assert views[0].agent_id == "a1"  # still a filterable tag


def test_agent_memory_creates_private_agent_view_when_enabled(monkeypatch):
    monkeypatch.setenv("MEMOS_AGENT_MEMORY_ENABLED", "true")
    views = _build(user_id="u1", agent_id="a1")

    owners = [(view.owner.owner_type, view.owner.owner_id) for view in views]
    assert ("agent", "a1") in owners
    agent_view = next(view for view in views if view.owner.owner_type == "agent")
    assert agent_view.user_id is None


def test_more_than_twenty_owner_ids_are_rejected():
    with pytest.raises(ValueError, match="at most 20"):
        _build(user_id=[f"u{i}" for i in range(21)])


def test_api_request_validator_rejects_oversized_id_lists():
    with pytest.raises(ValueError, match="at most 20"):
        APIADDRequest(
            user_id=[f"u{i}" for i in range(21)],
            messages=[{"role": "user", "content": "hi"}],
        )


def test_api_request_accepts_group_fields():
    req = APIADDRequest(
        user_id=["u1", "u2"],
        agent_id=["a1"],
        group_id="g-1",
        allow_public=True,
        messages=[{"role": "user", "content": "hi", "role_id": "u1", "role_name": "Alice"}],
    )

    assert req.user_id == ["u1", "u2"]
    assert req.agent_id == ["a1"]
    assert req.group_id == "g-1"


def test_stamp_owner_writes_owner_and_compatibility_fields():
    view = _build(user_id="u1", agent_id="a1", group_id="g-1")[0]
    memory = SimpleNamespace(metadata=SimpleNamespace())

    stamp_owner(memory, view, task_id="t-1")

    assert memory.metadata.owner_type == "user"
    assert memory.metadata.owner_id == "u1"
    assert memory.metadata.user_id == "u1"
    assert memory.metadata.agent_id == "a1"
    assert memory.metadata.group_id == "g-1"
    assert memory.metadata.task_id == "t-1"


def test_owner_scope_includes_legacy_and_public_branches():
    scope = owner_scope_filter(user_ids=["u1"], tenant="tenant-1")

    assert {"owner_type": "user", "owner_id": "u1"} in scope["or"]
    assert {"owner_type": None, "user_id": "u1"} in scope["or"]
    assert {"owner_type": "public", "owner_id": "tenant-1"} in scope["or"]


def test_private_scopes_do_not_leak_across_viewers():
    alice = owner_scope_filter(user_ids=["u1"], tenant="tenant-1")
    bob = owner_scope_filter(user_ids=["u2"], tenant="tenant-1")

    def ids(scope):
        return {
            (branch.get("owner_type"), branch.get("owner_id"), branch.get("user_id"))
            for branch in scope["or"]
        }

    assert ("user", "u1", None) in ids(alice)
    assert ("user", "u1", None) not in ids(bob)
    assert ("user", None, "u1") not in ids(bob)
    # Both may read tenant-public memory, nothing else is shared.
    assert ("public", "tenant-1", None) in ids(alice) & ids(bob)


def test_group_and_agent_scopes_are_opt_in():
    scope = owner_scope_filter(user_ids=["u1"], include_public=False, include_legacy=False)

    assert scope == {"owner_type": "user", "owner_id": "u1"}

    agent_scope = owner_scope_filter(agent_ids=["a1"], include_public=False)
    assert {"owner_type": "agent", "owner_id": "a1"} in agent_scope["or"]
    assert {"owner_type": None, "agent_id": "a1"} in agent_scope["or"]


def test_owner_scope_is_anded_with_client_filter():
    client = {"and": [{"session_id": "s1"}]}
    scope = owner_scope_filter(user_ids=["u1"], tenant="tenant-1")

    merged = inject_owner_scope(client, scope)

    assert merged == {"and": [client, scope]}
    assert inject_owner_scope(client, None) == client
    assert inject_owner_scope(None, scope) == scope


def test_allow_memory_view_trims_write_types():
    memories = [
        SimpleNamespace(metadata=SimpleNamespace(memory_type="LongTermMemory")),
        SimpleNamespace(metadata=SimpleNamespace(memory_type="PreferenceMemory")),
        SimpleNamespace(metadata=SimpleNamespace(memory_type="EventMemory")),
    ]

    kept = filter_by_allow_memory_view(memories, ["detail_factual"])

    assert [m.metadata.memory_type for m in kept] == ["LongTermMemory"]


def test_allow_memory_view_none_keeps_historical_behavior():
    memories = [SimpleNamespace(metadata=SimpleNamespace(memory_type="PreferenceMemory"))]

    assert filter_by_allow_memory_view(memories, None) == memories
    assert filter_by_allow_memory_view(memories, []) == []


def test_single_cube_add_dispatches_one_isolated_view_per_owner():
    from types import SimpleNamespace

    from memos.multi_mem_cube.single_cube import SingleCubeView

    cube = object.__new__(SingleCubeView)
    cube.cube_id = "tenant-1"
    cube.logger = SimpleNamespace(info=lambda *a, **k: None)
    cube._get_sync_mode = lambda: "sync"
    seen = []

    def fake_process(req, _context, _sync, owner_view=None):
        seen.append((owner_view.owner.owner_type, owner_view.owner.owner_id, req.messages))
        return []

    cube._process_text_mem = fake_process
    cube.add_memories(
        APIADDRequest(
            user_id=["u1", "u2"],
            writable_cube_ids=["tenant-1"],
            group_id="g-1",
            messages=_messages(),
            mode="fast",
        )
    )

    by_owner = {(owner, owner_id): messages for owner, owner_id, messages in seen}
    assert set(by_owner) == {("user", "u1"), ("user", "u2"), ("group", "g-1")}
    assert "Alice: I live in Beijing" in [m["content"] for m in by_owner[("user", "u1")]]
    assert "Bob: Me too" not in [m["content"] for m in by_owner[("user", "u1")]]
    assert "Bob: Me too" in [m["content"] for m in by_owner[("group", "g-1")]]


def test_add_request_keeps_cloud_message_fields_for_group_attribution():
    req = APIADDRequest(
        user_id=["u1", "u2"],
        writable_cube_ids=["tenant-1"],
        group_id="g-1",
        messages=_messages(),
    )

    # ``messages`` is the OpenAI typed-dict union and drops role_id/chat_time;
    # the raw sidecar is what owner/event extraction reads.
    assert "role_id" not in req.messages[0]
    assert req.raw_messages[0]["role_id"] == "u1"
    assert req.raw_messages[2]["chat_time"] == "2026-10-01T10:00:00Z"


def test_unlabelled_user_message_is_not_shared_between_participants():
    messages = [
        {"role": "user", "content": "annotated for u1", "role_id": "u1", "role_name": "Alice"},
        {"role": "user", "content": "who said this?"},
        {"role": "assistant", "content": "context"},
    ]
    views = _build(user_id=["u1", "u2"], group_id="g-1", messages=messages)

    u1_contents = [m["content"] for m in views[0].messages]
    u2_contents = [m["content"] for m in views[1].messages]

    assert any("annotated for u1" in content for content in u1_contents)
    assert not any("who said this?" in content for content in u1_contents + u2_contents)
    assert any("context" in content for content in u1_contents)


def test_agentic_filter_honours_legacy_compatibility_ids():
    from types import SimpleNamespace

    from memos.multi_mem_cube.single_cube import SingleCubeView

    cube = object.__new__(SingleCubeView)
    cube.cube_id = "tenant-1"
    req = SimpleNamespace(user_id=" u1 ", agent_id=None, group_id=None)
    memories = [
        {"id": "mine", "metadata": {"user_id": "u1"}},
        {"id": "other", "metadata": {"user_id": "u2"}},
        {"id": "summary", "metadata": {}},
        {"id": "owned", "metadata": {"owner_type": "user", "owner_id": "u2"}},
        {"id": "public", "metadata": {"owner_type": "public", "owner_id": "tenant-1"}},
    ]

    kept = cube._filter_agentic_owner(memories, req)

    assert {memory["id"] for memory in kept} == {"mine", "summary", "public"}
