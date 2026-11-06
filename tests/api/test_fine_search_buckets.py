"""fine 检索要覆盖 preference 分桶，且不把偏好记忆送去 LLM 改写。"""

from types import SimpleNamespace

import memos.api.handlers.add_handler  # noqa: F401  # 先加载 handlers，避免 single_cube 的循环导入
import memos.multi_mem_cube.single_cube as single_cube_module

from memos.memories.textual.item import TextualMemoryItem, TextualMemoryMetadata
from memos.multi_mem_cube.single_cube import SingleCubeView
from memos.types.general_types import SearchMode


def _memory(
    text: str,
    memory_type: str = "LongTermMemory",
    mem_id: str = "11111111-1111-4111-8111-111111111111",
):
    return TextualMemoryItem(
        id=mem_id,
        memory=text,
        metadata=TextualMemoryMetadata(user_id="example-agent", memory_type=memory_type),
    )


class _Retriever:
    def __init__(self, answerable: bool = True, gap_hint: tuple[str, bool] = ("hint", True)):
        self.enhanced_inputs = []
        self.answerable = answerable
        self.gap_hint = gap_hint

    def enhance_memories_with_query(self, query_history, memories):
        self.enhanced_inputs.append([mem.memory for mem in memories])
        return [
            TextualMemoryItem(id=mem.id, memory=f"enhanced:{mem.memory}", metadata=mem.metadata)
            for mem in memories
        ], True

    def evaluate_memory_answer_ability(self, query, memory_texts, top_k=None):
        return self.answerable

    def recall_for_missing_memories(self, query, memories):
        return self.gap_hint


def _view(retriever, searcher=None):
    view = object.__new__(SingleCubeView)
    view.cube_id = "example-agent"
    view.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    view.searcher = searcher or SimpleNamespace()
    view.naive_mem_cube = SimpleNamespace(text_mem=SimpleNamespace())
    view.mem_scheduler = SimpleNamespace(retriever=retriever)
    view._postformat_memories = lambda memories, *a, **k: memories
    return view


def _search_req(**overrides):
    from memos.api.product_models import APISearchRequest

    payload = {
        "user_id": "example-agent",
        "query": "示例告警研判 输出格式",
        "mode": "fine",
        "dedup": "mmr",
        "top_k": 15,
    }
    payload.update(overrides)
    return APISearchRequest(**payload)


def test_fine_search_uses_the_shared_entry_point(monkeypatch):
    retriever = _Retriever()
    raw = [_memory("普通记忆")]
    captured = {}

    def _fake_search_text_memories(**kwargs):
        captured.update(kwargs)
        return raw

    monkeypatch.setattr(single_cube_module, "search_text_memories", _fake_search_text_memories)
    view = _view(retriever)

    view._fine_search(_search_req(), SimpleNamespace(mem_cube_id="example-agent"))

    assert captured["mode"] == SearchMode.FINE
    # 偏好开关在 search_req 上
    assert captured["search_req"].include_preference is True


def test_preference_memories_bypass_enhancement(monkeypatch):
    retriever = _Retriever()
    raw = [
        _memory("普通记忆一", mem_id="11111111-1111-4111-8111-111111111111"),
        _memory("普通记忆二", mem_id="22222222-2222-4222-8222-222222222222"),
        _memory(
            "用户坚持只输出 JSON",
            memory_type="PreferenceMemory",
            mem_id="33333333-3333-4333-8333-333333333333",
        ),
    ]
    monkeypatch.setattr(single_cube_module, "search_text_memories", lambda **kwargs: list(raw))
    view = _view(retriever)

    results = view._fine_search(_search_req(), SimpleNamespace(mem_cube_id="example-agent"))

    assert retriever.enhanced_inputs == [["普通记忆一", "普通记忆二"]]
    ids = [mem.id for mem in results]
    assert "33333333-3333-4333-8333-333333333333" in ids
    preference = next(mem for mem in results if mem.id == "33333333-3333-4333-8333-333333333333")
    assert preference.memory == "用户坚持只输出 JSON"
    assert preference.metadata.memory_type == "PreferenceMemory"


def test_gap_recall_only_runs_when_memories_cannot_answer(monkeypatch):
    raw = [_memory("普通记忆")]
    monkeypatch.setattr(single_cube_module, "search_text_memories", lambda **kwargs: list(raw))

    searches = []

    def _fake_search(**kwargs):
        searches.append(kwargs)
        return [_memory("补召回的记忆", mem_id="44444444-4444-4444-8444-444444444444")]

    searcher = SimpleNamespace(search=_fake_search)

    view = _view(_Retriever(answerable=True), searcher=searcher)
    view._fine_search(_search_req(), SimpleNamespace(mem_cube_id="example-agent"))
    assert searches == []

    view = _view(_Retriever(answerable=False), searcher=searcher)
    results = view._fine_search(_search_req(), SimpleNamespace(mem_cube_id="example-agent"))
    assert len(searches) == 1
    assert searches[0]["mode"] == SearchMode.FAST
    assert searches[0]["top_k"] == 5  # top_k // 3
    assert "44444444-4444-4444-8444-444444444444" in [mem.id for mem in results]
