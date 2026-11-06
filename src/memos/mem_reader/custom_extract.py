"""Request-scoped ``custom_extract_prompt`` (cloud ``add/message`` parameter).

The caller names *what to extract* for one stage ("only travel facts", "only
visible text in images"). Only that guidance is overridable: each extraction
prompt keeps its own output protocol, so the memories still come back in the
default structure.

The override is held in a ``ContextVar`` for the duration of one add request
(``use_custom_extract_prompt``) instead of being threaded through every reader
signature. The async path re-establishes it inside the scheduler handler from
the message ``info``.
"""

from __future__ import annotations

import contextlib
import re

from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any


#: Memory kinds (cloud ``allow_memory_view`` vocabulary) plus input modalities.
CUSTOM_EXTRACT_KEYS: tuple[str, ...] = (
    "detail_factual",
    "preference",
    "profile",
    "event",
    "tool_memory",
    "image",
    "document",
)
MAX_CUSTOM_EXTRACT_CHARS = 4000

_CLOSING_TAG = re.compile(r"</\s*custom_extract_prompt\s*>", re.IGNORECASE)
_CJK = re.compile(r"[一-鿿]")

_current: ContextVar[dict[str, str] | None] = ContextVar("custom_extract_prompt", default=None)

_SECTION_ZH = (
    "\n\n# 业务自定义抽取要求（仅本次请求生效）\n"
    "下面的要求只决定「抽取什么、忽略什么」，优先于上文默认的抽取范围；"
    "输出的 JSON 结构、字段名与格式必须保持上文约定，不得改变。\n"
    "<custom_extract_prompt>\n{text}\n</custom_extract_prompt>\n"
)
_SECTION_EN = (
    "\n\n# Custom extraction requirements (this request only)\n"
    "The requirements below only decide *what to extract and what to ignore*; they take "
    "precedence over the default extraction scope above. The output JSON structure, field "
    "names and format defined above must stay unchanged.\n"
    "<custom_extract_prompt>\n{text}\n</custom_extract_prompt>\n"
)


def normalize_custom_extract_prompt(value: Any) -> dict[str, str] | None:
    """Validate a request value; ``None`` / ``{}`` mean "no override"."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("custom_extract_prompt must be an object")  # noqa: TRY004
    cleaned: dict[str, str] = {}
    for key, text in value.items():
        if key not in CUSTOM_EXTRACT_KEYS:
            raise ValueError(
                f"unsupported custom_extract_prompt key {key!r}; "
                f"allowed: {', '.join(CUSTOM_EXTRACT_KEYS)}"
            )
        if not isinstance(text, str):
            raise ValueError(f"custom_extract_prompt[{key!r}] must be a string")  # noqa: TRY004
        text = text.strip()
        if not text:
            raise ValueError(f"custom_extract_prompt[{key!r}] must not be empty")
        if len(text) > MAX_CUSTOM_EXTRACT_CHARS:
            raise ValueError(
                f"custom_extract_prompt[{key!r}] exceeds {MAX_CUSTOM_EXTRACT_CHARS} characters"
            )
        cleaned[key] = text
    return cleaned or None


@contextlib.contextmanager
def use_custom_extract_prompt(value: Mapping[str, str] | None) -> Iterator[None]:
    """Make ``value`` visible to every extraction prompt built inside the block."""
    token = _current.set(dict(value) if value else None)
    try:
        yield
    finally:
        _current.reset(token)


def current_custom_extract_prompt() -> dict[str, str] | None:
    return _current.get()


def apply_custom_extract_prompt(prompt: str, key: str, *, fallback_key: str | None = None) -> str:
    """Append the caller's guidance for ``key`` to ``prompt`` (no-op without one).

    ``fallback_key`` mirrors cloud: file content falls back to the
    ``detail_factual`` guidance when no ``document`` prompt is given.
    """
    spec = _current.get()
    if not spec:
        return prompt
    text = spec.get(key) or (spec.get(fallback_key) if fallback_key else None)
    if not text:
        return prompt
    text = _CLOSING_TAG.sub("", text)
    section = _SECTION_ZH if _CJK.search(prompt) else _SECTION_EN
    return prompt + section.format(text=text)
