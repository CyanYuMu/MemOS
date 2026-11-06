"""Trial run for one prompt: real code path, draft template, no writes.

Every run drives the same reader (or Dream stage) the production paths use, with
the template swapped through ``draft_prompt_override``. Nothing is written:
no memory, graph, vector, Redis, or prompt version. The only side effect is one
model call.

``run_trial`` returns a plain dict shaped like the ``/product/prompts/test``
response so the API layer stays thin.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from memos.context.context import (
    RequestContext,
    generate_trace_id,
    get_current_context,
    set_request_context,
)
from memos.exceptions import PromptBusyError, PromptInputError, PromptInputTooLargeError
from memos.llms import usage as llm_usage
from memos.log import get_logger
from memos.mem_reader.dry_run import (
    DRY_RUN_BACKENDS,
    build_dry_run_llm,
    build_dry_run_reader,
)
from memos.mem_reader.prompt_runtime import (
    draft_prompt_override,
    prompt_scope,
    record_prompt_usage,
)
from memos.mem_reader.utils import parse_json_result


if TYPE_CHECKING:
    from collections.abc import Iterator

    from memos.mem_reader.prompt_registry import PromptSpec


logger = get_logger(__name__)

MAX_MESSAGES = 30
MAX_INPUT_CHARS = 12000
MAX_RAW_OUTPUT_CHARS = 20000
LLM_TIMEOUT_SEC = 90
# The client gives up before our own join deadline so the worker can end by
# itself and release its concurrency slot.
LLM_REQUEST_TIMEOUT_SEC = 80


@dataclass
class TrialRun:
    """One labelled run inside a trial request."""

    label: str
    ok: bool
    parse_ok: bool = False
    fallback_triggered: bool = False
    contract_ok: bool = False
    missing: list[str] = field(default_factory=list)
    output: Any = None
    raw_output: str = ""
    truncated: bool = False
    latency_ms: int = 0
    model: str = ""
    usage: dict[str, int] | None = None
    used_slots: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    error: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "label": self.label,
            "ok": self.ok,
            "parse_ok": self.parse_ok,
            "fallback_triggered": self.fallback_triggered,
            "contract": {"ok": self.contract_ok, "missing": list(self.missing)},
            "output": self.output,
            "raw_output": self.raw_output,
            "truncated": self.truncated,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "usage": self.usage or {},
            "used_slots": list(self.used_slots),
            "warnings": list(self.warnings),
        }
        if self.error is not None:
            data["error"] = dict(self.error)
        return data


def validate_input(spec: PromptSpec, input_data: Any) -> dict[str, Any]:
    """Check a trial input against the prompt's ``input_kind``.

    Raises ``PromptInputError`` for a wrong shape and ``PromptInputTooLarge``
    when a documented limit is exceeded.
    """
    if not isinstance(input_data, dict):
        raise PromptInputError("input 必须是一个 JSON 对象。")
    kind = spec.input_kind
    if kind == "conversation":
        messages = input_data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise PromptInputError("conversation 输入需要 messages 数组，至少一条消息。")
        if len(messages) > MAX_MESSAGES:
            raise PromptInputTooLargeError(
                f"对话最多 {MAX_MESSAGES} 条消息，当前 {len(messages)} 条。"
            )
        total = 0
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or "role" not in message or "content" not in message:
                raise PromptInputError(f"第 {index + 1} 条消息缺少 role 或 content。")
            total += len(str(message.get("content") or ""))
        if total > MAX_INPUT_CHARS:
            raise PromptInputTooLargeError(
                f"对话总长度 {total} 字符，超过上限 {MAX_INPUT_CHARS}，请删减后重试。"
            )
        return {"messages": messages}
    if kind == "text":
        text = input_data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PromptInputError("text 输入需要非空的 text 字段。")
        if len(text) > MAX_INPUT_CHARS:
            raise PromptInputTooLargeError(f"文本 {len(text)} 字符，超过上限 {MAX_INPUT_CHARS}。")
        return {"text": text}
    if kind == "qa_pair":
        qa_pair = input_data.get("qa_pair")
        if not isinstance(qa_pair, str) or not qa_pair.strip():
            raise PromptInputError("qa_pair 输入需要非空的 qa_pair 字段。")
        if len(qa_pair) > MAX_INPUT_CHARS:
            raise PromptInputTooLargeError(
                f"问答 {len(qa_pair)} 字符，超过上限 {MAX_INPUT_CHARS}。"
            )
        return {"qa_pair": qa_pair}
    if kind == "memories":
        memories = input_data.get("memories")
        if not isinstance(memories, list) or not memories:
            raise PromptInputError("memories 输入需要 memories 数组，至少一条记忆。")
        total = 0
        for index, memory in enumerate(memories):
            if not isinstance(memory, dict) or not memory.get("id") or "memory" not in memory:
                raise PromptInputError(f"第 {index + 1} 条记忆缺少 id 或 memory 字段。")
            total += len(str(memory.get("memory") or ""))
        if total > MAX_INPUT_CHARS:
            raise PromptInputTooLargeError(f"记忆总长度 {total} 字符，超过上限 {MAX_INPUT_CHARS}。")
        return {"memories": memories}
    raise PromptInputError(f"暂不支持 {kind} 类型的试跑输入。")


def _model_name(llm: Any) -> str:
    config = getattr(llm, "config", None)
    return str(getattr(config, "model_name_or_path", "") or "")


def _contract(spec: PromptSpec, output: Any) -> tuple[bool, list[str]]:
    contract = spec.output_contract
    if not isinstance(output, dict):
        if not contract.required_keys and isinstance(output, list):
            return True, []
        return False, list(contract.required_keys)
    missing = [key for key in contract.required_keys if key not in output]
    if not missing and contract.item_keys and contract.required_keys:
        items = output.get(contract.required_keys[0])
        if isinstance(items, list) and items and isinstance(items[0], dict):
            first = items[0]
            missing.extend(key for key in contract.item_keys if key not in first)
    return not missing, missing


def _contract_subject(spec: PromptSpec, run: TrialRun) -> Any:
    """What the output contract is checked against.

    The contract describes the JSON the model returns. Extraction runs expose
    exactly that JSON as ``run.output``. A Dream stage post-processes the reply
    (reasoning becomes a ``DreamAction`` with ``new_content``, context binding
    becomes a list of groups), so its ``run.output`` never carries the contract
    keys; check the model's own reply when it parses, and fall back to the
    stage output when it does not (a fallback run should still fail).
    """
    if spec.key.startswith("dream.") and run.raw_output:
        try:
            parsed = parse_json_result(run.raw_output)
        except Exception:
            parsed = None
        if isinstance(parsed, dict | list) and parsed:
            return parsed
    return run.output


class _TimeoutProxy:
    """Give ``generate`` a client whose requests time out on their own.

    ``openai`` clients expose ``with_options(timeout=...)``; the proxy copies
    the LLM object shallowly for the call so the shared instance keeps its
    original client and nothing leaks between requests.
    """

    def __init__(self, llm: Any, timeout: int) -> None:
        self._llm = llm
        self._timeout = timeout

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        client = getattr(self._llm, "client", None)
        if client is None or not hasattr(client, "with_options"):
            return self._llm.generate(*args, **kwargs)
        proxy = copy.copy(self._llm)
        proxy.client = client.with_options(timeout=self._timeout)
        backup = getattr(self._llm, "backup_client", None)
        if backup is not None and hasattr(backup, "with_options"):
            proxy.backup_client = backup.with_options(timeout=self._timeout)
        return proxy.generate(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(object.__getattribute__(self, "_llm"), item)


def with_request_timeout(llm: Any, timeout: int = LLM_REQUEST_TIMEOUT_SEC) -> Any:
    """Wrap ``llm`` so its requests abort before the trial gives up."""
    return _TimeoutProxy(llm, timeout)


class _RecordingLLM:
    """Forward calls to a real model and keep ``(role, reply)`` pairs.

    One extraction run makes several model calls (the extractor plus auxiliary
    ones such as preference extraction). Recording the role lets the
    trial report the reply of the call it actually asked about instead of
    whichever call happened last.
    """

    def __init__(self, llm: Any, sink: list[tuple[str, str]], role: str) -> None:
        object.__setattr__(self, "_llm", llm)
        object.__setattr__(self, "_sink", sink)
        object.__setattr__(self, "_role", role)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        reply = self._llm.generate(*args, **kwargs)
        role = object.__getattribute__(self, "_role")
        sink = object.__getattribute__(self, "_sink")
        sink.append((role, reply if isinstance(reply, str) else str(reply)))
        return reply

    def __getattr__(self, item: str) -> Any:
        return getattr(object.__getattribute__(self, "_llm"), item)


def _record_reader_llms(reader: Any) -> list[tuple[str, str]]:
    """Wrap the reader's models so the trial can report the raw reply."""
    sink: list[tuple[str, str]] = []
    for attribute in ("llm", "general_llm", "preference_extractor_llm", "qwen_llm"):
        current = getattr(reader, attribute, None)
        if current is not None and not isinstance(current, _RecordingLLM):
            setattr(
                reader, attribute, _RecordingLLM(with_request_timeout(current), sink, attribute)
            )
    return sink


