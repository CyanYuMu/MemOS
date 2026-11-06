"""Search ``context_format``: extracted memory text, the original messages, or either by query.

``memory`` (default) returns the hits untouched. ``sources`` swaps each conversation
hit's text for the original messages it was extracted from. ``mixed`` (alias
``heuristic_routing``) picks one of the two per query with plain rules, never an LLM.
"""

from __future__ import annotations

import json
import os
import re

from typing import Any

from memos.search.version_select import JUDGED_TYPES


MEMORY = "memory"
SOURCES = "sources"
MIXED = "mixed"
VALID_MODES = (MEMORY, SOURCES, MIXED, "heuristic_routing")

MAX_CHARS_ENV = "MEMOS_CONTEXT_SOURCES_MAX_CHARS"
DEFAULT_MAX_CHARS = 2000

_ALIASES = {"": MEMORY, MEMORY: MEMORY, SOURCES: SOURCES, MIXED: MIXED, "heuristic_routing": MIXED}

# Questions that need the exact words, a date, a count or an order are answered from the source.
_SOURCE_CUES = re.compile(
    "原话|原文|一字不差|逐字|怎么说|说了什么|具体|细节"
    "|什么时候|哪天|哪年|几月|几号|几点|\\bwhen\\b|\\bwhat date\\b|\\bwhat time\\b"
    "|多少|几个|几次|几天|多久|\\bhow many\\b|\\bhow much\\b|\\bhow long\\b|\\bhow often\\b"
    "|先后|顺序|依次|第几|最先|最早|最后一次|\\bin what order\\b|\\bwhich came first\\b",
    re.IGNORECASE,
)


def normalize_mode(value: str | None) -> str | None:
    """The canonical mode for a request value, or ``None`` when it is not one of ours."""
    return _ALIASES.get((value or "").strip().lower())


def choose_mode(query: str, mode: str) -> str:
    """``memory`` or ``sources``: ``mixed`` decides by the wording of the query."""
    if mode != MIXED:
        return mode
    return SOURCES if _SOURCE_CUES.search(query or "") else MEMORY


def max_chars() -> int:
    try:
        return max(1, int(os.getenv(MAX_CHARS_ENV, DEFAULT_MAX_CHARS)))
    except ValueError:
        return DEFAULT_MAX_CHARS


def render_sources(sources: Any, limit: int) -> str:
    """One line per source (``[chat_time] role: content``), cut at a line boundary."""
    lines: list[str] = []
    for source in sources if isinstance(sources, list) else []:
        line = _render_source(source)
        if line and line not in lines:
            lines.append(line)

    picked: list[str] = []
    size = 0
    for line in lines:
        cost = len(line) + (1 if picked else 0)
        if size + cost > limit:
            if not picked:
                picked.append(line[: max(limit - 1, 0)] + "…")
            break
        picked.append(line)
        size += cost
    return "\n".join(picked)


def apply_context_format(
    results: dict[str, Any], query: str, mode: str, *, limit: int | None = None
) -> dict[str, Any]:
    """Return ``results`` with conversation hits rewritten when the mode resolves to ``sources``."""
    if choose_mode(query, mode) != SOURCES:
        return results
    groups = results.get("text_mem")
    if not groups:
        return results

    limit = limit or max_chars()
    rewritten = [
        {**group, "memories": [_rewrite(hit, limit) for hit in group.get("memories") or []]}
        for group in groups
    ]
    return {**results, "text_mem": rewritten}


def _rewrite(hit: dict[str, Any], limit: int) -> dict[str, Any]:
    meta = hit.get("metadata")
    if (
        not isinstance(meta, dict)
        or meta.get("memory_type") not in JUDGED_TYPES
        or meta.get("matched_version")
    ):
        return hit
    text = render_sources(meta.get("sources"), limit)
    if not text:
        return hit

    new_meta = {**meta, "extracted_memory": hit.get("memory"), "context_format": SOURCES}
    if "memory" in meta:
        new_meta["memory"] = text
    return {**hit, "memory": text, "metadata": new_meta}


def _render_source(source: Any) -> str:
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return ""
    if not isinstance(source, dict):
        return ""
    content = str(source.get("content") or "").strip()
    if not content:
        return ""

    kind = source.get("type") or ("chat" if source.get("role") else "doc")
    if kind != "chat":
        return content
    when = f"[{source['chat_time']}] " if source.get("chat_time") else ""
    who = f"{source['role']}: " if source.get("role") else ""
    return f"{when}{who}{content}"
