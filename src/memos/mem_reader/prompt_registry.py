"""Registry of every memory prompt the product can override per project.

The registry is the single source of truth for prompt defaults. The console and
the BFF never store default text: they read it from ``/product/prompts/*``.

A prompt is addressed by ``(key, lang)``. ``syntax`` records how the calling
code substitutes values and therefore how a draft is validated:

``dollar``
    ``body.replace("${name}", value)``.
``format``
    ``body.format(**values)``; literal braces must be doubled.
``brace``
    ``body.replace("{name}", value)``; literal braces stay as they are.

Defaults here are byte-for-byte the constants the readers and the Dream
pipeline used before this registry existed. ``tests/mem_reader/test_prompt_registry.py``
pins their content hashes.

The full inventory (file, line, caller, model) lives in ``docs/prompt_registry.md``.
"""

from __future__ import annotations

import re
import string

from dataclasses import dataclass, field, replace
from typing import Any

from memos.exceptions import ConfigurationError, PromptNotFoundError
from memos.templates.mem_reader_prompts import (
    GENERAL_STRUCT_STRING_READER_PROMPT,
    GENERAL_STRUCT_STRING_READER_PROMPT_ZH,
    SIMPLE_STRUCT_DOC_READER_PROMPT,
    SIMPLE_STRUCT_DOC_READER_PROMPT_ZH,
    SIMPLE_STRUCT_HALLUCINATION_FILTER_PROMPT,
    SIMPLE_STRUCT_MEM_READER_PROMPT,
    SIMPLE_STRUCT_MEM_READER_PROMPT_ZH,
)
from memos.templates.prefer_complete_prompt import (
    NAIVE_EXPLICIT_PREFERENCE_EXTRACT_PROMPT,
    NAIVE_EXPLICIT_PREFERENCE_EXTRACT_PROMPT_ZH,
    NAIVE_IMPLICIT_PREFERENCE_EXTRACT_PROMPT,
    NAIVE_IMPLICIT_PREFERENCE_EXTRACT_PROMPT_ZH,
)
from memos.templates.tool_mem_prompts import TOOL_TRAJECTORY_PROMPT_EN, TOOL_TRAJECTORY_PROMPT_ZH


_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_PROMPT_CHARS = 20000
MAX_NOTE_CHARS = 200
MAX_INSTRUCTION_CHARS = 2000
MAX_REQUEST_BYTES = 512 * 1024
MAX_TEST_REQUEST_BYTES = 256 * 1024
SUPPORTED_LANGS: tuple[str, ...] = ("zh", "en")
GROUPS: tuple[str, ...] = ("extract", "process", "dream")
SYNTAXES: tuple[str, ...] = ("dollar", "format", "brace")

_ZH = "zh"
_EN = "en"


@dataclass(frozen=True)
class Placeholder:
    """One substitution point inside a prompt body."""

    name: str
    token: str
    description: dict[str, str] = field(default_factory=dict)
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token": self.token,
            "required": self.required,
            "description": dict(self.description),
        }


@dataclass(frozen=True)
class OutputContract:
    """What the calling code expects the model to return."""

    format: str = "json"
    required_keys: tuple[str, ...] = ()
    item_keys: tuple[str, ...] = ()
    description: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "required_keys": list(self.required_keys),
            "item_keys": list(self.item_keys),
            "description": dict(self.description),
        }


@dataclass(frozen=True)
class PromptSpec:
    """One overridable prompt."""

    key: str
    group: str
    title: dict[str, str]
    summary: dict[str, str]
    languages: tuple[str, ...]
    syntax: str
    placeholders: tuple[Placeholder, ...]
    defaults: dict[str, str]
    input_kind: str
    output_contract: OutputContract
    input_schema: dict[str, Any] = field(default_factory=dict)
    sample_inputs: tuple[dict[str, Any], ...] = ()
    locked_suffix: str | None = None
    editable: bool = True

    def default_for(self, lang: str) -> str:
        try:
            return self.defaults[lang]
        except KeyError as exc:
            raise PromptNotFoundError(f"prompt {self.key!r} has no {lang!r} default") from exc

    def public_dict(self) -> dict[str, Any]:
        """Registration fields shown in ``detail`` (no defaults)."""
        return {
            "key": self.key,
            "group": self.group,
            "title": dict(self.title),
            "summary": dict(self.summary),
            "languages": list(self.languages),
            "syntax": self.syntax,
            "placeholders": [p.as_dict() for p in self.placeholders],
            "locked_suffix": self.locked_suffix,
            "input_kind": self.input_kind,
            "input_schema": self.input_schema,
            "output_contract": self.output_contract.as_dict(),
            "sample_inputs": [dict(s) for s in self.sample_inputs],
            "editable": self.editable,
        }

    def summary_dict(self) -> dict[str, Any]:
        """Fields shown in ``list``."""
        return {
            "key": self.key,
            "group": self.group,
            "title": dict(self.title),
            "summary": dict(self.summary),
            "languages": list(self.languages),
            "editable": self.editable,
        }