def _run_extraction(
    spec: PromptSpec,
    *,
    input_data: dict[str, Any],
    cube_id: str,
) -> tuple[Any, str, bool]:
    """Run the extraction code path for ``spec``; returns ``(output, raw, fallback)``."""
    backend = os.getenv("MEM_READER_BACKEND", "multimodal_struct")
    if backend not in DRY_RUN_BACKENDS:
        backend = "simple_struct"
    if spec.key in {"extract.doc", "extract.general_string"}:
        # Only the multimodal reader has a general_string branch; doc shares
        # its template substitution.
        backend = "multimodal_struct"
    reader = build_dry_run_reader(backend)
    sink = _record_reader_llms(reader)
    key = spec.key

    if key == "extract.chat":
        messages = input_data["messages"]
        groups = reader.get_memory(
            [messages],
            type="chat",
            info={"user_id": "trial", "session_id": "trial"},
            mode="fine",
            user_name=cube_id,
        )
        output = {
            "memory list": [item.model_dump(mode="json") for group in groups for item in group]
        }
        return output, _last_reply(sink), False
    if key in {"extract.doc", "extract.general_string"}:
        prompt_type = "doc" if key == "extract.doc" else "general_string"
        return _run_direct(reader, input_data["text"], prompt_type, sink)
    if key == "extract.tool_trajectory":
        result = reader._get_llm_tool_trajectory_response(_messages_text(input_data["messages"]))
        return result, json.dumps(result, ensure_ascii=False), False
    if key == "extract.event":
        from memos.mem_reader.event_extractor import EventExtractor

        extractor = EventExtractor(reader.llm)
        events = extractor.extract(input_data["messages"], need_embed=False)
        output = [
            event.model_dump(mode="json") if hasattr(event, "model_dump") else event
            for event in events
        ]
        return output, _last_reply(sink), False
    if key in {"extract.preference_explicit", "extract.preference_implicit"}:
        from memos.mem_reader.read_pref_memory.process_preference_memory import (
            _extract_explicit_preference,
            _extract_implicit_preference,
        )

        extract = (
            _extract_explicit_preference
            if key == "extract.preference_explicit"
            else _extract_implicit_preference
        )
        result = extract(input_data["qa_pair"], reader.general_llm)
        return result, _last_reply(sink), False
    if key == "process.hallucination_filter":
        from types import SimpleNamespace

        memories = [SimpleNamespace(memory=str(item["memory"])) for item in input_data["memories"]]
        kept = reader.filter_hallucination_in_memories(
            [{"role": "user", "content": item["memory"]} for item in input_data["memories"]],
            memories,
        )
        output = {"kept": [memory.memory for memory in kept]}
        return output, _last_reply(sink), False
    raise PromptInputError(f"{key} 没有可用的试跑路径。")


