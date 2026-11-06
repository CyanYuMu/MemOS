"""Feedback updates a memory in place: the id, the tree edges and the old text survive."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from memos.mem_feedback import feedback as feedback_module
from memos.mem_feedback.feedback import MemFeedback
from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from tests.memories.textual.fake_graph import FakeGraph


USER = "example-agent"


class VectorGraph(FakeGraph):
    """A graph store with a separate vector store, like the community backend."""

    vec_db = object()

    def __init__(self):
        super().__init__()
        self.update_calls: list[dict] = []
        self.vector_syncs: list[tuple[str, dict]] = []

    def update_node(self, id, fields, user_name=None):
        self.update_calls.append(dict(fields))
        super().update_node(id, fields, user_name=user_name)

    def _sync_vector_payload(self, id, fields):
        self.vector_syncs.append((id, dict(fields)))


def _memory(graph, text, **meta):
    meta.setdefault("memory_type", "LongTermMemory")
    meta.setdefault("tags", ["住址"])
    item = TextualMemoryItem(
        memory=text,
        metadata=TreeNodeTextualMemoryMetadata(
            status="activated",
            key="住址",
            embedding=[0.5],
            created_at="2026-10-01T10:00:00+00:00",
            **meta,
        ),
    )
    graph.seed(item)
    return item


def _feedback(graph, *, switch="off"):
    fb = MemFeedback.__new__(MemFeedback)
    fb.graph_store = graph
    fb.mem_reader = SimpleNamespace(memory_version_switch=switch)
    fb._retry_db_operation = lambda operation: operation()
    fb._batch_embed = Mock(return_value=[[0.9]])
    return fb


def _correction(text):
    return TextualMemoryItem(
        memory=text,
        metadata=TreeNodeTextualMemoryMetadata(
            memory_type="LongTermMemory",
            tags=["住址", "纠正"],
            key="住址",
            embedding=[0.9],
            background="[Feedback update background]: ...",
            sources=[],
        ),
    )


def _update(fb, old, text="用户住在上海"):
    return fb._single_update_operation(
        old, _correction(text), "u1", USER, operation={"operation": "UPDATE", "text": text}
    )


def test_an_update_keeps_the_node_its_edges_and_the_old_text():
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")
    graph.add_edge("parent", old.id, "PARENT")

    result = _update(_feedback(graph), old)

    assert result["id"] == old.id and result["archived_id"] == old.id
    assert result["origin_memory"] == "用户住在北京" and result["text"] == "用户住在上海"
    assert graph.nodes[old.id]["memory"] == "用户住在上海"
    assert graph.status(old.id) == "activated"
    assert ("parent", old.id, "PARENT") in graph.edges
    assert [h["memory"] for h in graph.history(old.id)] == ["用户住在北京"]
    assert graph.history(old.id)[0]["update_type"] == "feedback"
    assert graph.nodes[old.id]["metadata"]["version"] == 2
    assert graph.deleted == [] and len(graph.nodes) == 1


def test_the_vector_store_gets_the_new_vector_and_the_graph_node_does_not():
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")

    _update(_feedback(graph), old)

    assert all("embedding" not in call for call in graph.update_calls)
    assert graph.vector_syncs == [(old.id, {"embedding": [0.9], "memory": "用户住在上海"})]
    assert graph.payloads[old.id]["history"] == graph.nodes[old.id]["metadata"]["history"]


def test_a_backend_without_a_separate_vector_store_keeps_the_embedding_in_the_node():
    graph = FakeGraph()
    graph.update_node = MagicMock(wraps=graph.update_node)
    old = _memory(graph, "用户住在北京")

    _update(_feedback(graph), old)

    assert graph.update_node.call_args.kwargs["fields"]["embedding"] == [0.9]


def test_a_second_correction_stacks_on_the_history():
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")
    fb = _feedback(graph)
    _update(fb, old, "用户住在上海")

    _update(fb, graph.item(old.id), "用户住在杭州")

    assert [h["memory"] for h in graph.history(old.id)] == ["用户住在北京", "用户住在上海"]
    assert graph.nodes[old.id]["metadata"]["version"] == 3


def test_a_registered_hook_takes_over_and_the_built_in_is_the_fallback(monkeypatch):
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")
    fb = _feedback(graph)
    updated = old.model_copy(deep=True)
    updated.memory = "由 hook 写入"
    hook = Mock(return_value=(updated, None, None, {"memory": "由 hook 写入"}))
    monkeypatch.setattr(feedback_module, "trigger_single_hook", hook)

    _update(fb, old)

    assert graph.nodes[old.id]["memory"] == "由 hook 写入"
    assert graph.history(old.id) == []

    monkeypatch.setattr(feedback_module, "trigger_single_hook", Mock(side_effect=RuntimeError))
    _update(fb, old)
    assert [h["memory"] for h in graph.history(old.id)] == ["用户住在北京"]


def test_a_failed_write_leaves_the_old_text_and_history_untouched():
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")
    graph.update_node = Mock(side_effect=RuntimeError("neo4j down"))
    fb = _feedback(graph)

    try:
        _update(fb, old)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the failure must reach the caller")

    assert graph.nodes[old.id]["memory"] == "用户住在北京"
    assert graph.history(old.id) == []
    assert graph.vector_syncs == []


def test_a_working_memory_is_overwritten_the_way_it_always_was():
    graph = VectorGraph()
    old = _memory(graph, "草稿", memory_type="WorkingMemory")

    result = _update(_feedback(graph), old, "新草稿")

    assert result["id"] == old.id
    assert graph.nodes[old.id]["memory"] == "新草稿"
    assert graph.history(old.id) == []


# ───────────────────────────── candidates ─────────────────────────────


def test_candidates_with_tree_edges_are_kept():
    fb = MemFeedback.__new__(MemFeedback)
    fb.pref_feedback = False
    graph = VectorGraph()
    linked = _memory(graph, "有父节点的记忆")
    graph.add_edge("parent", linked.id, "PARENT")
    fb.searcher = SimpleNamespace(search=Mock(return_value=[(linked, 0.8)]), graph_store=graph)

    assert fb._retrieve("住在哪", user_name=USER) == [linked]


def test_missing_nodes_and_missing_tags_do_not_break_the_candidate_list():
    graph = VectorGraph()
    kept = _memory(graph, "用户住在北京")
    no_tags = _memory(graph, "没有标签")
    graph.nodes[no_tags.id]["metadata"].pop("tags")
    fast = _memory(graph, "原始节点", tags=["mode:fast"])
    fb = _feedback(graph)
    fb.semantics_feedback = Mock(return_value={"record": {"add": [], "update": []}})

    fb._feedback_memory(
        "u1",
        USER,
        [_correction("用户住在上海")],
        retrieved_memory_ids=[kept.id, "gone", no_tags.id, fast.id],
        chat_history=[{"role": "user", "content": "我住哪"}],
        feedback_content="我搬到上海了",
        info={},
    )

    seen = [m.id for m in fb.semantics_feedback.call_args.args[3]]
    assert seen == [kept.id, no_tags.id]


def test_every_feedback_gets_its_own_candidate_list():
    graph = VectorGraph()
    kept = _memory(graph, "用户住在北京")
    fb = _feedback(graph)
    lists = []

    def semantics(user_id, user_name, mem, current, *rest):
        lists.append(current)
        current.append("polluted")
        return {"record": {"add": [], "update": []}}

    fb.semantics_feedback = semantics

    fb._feedback_memory(
        "u1",
        USER,
        [_correction("a"), _correction("b"), _correction("c")],
        retrieved_memory_ids=[kept.id],
        chat_history=[],
        feedback_content="x",
        info={},
    )

    assert len({id(lst) for lst in lists}) == 3
    assert all(lst.count("polluted") == 1 for lst in lists)


# ───────────────────────────── whole flow, no ids given ─────────────────────────────


def _flow(graph, llm_answer):
    fb = _feedback(graph)
    fb.pref_feedback = False
    fb.memory_manager = Mock()
    fb.memory_manager.add.return_value = ["new-node"]
    fb._get_llm_response = Mock(return_value=llm_answer)
    return fb


def _run_flow(fb, graph, candidate, correction):
    fb.searcher = SimpleNamespace(search=Mock(return_value=[(candidate, 0.8)]), graph_store=graph)
    return fb.semantics_feedback(
        "u1",
        USER,
        _correction(correction),
        [],
        "user feedback: 我搬到上海了",
        [{"role": "user", "content": "我住在哪"}, {"role": "assistant", "content": "北京"}],
        {},
    )


def test_a_correction_without_ids_finds_the_node_and_updates_it_in_place():
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")
    graph.add_edge("parent", old.id, "PARENT")
    graph.add_edge(old.id, "child", "PARENT")
    fb = _flow(
        graph,
        {
            "operations": [
                {
                    "operation": "UPDATE",
                    "id": old.id,
                    "text": "用户住在上海",
                    "old_memory": "用户住在北京",
                }
            ]
        },
    )

    record = _run_flow(fb, graph, old, "用户住在上海")["record"]

    assert [r["id"] for r in record["update"]] == [old.id] and record["add"] == []
    assert graph.nodes[old.id]["memory"] == "用户住在上海"
    assert [h["memory"] for h in graph.history(old.id)] == ["用户住在北京"]
    assert {("parent", old.id, "PARENT"), (old.id, "child", "PARENT")} <= set(graph.edges)
    fb.memory_manager.add.assert_not_called()


def test_an_unrelated_feedback_is_still_added_as_a_new_memory():
    graph = VectorGraph()
    old = _memory(graph, "用户住在北京")
    fb = _flow(graph, {"operations": [{"operation": "ADD", "text": "用户不喜欢香菜"}]})

    record = _run_flow(fb, graph, old, "用户不喜欢香菜")["record"]

    assert record["update"] == [] and [r["id"] for r in record["add"]] == ["new-node"]
    assert graph.nodes[old.id]["memory"] == "用户住在北京"
    assert graph.history(old.id) == []
