"""Request/task-scoped lookup of project prompt overrides.

``prompt_scope`` is entered once at every entry point that calls an LLM on
behalf of a project (sync add, the async scheduler read, ``/product/extract_memory``,
Dream). It reads that project's overrides in one query and keeps them in a
``ContextVar``; ``prompt_for`` then just reads the ContextVar.

Outside a scope — a library user calling ``memos`` directly, a background job
that never opened one — ``prompt_for`` returns the default it was handed, so
behaviour is exactly what it was before overrides existed.

Nothing is cached across requests: a restart or a second replica cannot serve a
stale prompt, and the extra query is one indexed read per request.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memos.log import get_logger


if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from memos.mem_reader.prompt_store import ResolvedPrompt


logger = get_logger(__name__)

_OVERRIDES: ContextVar[dict[tuple[str, str], ResolvedPrompt] | None] = ContextVar(
    "project_prompt_overrides", default=None
)
_DRAFTS: ContextVar[dict[tuple[str, str], str] | None] = ContextVar("prompt_drafts", default=None)
_USAGE: ContextVar[list[PromptUsage] | None] = ContextVar("prompt_usage", default=None)


@dataclass(frozen=True)
class PromptUsage:
    """One template slot a run actually read."""

    key: str
    lang: str
    source: str
    version_id: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "lang": self.lang,
            "source": self.source,
            "version_id": self.version_id,
        }


def _record_usage(key: str, lang: str, source: str, version_id: int) -> None:
    usage = _USAGE.get()
    if usage is None:
        return
    if any(item.key == key and item.lang == lang for item in usage):
        return
    usage.append(PromptUsage(key=key, lang=lang, source=source, version_id=version_id))


@contextmanager
def record_prompt_usage() -> Iterator[list[PromptUsage]]:
    """Collect the slots ``prompt_for`` serves inside this context."""
    usage: list[PromptUsage] = []
    token = _USAGE.set(usage)
    try:
        yield usage
    finally:
        _USAGE.reset(token)


class _PromptScope:
    """Context manager that loads one project's overrides once."""

    def __init__(self, mem_cube_id: str | None) -> None:
        self.mem_cube_id = mem_cube_id or ""
        self._token: object | None = None

    def __enter__(self) -> dict[tuple[str, str], ResolvedPrompt]:
        # Imported here: the store pulls in the registry, which imports the
        # reader modules that call ``prompt_for`` — a module-level import here
        # would close that cycle.
        from memos.mem_reader.prompt_store import project_overrides

        overrides: dict[tuple[str, str], ResolvedPrompt] = {}
        try:
            overrides = project_overrides(self.mem_cube_id)
        except Exception:
            logger.warning(
                "[PromptScope] failed to load prompt overrides for %r; using defaults",
                self.mem_cube_id,
                exc_info=True,
            )
        self._token = _OVERRIDES.set(overrides)
        return overrides

    def __exit__(self, *_exc: object) -> None:
        if self._token is not None:
            _OVERRIDES.reset(self._token)  # type: ignore[arg-type]


def prompt_scope(mem_cube_id: str | None) -> AbstractContextManager:
    """Load every override of one project for the current context."""
    return _PromptScope(mem_cube_id)


def current_overrides() -> dict[tuple[str, str], ResolvedPrompt]:
    """The overrides visible to the current context (empty outside a scope)."""
    return _OVERRIDES.get() or {}


def prompt_for(key: str, lang: str, default: str) -> str:
    """The template to use for ``key`` in ``lang``.

    Precedence: a draft set by ``draft_prompt_override`` (trial runs), then the
    project override loaded by ``prompt_scope``, then ``default``.
    """
    drafts = _DRAFTS.get()
    if drafts and (key, lang) in drafts:
        _record_usage(key, lang, "draft", 0)
        return drafts[(key, lang)]
    resolved = current_overrides().get((key, lang))
    if resolved is None:
        _record_usage(key, lang, "default", 0)
        return default
    _record_usage(key, lang, "project", resolved.version_id)
    return resolved.body


@contextmanager
def draft_prompt_override(key: str, lang: str, body: str) -> Iterator[None]:
    """Use ``body`` for one slot in this context, without storing it.

    The trial run wraps its reader call in this so a draft never has to be
    saved, and never leaks to another request: the value lives in a ContextVar
    and is restored on exit.
    """
    drafts = dict(_DRAFTS.get() or {})
    drafts[(key, lang)] = body
    token = _DRAFTS.set(drafts)
    try:
        yield
    finally:
        _DRAFTS.reset(token)


def prompt_version(key: str, lang: str) -> int:
    """The override version in effect for this slot, or 0 when it is default."""
    resolved = current_overrides().get((key, lang))
    return resolved.version_id if resolved else 0
