"""Append-only storage for project-level prompt overrides.

One SQLite file, the same one the extract prompt has always used
(``MEMOS_PROMPT_CONFIG_DB`` or ``<MOS_CUBE_PATH>/extract_prompts.db``, which
lands on the PVC). Two tables:

- ``prompt_versions``: every save, reset, rollback and migration is a new row.
  Rows are never updated or deleted.
- ``extract_prompt_configs``: the pre-registry table. It is only read, once per
  connection, to seed ``prompt_versions`` for projects that saved a prompt
  before this module existed.

A slot is ``(mem_cube_id, prompt_key, lang)``. Its live row is the one with the
highest ``version_id``; a ``reset`` row (empty body) or no row at all means the
built-in default.

A single-replica deployment is assumed, the same limitation as the rest of the
SQLite state in this service.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memos import settings
from memos.exceptions import (
    PromptBusyError,
    PromptNotFoundError,
    PromptValidationError,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
)
from memos.log import get_logger
from memos.mem_reader.prompt_registry import (
    MAX_NOTE_CHARS,
    PROMPT_KEYS,
    SUPPORTED_LANGS,
    get_spec,
    iter_specs,
    validate_body,
)


if TYPE_CHECKING:
    from collections.abc import Iterator


logger = get_logger(__name__)

LEGACY_TABLE = "extract_prompt_configs"
LEGACY_KEY = "extract.chat"
MAX_VERSIONS_PAGE = 100
DEFAULT_VERSIONS_PAGE = 20

_DB_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS prompt_versions ("
    "version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "mem_cube_id TEXT NOT NULL, "
    "prompt_key TEXT NOT NULL, "
    "lang TEXT NOT NULL CHECK (lang IN ('zh','en')), "
    "kind TEXT NOT NULL CHECK (kind IN ('save','reset','rollback','migrated')), "
    "body TEXT NOT NULL, "
    "note TEXT NOT NULL DEFAULT '', "
    "author TEXT NOT NULL DEFAULT '', "
    "parent_version_id INTEGER, "
    "created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_prompt_versions_slot "
    "ON prompt_versions (mem_cube_id, prompt_key, lang, version_id DESC)",
    "CREATE TABLE IF NOT EXISTS extract_prompt_configs ("
    "mem_cube_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, updated_at TEXT NOT NULL)",
)


@dataclass(frozen=True)
class ResolvedPrompt:
    """The body a project should use for one slot."""

    body: str
    source: str
    version_id: int = 0
    version_no: int = 0
    updated_at: str | None = None
    author: str | None = None

    @property
    def is_default(self) -> bool:
        return self.source == "default"

    def as_state(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version_id": self.version_id,
            "version_no": self.version_no,
            "updated_at": self.updated_at,
            "author": self.author,
        }


def _db_path() -> Path:
    configured = os.getenv("MEMOS_PROMPT_CONFIG_DB", "").strip()
    if configured:
        return Path(configured)
    base = os.getenv("MOS_CUBE_PATH", "").strip() or str(settings.MEMOS_DIR)
    return Path(base) / "extract_prompts.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_timeout() -> float:
    raw = os.getenv("MEMOS_PROMPT_DB_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else 5.0
    except ValueError:
        return 5.0


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _busy() -> PromptBusyError:
    return PromptBusyError("提示词配置库正忙，请稍后重试。", 2)


def _connect() -> sqlite3.Connection:
    """Open one connection; the schema and migration run once per database file.

    Initialising on every connection took the write lock for pure reads and
    repeated the migration; it is now done once, under a lock, keyed by path.
    The connection is closed if that first initialisation fails.
    """
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    try:
        conn = sqlite3.connect(path, timeout=_db_timeout())
    except sqlite3.OperationalError as exc:
        if _is_locked(exc):
            raise _busy() from exc
        raise
    conn.row_factory = sqlite3.Row
    # Explicit transactions: the write paths begin with ``BEGIN IMMEDIATE`` so
    # a read-then-write sequence cannot interleave with another connection.
    conn.isolation_level = None
    try:
        if key not in _INITIALIZED:
            with _INIT_LOCK:
                if key not in _INITIALIZED:
                    with _transaction(conn):
                        for statement in _SCHEMA:
                            conn.execute(statement)
                        _migrate_legacy(conn)
                    _INITIALIZED.add(key)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _transaction(
    conn: sqlite3.Connection, *, immediate: bool = True
) -> Iterator[sqlite3.Connection]:
    """One explicit SQLite transaction; ``IMMEDIATE`` takes the write lock up front."""
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    except sqlite3.OperationalError as exc:
        if _is_locked(exc):
            raise _busy() from exc
        raise
    try:
        yield conn
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            if _is_locked(exc):
                raise _busy() from exc
            raise


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """Seed ``prompt_versions`` from the one-prompt-per-project table.

    The old behaviour replaced the built-in template regardless of language, so
    each legacy row becomes one ``migrated`` row per language, same body. The
    insert is a plain ``INSERT ... SELECT ... WHERE NOT EXISTS``: running it
    again (restart, two workers) cannot add a second copy, and it never
    resurrects a slot the user has since reset.
    """
    rows = conn.execute(f"SELECT mem_cube_id, prompt, updated_at FROM {LEGACY_TABLE}").fetchall()
    if not rows:
        return
    for mem_cube_id, prompt, updated_at in rows:
        for lang in SUPPORTED_LANGS:
            conn.execute(
                "INSERT INTO prompt_versions "
                "(mem_cube_id, prompt_key, lang, kind, body, note, author, parent_version_id, created_at) "
                "SELECT ?, ?, ?, 'migrated', ?, '', 'migration', NULL, ? "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM prompt_versions "
                "WHERE mem_cube_id = ? AND prompt_key = ? AND lang = ?"
                ")",
                (
                    mem_cube_id,
                    LEGACY_KEY,
                    lang,
                    prompt,
                    updated_at or _now(),
                    mem_cube_id,
                    LEGACY_KEY,
                    lang,
                ),
            )


def _latest_rows(conn: sqlite3.Connection, mem_cube_id: str) -> dict[tuple[str, str], sqlite3.Row]:
    """The live row of every slot a project owns, keyed by ``(key, lang)``."""
    rows = conn.execute(
        "SELECT prompt_key, lang, version_id, kind, body, created_at, author, "
        "ROW_NUMBER() OVER (PARTITION BY prompt_key, lang ORDER BY version_id) AS version_no "
        "FROM prompt_versions WHERE mem_cube_id = ? ORDER BY version_id DESC",
        (mem_cube_id,),
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        latest.setdefault((row["prompt_key"], row["lang"]), row)
    return latest


def _resolve_from_row(row: sqlite3.Row | None, default: str) -> ResolvedPrompt:
    if row is None or row["kind"] == "reset" or not row["body"]:
        return ResolvedPrompt(body=default, source="default")
    return ResolvedPrompt(
        body=row["body"],
        source="project",
        version_id=int(row["version_id"]),
        version_no=int(row["version_no"]),
        updated_at=row["created_at"],
        author=row["author"] or None,
    )


def resolve(mem_cube_id: str, key: str, lang: str) -> ResolvedPrompt:
    """Return the body that should be used for ``(project, key, lang)``."""
    spec = get_spec(key)
    default = spec.default_for(lang)
    if not mem_cube_id:
        return ResolvedPrompt(body=default, source="default")

    conn = _connect()
    try:
        with _DB_LOCK:
            row = conn.execute(
                "SELECT version_id, kind, body, created_at, author, "
                "ROW_NUMBER() OVER (ORDER BY version_id) AS version_no "
                "FROM prompt_versions "
                "WHERE mem_cube_id = ? AND prompt_key = ? AND lang = ? "
                "ORDER BY version_id DESC LIMIT 1",
                (mem_cube_id, key, lang),
            ).fetchone()
    finally:
        conn.close()
    return _resolve_from_row(row, default)


def project_overrides(mem_cube_id: str) -> dict[tuple[str, str], ResolvedPrompt]:
    """Every non-default slot of one project, read once.

    This is what ``prompt_scope`` puts into the ContextVar, so a request does a
    single query instead of one per prompt.
    """
    if not mem_cube_id:
        return {}
    conn = _connect()
    try:
        with _DB_LOCK:
            rows = _latest_rows(conn, mem_cube_id)
    finally:
        conn.close()

    overrides: dict[tuple[str, str], ResolvedPrompt] = {}
    for (key, lang), row in rows.items():
        if key not in PROMPT_KEYS or lang not in SUPPORTED_LANGS:
            continue
        spec = get_spec(key)
        if lang not in spec.languages:
            continue
        resolved = _resolve_from_row(row, spec.default_for(lang))
        if not resolved.is_default:
            overrides[(key, lang)] = resolved
    return overrides


def slot_states(mem_cube_id: str) -> dict[tuple[str, str], ResolvedPrompt]:
    """Every slot of one project, defaults included. Used by ``list`` and ``detail``."""
    conn = _connect()
    try:
        with _DB_LOCK:
            latest = _latest_rows(conn, mem_cube_id) if mem_cube_id else {}
    finally:
        conn.close()

    states: dict[tuple[str, str], ResolvedPrompt] = {}
    for spec in iter_specs():
        for lang in spec.languages:
            states[(spec.key, lang)] = _resolve_from_row(
                latest.get((spec.key, lang)), spec.default_for(lang)
            )
    return states


def list_items(mem_cube_id: str) -> list[dict[str, Any]]:
    """Registrations plus their per-language state, for the console list page."""
    states = slot_states(mem_cube_id)
    items: list[dict[str, Any]] = []
    for spec in iter_specs():
        item = spec.summary_dict()
        item["state"] = {lang: states[(spec.key, lang)].as_state() for lang in spec.languages}
        items.append(item)
    return items


def detail_item(mem_cube_id: str, key: str) -> dict[str, Any]:
    """Full registration, built-in defaults and live bodies for one prompt."""
    spec = get_spec(key)
    states = slot_states(mem_cube_id)
    data = spec.public_dict()
    data["defaults"] = {lang: spec.default_for(lang) for lang in spec.languages}
    data["effective"] = {
        lang: _effective_dict(states[(key, lang)], key, lang) for lang in spec.languages
    }
    return data


def list_versions(
    mem_cube_id: str,
    key: str,
    lang: str,
    *,
    limit: int = DEFAULT_VERSIONS_PAGE,
    before_version_id: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Version history of one slot, newest first."""
    spec = get_spec(key)
    if lang not in spec.languages:
        raise PromptNotFoundError(f"prompt {key!r} has no {lang!r} slot")

    page_size = max(1, min(int(limit or DEFAULT_VERSIONS_PAGE), MAX_VERSIONS_PAGE))
    query = (
        "SELECT version_id, lang, kind, body, note, author, created_at, parent_version_id, "
        "ROW_NUMBER() OVER (ORDER BY version_id) AS version_no "
        "FROM prompt_versions WHERE mem_cube_id = ? AND prompt_key = ? AND lang = ?"
    )
    params: list[Any] = [mem_cube_id, key, lang]
    if before_version_id is not None:
        query += " AND version_id < ?"
        params.append(int(before_version_id))
    query += " ORDER BY version_id DESC LIMIT ?"
    params.append(page_size + 1)

    conn = _connect()
    try:
        with _DB_LOCK:
            rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > page_size
    versions = [dict(row) for row in rows[:page_size]]
    return versions, has_more


