"""按请求累计 LLM token 用量（``memos/llms/usage.py``）。

为什么按 trace_id 记账而不是直接用 ContextVar 存累计对象：抽取阶段用的是
``ContextThreadPoolExecutor``，它只把自定义的 ``RequestContext`` 塞进工作线程，
不复制 contextvars context。父线程 ``set()`` 的对象工作线程读不到，工作线程里
``set()`` 的父线程也读不到。所以这里钉住三件事：

1. 同一 trace_id 下多次 record 会累加，``snapshot()`` 取走后清空；
2. 没有 ``start()`` 的链路（后台 dream、搜索里的顺手调用）不记账；
3. 并发的抽取线程写同一个桶不丢数。
"""

from memos.context.context import (
    ContextThreadPoolExecutor,
    RequestContext,
    set_request_context,
)
from memos.llms import usage


class _U:
    """够 ``record()`` 用的最小 usage 形状（``openai`` 的 CompletionUsage 同构）。"""

    def __init__(self, prompt: int | None, completion: int | None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


def _use_trace(trace_id: str) -> None:
    set_request_context(RequestContext(trace_id=trace_id))


def test_accumulates_within_one_request():
    _use_trace("t-accumulate")
    usage.start()
    usage.record(_U(10, 5))
    usage.record(_U(1, 2))

    assert usage.snapshot() == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "calls": 2,
    }


def test_snapshot_pops_the_entry():
    _use_trace("t-pop")
    usage.start()
    usage.record(_U(1, 1))

    assert usage.snapshot()["calls"] == 1
    assert usage.snapshot() is None


def test_record_without_start_is_ignored():
    _use_trace("t-no-start")
    usage.record(_U(5, 5))

    assert usage.snapshot() is None


def test_requests_do_not_share_a_bucket():
    _use_trace("t-a")
    usage.start()
    usage.record(_U(1, 0))

    _use_trace("t-b")
    usage.start()
    usage.record(_U(0, 2))

    _use_trace("t-a")
    assert usage.snapshot() == {
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "total_tokens": 1,
        "calls": 1,
    }

    _use_trace("t-b")
    assert usage.snapshot()["completion_tokens"] == 2


def test_usage_without_numbers_is_ignored():
    _use_trace("t-empty-usage")
    usage.start()
    usage.record(None)
    usage.record(_U(None, None))

    assert usage.snapshot() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }


def test_extraction_threads_accumulate_into_the_parent_bucket():
    """抽取跑在 ContextThreadPoolExecutor 里：4 个工并发写，不能丢数。"""
    _use_trace("t-worker")

    def work(_):
        for _ in range(25):
            usage.record(_U(2, 1))

    usage.start()
    with ContextThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(4)))

    assert usage.snapshot() == {
        "prompt_tokens": 200,
        "completion_tokens": 100,
        "total_tokens": 300,
        "calls": 100,
    }