def _last_reply(sink: list[tuple[str, str]], role: str = "llm") -> str:
    """The last reply of ``role``; the last reply of any role as a fallback."""
    for entry_role, reply in reversed(sink):
        if entry_role == role:
            return reply
    return sink[-1][1] if sink else ""


def _run_direct(
    reader: Any, text: str, prompt_type: str, sink: list[tuple[str, str]]
) -> tuple[Any, str, bool]:
    result = reader._get_llm_response(text, None, None, prompt_type)
    return result, _last_reply(sink), False


def _messages_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(f"{message['role']}: {message['content']}" for message in messages)


def _execute_run(
    spec: PromptSpec,
    *,
    input_data: dict[str, Any],
    cube_id: str,
    llm: Any,
    run: TrialRun,
) -> None:
    """Body of one trial run, with the prompt scope already opened."""
    if spec.key.startswith("dream."):
        from memos.dream.dry_run import run_dream_stage

        dream = run_dream_stage(spec.key, input_data["memories"], llm=with_request_timeout(llm))
        run.output = dream.output
        run.raw_output = dream.raw_output
        run.parse_ok = dream.parse_ok
        run.fallback_triggered = dream.fallback_triggered
        return

    output, raw, fallback = _run_extraction(spec, input_data=input_data, cube_id=cube_id)
    run.output = output
    run.raw_output = raw
    run.fallback_triggered = fallback
    try:
        parsed = parse_json_result(raw)
    except Exception:
        parsed = None
    # ``parse_json_result`` answers "{}" when the reply is not JSON; that is
    # exactly the case where the reader stores the raw text as one whole
    # UserMemory.
    if isinstance(parsed, dict | list) and parsed:
        run.parse_ok = True
        # "fields pass through": the model's JSON is the run's output, exactly
        # as the console should show it.
        run.output = parsed
    else:
        run.parse_ok = False
        run.fallback_triggered = True