def _fetch_version(
    conn: sqlite3.Connection, mem_cube_id: str, version_id: int, key: str, lang: str
) -> sqlite3.Row:
    """One version of this project's slot; other projects' ids are "not found"."""
    row = conn.execute(
        "SELECT version_id, kind, body, note, author, created_at, parent_version_id "
        "FROM prompt_versions "
        "WHERE version_id = ? AND mem_cube_id = ? AND prompt_key = ? AND lang = ?",
        (int(version_id), mem_cube_id, key, lang),
    ).fetchone()
    if row is None:
        raise PromptVersionNotFoundError(f"version {version_id} does not exist in this slot")
    return row


def save_slot(
    mem_cube_id: str,
    key: str,
    lang: str,
    *,
    body: str | None = None,
    restore_from_version_id: int | None = None,
    note: str = "",
    author: str = "",
    base_version_id: int = 0,
) -> dict[str, Any]:
    """Append a new version to one slot and return it with the live state.

    Raises ``PromptValidationError`` for a bad draft, ``PromptVersionConflictError``
    when ``base_version_id`` is stale, and ``PromptNotFoundError`` for an
    unknown key, language, or version to restore.
    """
    if not mem_cube_id:
        raise PromptValidationError("mem_cube_id must not be empty")
    spec = get_spec(key)
    if lang not in spec.languages:
        raise PromptNotFoundError(f"prompt {key!r} has no {lang!r} slot")
    if len(note or "") > MAX_NOTE_CHARS:
        raise PromptValidationError(f"note exceeds {MAX_NOTE_CHARS} characters")

    # Read the live version, decide, and insert inside one IMMEDIATE
    # transaction: two editors holding the same base_version_id must not both
    # win, and a rollback lookup must not race with a save.
    created_at = _now()
    conn = _connect()
    try:
        with _DB_LOCK, _transaction(conn):
            row = conn.execute(
                "SELECT version_id, kind, body, created_at, author, "
                "ROW_NUMBER() OVER (ORDER BY version_id) AS version_no "
                "FROM prompt_versions "
                "WHERE mem_cube_id = ? AND prompt_key = ? AND lang = ? "
                "ORDER BY version_id DESC LIMIT 1",
                (mem_cube_id, key, lang),
            ).fetchone()
            current = _resolve_from_row(row, spec.default_for(lang))
            if int(base_version_id or 0) != current.version_id:
                raise PromptVersionConflictError(
                    "prompt has been updated since it was read", current=current.as_state()
                )

            kind = "save"
            source_body = body
            if restore_from_version_id is not None:
                source_row = _fetch_version(conn, mem_cube_id, restore_from_version_id, key, lang)
                source_body = source_row["body"]
                kind = "rollback" if source_body else "reset"

            text = (source_body or "").strip()
            if kind == "reset":
                text = ""
            elif kind != "reset":
                found, _warnings = validate_body(key, lang, text)
                errors = [issue.as_dict() for issue in found]
                if errors:
                    raise PromptValidationError("prompt failed validation", errors=errors)

            target = spec.default_for(lang) if kind == "reset" else text
            if target == current.body:
                # Re-saving the live body is a no-op: no version churn.
                return {
                    "version_id": current.version_id,
                    "version_no": current.version_no,
                    "created_at": current.updated_at,
                    "effective": _effective_dict(current, key, lang),
                }

            version_no = int(
                conn.execute(
                    "SELECT COUNT(*) + 1 FROM prompt_versions "
                    "WHERE mem_cube_id = ? AND prompt_key = ? AND lang = ?",
                    (mem_cube_id, key, lang),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                "INSERT INTO prompt_versions "
                "(mem_cube_id, prompt_key, lang, kind, body, note, author, parent_version_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mem_cube_id,
                    key,
                    lang,
                    kind,
                    "" if kind == "reset" else text,
                    (note or "")[:MAX_NOTE_CHARS],
                    author or "",
                    current.version_id or None,
                    created_at,
                ),
            )
            version_id = int(cursor.lastrowid or 0)
    finally:
        conn.close()

    stored = ResolvedPrompt(
        body=target,
        source="default" if kind == "reset" else "project",
        version_id=0 if kind == "reset" else version_id,
        version_no=0 if kind == "reset" else version_no,
        updated_at=None if kind == "reset" else created_at,
        author=None if kind == "reset" else (author or None),
    )
    return {
        "version_id": version_id,
        "version_no": version_no,
        "created_at": created_at,
        "effective": _effective_dict(stored, key, lang),
    }