def _conversation(**extra: Any) -> dict[str, Any]:
    schema = {
        "type": "object",
        "required": ["messages"],
        "properties": {
            "messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["role", "content"],
                    "properties": {
                        "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                        "content": {"type": "string"},
                    },
                },
            }
        },
    }
    schema.update(extra)
    return schema


_TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}

_MEMORIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["memories"],
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "memory"],
                "properties": {"id": {"type": "string"}, "memory": {"type": "string"}},
            },
        }
    },
}

_QA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["qa_pair"],
    "properties": {"qa_pair": {"type": "string"}},
}

_CHAT_TITLE = {"zh": "对话抽取", "en": "Conversation extraction"}
_CHAT_SUMMARY = {"zh": "把一段对话整理成记忆条目", "en": "Turns a conversation into memory items"}

_CHAT_OUTPUT = OutputContract(
    format="json",
    required_keys=("memory list", "summary"),
    item_keys=("key", "memory_type", "value", "tags"),
    description={
        "zh": "返回 JSON 对象，memory list 是记忆数组，summary 是一句话总结。",
        "en": "A JSON object with a memory list array and a summary string.",
    },
)


def _import_event_prompt() -> str:
    from memos.mem_reader.event_extractor import EVENT_EXTRACTION_PROMPT

    return EVENT_EXTRACTION_PROMPT


