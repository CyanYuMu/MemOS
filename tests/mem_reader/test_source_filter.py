from memos.mem_reader.source_filter import MemorySourceFilter


def _text(sources):
    return MemorySourceFilter().build_prompt_text(sources)


def test_only_user_sources_reach_the_prompt():
    """Agent 输出、工具结果都不是用户陈述 —— 混进去就会被抽成"用户偏好"。"""
    out = _text(
        [
            {"role": "assistant", "content": "我建议把告警按等级分组"},
            {"role": "system", "content": "你是安全运营助手"},
            {"role": "tool", "content": "query_alarm 返回：高危 3 条"},
            {"role": "user", "content": "今天是周一"},
        ]
    )

    assert out == "user: 今天是周一"


def test_tool_source_type_is_blocked_even_with_the_user_role():
    out = _text([{"role": "user", "type": "tool", "content": "工具返回的内容"}])

    assert out == ""


def test_user_message_heading_is_stripped():
    """业务侧把用户原话包在 `### 用户消息：` 这种模板里，只抽标题后面那部分。"""
    out = _text([{"role": "user", "content": "### 用户消息：我喜欢草莓"}])

    assert out == "user: 我喜欢草莓"


def test_trailing_memory_context_is_cut_off():
    """模板里跟在用户问题后面的检索记忆，属于注入内容，不能当成用户说的。"""
    out = _text(
        [
            {
                "role": "user",
                "content": "我想吃草莓\n### 以下是可能和用户问题关联的对话记忆\n用户爱吃苹果",
            }
        ]
    )

    assert out == "user: 我想吃草莓"


def test_injected_context_markers_drop_the_source():
    markers = [
        "<retrieved_context>某搜索结果</retrieved_context>",
        "=== MemOS LONG-TERM MEMORY ===\n旧记忆",
        'Conversation info (untrusted metadata):\n{"channel": "x"}',
        "[cron: 每天9点] 提醒我看告警",
        'data: {"id": "chatcmpl-1"}\n{"choices": []}',
    ]

    for marker in markers:
        assert _text([{"role": "user", "content": marker}]) == "", marker


def test_empty_and_unknown_sources_are_skipped():
    assert _text(None) == ""
    assert _text([{"role": "user", "content": "   "}]) == ""
    assert _text(["not-a-dict"]) == ""
