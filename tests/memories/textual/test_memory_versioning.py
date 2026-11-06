"""Version chain helpers and the tolerant ``history`` read path."""

import json

from memos.memories.textual.item import (
    ArchivedTextualMemory,
    TextualMemoryItem,
    TreeNodeTextualMemoryMetadata,
)
from memos.memories.textual.tree_text_memory.organize import versioning


def _item(text, *, chat_time=None, created_at="2026-10-01T10:00:00+00:00", **meta):
    return TextualMemoryItem(
        memory=text,
        metadata=TreeNodeTextualMemoryMetadata(
            memory_type="LongTermMemory",
            chat_time=chat_time,
            created_at=created_at,
            embedding=[0.1, 0.2],
            # 这些 fixture 代表修复后的节点：时间是真 UTC，必须带标记。
            time_basis="utc",
            **meta,
        ),
    )


# ───────────────────────── history read path ─────────────────────────


def test_history_entries_stored_as_json_strings_are_read_back():
    """Graph stores keep history as JSON strings; reading used to fail validation."""
    entry = {"version": 1, "memory": "住在北京", "update_type": "conflict"}

    meta = TreeNodeTextualMemoryMetadata(history=[json.dumps(entry, ensure_ascii=False)])

    assert isinstance(meta.history[0], ArchivedTextualMemory)
    assert meta.history[0].memory == "住在北京"


def test_history_drops_unparseable_entries_instead_of_failing_the_node():
    meta = TreeNodeTextualMemoryMetadata(
        history=["not json", json.dumps({"version": 1, "memory": "ok"}), 42]
    )

    assert [h.memory for h in meta.history] == ["ok"]


def test_history_accepts_the_update_type_cloud_update_writes():
    meta = TreeNodeTextualMemoryMetadata(
        history=[{"version": 1, "memory": "old", "update_type": "update"}]
    )

    assert meta.history[0].update_type == "update"


def test_history_normalises_an_unknown_update_type():
    meta = TreeNodeTextualMemoryMetadata(
        history=[{"version": 1, "memory": "old", "update_type": "something-new"}]
    )

    assert meta.history[0].update_type == "unrelated"


def test_history_tolerates_none_and_whole_string():
    assert TreeNodeTextualMemoryMetadata(history=None).history == []
    packed = json.dumps([{"version": 1, "memory": "old"}])
    assert TreeNodeTextualMemoryMetadata(history=packed).history[0].memory == "old"


def test_new_validity_fields_are_optional():
    meta = TreeNodeTextualMemoryMetadata()

    assert meta.memory_form is None
    assert meta.valid_from is None
    assert meta.valid_to is None
    assert meta.event_time_start is None
    assert meta.event_time_end is None


# ───────────────────────────── order_pair ─────────────────────────────


def test_order_pair_uses_chat_time_when_both_have_it():
    """A back-filled old conversation is written later but said earlier."""
    old_said = _item(
        "住在北京", chat_time="2025-01-01T00:00:00+00:00", created_at="2026-10-21T00:00:00+00:00"
    )
    new_said = _item(
        "住在上海", chat_time="2025-07-01T00:00:00+00:00", created_at="2026-10-01T00:00:00+00:00"
    )

    assert versioning.order_pair(old_said, new_said) == (old_said, new_said)
    assert versioning.order_pair(new_said, old_said) == (old_said, new_said)


def test_order_pair_falls_back_to_write_order_without_chat_time():
    first = _item("a", created_at="2026-10-01T00:00:00+00:00")
    second = _item("b", created_at="2026-10-02T00:00:00+00:00")

    assert versioning.order_pair(second, first) == (first, second)
    assert versioning.order_pair(first, second) == (first, second)


def test_order_pair_never_mixes_chat_time_with_created_at():
    """created_at is local wall-clock labelled UTC; comparing it with a real UTC
    chat_time would be hours off."""
    with_chat = _item(
        "a", chat_time="2025-01-01T00:00:00+00:00", created_at="2026-10-05T00:00:00+00:00"
    )
    without_chat = _item("b", created_at="2026-10-02T00:00:00+00:00")

    older, newer = versioning.order_pair(with_chat, without_chat)

    assert (older, newer) == (without_chat, with_chat)


def test_order_pair_tie_makes_the_incoming_node_the_newer_one():
    incoming = _item("a", chat_time="2025-01-01T00:00:00+00:00")
    existing = _item("b", chat_time="2025-01-01T00:00:00+00:00")

    assert versioning.order_pair(incoming, existing) == (existing, incoming)


# ──────────────────────────── merge_into_head ────────────────────────────


def test_merge_into_head_moves_the_older_text_into_history():
    tail = _item("用户住在北京", chat_time="2025-01-01T00:00:00+00:00")
    head = _item("用户住在上海", chat_time="2025-07-01T00:00:00+00:00")

    fields = versioning.merge_into_head(head, tail)

    history = [json.loads(h) for h in fields["history"]]
    assert [h["memory"] for h in history] == ["用户住在北京"]
    assert history[0]["update_type"] == "conflict"
    assert history[0]["version"] == 1
    assert history[0]["timespec"]["valid_from"] == "2025-01-01T00:00:00+00:00"
    assert history[0]["timespec"]["valid_to"] == "2025-07-01T00:00:00+00:00"
    assert fields["version"] == 2
    assert fields["valid_from"] == "2025-07-01T00:00:00+00:00"
    assert "memory" not in fields