def _dream_specs() -> tuple[PromptSpec, ...]:
    from memos.dream.prompts import (
        CONTEXT_BINDING_PROMPT,
        CONTEXT_SUMMARY_PROMPT,
        MOTIVE_FORMATION_PROMPT,
    )
    from memos.dream.prompts.motive_prompt import MOTIVE_OUTPUT_SUFFIX
    from memos.dream.prompts.reasoning_prompt import CONSOLIDATION_REASONING_PROMPT

    return (
        PromptSpec(
            key="dream.motive",
            group="dream",
            title={"zh": "动机形成", "en": "Motive formation"},
            summary={
                "zh": "决定哪些记忆值得整理，并给出理由",
                "en": "Decides which memories are worth consolidating",
            },
            languages=(_EN,),
            syntax="format",
            placeholders=(
                Placeholder(
                    name="memories_block",
                    token="{memories_block}",
                    description={"zh": "待整理记忆的列表", "en": "Pending memories"},
                ),
                Placeholder(
                    name="max_motives",
                    token="{max_motives}",
                    description={"zh": "最多产出几个动机", "en": "Maximum number of motives"},
                    required=False,
                ),
            ),
            defaults={_EN: MOTIVE_FORMATION_PROMPT},
            input_kind="memories",
            input_schema=_MEMORIES_SCHEMA,
            output_contract=OutputContract(
                format="json",
                required_keys=(),
                item_keys=("motive_id", "description", "memory_ids"),
                description={
                    "zh": "JSON 数组，每项含 motive_id、description、memory_ids。",
                    "en": "A JSON array of motives with motive_id, description, memory_ids.",
                },
            ),
            locked_suffix=MOTIVE_OUTPUT_SUFFIX,
        ),
        PromptSpec(
            key="dream.reasoning",
            group="dream",
            title={"zh": "深度推理", "en": "Consolidation reasoning"},
            summary={
                "zh": "按动机把相关记忆想透，产出一条洞察",
                "en": "Reasons across memories into one insight",
            },
            languages=(_EN,),
            syntax="format",
            placeholders=(
                Placeholder(
                    name="motive_description",
                    token="{motive_description}",
                    description={"zh": "动机说明", "en": "Motive description"},
                ),
                Placeholder(
                    name="source_memories_block",
                    token="{source_memories_block}",
                    description={"zh": "触发整理的原始记忆", "en": "Source memories"},
                ),
                Placeholder(
                    name="related_memories_block",
                    token="{related_memories_block}",
                    description={"zh": "召回到的相关记忆", "en": "Recalled related memories"},
                ),
            ),
            defaults={_EN: CONSOLIDATION_REASONING_PROMPT},
            input_kind="memories",
            input_schema=_MEMORIES_SCHEMA,
            output_contract=OutputContract(
                format="json",
                required_keys=("dream_content",),
                item_keys=("hypothetical_question",),
                description={
                    "zh": "JSON 对象，含 dream_content 与 hypothetical_question。",
                    "en": "A JSON object with dream_content and hypothetical_question.",
                },
            ),
        ),
        PromptSpec(
            key="dream.context_binding",
            group="dream",
            title={"zh": "上下文绑定", "en": "Context binding"},
            summary={
                "zh": "把记忆归到长期上下文中",
                "en": "Groups memories into long-lived contexts",
            },
            languages=(_EN,),
            syntax="format",
            placeholders=(
                Placeholder(
                    name="memories_block",
                    token="{memories_block}",
                    description={"zh": "要归组的记忆", "en": "Memories to group"},
                ),
            ),
            defaults={_EN: CONTEXT_BINDING_PROMPT},
            input_kind="memories",
            input_schema=_MEMORIES_SCHEMA,
            output_contract=OutputContract(
                format="json",
                required_keys=("contexts",),
                item_keys=("key", "ids", "confidence", "reason"),
            ),
        ),
        PromptSpec(
            key="dream.context_summary",
            group="dream",
            title={"zh": "上下文摘要", "en": "Context summary"},
            summary={
                "zh": "把新记忆合并进已有上下文的摘要",
                "en": "Folds new memories into a context summary",
            },
            languages=(_EN,),
            syntax="format",
            placeholders=(
                Placeholder(
                    name="memories_block",
                    token="{memories_block}",
                    description={"zh": "要合并的记忆", "en": "Memories to fold in"},
                ),
                Placeholder(
                    name="existing_key",
                    token="{existing_key}",
                    description={"zh": "已有上下文的标题", "en": "Existing context label"},
                    required=False,
                ),
                Placeholder(
                    name="existing_memory",
                    token="{existing_memory}",
                    description={"zh": "已有上下文的正文", "en": "Existing context body"},
                    required=False,
                ),
            ),
            defaults={_EN: CONTEXT_SUMMARY_PROMPT},
            input_kind="memories",
            input_schema=_MEMORIES_SCHEMA,
            output_contract=OutputContract(
                format="json",
                required_keys=("key", "memory"),
                item_keys=("confidence",),
            ),
        ),
    )


