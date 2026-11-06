"""S4 through the real entries: the data plane, the async read path and Dream.

The earlier S4 tests stop at the reader. These go one layer up and call the
handlers the endpoints use, so ``prompt_scope`` and the surrounding request code
are the real ones; only storage is stood in for (no graph or vector database on
this machine).

Real model. Each case saves a marked template for project A and leaves project B
on the built-in default, then asserts the marker shows up for A only.
"""

import json
import os

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memos.api.handlers.add_handler import AddHandler
from memos.api.handlers.base_handler import HandlerDependencies
from memos.api.product_models import APIExtractRequest
from memos.dream.dry_run import run_dream_stage
from memos.mem_reader import prompt_store
from memos.mem_reader.dry_run import build_dry_run_llm, build_dry_run_reader
from memos.mem_reader.prompt_registry import default_body


pytestmark = pytest.mark.skipif(
    not os.getenv("MEMRADER_API_KEY"), reason="real model test: set MEMRADER_API_KEY"
)

MARKER = "TAGMARKER-9C41"
CUSTOM_MARKER = "CUSTOMMARK-2E58"
MESSAGES = [
    {
        "role": "user",
        "content": "我们把项目的部署方式改成了蓝绿发布，回滚只需要切换流量，不再停机。",
    },
    {"role": "assistant", "content": "明白，蓝绿发布下回滚靠切流量。"},
]
DREAM_MEMORIES = {
    "memories": [
        {"id": "m1", "memory": "The user keeps postponing the schema migration."},
        {"id": "m2", "memory": "The user said the migration is blocked by a review."},
    ]
}
READERS = ["simple_struct", "multimodal_struct"]


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "entries.db"))


def _save_marked_chat_prompt(cube: str) -> None:
    body = (
        default_body("extract.chat", "zh")
        + f"\n\n额外要求：每条记忆的 tags 数组里必须包含字符串 {MARKER}。"
    )
    prompt_store.save_slot(cube, "extract.chat", "zh", body=body)


def _marker_in(payload) -> bool:
    return MARKER in json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 1. /product/extract_memory -> ExtractHandler.handle_extract_memories
# ---------------------------------------------------------------------------


def _extract_handler(reader) -> AddHandler:
    deps = HandlerDependencies(
        llm=Mock(),
        naive_mem_cube=Mock(),
        mem_reader=reader,
        mem_scheduler=Mock(),
        feedback_server=Mock(),
    )
    return AddHandler(deps)


def _extract_request(cube: str, **overrides) -> APIExtractRequest:
    payload = {
        "user_id": "u1",
        "session_id": "s1",
        "writable_cube_ids": [cube],
        "messages": MESSAGES,
        "mode": "fine",
    }
    payload.update(overrides)
    return APIExtractRequest(**payload)


@pytest.mark.parametrize("backend", READERS)
def test_extract_memory_entry_uses_the_project_prompt(backend, monkeypatch):
    monkeypatch.setenv("MEM_READER_BACKEND", backend)
    _save_marked_chat_prompt("cube-a")
    handler = _extract_handler(build_dry_run_reader(backend))

    project_a = handler.handle_extract_memories(_extract_request("cube-a"))
    assert project_a.data, "the extract entry returned nothing for project A"
    assert _marker_in(project_a.data), "project A's prompt had no effect on extract_memory"

    project_b = handler.handle_extract_memories(_extract_request("cube-b"))
    assert project_b.data, "the extract entry returned nothing for project B"
    assert not _marker_in(project_b.data), "project B was affected by project A's prompt"


@pytest.mark.parametrize("backend", READERS)
def test_extract_memory_entry_still_appends_the_custom_prompt(backend, monkeypatch):
    """The caller's guidance is appended after the template, project one included."""
    monkeypatch.setenv("MEM_READER_BACKEND", backend)
    _save_marked_chat_prompt("cube-a")
    handler = _extract_handler(build_dry_run_reader(backend))

    response = handler.handle_extract_memories(
        _extract_request(
            "cube-a",
            custom_extract_prompt={
                "detail_factual": f"每条记忆的 tags 数组里还必须包含字符串 {CUSTOM_MARKER}。"
            },
        )
    )
    assert response.data, "the extract entry returned nothing"
    rendered = json.dumps(response.data, ensure_ascii=False, default=str)
    assert MARKER in rendered, "the project template was dropped when a custom prompt was set"
    assert CUSTOM_MARKER in rendered, "the custom prompt was not appended"


