"""Raw conversation messages behind ``/product/get_message`` (cloud ``get/message``).

Memories only keep what extraction distilled; this store keeps the messages exactly as
they were sent to ``add`` so a caller can read a conversation back. Messages are never
indexed for recall: the graph backend stores them as ``:RawMessage`` nodes (not
``:Memory``) and the file fallback lives outside every vector collection.

Messages are written by default; ``MEMOS_STORE_RAW_MESSAGES=false`` turns writing off.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)

MESSAGE_LABEL = "RawMessage"
DEFAULT_STORE_DIR = "/data/memos/messages"


def raw_message_storage_enabled() -> bool:
    value = os.getenv("MEMOS_STORE_RAW_MESSAGES", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def normalize_messages(messages: Any) -> list[dict[str, Any]]:
    """Keep the text of each message; parts without text (images, files) are dropped."""
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        return []

    normalized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, str):
            message = {"role": "user", "content": message}
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in content
            )
        if not isinstance(content, str) or not content.strip():
            continue
        normalized.append(
            {
                "role": str(message.get("role") or "user"),
                "role_name": message.get("role_name") or None,
                "content": content,
                "chat_time": str(message["chat_time"]) if message.get("chat_time") else None,
            }
        )
    return normalized


_order_lock = threading.Lock()
_last_order = 0


def _next_order() -> int:
    """Strictly increasing per process, so batches added in the same millisecond keep their order.

    Across replicas the order is only as good as their clocks: a replica running behind
    writes smaller values and its newest messages can fall out of the latest-N window.
    """
    global _last_order
    with _order_lock:
        _last_order = max(time.time_ns(), _last_order + 1)
        return _last_order


def message_id_for(conversation_id: str, role: str, content: str) -> str:
    """Stable id for one message, so a memory source can point back at it.

    Deterministic on purpose: the reader fills ``sources[].message_id`` with the same
    value for the same message. Identical text sent twice in one conversation shares
    an id; that is the price of not threading a generated id through extraction.
    """
    seed = f"{conversation_id}\x00{role}\x00{content}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _build_rows(
    user_name: str,
    user_ids: list[str],
    conversation_id: str,
    messages: list[dict[str, Any]],
    task_id: str | None,
) -> list[dict[str, Any]]:
    order = _next_order()
    now_ms = order // 1_000_000
    return [
        {
            "message_id": message_id_for(
                conversation_id, message.get("role", ""), message["content"]
            ),
            "user_name": user_name,
            "user_ids": user_ids,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "order": order,
            "seq": seq,
            "create_time": now_ms,
            **message,
        }
        for seq, message in enumerate(messages)
    ]


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("message_id", "role", "role_name", "content", "chat_time", "create_time")
    return {key: row[key] for key in keys if row.get(key) is not None}


def _require_scope(user_names: list[str] | None, conversation_id: str | None) -> None:
    if user_names is None and conversation_id is None:
        raise ValueError("delete needs user_names or conversation_id")


class MessageStore(ABC):
    """Messages keyed by tenant (``user_name``), conversation and participating users."""

    @abstractmethod
    def add(
        self,
        *,
        user_name: str,
        user_ids: list[str],
        conversation_id: str,
        messages: list[dict[str, Any]],
        task_id: str | None = None,
    ) -> int: ...

    @abstractmethod
    def list(
        self, *, user_name: str, user_id: str, conversation_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """The latest ``limit`` messages of the conversation, returned oldest first (cloud behaviour)."""

    @abstractmethod
    def delete(
        self, *, user_names: list[str] | None = None, conversation_id: str | None = None
    ) -> int:
        """Drop whole tenants, or one conversation of each; ``user_names=None`` means every tenant.

        At least one of the two must be given. Returns the count removed.
        """


class Neo4jMessageStore(MessageStore):
    def __init__(self, driver: Any, database: str | None = None):
        self.driver = driver
        self.database = database
        self._indexed = False

    def _session(self):
        if self.database:
            return self.driver.session(database=self.database)
        return self.driver.session()

    def _ensure_index(self) -> None:
        if self._indexed:
            return
        self._indexed = True
        query = (
            f"CREATE INDEX raw_message_scope IF NOT EXISTS "
            f"FOR (m:{MESSAGE_LABEL}) ON (m.user_name, m.conversation_id)"
        )
        try:
            with self._session() as session:
                session.run(query).consume()
        except Exception:
            logger.warning("Could not create the raw message index", exc_info=True)

    def add(
        self,
        *,
        user_name: str,
        user_ids: list[str],
        conversation_id: str,
        messages: list[dict[str, Any]],
        task_id: str | None = None,
    ) -> int:
        rows = _build_rows(user_name, user_ids, conversation_id, messages, task_id)
        if not rows:
            return 0
        self._ensure_index()
        query = f"UNWIND $rows AS row CREATE (m:{MESSAGE_LABEL}) SET m = row"
        with self._session() as session:
            session.run(query, rows=rows).consume()
        return len(rows)

    def list(
        self, *, user_name: str, user_id: str, conversation_id: str, limit: int
    ) -> list[dict[str, Any]]:
        query = (
            f"MATCH (m:{MESSAGE_LABEL} {{user_name: $user_name, conversation_id: $conversation_id}}) "
            "WHERE $user_id IN m.user_ids "
            "RETURN m ORDER BY m.order DESC, m.seq DESC LIMIT $limit"
        )
        with self._session() as session:
            records = list(
                session.run(
                    query,
                    user_name=user_name,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    limit=limit,
                )
            )
        return [_public_view(dict(record["m"])) for record in reversed(records)]

    def delete(
        self, *, user_names: list[str] | None = None, conversation_id: str | None = None
    ) -> int:
        _require_scope(user_names, conversation_id)
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if user_names is not None:
            clauses.append("m.user_name IN $user_names")
            params["user_names"] = user_names
        if conversation_id is not None:
            clauses.append("m.conversation_id = $conversation_id")
            params["conversation_id"] = conversation_id
        query = f"MATCH (m:{MESSAGE_LABEL}) WHERE {' AND '.join(clauses)} DETACH DELETE m"
        with self._session() as session:
            summary = session.run(query, **params).consume()
        return int(summary.counters.nodes_deleted) if summary.counters else 0


class JsonlMessageStore(MessageStore):
    """File fallback for deployments without a Neo4j driver: one JSONL file per conversation.

    The lock is per process, so this only suits a single worker; with several workers
    sharing the directory a large append can interleave and ``delete`` can race ``add``.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("MEMOS_MESSAGE_STORE_DIR", DEFAULT_STORE_DIR))
        self._lock = threading.RLock()

    @staticmethod
    def _slug(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def _file(self, user_name: str, conversation_id: str) -> Path:
        return self.root / self._slug(user_name) / f"{self._slug(conversation_id)}.jsonl"

    def add(
        self,
        *,
        user_name: str,
        user_ids: list[str],
        conversation_id: str,
        messages: list[dict[str, Any]],
        task_id: str | None = None,
    ) -> int:
        rows = _build_rows(user_name, user_ids, conversation_id, messages, task_id)
        if not rows:
            return 0
        path = self._file(user_name, conversation_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        return len(rows)

    def list(
        self, *, user_name: str, user_id: str, conversation_id: str, limit: int
    ) -> list[dict[str, Any]]:
        path = self._file(user_name, conversation_id)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt message line in %s", path)
                    continue
                if user_id in row.get("user_ids", []):
                    rows.append(row)
        rows.sort(key=lambda row: (row.get("order", 0), row.get("seq", 0)))
        return [_public_view(row) for row in rows[-limit:]]

    def delete(
        self, *, user_names: list[str] | None = None, conversation_id: str | None = None
    ) -> int:
        _require_scope(user_names, conversation_id)
        removed = 0
        with self._lock:
            for target in self._targets(user_names, conversation_id):
                removed += len(target.read_text(encoding="utf-8").splitlines())
                target.unlink()
        return removed

    def _targets(self, user_names: list[str] | None, conversation_id: str | None) -> list[Path]:
        if user_names is None:
            return list(self.root.glob(f"*/{self._slug(conversation_id or '')}.jsonl"))
        targets: list[Path] = []
        for user_name in user_names:
            if conversation_id is None:
                targets.extend((self.root / self._slug(user_name)).glob("*.jsonl"))
            else:
                target = self._file(user_name, conversation_id)
                if target.exists():
                    targets.append(target)
        return targets


def build_message_store(graph_db: Any | None = None) -> MessageStore:
    """Pick the Neo4j store when the cube exposes a driver, else the file one."""
    driver = getattr(graph_db, "driver", None)
    if driver is not None and hasattr(driver, "session"):
        return Neo4jMessageStore(driver, getattr(graph_db, "db_name", None))
    return JsonlMessageStore()