_REGISTRY_LIST: tuple[PromptSpec, ...] = (
    PromptSpec(
        key="extract.chat",
        group="extract",
        title=_CHAT_TITLE,
        summary=_CHAT_SUMMARY,
        languages=(_ZH, _EN),
        syntax="dollar",
        placeholders=(
            Placeholder(
                name="conversation",
                token="${conversation}",
                description={"zh": "整段对话原文", "en": "The conversation text"},
            ),
            Placeholder(
                name="custom_tags_prompt",
                token="${custom_tags_prompt}",
                description={
                    "zh": "请求级自定义标签说明，没有请求级标签时替换为空串",
                    "en": "Request-scoped tag hints, replaced with an empty string when absent",
                },
                required=False,
            ),
        ),
        defaults={
            _ZH: SIMPLE_STRUCT_MEM_READER_PROMPT_ZH,
            _EN: SIMPLE_STRUCT_MEM_READER_PROMPT,
        },
        input_kind="conversation",
        input_schema=_conversation(),
        sample_inputs=(
            {
                "id": "zh-deploy",
                "lang": "zh",
                "title": {"zh": "讨论部署方式", "en": "Deployment discussion"},
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": "我们把项目的部署方式改成蓝绿发布了，回滚只需要切换流量，不再停机。",
                        },
                        {"role": "assistant", "content": "明白，蓝绿发布下回滚靠切流量。"},
                    ]
                },
            },
            {
                "id": "en-tools",
                "lang": "en",
                "title": {"zh": "工具偏好", "en": "Tool preference"},
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": "I moved our CI to self-hosted runners last week.",
                        },
                        {
                            "role": "assistant",
                            "content": "Got it, builds now run on your own machines.",
                        },
                    ]
                },
            },
        ),
        output_contract=_CHAT_OUTPUT,
    ),
    PromptSpec(
        key="extract.doc",
        group="extract",
        title={"zh": "文档抽取", "en": "Document extraction"},
        summary={"zh": "把文档章节整理成记忆条目", "en": "Turns document chunks into memory items"},
        languages=(_ZH, _EN),
        syntax="brace",
        placeholders=(
            Placeholder(
                name="chunk_text",
                token="{chunk_text}",
                description={"zh": "文档章节的纯文本", "en": "Plain text of one document chunk"},
            ),
            Placeholder(
                name="custom_tags_prompt",
                token="{custom_tags_prompt}",
                description={"zh": "请求级自定义标签说明", "en": "Request-scoped tag hints"},
                required=False,
            ),
        ),
        defaults={
            _ZH: SIMPLE_STRUCT_DOC_READER_PROMPT_ZH,
            _EN: SIMPLE_STRUCT_DOC_READER_PROMPT,
        },
        input_kind="text",
        input_schema=_TEXT_SCHEMA,
        output_contract=_CHAT_OUTPUT,
    ),
    PromptSpec(
        key="extract.general_string",
        group="extract",
        title={"zh": "通用文本抽取", "en": "Plain text extraction"},
        summary={
            "zh": "把没有文档来源的纯文本整理成记忆条目",
            "en": "Turns plain text without a document source into memory items",
        },
        languages=(_ZH, _EN),
        syntax="brace",
        placeholders=(
            Placeholder(
                name="chunk_text",
                token="{chunk_text}",
                description={"zh": "要抽取的文本", "en": "The text to extract from"},
            ),
            Placeholder(
                name="custom_tags_prompt",
                token="{custom_tags_prompt}",
                description={"zh": "请求级自定义标签说明", "en": "Request-scoped tag hints"},
                required=False,
            ),
        ),
        defaults={
            _ZH: GENERAL_STRUCT_STRING_READER_PROMPT_ZH,
            _EN: GENERAL_STRUCT_STRING_READER_PROMPT,
        },
        input_kind="text",
        input_schema=_TEXT_SCHEMA,
        output_contract=_CHAT_OUTPUT,
    ),
    PromptSpec(
        key="extract.preference_explicit",
        group="extract",
        title={"zh": "显式偏好抽取", "en": "Explicit preference extraction"},
        summary={
            "zh": "从一问一答里找出用户明确说过的偏好",
            "en": "Finds preferences the user states outright",
        },
        languages=(_ZH, _EN),
        syntax="brace",
        placeholders=(
            Placeholder(
                name="qa_pair",
                token="{qa_pair}",
                description={"zh": "一轮问答的文本", "en": "One question/answer pair as text"},
            ),
        ),
        defaults={
            _ZH: NAIVE_EXPLICIT_PREFERENCE_EXTRACT_PROMPT_ZH,
            _EN: NAIVE_EXPLICIT_PREFERENCE_EXTRACT_PROMPT,
        },
        input_kind="qa_pair",
        input_schema=_QA_SCHEMA,
        output_contract=OutputContract(
            format="json",
            required_keys=("context_summary",),
            item_keys=("explicit_preference", "reasoning", "topic"),
            description={
                "zh": "JSON 数组，每项含 explicit_preference、context_summary、reasoning、topic。",
                "en": "A JSON array of objects with explicit_preference, context_summary, reasoning, topic.",
            },
        ),
    ),
    PromptSpec(
        key="extract.preference_implicit",
        group="extract",
        title={"zh": "隐式偏好抽取", "en": "Implicit preference extraction"},
        summary={
            "zh": "从一问一答里推断用户没说出口的偏好",
            "en": "Infers preferences the user did not state",
        },
        languages=(_ZH, _EN),
        syntax="brace",
        placeholders=(
            Placeholder(
                name="qa_pair",
                token="{qa_pair}",
                description={"zh": "一轮问答的文本", "en": "One question/answer pair as text"},
            ),
        ),
        defaults={
            _ZH: NAIVE_IMPLICIT_PREFERENCE_EXTRACT_PROMPT_ZH,
            _EN: NAIVE_IMPLICIT_PREFERENCE_EXTRACT_PROMPT,
        },
        input_kind="qa_pair",
        input_schema=_QA_SCHEMA,
        output_contract=OutputContract(
            format="json",
            required_keys=("context_summary",),
            item_keys=("implicit_preference", "reasoning", "topic"),
            description={
                "zh": "JSON 数组，每项含 implicit_preference、context_summary、reasoning、topic。",
                "en": "A JSON array of objects with implicit_preference, context_summary, reasoning, topic.",
            },
        ),
    ),
    PromptSpec(
        key="extract.event",
        group="extract",
        title={"zh": "事件抽取", "en": "Event extraction"},
        summary={
            "zh": "把对话里的具体事件整理成事件记忆",
            "en": "Turns events in a conversation into event memories",
        },
        languages=(_EN,),
        syntax="format",
        placeholders=(
            Placeholder(
                name="conversation",
                token="{conversation}",
                description={"zh": "整段对话原文", "en": "The conversation text"},
            ),
        ),
        defaults={_EN: _import_event_prompt()},
        input_kind="conversation",
        input_schema=_conversation(),
        output_contract=OutputContract(
            format="json",
            required_keys=(),
            item_keys=("title", "content"),
            description={
                "zh": "JSON 数组，每项是一个事件，含 title、content 等字段。",
                "en": "A JSON array of events with title and content.",
            },
        ),
    ),
    PromptSpec(
        key="extract.tool_trajectory",
        group="extract",
        title={"zh": "工具轨迹抽取", "en": "Tool trajectory extraction"},
        summary={
            "zh": "把一次工具调用的过程整理成可复用的经验",
            "en": "Turns a tool-calling trajectory into reusable experience",
        },
        languages=(_ZH, _EN),
        syntax="brace",
        placeholders=(
            Placeholder(
                name="messages",
                token="{messages}",
                description={"zh": "工具调用轨迹的文本", "en": "The trajectory rendered as text"},
            ),
        ),
        defaults={_ZH: TOOL_TRAJECTORY_PROMPT_ZH, _EN: TOOL_TRAJECTORY_PROMPT_EN},
        input_kind="conversation",
        input_schema=_conversation(),
        output_contract=OutputContract(
            format="json",
            required_keys=("trajectory",),
            item_keys=("correctness", "experience", "tool_used_status"),
        ),
    ),
    PromptSpec(
        key="process.hallucination_filter",
        group="process",
        title={"zh": "幻觉过滤", "en": "Hallucination filter"},
        summary={
            "zh": "逐条判断抽取出的记忆有没有对话依据",
            "en": "Checks each extracted memory against the conversation",
        },
        languages=(_EN,),
        syntax="format",
        placeholders=(
            Placeholder(
                name="messages_inline",
                token="{messages_inline}",
                description={"zh": "对话原文", "en": "The conversation"},
            ),
            Placeholder(
                name="memories_inline",
                token="{memories_inline}",
                description={"zh": "待判断的记忆", "en": "Memories to check"},
            ),
        ),
        defaults={_EN: SIMPLE_STRUCT_HALLUCINATION_FILTER_PROMPT},
        input_kind="memories",
        input_schema=_MEMORIES_SCHEMA,
        output_contract=OutputContract(
            format="json",
            required_keys=(),
            item_keys=("keep", "reason"),
            description={
                "zh": "JSON 对象，键是记忆下标，值是 {keep, reason}。",
                "en": "A JSON object keyed by memory index with {keep, reason}.",
            },
        ),
    ),
)


