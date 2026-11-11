"""Bounded, local-only event collection. This module uses only the standard library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

from contextlib import contextmanager
from pathlib import Path


EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "StopFailure",
    "SessionEnd",
)
NAMESPACE = "memos-claude-daily-v1"
MAX_TEXT = 12000
MAX_PAYLOAD = 96000
SECRET_KEY = re.compile(r"password|passwd|secret|token|api.?key|authorization|cookie", re.I)
PRIVATE_KEY = re.compile(r"thinking|reasoning|signature|encrypted", re.I)
SECRET_FILE = re.compile(
    r"(^|/)(\.env(?:\.[^/]*)?|credentials[^/]*|id_(?:rsa|ed25519)|[^/]*\.(?:pem|key|p12)|"
    r"\.claude/(?:settings[^/]*\.json|\.credentials\.json))$",
    re.I,
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_config(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("private_config_required")
    if path.stat().st_size > 100000:
        raise ValueError("config_too_large")
    config = json.loads(path.read_text())
    if config.get("version") != 1 or type(config.get("enabled")) is not bool:
        raise ValueError("config_invalid")
    if not config.get("projects") or not Path(config["stateDir"]).is_absolute():
        raise ValueError("config_scope_missing")
    if any(not Path(p["path"]).is_absolute() or not p.get("name") for p in config["projects"]):
        raise ValueError("config_project_invalid")
    if not isinstance(config.get("port"), int) or not 1024 <= config["port"] <= 65535:
        raise ValueError("config_port_invalid")
    return config


def clean(value, secrets=(), depth=0):
    """Keep public text/tool facts, never private reasoning or credential fields."""
    if depth > 8:
        return "[truncated:depth]"
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in {
            "thinking",
            "redacted_thinking",
            "reasoning",
        }:
            return "[private reasoning omitted]"
        return {
            str(k)[:100]: "[redacted]"
            if SECRET_KEY.search(str(k))
            else clean(v, secrets, depth + 1)
            for k, v in list(value.items())[:100]
            if not PRIVATE_KEY.search(str(k))
        }
    if isinstance(value, list):
        return [clean(v, secrets, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and len(secret) >= 6:
                value = value.replace(secret, "[redacted]")
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
            "[redacted:key]",
            value,
        )
        value = re.sub(
            r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}", "[redacted]", value
        )
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value)
        value = re.sub(
            r"(?i)((?:api[_-]?key|password|passwd|secret|access_token|auth_token|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+",
            r"\1[redacted]",
            value,
        )
        value = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[redacted]@", value)
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "[truncated:text]"
    return value if value is None or isinstance(value, int | float | bool) else "[unsupported]"


def project_for(config, cwd):
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    actual = Path(cwd).resolve()
    matches = [p for p in config["projects"] if actual.is_relative_to(Path(p["path"]).resolve())]
    return max(matches, key=lambda p: len(p["path"])) if matches else None


class CaptureStore:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["stateDir"])
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("state_symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise ValueError("private_state_required")
        self.path = self.root / "capture.sqlite"
        if self.path.is_symlink():
            raise ValueError("database_symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, native_id TEXT, project TEXT, current_turn TEXT,
                    started REAL, updated REAL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, session TEXT,
                    turn TEXT, kind TEXT, received REAL, payload TEXT);
                CREATE INDEX IF NOT EXISTS events_turn ON events(session, turn, seq);
                CREATE TABLE IF NOT EXISTS experiences (
                    id TEXT PRIMARY KEY, session_id TEXT, project TEXT, turn TEXT,
                    created REAL, evidence TEXT, summary TEXT, status TEXT,
                    reason TEXT, memory_ids TEXT, readback TEXT, attempts INTEGER DEFAULT 0,
                    next_attempt REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS health (id TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS delivery_observations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, experience TEXT, observed REAL,
                    status TEXT, reason TEXT);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=0.5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def experiences(self, limit=100):
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM experiences ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.decode(row) for row in rows]

    @staticmethod
    def decode(row):
        result = dict(row)
        for key in ("evidence", "memory_ids", "readback"):
            result[key] = json.loads(result[key])
        result["qualification"] = "observed_unverified"
        return result

    def reconcile(self, identifier):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, reason FROM experiences WHERE id=?", (identifier,)
            ).fetchone()
            if row is None or row["status"] not in {"invalid", "pending_reconcile", "sending"}:
                raise ValueError("not_reconcilable")
            db.execute(
                "INSERT INTO delivery_observations VALUES (NULL, ?, ?, ?, ?)",
                (identifier, time.time(), row["status"], row["reason"]),
            )
            # Explicit recheck is read-only remotely: NEVER put an attempted POST back to pending.
            db.execute(
                "UPDATE experiences SET status='pending_reconcile', next_attempt=0 WHERE id=?",
                (identifier,),
            )

    def health(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO health VALUES (?, ?)", (key, canonical(value)))


def receive(config, payload):
    if not config["enabled"] or not isinstance(payload, dict):
        return None
    kind = payload.get("hook_event_name")
    session = payload.get("session_id")
    project = project_for(config, payload.get("cwd"))
    if (
        kind not in EVENTS
        or not project
        or not isinstance(session, str)
        or not 1 <= len(session) <= 200
    ):
        return None
    allowed = (
        "prompt",
        "prompt_id",
        "tool_use_id",
        "tool_name",
        "tool_input",
        "tool_response",
        "error",
        "is_interrupt",
        "last_assistant_message",
        "reason",
        "source",
        "stop_hook_active",
    )
    body = {k: payload[k] for k in allowed if k in payload}
    tool_input = body.get("tool_input", {})
    if isinstance(tool_input, dict):
        sensitive = any(
            SECRET_FILE.search(str(tool_input.get(k, ""))) for k in ("file_path", "path")
        )
        # Outside project reads can contain personal data even within an opted-in session.
        file_path = tool_input.get("file_path") or tool_input.get("path")
        if file_path and isinstance(file_path, str):
            file_path = Path(payload["cwd"]) / file_path
            sensitive |= not file_path.resolve().is_relative_to(Path(project["path"]).resolve())
        command = tool_input.get("command", "")
        if isinstance(command, str) and re.search(
            r"(?:\.env\b|\.credentials|id_rsa|printenv|\benv\b)", command
        ):
            sensitive = True
        if sensitive:
            for key in ("tool_input", "tool_response", "error"):
                if key in body:
                    body[key] = "[redacted:sensitive_tool]"
    env_secrets = [v for k, v in os.environ.items() if SECRET_KEY.search(k)]
    body = clean(body, [*config.get("redactValues", []), *env_secrets])
    if len(canonical(body).encode()) > MAX_PAYLOAD:
        body = {
            "tool_name": body.get("tool_name"),
            "tool_use_id": body.get("tool_use_id"),
            "capture_issue": "truncated:payload",
            "excerpt": canonical(body)[:MAX_TEXT],
        }
    store = CaptureStore(config)
    if store.path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("capture_storage_limit")
    session_key = digest([str(Path(project["path"]).resolve()), session])
    now = time.time()
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_key,)).fetchone()
        turn = row["current_turn"] if row else "orphan"
        # A delayed result belongs to the turn that started that native tool call.
        # Do not attach it to a newer prompt that happened to arrive first.
        if kind in {"PostToolUse", "PostToolUseFailure"} and body.get("tool_use_id"):
            starts = db.execute(
                "SELECT turn, payload FROM events WHERE session=? AND kind='PreToolUse' ORDER BY seq DESC",
                (session_key,),
            ).fetchall()
            for start in starts:
                if json.loads(start["payload"]).get("tool_use_id") == body["tool_use_id"]:
                    turn = start["turn"]
                    break
        if kind == "UserPromptSubmit":
            # Without a native prompt ID, identical text may be a legitimate new turn.
            turn = str(payload.get("prompt_id") or uuid.uuid4().hex)
        identity = body.get("tool_use_id") or (
            body.get("prompt_id") if kind == "UserPromptSubmit" else None
        )
        identifier = digest([session_key, turn, kind, identity, body])
        if kind == "UserPromptSubmit" and not identity:
            identifier = digest([identifier, turn])
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET current_turn=excluded.current_turn, updated=excluded.updated",
            (
                session_key,
                session,
                project["name"],
                turn if kind == "UserPromptSubmit" or row is None else row["current_turn"],
                now,
                now,
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO events (id, session, turn, kind, received, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (identifier, session_key, turn, kind, now, canonical(body)),
        )
    return identifier
