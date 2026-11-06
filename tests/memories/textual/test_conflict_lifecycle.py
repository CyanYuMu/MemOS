"""Conflict lifecycle: state memories form a version chain, events are kept apart.

These run the real ``NodeHandler`` against an in-memory graph that enforces
Neo4j's value rules, so a field that would be rejected by the database fails here.
"""

import logging

from unittest.mock import MagicMock

import pytest

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize.handler import (
    NodeHandler,
    parse_relation,
)
from tests.memories.textual.fake_graph import FakeGraph


USER = "example-agent"


class ScriptedLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def generate(self, prompt):
        self.prompts.append(prompt[0]["content"])
        return self.answers.pop(0) if self.answers else "independent"


def _embedder():
    embedder = MagicMock()
    embedder.embed.return_value = [[0.5]]
    return embedder


def _mk(graph, text, emb, *, chat_time=None, created_at="2026-10-01T10:00:00+00:00", **meta):
    meta.setdefault("memory_type", "LongTermMemory")
    # 这些 fixture 代表修复后的节点：时间是真 UTC，必须带标记，
    # 否则会被当成 T1.3 之前的北京墙钟重新解释。
    meta.setdefault("time_basis", "utc")
    item = TextualMemoryItem(
        memory=text,
        metadata=TreeNodeTextualMemoryMetadata(
            chat_time=chat_time,
            created_at=created_at,
            embedding=[emb],
            status="activated",
            **meta,
        ),
    )
    graph.seed(item)
    return item


def _handler(graph, *answers):
    llm = ScriptedLLM(*answers)
    return NodeHandler(graph, llm, _embedder()), llm


# ───────────────────────────── parse_relation ─────────────────────────────


def test_parse_relation_reads_the_four_labels_and_the_legacy_one():
    assert parse_relation("supersedes") == "supersedes"
    assert parse_relation("  Redundant.\n") == "redundant"
    assert parse_relation("both_valid") == "both_valid"
    assert parse_relation("contradictory") == "supersedes"
    assert parse_relation("independent") == "independent"


def test_parse_relation_treats_anything_else_as_independent():
    assert parse_relation("") == "independent"
    assert parse_relation("I am not sure") == "independent"


@pytest.mark.parametrize(
    "answer",
    [
        "This is not redundant",
        "I cannot tell whether A supersedes B",
        "Neither supersedes the other",
        "redundant or independent",
        "supersedes, because B is newer",
    ],
)
def test_parse_relation_does_not_pick_a_label_out_of_a_sentence(answer):
    assert parse_relation(answer) == "independent"


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        ('"supersedes"', "supersedes"),
        ("**redundant**", "redundant"),
        ("`both_valid`.", "both_valid"),
    ],
)
def test_parse_relation_tolerates_quotes_and_markup_around_a_bare_label(answer, label):
    assert parse_relation(answer) == label


# ─────────────────────────── version chain (supersedes) ───────────────────────────