@dataclass(frozen=True)
class PromptIssue:
    """One validation finding, shaped for the console."""

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


# Realistic, deliberately different inputs per prompt. Kept in one table so a
# new prompt cannot quietly ship without one: ``tests/mem_reader/test_prompt_store.py``
# asserts every registration has at least one sample.
_SAMPLES: dict[str, tuple[dict[str, Any], ...]] = {
    "extract.doc": (
        {
            "id": "zh-doc",
            "lang": "zh",
            "title": {"zh": "接口文档片段", "en": "API doc excerpt"},
            "input": {
                "text": "## 发布流程\n1. 在预发环境跑全量回归。\n2. 蓝绿切换流量，观察 30 分钟。\n3. 出现 5xx 升高时切回旧集群。"
            },
        },
        {
            "id": "en-doc",
            "lang": "en",
            "title": {"zh": "运维手册片段", "en": "Runbook excerpt"},
            "input": {
                "text": "## Backup\nSnapshots run every 6 hours and are kept for 14 days. Restore is tested monthly."
            },
        },
    ),
    "extract.general_string": (
        {
            "id": "zh-text",
            "lang": "zh",
            "title": {"zh": "会议纪要摘录", "en": "Meeting note excerpt"},
            "input": {"text": "本周三评审确定：灰度比例从 5% 提到 20%，下周一全量。"},
        },
        {
            "id": "en-text",
            "lang": "en",
            "title": {"zh": "发布说明", "en": "Release note"},
            "input": {
                "text": "The 3.2 release drops Python 3.9 support and adds a cached search path."
            },
        },
    ),
    "extract.preference_explicit": (
        {
            "id": "zh-seat",
            "lang": "zh",
            "title": {"zh": "座位偏好", "en": "Seat preference"},
            "input": {"qa_pair": "用户：帮我订机票，我更喜欢靠窗的座位。\n助手：好的，已选靠窗。"},
        },
        {
            "id": "en-coffee",
            "lang": "en",
            "title": {"zh": "饮品偏好", "en": "Drink preference"},
            "input": {
                "qa_pair": "User: Book the hotel with a gym, I always pick hotels with a gym.\nAssistant: Noted."
            },
        },
    ),
    "extract.preference_implicit": (
        {
            "id": "zh-noisy",
            "lang": "zh",
            "title": {"zh": "对噪音的容忍", "en": "Noise tolerance"},
            "input": {
                "qa_pair": "用户：这家酒店晚上有点吵，下次想找个安静的。\n助手：那我记下安静这一点。"
            },
        },
        {
            "id": "en-early",
            "lang": "en",
            "title": {"zh": "作息推断", "en": "Schedule inference"},
            "input": {
                "qa_pair": "User: Can we move the sync to 7am my time? My mornings are the only quiet part of the day.\nAssistant: Sure."
            },
        },
    ),
    "extract.event": (
        {
            "id": "en-incident",
            "lang": "en",
            "title": {"zh": "故障复盘对话", "en": "Incident conversation"},
            "input": {
                "messages": [
                    {"role": "user", "content": "昨天下午的支付超时是什么原因？"},
                    {
                        "role": "assistant",
                        "content": "数据库连接池在 15:20 打满，已于 15:45 扩容恢复。",
                    },
                ]
            },
        },
    ),
    "extract.tool_trajectory": (
        {
            "id": "zh-weather",
            "lang": "zh",
            "title": {"zh": "一次工具调用", "en": "One tool call"},
            "input": {
                "messages": [
                    {"role": "user", "content": "帮我查一下明天北京的天气"},
                    {"role": "assistant", "content": "调用 weather 工具，参数 city=北京"},
                    {"role": "assistant", "content": "明天北京晴，最高 18 度。"},
                ]
            },
        },
        {
            "id": "en-search",
            "lang": "en",
            "title": {"zh": "检索工具调用", "en": "Search tool call"},
            "input": {
                "messages": [
                    {"role": "user", "content": "Find the current price of that stock."},
                    {"role": "assistant", "content": "Called search with query='XYZ price'."},
                ]
            },
        },
    ),

    "dream.motive": (
        {
            "id": "en-unresolved",
            "lang": "en",
            "title": {"zh": "未解决的反复话题", "en": "A recurring unresolved topic"},
            "input": {
                "memories": [
                    {
                        "id": "m1",
                        "memory": "The user asked how to handle a teammate who keeps missing deadlines.",
                    },
                    {
                        "id": "m2",
                        "memory": "The user mentioned the same teammate was absent from the release for two sprints.",
                    },
                ]
            },
        },
    ),
    "dream.reasoning": (
        {
            "id": "en-cluster",
            "lang": "en",
            "title": {"zh": "一组关于同一项目的记忆", "en": "Memories about one project"},
            "input": {
                "memories": [
                    {"id": "m1", "memory": "The user keeps postponing the migration."},
                    {
                        "id": "m2",
                        "memory": "The user said the migration is blocked by a schema review.",
                    },
                ]
            },
        },
    ),
    "dream.context_binding": (
        {
            "id": "en-two-contexts",
            "lang": "en",
            "title": {"zh": "两组可分的记忆", "en": "Two separable groups"},
            "input": {
                "memories": [
                    {"id": "m1", "memory": "The user prefers window seats."},
                    {"id": "m2", "memory": "The user reviews pull requests every morning."},
                ]
            },
        },
    ),
    "dream.context_summary": (
        {
            "id": "en-fold-in",
            "lang": "en",
            "title": {"zh": "合并进已有上下文", "en": "Folding into a context"},
            "input": {
                "memories": [
                    {"id": "m1", "memory": "The user now books aisle seats for long flights."},
                ]
            },
        },
    ),
    "process.hallucination_filter": (
        {
            "id": "en-check",
            "lang": "en",
            "title": {"zh": "有依据与无依据各一条", "en": "One supported, one not"},
            "input": {
                "memories": [
                    {"id": "0", "memory": "The user plans to visit Tokyo next week."},
                    {"id": "1", "memory": "The user plans to visit Osaka."},
                ]
            },
        },
    ),
}