def _effective_dict(resolved: ResolvedPrompt, key: str, lang: str) -> dict[str, Any]:
    return {
        "body": resolved.body,
        "source": resolved.source,
        "version_id": resolved.version_id,
        "version_no": resolved.version_no,
        "updated_at": resolved.updated_at,
        "author": resolved.author,
    }


def reset_slots(
    mem_cube_id: str,
    *,
    key: str | None = None,
    lang: str | None = None,
    author: str = "",
) -> list[dict[str, Any]]:
    """Append ``reset`` rows for every slot that is currently overridden.

    ``key`` unset means the whole project, ``lang`` unset means every language
    of the selected key. Returns the slots that actually changed.
    """
    if not mem_cube_id:
        raise PromptValidationError("mem_cube_id must not be empty")
    specs = [get_spec(key)] if key else list(iter_specs())
    if lang is not None and lang not in SUPPORTED_LANGS:
        raise PromptValidationError(f"unsupported language {lang!r}")

    created_at = _now()
    reset: list[dict[str, Any]] = []
    conn = _connect()
    try:
        with _DB_LOCK, _transaction(conn):
            latest = _latest_rows(conn, mem_cube_id)
            for spec in specs:
                langs = (lang,) if lang else spec.languages
                for current_lang in langs:
                    if current_lang not in spec.languages:
                        continue
                    row = latest.get((spec.key, current_lang))
                    if row is None or row["kind"] == "reset" or not row["body"]:
                        continue
                    version_no = int(
                        conn.execute(
                            "SELECT COUNT(*) + 1 FROM prompt_versions "
                            "WHERE mem_cube_id = ? AND prompt_key = ? AND lang = ?",
                            (mem_cube_id, spec.key, current_lang),
                        ).fetchone()[0]
                    )
                    cursor = conn.execute(
                        "INSERT INTO prompt_versions "
                        "(mem_cube_id, prompt_key, lang, kind, body, note, author, parent_version_id, created_at) "
                        "VALUES (?, ?, ?, 'reset', '', '', ?, ?, ?)",
                        (
                            mem_cube_id,
                            spec.key,
                            current_lang,
                            author or "",
                            row["version_id"],
                            created_at,
                        ),
                    )
                    reset.append(
                        {
                            "key": spec.key,
                            "lang": current_lang,
                            "version_id": int(cursor.lastrowid or 0),
                            "version_no": version_no,
                        }
                    )
    finally:
        conn.close()
    return reset