def test_a_new_value_supersedes_the_old_one_and_keeps_it_as_history():
    graph = FakeGraph()
    beijing = _mk(graph, "用户住在北京", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    shanghai = _mk(graph, "用户住在上海", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    graph.neighbors[(2.0,)] = [beijing.id]
    handler, llm = _handler(graph, "supersedes")

    relations = handler.detect(shanghai, scope="LongTermMemory", user_name=USER)
    archived = handler.resolve(*relations[0], user_name=USER)

    assert archived == beijing.id
    assert graph.nodes[shanghai.id]["memory"] == "用户住在上海"
    assert graph.status(shanghai.id) == "activated"
    assert graph.status(beijing.id) == "archived"
    assert [h["memory"] for h in graph.history(shanghai.id)] == ["用户住在北京"]
    assert graph.history(shanghai.id)[0]["timespec"]["valid_to"] == "2025-07-01T00:00:00+00:00"
    assert graph.nodes[shanghai.id]["metadata"]["version"] == 2
    assert (beijing.id, shanghai.id) in graph.merged_to()
    assert graph.deleted == []
    assert len(llm.prompts) == 1, "supersedes needs no fusion call"


def test_the_detector_shows_the_earlier_statement_first():
    """Direction comes from the timestamps, so a back-filled conversation still reads
    'Shanghai replaces Beijing'."""
    graph = FakeGraph()
    shanghai = _mk(graph, "用户住在上海", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    beijing = _mk(
        graph,
        "用户住在北京",
        1.0,
        chat_time="2025-01-01T00:00:00+00:00",
        created_at="2026-10-21T00:00:00+00:00",
    )
    graph.neighbors[(1.0,)] = [shanghai.id]
    handler, llm = _handler(graph, "supersedes")

    handler.detect(beijing, scope="LongTermMemory", user_name=USER)

    prompt = llm.prompts[0]
    assert prompt.index("用户住在北京") < prompt.index("用户住在上海")
    assert "2025-01-01" in prompt and "2025-07-01" in prompt


def test_a_back_filled_older_conversation_does_not_overwrite_the_newer_fact():
    graph = FakeGraph()
    shanghai = _mk(
        graph,
        "用户住在上海",
        2.0,
        chat_time="2025-07-01T00:00:00+00:00",
        created_at="2026-10-01T00:00:00+00:00",
    )
    beijing = _mk(
        graph,
        "用户住在北京",
        1.0,
        chat_time="2025-01-01T00:00:00+00:00",
        created_at="2026-10-21T00:00:00+00:00",
    )
    graph.neighbors[(1.0,)] = [shanghai.id]
    handler, _ = _handler(graph, "supersedes")

    relations = handler.detect(beijing, scope="LongTermMemory", user_name=USER)
    archived = handler.resolve(*relations[0], user_name=USER)

    assert archived == beijing.id, "the node written last is the older fact and is folded away"
    assert graph.nodes[shanghai.id]["memory"] == "用户住在上海"
    assert graph.status(shanghai.id) == "activated"
    assert graph.status(beijing.id) == "archived"
    assert [h["memory"] for h in graph.history(shanghai.id)] == ["用户住在北京"]


def test_consecutive_resolves_on_one_new_node_keep_every_earlier_version():
    """The second pair used to be resolved against a stale copy of the new node and
    wrote its history over the first."""
    graph = FakeGraph()
    old_1 = _mk(graph, "住在北京", 1.0, chat_time="2024-01-01T00:00:00+00:00")
    old_2 = _mk(graph, "住在深圳", 2.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "住在上海", 3.0, chat_time="2025-07-01T00:00:00+00:00")
    handler, _ = _handler(graph)

    handler.resolve(new, old_1, "supersedes", user_name=USER)
    handler.resolve(new, old_2, "supersedes", user_name=USER)

    assert [h["memory"] for h in graph.history(new.id)] == ["住在北京", "住在深圳"]
    assert [h["version"] for h in graph.history(new.id)] == [1, 2]
    assert graph.nodes[new.id]["metadata"]["version"] == 3


def test_edges_of_the_replaced_node_move_to_the_current_one():
    graph = FakeGraph()
    old = _mk(graph, "住在北京", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "住在上海", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    graph.add_edge("parent", old.id, "PARENT")
    handler, _ = _handler(graph)

    handler.resolve(new, old, "supersedes", user_name=USER)

    assert ("parent", new.id, "PARENT") in graph.edges


def test_neo4j_and_qdrant_carry_the_same_version_fields():
    graph = FakeGraph()
    old = _mk(graph, "住在北京", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "住在上海", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    handler, _ = _handler(graph)

    handler.resolve(new, old, "supersedes", user_name=USER)

    for node_id in (new.id, old.id):
        node, payload = graph.nodes[node_id]["metadata"], graph.payloads[node_id]
        for key in ("status", "history", "version", "valid_from", "valid_to", "memory_form"):
            assert node.get(key) == payload.get(key), (node_id, key)


def test_a_node_that_is_no_longer_active_is_not_resolved_again():
    graph = FakeGraph()
    old = _mk(graph, "住在北京", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "住在上海", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    handler, _ = _handler(graph)
    handler.resolve(new, old, "supersedes", user_name=USER)

    again = handler.resolve(new, old, "supersedes", user_name=USER)

    assert again is None
    assert len(graph.history(new.id)) == 1


# ──────────────────────────────── events ────────────────────────────────


def test_two_business_trips_are_both_kept():
    graph = FakeGraph()
    xian = _mk(
        graph,
        "用户 2025-03-10 去西安出差",
        1.0,
        chat_time="2025-03-11T00:00:00+00:00",
        memory_form="event",
        event_time_start="2025-03-10T00:00:00+00:00",
    )
    hangzhou = _mk(
        graph,
        "用户 2025-05-06 去杭州出差",
        2.0,
        chat_time="2025-05-07T00:00:00+00:00",
        memory_form="event",
        event_time_start="2025-05-06T00:00:00+00:00",
    )
    graph.neighbors[(2.0,)] = [xian.id]
    handler, llm = _handler(graph, "supersedes")

    relations = handler.detect(hangzhou, scope="LongTermMemory", user_name=USER)

    assert relations == []
    assert llm.prompts == [], "an event is never judged, so no LLM call is spent on it"
    assert graph.status(xian.id) == "activated"
    assert graph.status(hangzhou.id) == "activated"


def test_a_state_is_not_judged_against_an_event():
    graph = FakeGraph()
    trip = _mk(graph, "用户 2025-03-10 去西安出差", 1.0, memory_form="event")
    state = _mk(graph, "用户住在上海", 2.0)
    graph.neighbors[(2.0,)] = [trip.id]
    handler, llm = _handler(graph, "supersedes")

    assert handler.detect(state, scope="LongTermMemory", user_name=USER) == []
    assert llm.prompts == []


def test_legacy_nodes_without_a_form_are_still_judged_as_states():
    graph = FakeGraph()
    old = _mk(graph, "用户住在北京", 1.0)
    new = _mk(graph, "用户住在上海", 2.0)
    graph.neighbors[(2.0,)] = [old.id]
    handler, _ = _handler(graph, "supersedes")

    assert len(handler.detect(new, scope="LongTermMemory", user_name=USER)) == 1


def test_both_valid_and_independent_pairs_change_nothing():
    graph = FakeGraph()
    a = _mk(graph, "用户喜欢咖啡", 1.0)
    b = _mk(graph, "用户也喜欢茶", 2.0)
    graph.neighbors[(2.0,)] = [a.id]

    for answer in ("both_valid", "independent"):
        handler, _ = _handler(graph, answer)
        assert handler.detect(b, scope="LongTermMemory", user_name=USER) == []


# ───────────────────────── redundant / fusion fallback ─────────────────────────


def test_a_redundant_pair_is_fused_into_the_newer_node():
    graph = FakeGraph()
    old = _mk(graph, "用户喜欢喝美式", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "用户爱喝美式咖啡", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    handler, _ = _handler(graph, "<answer>用户喜欢喝美式咖啡</answer>")

    archived = handler.resolve(new, old, "redundant", user_name=USER)

    assert archived == old.id
    assert graph.nodes[new.id]["memory"] == "用户喜欢喝美式咖啡"
    assert graph.status(old.id) == "archived"
    assert (old.id, new.id) in graph.merged_to()


def test_a_failed_fusion_archives_the_older_node_instead_of_deleting_it():
    graph = FakeGraph()
    old = _mk(graph, "用户喜欢喝美式", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "用户爱喝美式咖啡", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    handler, _ = _handler(graph, "<answer>No</answer>")

    handler.resolve(new, old, "redundant", user_name=USER)

    assert graph.deleted == []
    assert graph.nodes[old.id]["memory"] == "用户喜欢喝美式"
    assert graph.nodes[new.id]["memory"] == "用户爱喝美式咖啡"
    assert graph.status(old.id) == "archived"
    assert (old.id, new.id) in graph.merged_to()
    assert [h["memory"] for h in graph.history(new.id)] == ["用户喜欢喝美式"]
    assert graph.history(new.id)[0]["update_type"] == "duplicate"
    assert graph.nodes[old.id]["metadata"]["valid_to"] == "2025-07-01T00:00:00+00:00"


def test_a_preference_keeps_merge_semantics_even_when_the_pair_is_superseding():
    graph = FakeGraph()
    old = _mk(graph, "原文A", 1.0, memory_type="PreferenceMemory", preference="回复要简短")
    new = _mk(graph, "原文B", 2.0, memory_type="PreferenceMemory", preference="回复要详细")
    handler, llm = _handler(graph, "<answer>回复要详细</answer>")

    handler.resolve(new, old, "supersedes", user_name=USER)

    assert len(llm.prompts) == 1, "preferences go through the fusion prompt"
    assert graph.history(new.id) == []
    assert graph.nodes[new.id]["memory"] == "回复要详细"


def test_fusion_keeps_the_earliest_created_at_and_never_writes_update_at():
    graph = FakeGraph()
    old = _mk(
        graph,
        "a",
        1.0,
        created_at="2026-09-01T00:00:00+00:00",
        chat_time="2025-01-01T00:00:00+00:00",
    )
    new = _mk(
        graph,
        "b",
        2.0,
        created_at="2026-10-01T00:00:00+00:00",
        chat_time="2025-07-01T00:00:00+00:00",
    )
    handler, _ = _handler(graph)

    merged = handler._merge_metadata("fused", new.metadata, old.metadata)

    assert merged.created_at == "2026-09-01T00:00:00+00:00"
    assert merged.chat_time == "2025-07-01T00:00:00+00:00"
    assert "update_at" not in merged.model_dump()


# ───────────────────────────────── MERGED_TO ─────────────────────────────────


def test_merged_to_never_closes_a_cycle(caplog):
    graph = FakeGraph()
    old = _mk(graph, "住在北京", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    new = _mk(graph, "住在上海", 2.0, chat_time="2025-07-01T00:00:00+00:00")
    graph.add_edge(new.id, old.id, "MERGED_TO")  # a historical edge pointing the other way
    handler, _ = _handler(graph)

    with caplog.at_level(logging.WARNING):
        handler.resolve(new, old, "supersedes", user_name=USER)

    assert (old.id, new.id) not in graph.merged_to()
    assert graph.status(old.id) == "archived", "the merge still happens, only the edge is skipped"
    assert any("cycle" in r.getMessage() for r in caplog.records)


def test_merged_to_chains_are_followed_to_the_end():
    graph = FakeGraph()
    a = _mk(graph, "a", 1.0, chat_time="2025-01-01T00:00:00+00:00")
    b = _mk(graph, "b", 2.0, chat_time="2025-02-01T00:00:00+00:00")
    c = _mk(graph, "c", 3.0, chat_time="2025-03-01T00:00:00+00:00")
    graph.add_edge(c.id, b.id, "MERGED_TO")
    graph.add_edge(b.id, a.id, "MERGED_TO")
    handler = _handler(graph)[0]

    assert handler._would_cycle(a.id, c.id) is True
    assert handler._would_cycle(c.id, a.id) is False