def _build_registry() -> dict[str, PromptSpec]:
    registry: dict[str, PromptSpec] = {}
    for spec in (*_REGISTRY_LIST, *_dream_specs()):
        if not spec.sample_inputs and spec.key in _SAMPLES:
            spec = replace(spec, sample_inputs=_SAMPLES[spec.key])
        if spec.key in registry:
            raise ConfigurationError(f"duplicate prompt key {spec.key!r}")
        if spec.group not in GROUPS:
            raise ConfigurationError(f"prompt {spec.key!r} has unknown group {spec.group!r}")
        if spec.syntax not in SYNTAXES:
            raise ConfigurationError(f"prompt {spec.key!r} has unknown syntax {spec.syntax!r}")
        if not spec.languages or set(spec.languages) - set(SUPPORTED_LANGS):
            raise ConfigurationError(f"prompt {spec.key!r} has unsupported languages")
        if set(spec.defaults) != set(spec.languages):
            raise ConfigurationError(f"prompt {spec.key!r} defaults do not match its languages")
        registry[spec.key] = spec
    return registry


REGISTRY: dict[str, PromptSpec] = _build_registry()
PROMPT_KEYS: tuple[str, ...] = tuple(REGISTRY)


def get_spec(key: str) -> PromptSpec:
    """Return the registration for ``key``."""
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise PromptNotFoundError(f"unknown prompt key {key!r}") from exc