# ---------------------------------------------------------------------------
# 2. async add read path -> MemReadMessageHandler._process_memories_with_reader
# ---------------------------------------------------------------------------


class FakeTextMem:
    """Just enough of ``TreeTextMemory`` for the read handler's fine path."""

    def __init__(self, items: list) -> None:
        self._items = {item.id: item for item in items}
        self.added: list = []
        self.deleted: list[str] = []
        self.memory_manager = SimpleNamespace(reorganizer=None, remove_and_refresh_memory=Mock())
        self.graph_store = None

    def get(self, mem_id: str, user_name: str | None = None):
        return self._items[mem_id]

    def add(self, memories, user_name: str | None = None):
        self.added.extend(memories)
        return [memory.id for memory in memories]

    def delete(self, mem_ids, user_name: str | None = None):
        self.deleted.extend(mem_ids)

    def add_rawfile_nodes_n_edges(self, *_args, **_kwargs):
        return None


class FakeSchedulerContext:
    def __init__(self, reader) -> None:
        self._reader = reader
        self.services = Mock()

    def get_mem_reader(self):
        return self._reader

    def get_mem_cube(self):
        return Mock()


@pytest.mark.parametrize("backend", READERS)
def test_async_read_path_uses_the_project_prompt(backend, monkeypatch):
    from memos.mem_scheduler.task_schedule_modules.handlers.mem_read_handler import (
        MemReadMessageHandler,
    )

    _save_marked_chat_prompt("cube-a")
    reader = build_dry_run_reader(backend)
    handler = MemReadMessageHandler(FakeSchedulerContext(reader))

    def fine_memories(cube: str) -> list:
        fast_items = [
            item
            for group in reader.get_memory(
                [MESSAGES],
                type="chat",
                info={"user_id": "u1", "session_id": "s1"},
                mode="fast",
                user_name=cube,
            )
            for item in group
        ]
        assert fast_items, f"fast mode produced no memories for {cube}"
        text_mem = FakeTextMem(fast_items)
        handler._process_memories_with_reader(
            mem_ids=[item.id for item in fast_items],
            user_id="u1",
            mem_cube_id=cube,
            text_mem=text_mem,
            user_name=cube,
            info={},
        )
        return text_mem.added

    project_a = fine_memories("cube-a")
    assert project_a, "the fine path added nothing for project A"
    assert _marker_in([memory.model_dump() for memory in project_a]), (
        "project A's prompt had no effect on the async read path"
    )

    project_b = fine_memories("cube-b")
    assert project_b, "the fine path added nothing for project B"
    assert not _marker_in([memory.model_dump() for memory in project_b]), (
        "project B was affected by project A's prompt"
    )


# ---------------------------------------------------------------------------
# 3. Dream scheduler entry -> MemDreamMessageHandler._run_dream
# ---------------------------------------------------------------------------


def test_dream_entry_runs_the_project_prompt(monkeypatch):
    from memos.mem_scheduler.task_schedule_modules.handlers.mem_dream_handler import (
        MemDreamMessageHandler,
    )
    from memos.plugins import hooks
    from memos.plugins.hook_defs import H

    draft_marker = "DRAFTMARK-7A20"
    body = (
        default_body("dream.motive", "en")
        + f"\n\nEvery motive description must end with the exact text {draft_marker}."
    )
    prompt_store.save_slot("cube-a", "dream.motive", "en", body=body)

    runs: dict[str, object] = {}

    def fake_plugin(**kwargs):
        runs["run"] = run_dream_stage(
            "dream.motive", DREAM_MEMORIES["memories"], llm=build_dry_run_llm()
        )

    monkeypatch.setitem(hooks._hooks, H.DREAM_EXECUTE, [fake_plugin])

    handler = MemDreamMessageHandler(Mock())
    handler._run_dream(
        user_id="u1",
        mem_cube_id="cube-a",
        user_name="cube-a",
        signal_snapshot=None,
        text_mem=Mock(),
    )

    run = runs["run"]
    assert run.output is not None, run.raw_output
    assert draft_marker in json.dumps(run.output, ensure_ascii=False), run.raw_output
