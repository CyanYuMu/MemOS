"""Backfill of the version chain from nodes archived by the old merge flow."""

import json

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize import backfill
from memos.memories.textual.tree_text_memory.organize.backfill import (
    FOLDED_MARKER,
    backfill_tenant,
)
from tests.memories.textual.fake_graph import FakeGraph


USER = "example-agent"


def _node(graph, text, *, status="activated", chat_time=None, **meta):
    meta.setdefault("memory_type", "LongTermMemory")
    # 修复后的节点：时间是真 UTC，必须带标记，否则会被当成北京墙钟重解释。
    meta.setdefault("time_basis", "utc")
    item = TextualMemoryItem(
        memory=text,
        metadata=TreeNodeTextualMemoryMetadata(
            chat_time=chat_time,
            created_at="2026-10-01T10:00:00+00:00",
            embedding=[0.5],
            status=status,
            **meta,
        ),
    )
    graph.seed(item)
    return item


def _pair(graph, old_text="用户住在北京", new_text="用户住在上海"):
    old = _node(graph, old_text, status="archived", chat_time="2025-01-01T00:00:00+00:00")
    new = _node(graph, new_text, chat_time="2025-07-01T00:00:00+00:00")
    graph.add_edge(old.id, new.id, "MERGED_TO")
    return old, new


def test_a_dry_run_reports_the_plan_and_writes_nothing():
    graph = FakeGraph()
    old, new = _pair(graph)
    before = dict(graph.nodes[new.id]["metadata"])

    report = backfill_tenant(graph, USER)

    assert report.plan == [(old.id, new.id)]
    assert report.folded == 0
    assert graph.nodes[new.id]["metadata"] == before
    assert FOLDED_MARKER not in graph.nodes[old.id]["metadata"]


def test_apply_moves_the_old_text_into_the_survivors_history():
    graph = FakeGraph()
    old, new = _pair(graph)

    report = backfill_tenant(graph, USER, apply=True)

    assert report.folded == 1
    assert [h["memory"] for h in graph.history(new.id)] == ["用户住在北京"]
    assert graph.history(new.id)[0]["timespec"]["valid_to"] == "2025-07-01T00:00:00+00:00"
    assert graph.nodes[new.id]["memory"] == "用户住在上海"
    assert graph.nodes[new.id]["metadata"]["version"] == 2
    assert graph.nodes[old.id]["metadata"][FOLDED_MARKER] == new.id
    assert graph.status(old.id) == "archived"
    assert graph.deleted == []
    assert graph.payloads[new.id]["history"] == graph.nodes[new.id]["metadata"]["history"]


def test_a_second_run_finds_nothing_to_do():
    graph = FakeGraph()
    _, new = _pair(graph)
    backfill_tenant(graph, USER, apply=True)
    history = list(graph.nodes[new.id]["metadata"]["history"])

    report = backfill_tenant(graph, USER, apply=True)

    assert report.plan == []
    assert report.skipped["already_folded"] == 1
    assert graph.nodes[new.id]["metadata"]["history"] == history


def test_a_text_already_in_history_is_not_added_again():
    graph = FakeGraph()
    old, new = _pair(graph)
    backfill_tenant(graph, USER, apply=True)
    graph.nodes[old.id]["metadata"].pop(FOLDED_MARKER)

    report = backfill_tenant(graph, USER, apply=True)

    assert report.skipped["already_in_history"] == 1
    assert len(graph.history(new.id)) == 1


def test_a_chain_of_merges_lands_on_the_last_survivor():
    graph = FakeGraph()
    first = _node(graph, "住在北京", status="archived", chat_time="2024-01-01T00:00:00+00:00")
    second = _node(graph, "住在深圳", status="archived", chat_time="2025-01-01T00:00:00+00:00")
    head = _node(graph, "住在上海", chat_time="2025-07-01T00:00:00+00:00")
    graph.add_edge(first.id, second.id, "MERGED_TO")
    graph.add_edge(second.id, head.id, "MERGED_TO")

    report = backfill_tenant(graph, USER, apply=True)

    assert report.folded == 2
    assert [h["memory"] for h in graph.history(head.id)] == ["住在北京", "住在深圳"]
    assert [h["version"] for h in graph.history(head.id)] == [1, 2]
    assert graph.nodes[head.id]["metadata"]["version"] == 3


def test_near_duplicates_and_preferences_are_left_alone():
    graph = FakeGraph()
    _, dup_new = _pair(graph, "用户喜欢喝美式", "用户喜欢喝美式咖啡")
    pref_old = _node(graph, "A", status="archived", memory_type="PreferenceMemory")
    pref_new = _node(graph, "B", memory_type="PreferenceMemory")
    graph.add_edge(pref_old.id, pref_new.id, "MERGED_TO")

    report = backfill_tenant(graph, USER, apply=True)

    assert report.plan == []
    assert report.skipped["same_text"] == 1
    assert report.skipped["preference"] == 1
    assert graph.history(dup_new.id) == [] and graph.history(pref_new.id) == []


def test_archived_nodes_without_a_usable_target_are_counted_not_touched():
    graph = FakeGraph()
    lone = _node(graph, "孤立的旧节点", status="archived")
    a = _node(graph, "a", status="archived")
    b = _node(graph, "b", status="archived")
    graph.add_edge(a.id, b.id, "MERGED_TO")
    graph.add_edge(b.id, a.id, "MERGED_TO")
    gone = _node(graph, "指向已删除节点", status="archived")
    graph.add_edge(gone.id, "missing", "MERGED_TO")

    report = backfill_tenant(graph, USER, apply=True)

    assert report.plan == []
    assert report.skipped["no_unique_merged_to"] == 3
    assert report.skipped["survivor_missing"] == 1
    assert FOLDED_MARKER not in graph.nodes[lone.id]["metadata"]


def test_a_node_merged_into_two_different_survivors_is_not_guessed_at():
    graph = FakeGraph()
    old = _node(graph, "住在北京", status="archived", chat_time="2024-01-01T00:00:00+00:00")
    first = _node(graph, "住在上海", chat_time="2025-01-01T00:00:00+00:00")
    second = _node(graph, "住在深圳", chat_time="2025-07-01T00:00:00+00:00")
    graph.add_edge(old.id, first.id, "MERGED_TO")
    graph.add_edge(old.id, second.id, "MERGED_TO")

    report = backfill_tenant(graph, USER, apply=True)

    assert report.plan == []
    assert report.skipped["no_unique_merged_to"] == 1
    assert graph.history(first.id) == [] and graph.history(second.id) == []


def test_main_builds_the_graph_once_and_stays_a_dry_run_by_default(monkeypatch, capsys):
    graph = FakeGraph()
    _, new = _pair(graph)
    built = []
    monkeypatch.setattr(backfill, "_build_graph", lambda: built.append(1) or graph)

    assert backfill.main(["--user-name", USER, "--user-name", "other"]) == 0

    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert built == [1]
    assert [r["user_name"] for r in reports] == [USER, "other"]
    assert reports[0]["to_fold"] == 1 and reports[0]["folded"] == 0
    assert graph.history(new.id) == []


def test_build_graph_goes_through_the_graph_store_factory(monkeypatch):
    from memos.graph_dbs.factory import GraphStoreFactory

    seen = {}
    monkeypatch.setattr(
        GraphStoreFactory, "from_config", classmethod(lambda cls, cfg: seen.setdefault("cfg", cfg))
    )
    monkeypatch.setattr(
        "memos.api.handlers.config_builders.build_graph_db_config", lambda: {"marker": 1}
    )

    assert backfill._build_graph() == {"marker": 1}
