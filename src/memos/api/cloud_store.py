"""Small metadata store for cloud-parity entities (knowledge bases, profiles).

These entities are not memories: they must never show up in memory recall, so
they are stored outside the ``Memory`` label / ``text_mem`` collections.

Two backends are provided:

* :class:`Neo4jMetadataStore` — production. Uses the graph driver already held
  by the memory cube and stores each entity as a ``:CloudEntity`` node whose
  ``document`` property is JSON.
* :class:`JsonMetadataStore` — fallback for deployments without a Neo4j driver
  and for tests. One JSON file per entity under ``MEMOS_CLOUD_STORE_DIR``.

The interface is intentionally tiny: ``put`` / ``get`` / ``list`` / ``delete``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)

ENTITY_LABEL = "CloudEntity"
DEFAULT_STORE_DIR = "/data/memos/cloud"


class MetadataStore(ABC):
    """Tenant-scoped document store keyed by ``(kind, entity_id)``."""

    @abstractmethod
    def put(self, kind: str, document: dict[str, Any]) -> None: ...

    @abstractmethod
    def get(self, kind: str, entity_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, kind: str, user_name: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def delete(self, kind: str, entity_id: str) -> bool: ...


class Neo4jMetadataStore(MetadataStore):
    """Store entities as ``:CloudEntity`` nodes in the existing Neo4j database."""

    def __init__(self, driver: Any, database: str | None = None):
        self.driver = driver
        self.database = database

    def _session(self):
        if self.database:
            return self.driver.session(database=self.database)
        return self.driver.session()

    def put(self, kind: str, document: dict[str, Any]) -> None:
        props = {
            "entity_id": document["entity_id"],
            "entity_kind": kind,
            "user_name": document.get("user_name") or "",
            "created_at": document.get("created_at") or "",
            "updated_at": document.get("updated_at") or "",
            "document": json.dumps(document, ensure_ascii=False, default=str),
        }
        query = f"MERGE (n:{ENTITY_LABEL} {{entity_id: $entity_id}}) SET n += $props"
        with self._session() as session:
            session.run(query, entity_id=document["entity_id"], props=props)

    def get(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        query = (
            f"MATCH (n:{ENTITY_LABEL} {{entity_id: $entity_id, entity_kind: $kind}}) "
            "RETURN n.document AS document"
        )
        with self._session() as session:
            record = session.run(query, entity_id=entity_id, kind=kind).single()
        return _parse_document(record["document"]) if record else None

    def list(self, kind: str, user_name: str | None = None) -> list[dict[str, Any]]:
        if user_name is None:
            query = f"MATCH (n:{ENTITY_LABEL} {{entity_kind: $kind}}) RETURN n.document AS document"
            params: dict[str, Any] = {"kind": kind}
        else:
            query = (
                f"MATCH (n:{ENTITY_LABEL} {{entity_kind: $kind, user_name: $user_name}}) "
                "RETURN n.document AS document"
            )
            params = {"kind": kind, "user_name": user_name}

        with self._session() as session:
            records = list(session.run(query, **params))

        documents = [
            doc for record in records if (doc := _parse_document(record["document"])) is not None
        ]
        documents.sort(key=lambda doc: str(doc.get("created_at") or ""), reverse=True)
        return documents

    def delete(self, kind: str, entity_id: str) -> bool:
        query = (
            f"MATCH (n:{ENTITY_LABEL} {{entity_id: $entity_id, entity_kind: $kind}}) "
            "DETACH DELETE n"
        )
        with self._session() as session:
            result = session.run(query, entity_id=entity_id, kind=kind)
            summary = result.consume()
        return bool(summary.counters and summary.counters.nodes_deleted)


class JsonMetadataStore(MetadataStore):
    """File-backed fallback store (also used by tests)."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("MEMOS_CLOUD_STORE_DIR", DEFAULT_STORE_DIR))
        self._lock = threading.RLock()

    def _path(self, kind: str, entity_id: str) -> Path:
        safe_kind = "".join(ch for ch in kind if ch.isalnum() or ch in ("_", "-"))
        safe_id = "".join(ch for ch in entity_id if ch.isalnum() or ch in ("_", "-"))
        if not safe_kind or not safe_id:
            raise ValueError("invalid entity kind or id")
        return self.root / safe_kind / f"{safe_id}.json"

    def put(self, kind: str, document: dict[str, Any]) -> None:
        path = self._path(kind, document["entity_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, ensure_ascii=False, default=str)
                os.replace(tmp, path)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise

    def get(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        path = self._path(kind, entity_id)
        if not path.exists():
            return None
        with self._lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Corrupt metadata document: %s", path, exc_info=True)
                return None

    def list(self, kind: str, user_name: str | None = None) -> list[dict[str, Any]]:
        safe_kind = "".join(ch for ch in kind if ch.isalnum() or ch in ("_", "-"))
        directory = self.root / safe_kind
        if not directory.exists():
            return []
        documents: list[dict[str, Any]] = []
        with self._lock:
            for path in directory.glob("*.json"):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    logger.warning("Skipping corrupt metadata document: %s", path)
                    continue
                if user_name is not None and document.get("user_name") != user_name:
                    continue
                documents.append(document)
        documents.sort(key=lambda doc: str(doc.get("created_at") or ""), reverse=True)
        return documents

    def delete(self, kind: str, entity_id: str) -> bool:
        path = self._path(kind, entity_id)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            return True


def _parse_document(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_metadata_store(graph_db: Any | None = None) -> MetadataStore:
    """Pick the Neo4j store when the cube exposes a driver, else the JSON one."""
    driver = getattr(graph_db, "driver", None)
    if driver is not None and hasattr(driver, "session"):
        return Neo4jMetadataStore(driver, getattr(graph_db, "db_name", None))
    return JsonMetadataStore()
