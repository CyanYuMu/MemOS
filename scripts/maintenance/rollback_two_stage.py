#!/usr/bin/env python3
"""Roll back the coarse stage of a two-stage add (T3.5).

Deletes only ``stage='coarse'`` nodes belonging to the given business task id;
fine-stage results are left untouched. Dry-run by default; idempotent.

Usage::

    python scripts/maintenance/rollback_two_stage.py --task-id <task_id>
    python scripts/maintenance/rollback_two_stage.py --task-id <task_id> --apply
"""

from __future__ import annotations

import argparse
import os
import sys

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


_SELECT = """
MATCH (n:Memory)
WHERE n.task_id = $task_id
  AND n.stage = 'coarse'
  AND ($tenant IS NULL OR n.user_name = $tenant)
RETURN n.id AS id
"""

_DELETE = """
UNWIND $ids AS id
MATCH (n:Memory {id: id})
WHERE n.stage = 'coarse'
DETACH DELETE n
"""


def rollback(
    query_runner: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    *,
    task_id: str,
    apply: bool,
    tenant: str | None = None,
    vector_cleanup: Callable[[list[str]], None] | None = None,
) -> dict[str, int]:
    rows = query_runner(_SELECT, {"task_id": task_id, "tenant": tenant})
    ids = [str(row["id"]) for row in rows]
    if apply and ids:
        query_runner(_DELETE, {"ids": ids})
        if vector_cleanup is not None:
            vector_cleanup(ids)
        # Re-query to report what actually disappeared (idempotent).
        remaining = query_runner(_SELECT, {"task_id": task_id, "tenant": tenant})
        deleted = len(ids) - len(remaining)
    else:
        deleted = 0
    return {"matched": len(ids), "deleted": deleted}


def _vector_cleanup(ids: list[str]) -> None:
    """Remove rolled-back points from Qdrant; no-op when it is not configured."""
    url = os.getenv("QDRANT_URL")
    collection = os.getenv("QDRANT_COLLECTION")
    if not url or not collection:
        print("[rollback-two-stage] QDRANT_URL/QDRANT_COLLECTION unset; vectors left in place")
        return
    from qdrant_client import QdrantClient

    client = QdrantClient(url=url, api_key=os.getenv("QDRANT_API_KEY"))
    client.delete(collection_name=collection, points_selector=ids)


def _run(task_id: str, apply: bool, tenant: str | None) -> dict[str, int]:
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
        return rollback(
            runner,
            task_id=task_id,
            apply=apply,
            tenant=tenant,
            vector_cleanup=_vector_cleanup if apply else None,
        )
    finally:
        driver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True, help="Business task id")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    parser.add_argument("--tenant", default=None, help="Only this user_name/cube")
    args = parser.parse_args(argv)

    stats = _run(args.task_id, args.apply, args.tenant)
    print(
        f"[rollback-two-stage] mode={'APPLY' if args.apply else 'DRY-RUN'} "
        f"task={args.task_id} stats={stats}"
    )
    if not args.apply:
        print("[rollback-two-stage] dry run only; re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
