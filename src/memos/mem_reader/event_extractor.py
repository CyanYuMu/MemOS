"""Real-time event extraction (cloud ``event`` memory view).

An event is a structured unit: a title, a content summary, the event time
(``chat_time`` of the source message wins over the model's guess), the
participants and the location. Extraction is opt-in through
``allow_memory_view`` containing ``"event"``.
"""

from __future__ import annotations

import json
import os
import re

from dataclasses import dataclass
from typing import Any

from memos.log import get_logger
from memos.mem_reader.custom_extract import apply_custom_extract_prompt
from memos.mem_reader.prompt_runtime import prompt_for
from memos.memories.textual.item import (
    SourceMessage,
    TextualMemoryItem,
    TreeNodeTextualMemoryMetadata,
)
from memos.utils import normalize_to_utc_iso, utc_now_iso


logger = get_logger(__name__)

EVENT_MEMORY_TYPE = "EventMemory"
MAX_WINDOW_CHARS = 12000
DEFAULT_MAX_WINDOWS = 20

EVENT_EXTRACTION_PROMPT = """You extract structured events from a conversation.

Return ONLY a JSON array. Each item must have:
- "title": a short event title (<= 40 chars)
- "content": a factual summary of what happened
- "event_time": a list of time expressions mentioned (may be empty)
- "event_location": a list of locations (may be empty)
- "event_roles": a list of participants (may be empty)

Rules:
- Only extract real events (things that happened / are planned), not opinions or preferences.
- Keep the speaker attribution when the text carries it ("张三说...").
- Output `[]` when no event is present.
- Do not wrap the JSON in markdown.

Conversation:
{conversation}
"""


class EventExtractor:
    """Extract ``EventMemory`` items, one LLM call per window of at most ``MAX_WINDOW_CHARS``."""

    def __init__(self, llm: Any):
        self.llm = llm

    def extract(
        self,
        messages: list[Any],
        *,
        info: dict[str, Any] | None = None,
        user_name: str | None = None,
        session_id: str | None = None,
        need_embed: bool = False,
        embedder: Any = None,
    ) -> list[TextualMemoryItem]:
        if self.llm is None or not messages:
            return []

        lines = _message_lines(messages)
        if not "\n".join(line.text for line in lines).strip():
            return []

        windows = _split_windows(lines)
        limit = _max_windows()
        if len(windows) > limit:
            logger.warning(
                "Event extraction: %d windows, only the first %d are read", len(windows), limit
            )
            windows = windows[:limit]

        first_time = _preferred_chat_time([line.chat_time for line in lines if line.chat_time])
        items: list[TextualMemoryItem] = []
        seen: set[tuple] = set()
        last_time: str | None = None
        for window in windows:
            own_time = _preferred_chat_time([line.chat_time for line in window if line.chat_time])
            chat_time = own_time or last_time or first_time
            last_time = (
                _preferred_chat_time(
                    [line.chat_time for line in reversed(window) if line.chat_time]
                )
                or last_time
            )
            kept = self._extract_window(
                "\n".join(line.text for line in window if line.text),
                chat_time,
                seen,
                info=info,
                session_id=session_id,
                need_embed=need_embed,
                embedder=embedder,
            )
            items.extend(item for item, _ in kept)
            seen.update(key for _, key in kept)
        return items

    def _extract_window(
        self,
        conversation: str,
        preferred_time: str | None,
        seen: set[tuple],
        *,
        info: dict[str, Any] | None,
        session_id: str | None,
        need_embed: bool,
        embedder: Any,
    ) -> list[tuple[TextualMemoryItem, tuple]]:
        template = prompt_for("extract.event", "en", EVENT_EXTRACTION_PROMPT)
        try:
            prompt = apply_custom_extract_prompt(
                template.format(conversation=conversation), "event"
            )
        except (KeyError, IndexError, ValueError):
            logger.warning(
                "[EventExtractor] project prompt for extract.event does not render; "
                "using the built-in one"
            )
            prompt = apply_custom_extract_prompt(
                EVENT_EXTRACTION_PROMPT.format(conversation=conversation), "event"
            )
        try:
            raw = self.llm.generate([{"role": "user", "content": prompt}])
        except Exception as e:
            logger.warning("Event extraction LLM call failed: %s", e, exc_info=True)
            return []

        kept: list[tuple[TextualMemoryItem, tuple]] = []
        day = (preferred_time or "")[:10]
        for event in _parse_event_array(raw):
            title = str(event.get("title") or "").strip()
            content = str(event.get("content") or "").strip()
            if not content:
                continue
            key = _event_key(title, content, event.get("event_time"), day)
            if key in seen:
                continue
            memory = f"{title}: {content}" if title and title not in content else content
            if need_embed and embedder is not None:
                try:
                    embedding = embedder.embed([memory])[0]
                except Exception:  # noqa: BLE001 - embedding is optional
                    embedding = None
            else:
                embedding = None

            event_times = _as_list(event.get("event_time"))
            metadata = TreeNodeTextualMemoryMetadata(
                memory_type=EVENT_MEMORY_TYPE,
                status="activated",
                key=title or None,
                user_id=info.get("user_id") if info else None,
                session_id=session_id,
                chat_time=preferred_time,
                embedding=embedding,
                sources=[
                    SourceMessage(
                        type="chat",
                        chat_time=preferred_time,
                        content=conversation[:500],
                    )
                ],
                tags=(info or {}).get("custom_tags"),
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                background=conversation[:500],
            )
            # Extra metadata is allowed by the model; these are the cloud
            # event fields, surfaced by the formatter/BFF.
            metadata.event_time = event_times
            metadata.event_location = _as_list(event.get("event_location"))
            metadata.event_roles = _as_list(event.get("event_roles"))
            kept.append((TextualMemoryItem(memory=memory, metadata=metadata), key))

        return kept


