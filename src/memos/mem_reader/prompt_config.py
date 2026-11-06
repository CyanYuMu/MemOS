"""Project-scoped extract prompt configuration, kept for the old endpoints.

Before the prompt registry there was exactly one customisable prompt per
project: the full chat extraction template. The console's five ``*_extract_prompt``
endpoints still speak that shape, so this module stays as a thin facade over
``prompt_store``. Everything here maps onto the ``extract.chat`` slot:

- ``get_saved_prompt`` reads the ``zh`` slot,
- ``save_prompt`` writes the same body to ``zh`` and ``en`` (the old behaviour
  replaced the template regardless of language),
- ``delete_prompt`` appends a ``reset`` row.

Readers no longer read this module: every extraction path takes its template
from ``prompt_runtime.prompt_for`` inside a ``prompt_scope``.

Storage lives in ``prompt_store``: SQLite on the PVC (``MEMOS_PROMPT_CONFIG_DB``
or ``<MOS_CUBE_PATH>/extract_prompts.db``).
"""

from __future__ import annotations

from memos.log import get_logger
from memos.mem_reader import prompt_store
from memos.mem_reader.prompt_registry import default_body


logger = get_logger(__name__)

LEGACY_KEY = "extract.chat"
PROMPT_PLACEHOLDER = "${conversation}"
MAX_EXTRACT_PROMPT_CHARS = 20000
EXTRACTION_TYPES: tuple[str, ...] = ("text_mem", "pref_mem", "event_mem")


def default_extract_prompt() -> str:
    """The prompt used when a project has never saved one."""
    return default_body(LEGACY_KEY, "zh")


def normalize_extract_prompt(value: str) -> str:
    """Validate a prompt before it is saved.

    ``${conversation}`` is mandatory: without it the reader would call the model
    with a prompt that contains no conversation, and extraction would silently
    return nothing.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("prompt must not be empty")
    if len(text) > MAX_EXTRACT_PROMPT_CHARS:
        raise ValueError(f"prompt exceeds {MAX_EXTRACT_PROMPT_CHARS} characters")
    if PROMPT_PLACEHOLDER not in text:
        raise ValueError(f"prompt must contain {PROMPT_PLACEHOLDER}")
    return text


def get_saved_prompt(mem_cube_id: str) -> tuple[str, str] | None:
    """Return ``(prompt, updated_at)`` for a project, or ``None`` when unset."""
    if not mem_cube_id:
        return None
    resolved = prompt_store.resolve(mem_cube_id, LEGACY_KEY, "zh")
    if resolved.is_default:
        return None
    return resolved.body, resolved.updated_at or ""


def save_prompt(mem_cube_id: str, prompt: str) -> str:
    """Validate and persist a project prompt.

    Returns the stored ``updated_at``; empty when both slots already held the
    same body and nothing new was appended.
    """
    if not mem_cube_id:
        raise ValueError("mem_cube_id must not be empty")
    text = normalize_extract_prompt(prompt)
    saved_at = ""
    for lang in ("zh", "en"):
        current = prompt_store.resolve(mem_cube_id, LEGACY_KEY, lang)
        result = prompt_store.save_slot(
            mem_cube_id,
            LEGACY_KEY,
            lang,
            body=text,
            note="legacy extract prompt save",
            author="legacy",
            base_version_id=current.version_id,
        )
        saved_at = result["created_at"] or saved_at
    return saved_at


def delete_prompt(mem_cube_id: str) -> bool:
    """Drop a saved prompt (reset to default); reports whether a row existed."""
    if not mem_cube_id:
        return False
    reset = prompt_store.reset_slots(mem_cube_id, key=LEGACY_KEY, author="legacy")
    return bool(reset)