def test_merge_into_head_keeps_both_sides_earlier_versions_in_order():
    tail = _item(
        "住在深圳",
        chat_time="2025-03-01T00:00:00+00:00",
        history=[
            {
                "version": 1,
                "memory": "住在北京",
                "update_type": "conflict",
                "timespec": {
                    "valid_from": "2025-01-01T00:00:00+00:00",
                    "valid_to": "2025-03-01T00:00:00+00:00",
                },
            }
        ],
    )
    head = _item("住在上海", chat_time="2025-07-01T00:00:00+00:00")

    fields = versioning.merge_into_head(head, tail)

    history = [json.loads(h) for h in fields["history"]]
    assert [(h["version"], h["memory"]) for h in history] == [(1, "住在北京"), (2, "住在深圳")]
    assert history[0]["timespec"]["valid_to"] == "2025-03-01T00:00:00+00:00"
    assert history[1]["timespec"]["valid_to"] == "2025-07-01T00:00:00+00:00"
    assert fields["version"] == 3


def test_merge_into_head_is_idempotent_for_the_same_pair():
    tail = _item("住在北京", chat_time="2025-01-01T00:00:00+00:00")
    head = _item("住在上海", chat_time="2025-07-01T00:00:00+00:00")
    first = versioning.merge_into_head(head, tail)
    head.metadata.history = [ArchivedTextualMemory(**json.loads(h)) for h in first["history"]]

    second = versioning.merge_into_head(head, tail)

    assert len(second["history"]) == 1


def test_merge_into_head_caps_history_to_the_newest_versions(monkeypatch):
    monkeypatch.setattr(versioning, "MAX_HISTORY_VERSIONS", 3)
    head = _item("v5", chat_time="2025-05-01T00:00:00+00:00")
    tail = _item(
        "v4",
        chat_time="2025-04-01T00:00:00+00:00",
        history=[
            {
                "version": i,
                "memory": f"v{i}",
                "update_type": "conflict",
                "timespec": {"valid_from": f"2025-0{i}-01T00:00:00+00:00"},
            }
            for i in (1, 2, 3)
        ],
    )

    fields = versioning.merge_into_head(head, tail)

    assert [json.loads(h)["memory"] for h in fields["history"]] == ["v2", "v3", "v4"]


def test_merge_into_head_values_are_neo4j_safe():
    head = _item("住在上海", chat_time="2025-07-01T00:00:00+00:00")
    tail = _item("住在北京", chat_time="2025-01-01T00:00:00+00:00")

    fields = versioning.merge_into_head(head, tail)

    assert all(isinstance(h, str) for h in fields["history"])
    assert all(isinstance(v, (str, int, list)) for v in fields.values())


# ───────────────────────── apply_feedback_update ─────────────────────────


def test_feedback_update_keeps_the_old_text_in_history_and_creates_no_archive_node():
    old = _item("用户住在北京", created_at="2026-09-01T00:00:00+00:00", key="住址")
    new = _item("用户住在上海", chat_time="2026-10-20T00:00:00+00:00", key="住址")

    updated, archived_item, archived_metadata, fields = versioning.apply_feedback_update(
        old, new, "u1"
    )

    assert archived_item is None and archived_metadata is None
    assert updated.id == old.id
    assert updated.memory == "用户住在上海"
    history = [json.loads(h) for h in fields["history"]]
    assert history[0]["memory"] == "用户住在北京"
    assert history[0]["update_type"] == "feedback"
    assert history[0]["timespec"]["valid_to"] == "2026-10-20T00:00:00+00:00"
    assert fields["version"] == 2
    assert fields["valid_from"] == "2026-10-20T00:00:00+00:00"
    assert fields["memory"] == "用户住在上海"
    assert fields["embedding"] == [0.1, 0.2]


def test_feedback_update_fields_are_neo4j_safe():
    old = _item("a")
    new = _item("b")

    _, _, _, fields = versioning.apply_feedback_update(old, new, "u1")

    assert all(isinstance(h, str) for h in fields["history"])
    assert all(isinstance(s, str) for s in fields["sources"])


def test_feedback_update_does_not_mutate_the_input_item():
    old = _item("a")
    new = _item("b")

    versioning.apply_feedback_update(old, new, "u1")

    assert old.memory == "a"
    assert old.metadata.history == []


def test_display_time_shows_the_chat_time_and_never_the_local_created_at():
    from types import SimpleNamespace

    from memos.memories.textual.tree_text_memory.organize import versioning

    stamped = SimpleNamespace(
        metadata=SimpleNamespace(
            chat_time="2025-07-01T08:00:00+08:00", created_at="2025-07-01T09:00:00+00:00"
        )
    )
    unstamped = SimpleNamespace(
        metadata=SimpleNamespace(chat_time=None, created_at="2025-07-01T09:00:00+00:00")
    )

    assert versioning.display_time(stamped) == "2025-07-01T00:00:00+00:00"
    assert versioning.display_time(unstamped) == "unknown"