@dataclass(frozen=True)
class _Line:
    prefix: str
    body: str
    chat_time: str | None = None

    @property
    def text(self) -> str:
        return self.prefix + self.body


def _message_lines(messages: list[Any]) -> list[_Line]:
    lines: list[_Line] = []
    for message in messages or []:
        if isinstance(message, str):
            lines.append(_Line("", message))
            continue
        if not isinstance(message, dict):
            continue

        role = message.get("role") or "user"
        role_name = message.get("role_name")
        speaker = f"{role_name} ({role})" if role_name else str(role)
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in content
            )
        chat_time = str(message["chat_time"]) if message.get("chat_time") else None
        if content:
            lines.append(_Line(f"{speaker[:100]}: ", str(content), chat_time))
        elif chat_time:
            lines.append(_Line("", "", chat_time))
    return lines


def _max_windows() -> int:
    try:
        return max(1, int(os.getenv("MEMOS_EVENT_MAX_WINDOWS", DEFAULT_MAX_WINDOWS)))
    except ValueError:
        return DEFAULT_MAX_WINDOWS


def _fit(lines: list[_Line], limit: int):
    """Yield the lines, cutting one that alone exceeds ``limit`` into speaker-prefixed pieces."""
    for line in lines:
        if len(line.text) <= limit:
            yield line
            continue
        step = max(1, limit - len(line.prefix))
        for start in range(0, len(line.body), step):
            yield _Line(line.prefix, line.body[start : start + step], line.chat_time)


def _split_windows(lines: list[_Line], limit: int = MAX_WINDOW_CHARS) -> list[list[_Line]]:
    """Pack whole lines into windows whose joined text is at most ``limit`` characters."""
    windows: list[list[_Line]] = []
    current: list[_Line] = []
    size = 0
    for line in _fit(lines, limit):
        cost = len(line.text) + (1 if size and line.text else 0)
        if size and size + cost > limit:
            windows.append(current)
            current, size, cost = [], 0, len(line.text)
        current.append(line)
        size += cost
    if current:
        windows.append(current)
    return windows


def _norm(text: Any) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").casefold())


def _event_key(title: str, content: str, event_time: Any, day: str) -> tuple:
    """Same event told again in a later window of the same day; another day is another event."""
    head = _norm(title) or _norm(content)[:80]
    return (head, tuple(sorted(_norm(t) for t in _as_list(event_time))), day)


def _preferred_chat_time(chat_times: list[str]) -> str | None:
    for value in chat_times:
        if value and value.strip():
            return normalize_to_utc_iso(value) or value.strip()
    return None


def _parse_event_array(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning("Event extraction returned invalid JSON: %s", text[:200])
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