def iter_specs() -> tuple[PromptSpec, ...]:
    """All registrations, in display order."""
    return tuple(REGISTRY.values())


def supported_langs(key: str) -> tuple[str, ...]:
    return get_spec(key).languages


def default_body(key: str, lang: str) -> str:
    """The built-in body for ``(key, lang)``."""
    return get_spec(key).default_for(lang)


_DOLLAR_RE = re.compile(r"\$\{([^{}]*)\}")


def _dollar_errors(spec: PromptSpec, body: str) -> list[PromptIssue]:
    known = {p.name for p in spec.placeholders}
    issues: list[PromptIssue] = []
    unknown = sorted({name for name in _DOLLAR_RE.findall(body) if name not in known})
    for name in unknown:
        issues.append(
            PromptIssue(
                code="unknown_placeholder",
                message=f"占位符 ${{{name}}} 不属于这条提示词，请删掉它或改用已支持的占位符。",
            )
        )
    return issues


def _format_errors(spec: PromptSpec, body: str) -> tuple[list[PromptIssue], set[str]]:
    """Check ``format`` syntax and report the field names the body uses."""
    known = {p.name for p in spec.placeholders}
    issues: list[PromptIssue] = []
    try:
        parsed = list(string.Formatter().parse(body))
    except ValueError as exc:
        return [
            PromptIssue(
                code="bad_braces",
                message=(
                    f"花括号没有配对（{exc}）。这条提示词用 Python 格式语法，"
                    "正文里要原样保留的花括号必须写成双花括号。"
                ),
            )
        ], set()

    fields: set[str] = set()
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not _FIELD_NAME.fullmatch(field_name):
            # ``{x.__class__}``, ``{x[0]}``, ``{0}``: the field is evaluated by
            # ``str.format`` before any check, so anything but a plain name has
            # to be rejected here.
            issues.append(
                PromptIssue(
                    code="unsupported_field",
                    message=(
                        f"占位符 {{{field_name}}} 的字段名不合法：只能写占位符名字本身，"
                        "由字母或下划线开头、只含字母数字下划线，例如 {conversation}；"
                        "不支持属性访问、下标、位置参数。"
                    ),
                )
            )
            continue
        root = field_name
        if conversion:
            issues.append(
                PromptIssue(
                    code="unsupported_conversion",
                    message=(
                        f"占位符 {{{field_name}!{conversion}}} 里的转换写法不支持。"
                        f"请只写 {{{field_name}}}。"
                    ),
                )
            )
            continue
        if format_spec:
            issues.append(
                PromptIssue(
                    code="unsupported_format_spec",
                    message=(
                        f"占位符 {{{field_name}:{format_spec}}} 里的格式说明不支持。"
                        f"请只写 {{{field_name}}}。"
                    ),
                )
            )
            continue
        fields.add(root)
        if root not in known:
            issues.append(
                PromptIssue(
                    code="unknown_field",
                    message=f"占位符 {{{field_name}}} 不属于这条提示词，请删掉它或改用已支持的占位符。",
                )
            )

    if issues:
        # A field that already failed the name check would raise inside
        # ``str.format``; there is nothing left to learn from rendering it.
        return issues, fields

    values = {p.name: "示例" for p in spec.placeholders}
    try:
        body.format(**values)
    except Exception:
        # The exception text can name internals; the user gets a fixed message.
        issues.append(
            PromptIssue(
                code="render_failed",
                message="用占位符试渲染失败。请检查花括号：要原样保留的花括号请写成双花括号。",
            )
        )
    return issues, fields


