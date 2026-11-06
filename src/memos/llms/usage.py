"""按请求累计模型 token 用量。

为什么不用 ContextVar 直接存累计值
==================================

抽取阶段用的是 ``ContextThreadPoolExecutor``（``memos/context/context.py``），它只
把**自定义的 RequestContext**（trace_id / api_path …）传给工作线程，**不会复制
Python 的 contextvars context**——工作线程里的 ContextVar 是空的。所以在这里
``set()`` 一个累计对象，工作线程读不到；在工作线程里 ``set()``，请求线程也读不到。
（实测：4 个工各记 25 次，父线程 snapshot 仍是 0。）

它传播的是 trace_id，那就按 trace_id 记账：

- ``start()``：当前 trace_id → 一个新的累计对象；
- ``record()``：按当前 trace_id 找到那个对象，**原地累加**（带锁，多线程同时写不丢数）；
- ``snapshot()``：取出并**删除**该项，避免长期驻留。

没有调用过 ``start()`` 的链路（后台 dream、搜索里顺手的模型调用）``record()``
直接返回，不会留下永远没人读的条目。
"""

from __future__ import annotations

import threading

from memos.context.context import get_current_trace_id


_registry: dict[str | None, _Usage] = {}
_lock = threading.Lock()


class _Usage:
    __slots__ = ("calls", "completion_tokens", "prompt_tokens")

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def add(self, prompt: int | None, completion: int | None) -> None:
        self.prompt_tokens += int(prompt or 0)
        self.completion_tokens += int(completion or 0)
        self.calls += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "calls": self.calls,
        }


def start() -> None:
    """一次请求开始时调用，重置这个 trace_id 的累计值。"""
    with _lock:
        _registry[get_current_trace_id()] = _Usage()


def record(usage) -> None:
    """记一次 chat.completions 的 usage（``CompletionUsage`` 或同形状的对象）。"""
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt is None and completion is None:
        return
    key = get_current_trace_id()
    with _lock:
        u = _registry.get(key)
        if u is None:
            # 没有 start()：不在需要统计的请求里，丢掉即可
            return
        u.add(prompt, completion)


def snapshot() -> dict[str, int] | None:
    with _lock:
        u = _registry.pop(get_current_trace_id(), None)
        return u.snapshot() if u is not None else None


def reset() -> None:
    with _lock:
        _registry.pop(get_current_trace_id(), None)
