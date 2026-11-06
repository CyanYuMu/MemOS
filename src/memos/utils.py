import functools
import json
import time
import traceback

from contextlib import ContextDecorator
from datetime import datetime, timezone
from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)

# Naive timestamps in this code base are UTC by definition. The deployed image
# used to write Beijing wall-clock values with a "+00:00" suffix (see
# docs/cloud-能力补齐-难度分级.md T1.3); all new writes go through these helpers
# so that the offset in the string is always the truth.
_NAIVE_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)
_CHAT_TIME_FORMATS = (
    "%I:%M %p on %d %B, %Y",
    "%I:%M%p on %d %B, %Y",
)


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Current UTC time in ISO 8601 with an explicit ``+00:00`` offset."""
    return utc_now().isoformat()


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort parse of an ISO-8601 / common timestamp into a datetime.

    Accepts ``str``, ``int``/``float`` epoch values (seconds or milliseconds),
    and ``datetime`` instances. Naive values are interpreted as UTC, matching
    the code base's baseline. Returns ``None`` when the value cannot be
    parsed; callers that must keep free-form text should fall back to the raw
    value instead of raising.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
        if abs(seconds) >= 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        for fmt in (*_NAIVE_DATETIME_FORMATS, *_CHAT_TIME_FORMATS):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    # Naive timestamps in this code base are UTC by definition. Returning an
    # aware value keeps sorting/comparison safe when old rows (naive) and new
    # rows (aware, see T1.3) coexist.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_to_utc_iso(value: Any) -> str | None:
    """Normalize a timestamp to ISO 8601 UTC (``+00:00``), or ``None``.

    Naive datetimes are treated as UTC, which is the code base's baseline.
    """
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()



class timed_stage(ContextDecorator):  # noqa: N801
    """Unified timing helper for business-stage instrumentation.

    Works as **both** a context-manager and a decorator - one tool for all
    timing needs.

    Context-manager (when the stage is a *code block* inside a function)::

        with timed_stage("add", "parse", cube_id=cube_id) as ts:
            items = self._parse(...)
            ts.set(msg_count=10, window_count=len(windows))

    Decorator (when the stage is *an entire function*)::

        @timed_stage("add", "write_db")
        def _write_to_db(self, ...):
            ...

    Decorator with dynamic fields extracted from arguments::

        @timed_stage("search", "recall",
                     extra=lambda self, req, **kw: {"cube_id": self.cube_id})
        def _vector_recall(self, req, ...):
            ...

    Output format (SLS-friendly, one-line structured log)::

        [STAGE] biz=add stage=parse cube_id=xxx duration_ms=150 msg_count=10
    """

    def __init__(
        self,
        biz: str = "",
        stage: str = "",
        *,
        extra: dict[str, Any] | None = None,
        level: str = "info",
        **fields: Any,
    ):
        self._biz = biz
        self._stage = stage
        self._extra_factory = extra if callable(extra) else None
        self._static_extra = extra if isinstance(extra, dict) else None
        self._level = level
        self._fields: dict[str, Any] = dict(fields)
        self._start: float = 0.0
        self.duration_ms: int = 0

    # -- context-manager protocol ------------------------------------------

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.duration_ms = int((time.perf_counter() - self._start) * 1000)
        self._emit(self.duration_ms, exc_type)
        return False

    # -- decorator protocol (extends ContextDecorator) ---------------------

    def __call__(self, func=None):
        if func is None:
            return super().__call__(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if self._extra_factory is not None:
                try:
                    dynamic = self._extra_factory(*args, **kwargs)
                    if dynamic:
                        self._fields.update(dynamic)
                except Exception as e:
                    logger.warning("[STAGE] extra callback error: %r", e)

            stage_name = self._stage or func.__name__
            self._stage = stage_name
            self._start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                self.duration_ms = int((time.perf_counter() - self._start) * 1000)
                self._emit(self.duration_ms)

        return wrapper

    # -- public API --------------------------------------------------------

    def set(self, **fields: Any):
        """Add / overwrite fields after execution (e.g. counts only known after the block runs)."""
        self._fields.update(fields)

    @staticmethod
    def emit_now(biz: str, stage: str, **fields: Any):
        """Fire a one-shot structured log without timing (e.g. summary rollups)."""
        parts = [f"biz={biz}", f"stage={stage}"]
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        logger.info("[STAGE] " + " ".join(parts))

    # -- internals ---------------------------------------------------------

    def _emit(self, duration_ms: int, exc_type=None):
        parts: list[str] = []
        if self._biz:
            parts.append(f"biz={self._biz}")
        if self._stage:
            parts.append(f"stage={self._stage}")
        parts.append(f"duration_ms={duration_ms}")

        if self._static_extra:
            self._fields.update(self._static_extra)

        for k, v in self._fields.items():
            parts.append(f"{k}={v}")

        if exc_type is not None:
            parts.append(f"error={exc_type.__name__}")

        msg = "[STAGE] " + " ".join(parts)
        getattr(logger, self._level, logger.info)(msg)


def timed_with_status(
    func=None,
    *,
    log_prefix="",
    log_args=None,
    log_extra_args=None,
    fallback=None,
):
    """
    Parameters:
    - log: enable timing logs (default True)
    - log_prefix: prefix; falls back to function name
    - log_args: names to include in logs (str or list/tuple of str), values are taken from kwargs by name.
    - log_extra_args:
        - can be a dict: fixed contextual fields that are always attached to logs;
        - or a callable: like `fn(*args, **kwargs) -> dict`, used to dynamically generate contextual fields at runtime.
    """

    if isinstance(log_args, str):
        effective_log_args = [log_args]
    else:
        effective_log_args = list(log_args) if log_args else []

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            exc_type = None
            exc_message = None
            result = None
            success_flag = False

            try:
                result = fn(*args, **kwargs)
                success_flag = True
                return result
            except Exception as e:
                exc_type = type(e)
                stack_info = "".join(traceback.format_stack()[:-1])
                exc_message = f"{stack_info}{traceback.format_exc()}"
                success_flag = False

                if fallback is not None and callable(fallback):
                    result = fallback(e, *args, **kwargs)
                    return result
                # No fallback: re-raise so callers see the real error instead
                # of a silent None return. The `finally` block still runs and
                # emits the [TIMER_WITH_STATUS] FAILED log line before the
                # exception propagates.
                raise
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0

                ctx_parts = []
                # 1) Collect parameters from kwargs by name
                for key in effective_log_args:
                    val = kwargs.get(key)
                    ctx_parts.append(f"{key}={val}")

                # 2) Support log_extra_args as dict or callable, so we can dynamically
                #    extract values from self or other runtime context
                extra_items = {}
                try:
                    if callable(log_extra_args):
                        extra_items = log_extra_args(*args, **kwargs) or {}
                    elif isinstance(log_extra_args, dict):
                        extra_items = log_extra_args
                except Exception as e:
                    logger.warning(f"[TIMER_WITH_STATUS] log_extra_args callback error: {e!r}")

                if extra_items:
                    ctx_parts.extend(f"{key}={val}" for key, val in extra_items.items())

                ctx_str = f" [{', '.join(ctx_parts)}]" if ctx_parts else ""

                status = "SUCCESS" if success_flag else "FAILED"
                status_info = f", status: {status}"
                if not success_flag and exc_type is not None:
                    status_info += (
                        f", error_type: {exc_type.__name__}, error_message: {exc_message}"
                    )

                msg = (
                    f"[TIMER_WITH_STATUS] {log_prefix or fn.__name__} "
                    f"took {elapsed_ms:.0f} ms{status_info}, args: {ctx_str}"
                )

                logger.info(msg)

        return wrapper

    if func is None:
        return decorator
    return decorator(func)


def timed(func=None, *, log=True, log_prefix=""):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = fn(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            if log is not True:
                return result

            # 100ms threshold
            if elapsed_ms >= 100.0:
                logger.info(f"[TIMER] {log_prefix or fn.__name__} took {elapsed_ms:.0f} ms")

            return result

        return wrapper

    # Handle both @timed and @timed(log=True) cases
    if func is None:
        return decorator
    return decorator(func)


# --- JSON 字符串字段归一化 -------------------------------------------------
#
# 图库无法直接存 dict/list-of-dict，写入前要序列化；读回来再反序列化。
# 这两件事必须成对且幂等，否则每次重写都会给同一个值再套一层转义，
# 体积随之翻倍（历史上 sources 就被套到了 18 层）。

_MAX_JSON_DECODE_DEPTH = 64


def serialize_sources(values: Any) -> list[str] | None:
    """把 sources 元素序列化成 JSON 字符串，已经序列化过的原样保留。"""
    if not values:
        return None
    if not isinstance(values, list):
        values = [values]
    out: list[str] = []
    for item in values:
        if isinstance(item, str):
            out.append(item)
        elif hasattr(item, "model_dump"):
            out.append(json.dumps(item.model_dump(exclude_none=True), ensure_ascii=False))
        else:
            out.append(json.dumps(item, ensure_ascii=False))
    return out


def decode_json_string(value: Any) -> Any:
    """逐层解开被重复 json.dumps 的字符串，直到得到原始类型。

    历史数据形如 ``{"type": "doc", "content": "<又一层 JSON 字符串>"}``，
    每层只多包一个信封。这里的策略是：字符串能解析就继续；解析成
    ``type=doc`` 且只有 type/content 两个键的信封时，沿 content 继续。
    普通文本、以及真正的 chat 消息（带 role 等键）原样返回。
    设置层数上限，避免脏数据造成死循环。
    """
    depth = 0
    while depth < _MAX_JSON_DECODE_DEPTH:
        if isinstance(value, str):
            stripped = value.lstrip()
            if not stripped or stripped[0] not in '{"[':
                break
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                break
            if isinstance(parsed, str) and parsed == value:
                break
            value = parsed
            depth += 1
            continue

        if isinstance(value, dict):
            inner = value.get("content")
            is_envelope = set(value) <= {"type", "content"} and value.get("type") in (None, "doc")
            if is_envelope and isinstance(inner, dict):
                value = inner
                depth += 1
                continue
            if (
                is_envelope
                and isinstance(inner, str)
                and inner.lstrip()[:1] in {'"', "{"}
            ):
                try:
                    json.loads(inner)
                except (TypeError, ValueError):
                    break
                value = inner
                depth += 1
                continue

        break
    return value


def decode_json_list(values: Any) -> list[Any] | None:
    """对列表里的每个元素逐层解包（用于 sources 这类历史遗留字段）。"""
    if not values:
        return None
    if not isinstance(values, list):
        values = [values]
    return [decode_json_string(item) for item in values]
