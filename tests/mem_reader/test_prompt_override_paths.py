"""Every extraction and processing path reads its template through ``prompt_for``.

These are offline tests: the LLM is a stub that records the prompt it received,
so a failing assertion points at the wiring, not at the model.
"""

import json

from types import SimpleNamespace

import pytest

from memos.mem_reader import prompt_store
from memos.mem_reader.multi_modal_struct import MultiModalStructMemReader
from memos.mem_reader.prompt_config import save_prompt
from memos.mem_reader.prompt_registry import default_body
from memos.mem_reader.prompt_runtime import prompt_for, prompt_scope
from memos.mem_reader.read_pref_memory.process_preference_memory import (
    _extract_explicit_preference,
    _extract_implicit_preference,
)
from memos.mem_reader.simple_struct import SimpleStructMemReader


CUBE = "cube-path"


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "paths.db"))


class RecordingLLM:
    def __init__(self, reply: str = '{"memory list": [], "summary": ""}'):
        self.reply = reply
        self.prompts: list[str] = []

    def generate(self, messages, **_kwargs):
        self.prompts.append(messages[0]["content"])
        return self.reply


def _multimodal_reader(llm) -> MultiModalStructMemReader:
    reader = object.__new__(MultiModalStructMemReader)
    reader.config = SimpleNamespace(remove_prompt_example=False)
    reader.llm = llm
    reader.general_llm = llm
    return reader


def _simple_reader(llm) -> SimpleStructMemReader:
    reader = object.__new__(SimpleStructMemReader)
    reader.config = SimpleNamespace(remove_prompt_example=False)
    reader.llm = llm
    reader.general_llm = llm
    return reader


def _save(key: str, lang: str, body: str) -> None:
    if key == "extract.chat":
        save_prompt(CUBE, body)
        return
    prompt_store.save_slot(CUBE, key, lang, body=body)


def test_multimodal_chat_uses_the_project_prompt():
    llm = RecordingLLM()
    _save("extract.chat", "zh", "对话模板\n${conversation}")
    with prompt_scope(CUBE):
        _multimodal_reader(llm)._get_llm_response("用户提到昨天的会议", None, None, "chat")
    assert llm.prompts[-1].startswith("对话模板\n用户提到昨天的会议")


def test_multimodal_doc_uses_the_project_prompt():
    llm = RecordingLLM()
    _save("extract.doc", "zh", "文档模板\n{chunk_text}")
    with prompt_scope(CUBE):
        _multimodal_reader(llm)._get_llm_response("这是一段中文文档正文", None, None, "doc")
    assert llm.prompts[-1].startswith("文档模板\n这是一段中文文档正文")


def test_multimodal_general_string_uses_the_project_prompt():
    llm = RecordingLLM()
    _save("extract.general_string", "zh", "通用文本模板\n{chunk_text}")
    with prompt_scope(CUBE):
        _multimodal_reader(llm)._get_llm_response(
            "一段没有来源的中文文本", None, None, "general_string"
        )
    assert llm.prompts[-1].startswith("通用文本模板\n一段没有来源的中文文本")


def test_multimodal_tool_trajectory_uses_the_project_prompt():
    llm = RecordingLLM(reply='{"trajectory": "x"}')
    _save("extract.tool_trajectory", "zh", "轨迹模板\n{messages}")
    with prompt_scope(CUBE):
        result = _multimodal_reader(llm)._get_llm_tool_trajectory_response(
            "用户让它查天气，调用了天气工具"
        )
    assert result == {"trajectory": "x"}
    assert llm.prompts[-1].startswith("轨迹模板\n")


def test_event_extraction_uses_the_project_prompt():
    from memos.mem_reader.event_extractor import EventExtractor

    llm = RecordingLLM(reply='[{"title": "会议", "content": "团队开了会"}]')
    _save("extract.event", "en", "事件模板\n{conversation}")
    with prompt_scope(CUBE):
        EventExtractor(llm).extract(
            [{"role": "user", "content": "我们开了一个会"}], need_embed=False
        )
    assert llm.prompts[-1].startswith("事件模板\n")


