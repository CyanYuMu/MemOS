"""C1: a timed-out trial must keep its slot until the model call really ends.

The slow stand-in here tests concurrency bookkeeping, not the model: what
matters is that the limiter is not released while a worker thread is still
calling out.
"""

import threading
import time

from types import SimpleNamespace

import pytest

from memos.exceptions import PromptBusyError
from memos.mem_reader import trial as trial_module
from memos.mem_reader.prompt_registry import get_spec


CUBE = "cube-c1"
CONVERSATION = {"messages": [{"role": "user", "content": "我们把部署方式改成了蓝绿发布。"}]}


class SlowReader:
    """Blocks in the model call for ``delay`` seconds."""

    def __init__(self, delay: float, release: threading.Event | None = None) -> None:
        self.delay = delay
        self.release = release
        self.started = threading.Event()
        self.finished = threading.Event()
        self.config = type("C", (), {"model_name_or_path": "slow-model"})()
        self.llm = self
        self.general_llm = self
        self.preference_extractor_llm = self
        self.qwen_llm = None

    def generate(self, *_args, **_kwargs):
        return '{"memory list": [], "summary": "s"}'

    def get_memory(self, *_args, **_kwargs):
        self.started.set()
        try:
            if self.release is not None:
                self.release.wait(self.delay + 5)
            else:
                time.sleep(self.delay)
        finally:
            self.finished.set()
        return []


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "c1.db"))


def _install(monkeypatch, reader):
    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend="simple_struct": reader
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: reader)


def _run(cube: str, lease, timeout_sec: float):
    return trial_module.run_trial_with_timeout(
        get_spec("extract.chat"),
        label="draft",
        lang="zh",
        body=None,
        input_data=CONVERSATION,
        cube_id=cube,
        lease=lease,
        timeout_sec=timeout_sec,
    )


def test_a_timed_out_run_keeps_its_slot_until_the_worker_ends(monkeypatch):
    release = threading.Event()
    reader = SlowReader(delay=30, release=release)
    _install(monkeypatch, reader)
    limiter = trial_module.TrialLimiter(capacity=2)

    lease = limiter.lease(CUBE, runs=1)
    run = _run(CUBE, lease, timeout_sec=0.2)

    assert run.error is not None and run.error["code"] == "llm_timeout"
    assert reader.started.is_set()
    # The worker is still inside the model call, so the slot must stay taken.
    with pytest.raises(PromptBusyError):
        limiter.acquire(CUBE)

    release.set()
    assert reader.finished.wait(5)

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            limiter.acquire(CUBE)
        except PromptBusyError:
            time.sleep(0.02)
        else:
            limiter.release(CUBE)
            break
    else:
        pytest.fail("the slot was never released after the worker finished")


def test_a_normal_run_releases_its_slot(monkeypatch):
    _install(monkeypatch, SlowReader(delay=0))
    limiter = trial_module.TrialLimiter(capacity=2)

    lease = limiter.lease(CUBE, runs=1)
    run = _run(CUBE, lease, timeout_sec=5)
    assert run.ok is True

    limiter.acquire(CUBE)
    limiter.release(CUBE)


def test_a_multi_run_lease_releases_only_after_the_last_worker(monkeypatch):
    reader = SlowReader(delay=0)
    _install(monkeypatch, reader)
    limiter = trial_module.TrialLimiter(capacity=2)

    lease = limiter.lease(CUBE, runs=2)
    _run(CUBE, lease, timeout_sec=5)
    # One of the two runs is still outstanding, so the slot stays taken.
    with pytest.raises(PromptBusyError):
        limiter.acquire(CUBE)
    _run(CUBE, lease, timeout_sec=5)
    limiter.acquire(CUBE)
    limiter.release(CUBE)


def test_the_trial_llm_client_gets_a_request_timeout(monkeypatch):
    """The model client must be able to give up on its own."""

    class FakeClient:
        def __init__(self):
            self.timeouts: list[float] = []

        def with_options(self, **kwargs):
            self.timeouts.append(kwargs.get("timeout"))
            return self

    class FakeLLM:
        def __init__(self):
            self.client = FakeClient()
            self.config = type("C", (), {"model_name_or_path": "m"})()

        def generate(self, *_args, **_kwargs):
            return "{}"

    llm = FakeLLM()
    timed = trial_module.with_request_timeout(llm)
    timed.generate([])
    assert llm.client.timeouts == [trial_module.LLM_REQUEST_TIMEOUT_SEC]
    assert trial_module.LLM_REQUEST_TIMEOUT_SEC <= 90


def test_the_dream_stage_model_gets_a_request_timeout():
    """G5: the Dream branch calls the model directly, so it needs its own timeout."""

    class FakeClient:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def with_options(self, **kwargs):
            self.timeouts.append(kwargs.get("timeout"))
            return self

    class FakeLLM:
        def __init__(self) -> None:
            self.client = FakeClient()
            self.config = SimpleNamespace(model_name_or_path="m")

        def generate(self, *_args, **_kwargs):
            return "{}"

    llm = FakeLLM()
    trial_module.run_trial(
        get_spec("dream.motive"),
        label="draft",
        lang="en",
        body=None,
        input_data={"memories": [{"id": "m1", "memory": "The user keeps postponing."}]},
        cube_id=CUBE,
        llm=llm,
    )
    assert llm.client.timeouts == [trial_module.LLM_REQUEST_TIMEOUT_SEC]


class UsageReader:
    """Records two model calls per run, then waits for the other run."""

    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier
        self.llm = self
        self.general_llm = self
        self.preference_extractor_llm = self
        self.qwen_llm = None
        self.config = SimpleNamespace(model_name_or_path="usage-model")

    def generate(self, *_args, **_kwargs):
        return '{"memory list": [], "summary": "s"}'

    def get_memory(self, *_args, **_kwargs):
        from memos.llms import usage as llm_usage

        for _ in range(2):
            llm_usage.record(SimpleNamespace(prompt_tokens=10, completion_tokens=5))
        self.barrier.wait(10)
        return [[]]


def test_two_concurrent_runs_keep_their_own_usage(monkeypatch):
    """G6: runs must not share one usage bucket just because trace_id is empty."""
    barrier = threading.Barrier(2, timeout=10)
    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend=None: UsageReader(barrier)
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: UsageReader(barrier))

    runs: dict[str, object] = {}

    def worker(label: str) -> None:
        runs[label] = trial_module.run_trial(
            get_spec("extract.chat"),
            label=label,
            lang="zh",
            body=None,
            input_data=CONVERSATION,
            cube_id=CUBE,
            llm=UsageReader(barrier),
        )

    threads = [threading.Thread(target=worker, args=(f"run-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert runs["run-0"].usage["calls"] == 2
    assert runs["run-1"].usage["calls"] == 2
