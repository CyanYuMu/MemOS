"""检索出栈前只允许 activated 的记忆返回。

生产实测（2026-10-16）：检索结果里混着 archived 与 deleted 的记忆。根因是候选集
阶段有一处 `get_by_metadata` 没传 status（见 tests/memories/textual/test_tree_retriever.py），
但即便候选集干净，出栈这一层也应该再兜一道——只有 activated 是对用户有意义的，
归档的是被合并掉的旧版本，软删的是用户已经删掉的。
"""

from memos.api.handlers.search_handler import SearchHandler
from memos.dream.search import _format_context_hit as dream_format_context_hit


def _mem(memory_id: str, status: str | None, memory_type: str = "LongTermMemory") -> dict:
    metadata = {"memory_type": memory_type, "relativity": 1.0}
    if status is not None:
        metadata["status"] = status
    return {"id": memory_id, "memory": f"content of {memory_id}", "metadata": metadata}


def _results(*memories: dict, cube_id: str = "example-agent") -> dict:
    return {
        "text_mem": [
            {"cube_id": cube_id, "memories": list(memories), "total_nodes": len(memories)}
        ],
        "pref_mem": [],
        "tool_mem": [],
    }


def _ids(results: dict, bucket: str = "text_mem") -> list[str]:
    return [m["id"] for group in results[bucket] for m in group["memories"]]


def test_keeps_activated_only():
    results = _results(
        _mem("keep", "activated"),
        _mem("gone-archived", "archived"),
        _mem("gone-deleted", "deleted"),
        _mem("gone-resolving", "resolving"),
    )

    out = SearchHandler._drop_inactive(results)

    assert _ids(out) == ["keep"]


def test_drops_memories_that_carry_no_status():
    """只有 activated 能返回；拿不到 status 就无法证明它是 activated，按不可返回处理。"""
    results = _results(_mem("keep", "activated"), _mem("unknown", None))

    out = SearchHandler._drop_inactive(results)

    assert _ids(out) == ["keep"]


def test_filters_every_bucket_and_fixes_total_nodes():
    results = {
        "text_mem": [
            {
                "cube_id": "example-agent",
                "memories": [_mem("t-keep", "activated"), _mem("t-gone", "archived")],
                "total_nodes": 2,
            }
        ],
        "pref_mem": [
            {
                "cube_id": "example-agent",
                "memories": [
                    _mem("p-keep", "activated", "PreferenceMemory"),
                    _mem("p-gone", "deleted", "PreferenceMemory"),
                ],
                "total_nodes": 2,
            }
        ],
        "tool_mem": [
            {
                "cube_id": "example-agent",
                "memories": [_mem("x-gone", "archived", "ToolTrajectoryMemory")],
                "total_nodes": 1,
            }
        ],
    }

    out = SearchHandler._drop_inactive(results)

    assert _ids(out, "text_mem") == ["t-keep"]
    assert _ids(out, "pref_mem") == ["p-keep"]
    assert _ids(out, "tool_mem") == []
    assert out["text_mem"][0]["total_nodes"] == 1
    assert out["pref_mem"][0]["total_nodes"] == 1
    assert out["tool_mem"][0]["total_nodes"] == 0


def test_survives_missing_or_empty_buckets():
    assert SearchHandler._drop_inactive({}) == {}
    assert SearchHandler._drop_inactive({"text_mem": None}) == {"text_mem": None}


def test_dream_bucket_is_exempt():
    """dream_mem 的终态就是 completed（或 context_only / skipped），不是"被归档"。

    这里按 activated 过滤会把整个桶清空——那正是这两类此前一条都召回不到的原因之一。
    """
    results = {
        "text_mem": [
            {
                "cube_id": "example-agent",
                "memories": [_mem("t-keep", "activated")],
                "total_nodes": 1,
            }
        ],
        "dream_mem": [
            {
                "cube_id": "example-agent",
                "memories": [
                    _mem("d-completed", "completed", "DreamDiary"),
                    _mem("i-context", "context_only", "InsightMemory"),
                    _mem("d-deleted", "deleted", "DreamDiary"),
                ],
                "total_nodes": 3,
            }
        ],
    }

    out = SearchHandler._drop_inactive(results)

    assert _ids(out, "text_mem") == ["t-keep"]
    assert _ids(out, "dream_mem") == ["d-completed", "i-context", "d-deleted"]
    assert out["dream_mem"][0]["total_nodes"] == 3


def test_context_hits_echo_their_status():
    """Context 命中走的是另一套 metadata 组装（`memos.dream.search._format_context_hit`），
    原先不回填 status，于是出栈过滤会把它整类误杀——它和图里的 Context 节点一样是 activated。"""
    hit = {"id": "19bd661e-d563-45d9-974f-8d48a1d3aadc", "memory": "m", "score": 0.7}

    formatted = dream_format_context_hit(hit)

    assert formatted["metadata"]["status"] == "activated"
    assert SearchHandler._drop_inactive(
        {"text_mem": [{"cube_id": "c", "memories": [formatted], "total_nodes": 1}]}
    )["text_mem"][0]["memories"]