def test_explicit_preference_extraction_uses_the_project_prompt():
    llm = RecordingLLM(
        reply='[{"explicit_preference": "喜欢咖啡", "context_summary": "聊饮品", '
        '"reasoning": "直说", "topic": "饮品"}]'
    )
    _save("extract.preference_explicit", "zh", "显式偏好模板\n{qa_pair}")
    with prompt_scope(CUBE):
        result = _extract_explicit_preference("用户：我喜欢喝咖啡", llm)
    assert result is not None and result[0]["preference"] == "喜欢咖啡"
    assert llm.prompts[-1].startswith("显式偏好模板\n")


def test_implicit_preference_extraction_uses_the_project_prompt():
    llm = RecordingLLM(
        reply='[{"implicit_preference": "偏好安静环境", "context_summary": "聊办公", '
        '"reasoning": "推断", "topic": "办公"}]'
    )
    _save("extract.preference_implicit", "zh", "隐式偏好模板\n{qa_pair}")
    with prompt_scope(CUBE):
        result = _extract_implicit_preference("用户：办公室太吵了", llm)
    assert result is not None and result[0]["preference"] == "偏好安静环境"
    assert llm.prompts[-1].startswith("隐式偏好模板\n")


def test_hallucination_filter_uses_the_project_prompt():
    llm = RecordingLLM(reply=json.dumps({"0": {"keep": True, "reason": "有依据"}}))
    _save(
        "process.hallucination_filter",
        "en",
        "过滤模板\n对话：{messages_inline}\n记忆：{memories_inline}",
    )
    reader = _simple_reader(llm)
    with prompt_scope(CUBE):
        kept = reader.filter_hallucination_in_memories(
            [{"role": "user", "content": "我喜欢咖啡"}, {"role": "assistant", "content": "好的"}],
            [SimpleNamespace(memory="用户喜欢咖啡")],
        )
    assert [m.memory for m in kept] == ["用户喜欢咖啡"]
    assert llm.prompts[-1].startswith("过滤模板")


def test_a_broken_project_prompt_falls_back_to_the_built_in_one():
    llm = RecordingLLM(reply='[{"title": "会议", "content": "团队开了会"}]')
    # Stored bodies are validated on save, so craft the broken state directly.
    prompt_store.save_slot(CUBE, "extract.event", "en", body="事件模板 {conversation}")
    conn = __import__("sqlite3").connect(str(prompt_store._db_path()))
    with conn:
        conn.execute(
            "UPDATE prompt_versions SET body = ? WHERE prompt_key = 'extract.event'",
            ("事件模板 {missing_field}",),
        )
    conn.close()

    from memos.mem_reader.event_extractor import EventExtractor

    with prompt_scope(CUBE):
        EventExtractor(llm).extract([{"role": "user", "content": "开会"}], need_embed=False)
    assert llm.prompts[-1].startswith(default_body("extract.event", "en").split("\n")[0])










def test_async_read_path_scopes_prompts_to_the_cube_not_the_user_name(tmp_path, monkeypatch):
    """The scheduler read path must load overrides for the cube it writes to.

    ``user_name`` and ``mem_cube_id`` can differ; scoping by ``user_name`` made
    the async path miss the project prompt entirely.
    """
    from memos.mem_scheduler.task_schedule_modules.handlers.mem_read_handler import (
        MemReadMessageHandler,
    )
    from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata

    prompt_store.save_slot("cube-async", "extract.chat", "zh", body="异步模板 ${conversation}")

    seen: list[str] = []

    class FakeReader:
        save_rawfile = False
        memory_version_switch = "off"
        graph_db = None

        def fine_transfer_simple_mem(self, *_args, **_kwargs):
            seen.append(prompt_for("extract.chat", "zh", "内置模板"))
            return []

    item = TextualMemoryItem(
        memory="用户提到昨天的会议",
        metadata=TreeNodeTextualMemoryMetadata(user_id="u1", session_id="s1"),
    )
    text_mem = SimpleNamespace(get=lambda mem_id, user_name=None: item)

    handler = MemReadMessageHandler(
        scheduler_context=SimpleNamespace(get_mem_reader=lambda: FakeReader())
    )
    handler._process_memories_with_reader(
        mem_ids=["m1"],
        user_id="u1",
        mem_cube_id="cube-async",
        text_mem=text_mem,
        user_name="user-name-of-u1",
    )

    assert seen == ["异步模板 ${conversation}"]
