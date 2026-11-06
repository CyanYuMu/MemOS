#!/usr/bin/env python3
"""Expire memories whose ``expires_at`` has passed (T2.5 cleanup task).

Can run as a scheduled job. Dry-run by
default; ``--apply`` marks matching nodes ``status='expired'``. Frozen nodes are
never touched and already-expired nodes are skipped, so the job is idempotent.

Usage::

    python scripts/maintenance/expire_memories.py             # dry run
    python scripts/maintenance/expire_memories.py --apply
    python scripts/maintenance/expire_memories.py --apply --tenant <cube_id>
"""

from __future__ import annotations

import argparse
import os
import sys

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


BATCH_SIZE = 500

_SELECT = """
MATCH (n:Memory)
WHERE n.expires_at IS NOT NULL
  AND n.expires_at <= $now
  AND n.status IN ['activated', 'archived']
  AND ($tenant IS NULL OR n.user_name = $tenant)
  AND ($last_id IS NULL OR n.id > $last_id)
RETURN n.id AS id
ORDER BY n.id
LIMIT $limit
"""

_UPDATE = """
UNWIND $ids AS id
MATCH (n:Memory {id: id})
WHERE n.status IN ['activated', 'archived']
SET n.status = 'expired',
    n.updated_at = datetime($now)
"""


def expire_batch(
    query_runner: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    *,
    apply: bool,
    tenant: str | None = None,
    now: str | None = None,
    batch_size: int = BATCH_SIZE,
    vector_sync: Callable[[list[str], str], None] | None = None,
) -> dict[str, int]:
    cutoff = now or datetime.now(timezone.utc).isoformat()
    matched = 0
    expired = 0
    last_id: str | None = None

    while True:
        rows = query_runner(
            _SELECT,
            {"now": cutoff, "tenant": tenant, "limit": batch_size, "last_id": last_id},
        )
        if not rows:
            break
        ids = [str(row["id"]) for row in rows]
        matched += len(ids)
        last_id = ids[-1]
        if apply:
            query_runner(_UPDATE, {"ids": ids, "now": cutoff})
            if vector_sync is not None:
                # The raw Cypher above bypasses the graph adapter's vector
                # sync; without this the vector payload keeps status=activated
                # and expired memories stay recallable.
                vector_sync(ids, cutoff)
            expired += len(ids)
        if len(rows) < batch_size:
            break

    return {"matched": matched, "expired": expired}


def _vector_sync(ids: list[str], cutoff: str) -> None:
    """Mirror the ``status=expired`` update into Qdrant when configured."""
    url = os.getenv("QDRANT_URL")
    collection = os.getenv("QDRANT_COLLECTION")
    if not url or not collection:
        print("[expire-memories] QDRANT_URL/QDRANT_COLLECTION unset; vector payloads not synced")
        return
    from qdrant_client import QdrantClient

    client = QdrantClient(url=url, api_key=os.getenv("QDRANT_API_KEY"))
    client.set_payload(
        collection_name=collection,
        payload={"status": "expired", "updated_at": cutoff},
        points=ids,
    )


def _run(apply: bool, tenant: str | None) -> dict[str, int]:
    from neo4j import GraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    database = os.getenv("NEO4J_DB_NAME") or None

    driver = GraphDatabase.driver(uri, auth=(user, password))

    def runner(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with driver.session(database=database) as session:
            return [dict(record) for record in session.run(query, **params)]

    try:
        return expire_batch(
            runner,
            apply=apply,
            tenant=tenant,
            vector_sync=_vector_sync if apply else None,
        )
    finally:
        driver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    parser.add_argument("--tenant", default=None, help="Only this user_name/cube")
    args = parser.parse_args(argv)

    stats = _run(args.apply, args.tenant)
    print(f"[expire-memories] mode={'APPLY' if args.apply else 'DRY-RUN'} stats={stats}")
    if not args.apply:
        print("[expire-memories] dry run only; re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
