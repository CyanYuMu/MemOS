"""Request-level validation for add/extract payloads.

The cloud contract caps a single add request (``features/multimodal.md``):

* at most 20 files per request;
* at most 100 MB and 500 pages per file;
* at most 20,000 input tokens for ``add/message``;
* multimodal requests always run asynchronously, regardless of ``async_mode``.

The limits are deployment-tunable through environment variables so a
deployment can tighten or relax them without a code change.
"""

import os
import re

from dataclasses import dataclass
from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)

FILE_PART_TYPES = {
    "file",
    "input_file",
    "image",
    "input_image",
    "image_url",
}

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Environment variable [%s]=%r is not an integer, using %d", name, raw, default
        )
        return default
    if value < 0:
        logger.warning("Environment variable [%s]=%r must be >= 0, using %d", name, raw, default)
        return default
    return value


@dataclass(frozen=True)
class AddLimits:
    max_files: int
    max_file_size_bytes: int
    max_file_pages: int
    max_input_tokens: int


def add_limits() -> AddLimits:
    limits = AddLimits(
        max_files=_env_int("MEMOS_MAX_FILES_PER_REQUEST", 20),
        max_file_size_bytes=_env_int("MEMOS_MAX_FILE_SIZE_MB", 100) * 1024 * 1024,
        max_file_pages=_env_int("MEMOS_MAX_FILE_PAGES", 500),
        max_input_tokens=_env_int("MEMOS_MAX_ADD_TOKENS", 20_000),
    )
    return limits


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def iter_file_parts(node: Any):
    """Yield every file-like part in a messages payload."""
    if isinstance(node, list | tuple):
        for item in node:
            yield from iter_file_parts(item)
        return
    if not isinstance(node, dict):
        return

    part_type = str(node.get("type", "")).lower()
    if part_type in FILE_PART_TYPES:
        yield node
        return

    # Tool-output containers carry input_file / input_image keys without a
    # top-level "type".
    for key in ("input_file", "input_image", "input_image_url"):
        child = node.get(key)
        if child is not None:
            yield {"type": key, key: child}
    if node.get("input_text") is not None:
        yield from _iter_embedded_parts(node.get("input_text"))

    for key in ("content", "file"):
        if key in node:
            yield from iter_file_parts(node[key])


def _iter_embedded_parts(node: Any):
    """Walk a tool-output wrapper without re-visiting text fields."""
    if isinstance(node, list | tuple):
        for item in node:
            yield from _iter_embedded_parts(item)
    elif isinstance(node, dict):
        for key in ("input_file", "input_image", "input_image_url"):
            child = node.get(key)
            if child is not None:
                yield {"type": key, key: child}
        for key in ("content", "file"):
            if key in node:
                yield from iter_file_parts(node[key])


def _text_fragments(node: Any):
    """Yield text fragments worth counting toward the input-token budget."""
    if isinstance(node, str):
        yield node
        return
    if isinstance(node, list | tuple):
        for item in node:
            yield from _text_fragments(item)
        return
    if not isinstance(node, dict):
        return

    for key in ("content", "text", "input_text"):
        value = node.get(key)
        if isinstance(value, str):
            yield value
        elif isinstance(value, list | tuple | dict):
            yield from _text_fragments(value)

    for key in ("messages", "chat_history"):
        if key in node:
            yield from _text_fragments(node[key])


def _file_object(part: dict[str, Any]) -> dict[str, Any]:
    for key in ("file", "input_file", "image", "image_url", "input_image"):
        value = _as_dict(part.get(key))
        if value is not None:
            return value
    return part


def file_size_bytes(part: dict[str, Any]) -> int | None:
    """Return the file size when the payload declares it, else ``None``.

    Remote URLs are intentionally not fetched: the limit is enforced for
    inline payloads (base64 / data URLs) and for explicit size fields.
    """
    file_obj = _file_object(part)
    for key in ("size", "file_size", "bytes", "size_bytes"):
        value = file_obj.get(key, part.get(key))
        if isinstance(value, int | float) and value >= 0:
            return int(value)

    data = file_obj.get("file_data", part.get("file_data"))
    if not isinstance(data, str):
        return None
    if data.startswith("data:"):
        _, _, payload = data.partition(",")
        return _base64_size(payload)
    if data.startswith(("http://", "https://")):
        return None
    return _base64_size(data)


def _base64_size(payload: str) -> int | None:
    """Estimate decoded size without allocating the decoded bytes."""
    compact = (
        payload
        if not any(char in payload for char in ("\n", "\r", "\t", " "))
        else "".join(payload.split())
    )
    if not compact or not _BASE64_RE.match(compact):
        return None
    padding = len(compact) - len(compact.rstrip("="))
    return len(compact) * 3 // 4 - padding


def file_page_count(part: dict[str, Any]) -> int | None:
    file_obj = _file_object(part)
    for key in ("page_count", "pages", "num_pages", "page_num"):
        value = file_obj.get(key, part.get(key))
        if isinstance(value, int) and value >= 0:
            return value
    return None


@dataclass(frozen=True)
class AddRequestInspection:
    file_count: int
    has_multimodal: bool
    estimated_input_tokens: int

    def errors(self, limits: AddLimits | None = None) -> list[str]:
        limits = limits or add_limits()
        problems: list[str] = []
        if self.file_count > limits.max_files:
            problems.append(f"Too many files: {self.file_count} > {limits.max_files} per request.")
        if self.estimated_input_tokens > limits.max_input_tokens:
            problems.append(
                f"Input too large: ~{self.estimated_input_tokens} tokens "
                f"> {limits.max_input_tokens}."
            )
        return problems


def inspect_add_messages(messages: Any) -> AddRequestInspection:
    """Inspect an add/extract payload and report multimodal facts + errors."""
    from memos.mem_reader.utils import count_tokens_text

    parts = list(iter_file_parts(messages))
    problems: list[str] = []
    limits = add_limits()

    for part in parts:
        size = file_size_bytes(part)
        if size is not None and size > limits.max_file_size_bytes:
            problems.append(
                f"File too large: {size} bytes > {limits.max_file_size_bytes} bytes "
                f"(MEMOS_MAX_FILE_SIZE_MB={limits.max_file_size_bytes // (1024 * 1024)})."
            )
        pages = file_page_count(part)
        if pages is not None and pages > limits.max_file_pages:
            problems.append(f"File too long: {pages} pages > {limits.max_file_pages} pages.")

    text = "\n".join(_text_fragments(messages))
    # Avoid tokenizing multi-megabyte bodies just to prove they exceed the cap.
    tokenize_cap = limits.max_input_tokens * 8
    if len(text) > tokenize_cap:
        estimated_tokens = limits.max_input_tokens + 1
    else:
        estimated_tokens = count_tokens_text(text)
    inspection = AddRequestInspection(
        file_count=len(parts),
        has_multimodal=bool(parts),
        estimated_input_tokens=estimated_tokens,
    )
    problems.extend(inspection.errors(limits))
    if problems:
        raise ValueError("; ".join(problems))
    return inspection