def _brace_errors(spec: PromptSpec, body: str) -> list[PromptIssue]:
    issues: list[PromptIssue] = []
    for placeholder in spec.placeholders:
        if placeholder.required and placeholder.token not in body:
            issues.append(
                PromptIssue(
                    code="missing_placeholder",
                    message=(
                        f"缺少必填占位符 {placeholder.token}。"
                        "请把它放回正文，否则模型看不到对应的内容。"
                    ),
                )
            )
    return issues


def _output_warnings(spec: PromptSpec, body: str) -> list[PromptIssue]:
    contract = spec.output_contract
    keywords = [k for k in (*contract.required_keys, *contract.item_keys) if k]
    if not keywords:
        return []
    lowered = body.lower()
    present = [k for k in keywords if k.lower() in lowered]
    if not present:
        return [
            PromptIssue(
                code="missing_output_key",
                message=(
                    f"正文里没有出现输出结构要求的关键字（{', '.join(keywords)}）。"
                    "模型可能返回不了可解析的结构，试跑可以验证。"
                ),
            )
        ]
    if "{" not in body and "json" not in lowered:
        return [
            PromptIssue(
                code="output_format_incomplete",
                message=(
                    "正文提到了输出字段，但没有给出 JSON 示例或格式说明。建议保留一段输出结构说明。"
                ),
            )
        ]
    return []


def validate_body(key: str, lang: str, body: str) -> tuple[list[PromptIssue], list[PromptIssue]]:
    """Static checks for one draft; returns ``(errors, warnings)``.

    Errors block saving, warnings do not.
    """
    spec = get_spec(key)
    text = body or ""
    errors: list[PromptIssue] = []
    warnings: list[PromptIssue] = []

    if not text.strip():
        return [PromptIssue(code="empty_prompt", message="正文不能为空。")], []
    if len(text) > MAX_PROMPT_CHARS:
        errors.append(
            PromptIssue(
                code="too_long",
                message=f"正文 {len(text)} 字符，超过上限 {MAX_PROMPT_CHARS}，请精简后再保存。",
            )
        )

    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        errors.append(
            PromptIssue(
                code="unsupported_character",
                message="正文里含有无法保存的字符（孤立的代理项），请重新输入这段文字。",
            )
        )

    if lang not in spec.languages:
        errors.append(
            PromptIssue(
                code="unknown_language",
                message=f"这条提示词没有 {lang} 语种的默认文本，可选的语言是 {', '.join(spec.languages)}。",
            )
        )

    if spec.syntax == "dollar":
        errors.extend(_dollar_errors(spec, text))
        for placeholder in spec.placeholders:
            if placeholder.required and placeholder.token not in text:
                errors.append(
                    PromptIssue(
                        code="missing_placeholder",
                        message=(
                            f"缺少必填占位符 {placeholder.token}。"
                            "请把它放回正文，否则模型看不到对应的内容。"
                        ),
                    )
                )
    elif spec.syntax == "format":
        format_issues, fields = _format_errors(spec, text)
        errors.extend(format_issues)
        for placeholder in spec.placeholders:
            if placeholder.required and placeholder.name not in fields:
                errors.append(
                    PromptIssue(
                        code="missing_placeholder",
                        message=(
                            f"缺少必填占位符 {{{placeholder.name}}}。"
                            "请把它放回正文，否则模型看不到对应的内容。"
                        ),
                    )
                )
    else:
        errors.extend(_brace_errors(spec, text))

    if not errors:
        warnings.extend(_output_warnings(spec, text))
    return errors, warnings