_LANG_NAMES = {"zh": "中文", "en": "英文"}


def _lang_name(lang: str | None) -> str:
    return _LANG_NAMES.get(lang or "", lang or "未知")


def _draft_not_used_warning(draft_lang: str, used_lang: str | None) -> dict[str, str]:
    draft = _lang_name(draft_lang)
    if used_lang:
        used = _lang_name(used_lang)
        return {
            "code": "draft_not_used",
            "message": (
                f"这段输入按{used}处理，你编辑的{draft}草稿没有参与这次运行。"
                f"要试{draft}模板，请换一段{draft}输入，或把草稿保存到{used}。"
            ),
        }
    return {
        "code": "draft_not_used",
        "message": (
            f"你编辑的{draft}草稿没有参与这次运行：这次输入没有取用任何模板。请检查输入内容后重试。"
        ),
    }


def run_trial(
    spec: PromptSpec,
    *,
    label: str,
    lang: str,
    body: str | None,
    input_data: dict[str, Any],
    cube_id: str,
    llm: Any,
    ignore_project: bool = False,
    draft_provided: bool = False,
) -> TrialRun:
    """Run one extraction or Dream stage.

    ``body`` is the template to try: a draft, the effective project body, or
    ``None`` to let the scope decide. ``ignore_project`` runs against the
    built-in defaults only, which is what the ``default`` side of
    ``compare_default`` must do.
    """
    started = time.perf_counter()
    # A run has no HTTP request behind it, so its trace id would be empty and
    # every concurrent trial (and background Dream) would share one usage
    # bucket. Give each run its own, and put the caller's context back after.
    previous_context = get_current_context()
    set_request_context(RequestContext(trace_id=f"prompt-trial-{generate_trace_id()}"))
    run = TrialRun(label=label, ok=True, model=_model_name(llm))
    scope_cube = "" if ignore_project else cube_id
    try:
        llm_usage.start()
        try:
            with prompt_scope(scope_cube), record_prompt_usage() as used:
                if body is None:
                    _execute_run(spec, input_data=input_data, cube_id=cube_id, llm=llm, run=run)
                else:
                    with draft_prompt_override(spec.key, lang, body):
                        _execute_run(spec, input_data=input_data, cube_id=cube_id, llm=llm, run=run)
            run.used_slots = [usage.as_dict() for usage in used]
            if draft_provided and not any(
                usage.key == spec.key and usage.source == "draft" for usage in used
            ):
                used_lang = next((usage.lang for usage in used if usage.key == spec.key), None)
                run.warnings.append(_draft_not_used_warning(lang, used_lang))
        except Exception as exc:
            logger.warning("[PromptTrial] run %s failed: %s", label, exc, exc_info=True)
            run.ok = False
            run.error = {
                "code": "llm_error",
                "message": "这次试跑没有跑完，模型调用或解析出错。可以稍后重试，或先用内置默认跑一次对比。",
            }
        finally:
            run.usage = llm_usage.snapshot()
    finally:
        set_request_context(previous_context)

    # Check the contract before the raw reply is cut for transport, or a long
    # reply would stop parsing and be reported as breaking the contract.
    run.contract_ok, run.missing = _contract(spec, _contract_subject(spec, run))
    if run.raw_output and len(run.raw_output) > MAX_RAW_OUTPUT_CHARS:
        run.raw_output = run.raw_output[:MAX_RAW_OUTPUT_CHARS]
        run.truncated = True
    run.latency_ms = int((time.perf_counter() - started) * 1000)
    return run


