"""幻觉守卫插件：抽取完成后、写库前的事后幻觉过滤。

上游的 ``filter_hallucination_in_memories`` 只接在 ``SimpleStructMemReader`` 上，
而本部署使用的多模态 reader（默认 backend）没有这段接线，故以插件形式补上：

- ``MEMORY_ITEMS_AFTER_FINE_EXTRACT``：对话抽取路径（逐窗口触发）
- ``MEMORY_ITEMS_AFTER_PREF_EXTRACT``：偏好抽取路径（触发点由本仓库补丁新增）

复用上游过滤函数与 prompt（keep/drop 判定）；fail-open：过滤异常/限流/解析失败
一律原样返回，不阻塞写入。默认影子模式（只记 ``WOULD-DROP`` 日志）。

判定标准是上游 prompt 的 grounding 校验（keep 仅当"记忆是用户原话的直接反映"），
比狭义的真假判定宽：助手侧内容（执行结果、判断）同样会被拦。

还原消息时把 ``chat_time`` 拼进 content：过滤 prompt 的时间戳豁免需要会话时间
作依据，而上游过滤函数只渲染 role/content，不带上时间会把带时间的合规记忆
误判为无支撑。

环境变量：
- ``MEMOS_HALLUCINATION_GUARD``         总开关（默认 false，关闭时零开销）
- ``MEMOS_HALLUCINATION_GUARD_PREF``    偏好路径开关（默认 true）
- ``MEMOS_HALLUCINATION_GUARD_ENFORCE`` true 才真丢弃；未设 = 只记日志

注意：过滤 prompt 判定较严，``implicit_preference``（行为归纳）有误伤风险。
"""

from __future__ import annotations

import logging
import os

from typing import Any

from memos.plugins.base import MemOSPlugin
from memos.plugins.hook_defs import H


logger = logging.getLogger(__name__)


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _src_value(src: Any, key: str) -> Any:
    if isinstance(src, dict):
        return src.get(key)
    return getattr(src, key, None)


def _sources_to_messages(items: list[Any]) -> list[dict]:
    """从 items 的 metadata.sources 还原 [{"role", "content"}]（去重、保序）。

    ``chat_time`` 拼进 content，供过滤 prompt 的时间戳豁免核对。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for it in items:
        meta = getattr(it, "metadata", None)
        sources = getattr(meta, "sources", None) if meta is not None else None
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        for s in sources:
            role = _src_value(s, "role")
            content = _src_value(s, "content")
            if not role or not content:
                continue
            chat_time = _src_value(s, "chat_time") or ""
            key = (str(role), str(chat_time), str(content))
            if key in seen:
                continue
            seen.add(key)
            text = f"[{chat_time}] {content}" if chat_time else str(content)
            out.append({"role": str(role), "content": text})
    return out


def _guard_items(*, items, mem_reader, scope: str) -> list[Any]:
    """共用过滤逻辑；scope 只用于日志标记（chat / pref）。"""
    if not items or mem_reader is None:
        return items
    enforce = _flag("MEMOS_HALLUCINATION_GUARD_ENFORCE")

    messages = _sources_to_messages(list(items))
    if len(messages) < 2:  # 与上游一致：单条消息无法做 grounding 校验
        return items

    try:
        revised = mem_reader.filter_hallucination_in_memories(
            messages=messages, memory_list=list(items)
        )
    except Exception:
        logger.exception("[hallucination_guard][%s] filter failed; keep items unchanged", scope)
        return items

    kept_ids = {id(x) for x in revised}
    dropped = [x for x in items if id(x) not in kept_ids]
    if dropped:
        for d in dropped:
            logger.warning(
                "[hallucination_guard][%s] %s | %s",
                scope,
                "DROPPED" if enforce else "WOULD-DROP",
                (getattr(d, "memory", "") or "").replace("\n", " ")[:200],
            )
    else:
        logger.info("[hallucination_guard][%s] all %d item(s) kept", scope, len(items))
    return revised if enforce else items


def on_memory_items_after_fine_extract(
    *, items, user_context=None, mem_reader=None, extract_mode=None, **kw
):
    """对话抽取路径回调（pipe_key="items"）。"""
    if not _flag("MEMOS_HALLUCINATION_GUARD"):
        return items
    return _guard_items(items=items, mem_reader=mem_reader, scope="chat")


def on_memory_items_after_pref_extract(
    *, items, user_context=None, mem_reader=None, extract_mode=None, **kw
):
    """偏好抽取路径回调（pipe_key="items"）。"""
    if not _flag("MEMOS_HALLUCINATION_GUARD"):
        return items
    if not _flag("MEMOS_HALLUCINATION_GUARD_PREF", default="true"):
        return items
    return _guard_items(items=items, mem_reader=mem_reader, scope="pref")


class HallucinationGuardPlugin(MemOSPlugin):
    """插件入口：注册两条路径的过滤回调。"""

    name = "hallucination_guard"
    version = "0.2.1"
    description = "抽取后幻觉过滤（对话 + 偏好，影子/强制两模式）"
    priority = 5
    enabled_by_default = False

    def on_load(self) -> None:
        self.register_hook(H.MEMORY_ITEMS_AFTER_FINE_EXTRACT, on_memory_items_after_fine_extract)
        self.register_hook(H.MEMORY_ITEMS_AFTER_PREF_EXTRACT, on_memory_items_after_pref_extract)
        logger.info(
            "[hallucination_guard] plugin loaded (enabled=%s, pref=%s, enforce=%s)",
            _flag("MEMOS_HALLUCINATION_GUARD"),
            _flag("MEMOS_HALLUCINATION_GUARD_PREF", default="true"),
            _flag("MEMOS_HALLUCINATION_GUARD_ENFORCE"),
        )
