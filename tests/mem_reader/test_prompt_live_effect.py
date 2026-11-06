"""S4 against the real model: a saved prompt changes what both readers extract.

The draft is the built-in Chinese chat template plus one instruction that is
easy to detect in the output (a fixed tag). Project A saves it, project B does
not; the same conversation is extracted for both. After resetting A, the marker
must disappear again.
"""

import os

import pytest

from memos.mem_reader import prompt_store
from memos.mem_reader.dry_run import build_dry_run_reader
from memos.mem_reader.prompt_registry import default_body
from memos.mem_reader.prompt_runtime import prompt_scope


pytestmark = pytest.mark.skipif(
    not os.getenv("MEMRADER_API_KEY"), reason="real model test: set MEMRADER_API_KEY"
)

MARKER = "TAGMARKER-7F3A"
MESSAGES = [
    {
        "role": "user",
        "content": "我们把项目的部署方式改成了蓝绿发布，回滚只需要切换流量，不再停机。",
    },
    {"role": "assistant", "content": "明白，蓝绿发布下回滚靠切流量。"},
]


def _extract(reader, cube: str):
    with prompt_scope(cube):
        groups = reader.get_memory(
            [MESSAGES],
            type="chat",
            info={"user_id": "u1", "session_id": "s1"},
            mode="fine",
            user_name=cube,
        )
    return [memory for group in groups for memory in group]


def _has_marker(memories) -> bool:
    for memory in memories:
        if MARKER in (memory.memory or ""):
            return True
        if any(MARKER in str(tag) for tag in (memory.metadata.tags or [])):
            return True
    return False


@pytest.mark.parametrize("backend", ["simple_struct", "multimodal_struct"])
def test_project_prompt_changes_extraction_for_that_project_only(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "live.db"))
    body = (
        default_body("extract.chat", "zh")
        + f"\n\n额外要求：每条记忆的 tags 数组里必须包含字符串 {MARKER}。"
    )
    prompt_store.save_slot("cube-a", "extract.chat", "zh", body=body)

    reader = build_dry_run_reader(backend)

    project_a = _extract(reader, "cube-a")
    assert project_a, "the model returned no memories for project A"
    assert _has_marker(project_a), "project A's prompt had no visible effect"

    project_b = _extract(reader, "cube-b")
    assert project_b, "the model returned no memories for project B"
    assert not _has_marker(project_b), "project B was affected by project A's prompt"

    prompt_store.reset_slots("cube-a", key="extract.chat")
    after_reset = _extract(reader, "cube-a")
    assert not _has_marker(after_reset), "the marker survived a reset"