class TrialLimiter:
    """Concurrency guard for trial runs: a global cap plus one per project."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self._semaphore = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def acquire(self, cube_id: str) -> None:
        """Take the global slot and the project slot, or raise ``PromptBusyError``."""
        cube = cube_id or ""
        with self._lock:
            if cube in self._in_flight:
                raise PromptBusyError("同一个项目已有一轮试跑在进行，等它结束再试。", 2)
            self._in_flight.add(cube)
        if not self._semaphore.acquire(blocking=False):
            with self._lock:
                self._in_flight.discard(cube)
            raise PromptBusyError("当前试跑请求较多，请稍等几秒再试。", 2)

    def release(self, cube_id: str) -> None:
        cube = cube_id or ""
        with self._lock:
            if cube not in self._in_flight:
                return
            self._in_flight.discard(cube)
        self._semaphore.release()

    @contextmanager
    def slot(self, cube_id: str) -> Iterator[None]:
        self.acquire(cube_id)
        try:
            yield
        finally:
            self.release(cube_id)

    def lease(self, cube_id: str, runs: int) -> TrialLease:
        """One slot shared by ``runs`` worker threads.

        The slot is released when the last worker finishes, not when the HTTP
        request returns: a timed-out run keeps calling the model.
        """
        self.acquire(cube_id)
        return TrialLease(self, cube_id, runs)


class TrialLease:
    """Releases a trial slot only after every worker run has finished."""

    def __init__(self, limiter: TrialLimiter, cube_id: str, runs: int) -> None:
        self._limiter = limiter
        self._cube_id = cube_id
        self._lock = threading.Lock()
        self._pending = max(1, int(runs))

    def done(self) -> None:
        with self._lock:
            if self._pending <= 0:
                return
            self._pending -= 1
            if self._pending > 0:
                return
        self._limiter.release(self._cube_id)

    def abandon(self) -> None:
        """Release the slot for runs that will never start (startup failure)."""
        with self._lock:
            if self._pending <= 0:
                return
            self._pending = 0
        self._limiter.release(self._cube_id)


def default_limiter() -> TrialLimiter:
    raw = os.getenv("PROMPT_TEST_MAX_CONCURRENCY", "4").strip() or "4"
    try:
        capacity = int(raw)
    except ValueError:
        capacity = 4
    return TrialLimiter(capacity)


def run_trial_with_timeout(
    spec: PromptSpec,
    *,
    label: str,
    lang: str,
    body: str | None,
    input_data: dict[str, Any],
    cube_id: str,
    lease: TrialLease | None = None,
    ignore_project: bool = False,
    draft_provided: bool = False,
    timeout_sec: int = LLM_TIMEOUT_SEC,
) -> TrialRun:
    """``run_trial`` bounded by one call's timeout.

    The worker thread is a daemon: on timeout the request returns immediately
    but the concurrency slot stays taken, because the slot is released by the
    worker itself (``lease.done()``) once the model call really ends.
    """
    try:
        llm = build_dry_run_llm()
    except BaseException:
        if lease is not None:
            lease.done()
        raise
    outcome: dict[str, TrialRun] = {}

    def worker() -> None:
        try:
            outcome["run"] = run_trial(
                spec,
                label=label,
                lang=lang,
                body=body,
                input_data=input_data,
                cube_id=cube_id,
                llm=llm,
                ignore_project=ignore_project,
                draft_provided=draft_provided,
            )
        finally:
            if lease is not None:
                lease.done()

    thread = threading.Thread(target=worker, name="prompt-trial", daemon=True)
    try:
        thread.start()
    except BaseException:
        if lease is not None:
            lease.done()
        raise
    thread.join(timeout_sec)
    if thread.is_alive():
        return TrialRun(
            label=label,
            ok=False,
            model=_model_name(llm),
            latency_ms=timeout_sec * 1000,
            error={
                "code": "llm_timeout",
                "message": "模型在 90 秒内没有返回，这次试跑已中断。可以稍后重试，或换一段更短的输入。",
            },
        )
    return outcome["run"]


def call_with_timeout(fn: Any, seconds: int = LLM_TIMEOUT_SEC) -> Any:
    """Run ``fn`` in a daemon thread; raise ``TimeoutError`` past ``seconds``."""
    outcome: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:
            error["exc"] = exc

    thread = threading.Thread(target=worker, name="prompt-llm", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"model did not answer within {seconds}s")
    if "exc" in error:
        raise error["exc"]
    return outcome.get("value")
