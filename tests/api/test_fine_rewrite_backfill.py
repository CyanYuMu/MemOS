"""REWRITE 改写只增强不回删：模型没提到的条目保留原文。"""

from types import SimpleNamespace

import memos.mem_scheduler.memory_manage_modules.enhancement_pipeline as pipeline_module

from memos.mem_scheduler.memory_manage_modules.enhancement_pipeline import EnhancementPipeline
from memos.memories.textual.item import TextualMemoryItem, TextualMemoryMetadata
from memos.types.general_types import FineStrategy


def _memory(text: str, mem_id: str):
    return TextualMemoryItem(
        id=mem_id,
        memory=text,
        metadata=TextualMemoryMetadata(user_id="example-agent", memory_type="UserMemory"),
    )


class _LLM:
    def __init__(self, response: str):
        self.response = response

    def generate(self, messages):
        return self.response


def _pipeline(response: str) -> EnhancementPipeline:
    return EnhancementPipeline(
        process_llm=_LLM(response),
        config=SimpleNamespace(
            scheduler_retriever_batch_size=None, scheduler_retriever_enhance_retries=0
        ),
        build_prompt=lambda name, **kwargs: "prompt",
    )


def test_rewrite_keeps_memories_the_llm_skipped(monkeypatch):
    monkeypatch.setattr(pipeline_module, "FINE_STRATEGY", FineStrategy.REWRITE)
    memories = [
        _memory("记忆一", "11111111-1111-4111-8111-111111111111"),
        _memory("记忆二", "22222222-2222-4222-8222-222222222222"),
        _memory("记忆三", "33333333-3333-4333-8333-333333333333"),
    ]
    # 只改写第 0、2 条，第 1 条模型没回
    pipeline = _pipeline("- [0] 改写后的一\n- [2] 改写后的三")

    enhanced, ok = pipeline._process_enhancement_batch(
        batch_index=0, query_history=["query"], memories=memories, retries=0
    )

    assert ok is True
    assert [mem.id for mem in enhanced] == [mem.id for mem in memories]
    assert [mem.memory for mem in enhanced] == ["改写后的一", "记忆二", "改写后的三"]
    assert [mem.metadata.memory_type for mem in enhanced] == ["UserMemory"] * 3
