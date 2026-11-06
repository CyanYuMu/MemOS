#!/usr/bin/env python3
"""Backfill owner fields for the memory graph and the vector index (T3.4).

Every memory gets exactly one owner. Existing nodes predate that field, so they
are backfilled as follows:

* ``user_id`` present  -> ``owner_type='user'``, ``owner_id=<user_id>``;
* no ``user_id``       -> ``owner_type='public'``, ``owner_id=<user_name>``
  (tenant-wide / reorganizer nodes).

Using the tenant as ``owner_id`` for private nodes would make every participant
of the cube see every legacy memory as "their own" after enforcement, so it is
deliberately avoided.

Dry-run is the default; pass ``--apply`` to write. The script only touches
nodes/points where ``owner_type`` is missing, so it is idempotent and safe to
re-run.

Usage::

    export NEO4J_URI=bolt://... NEO4J_USER=... NEO4J_PASSWORD=...
    export QDRANT_URL=http://... QDRANT_API_KEY=... QDRANT_COLLECTION=memos
    python scripts/maintenance/backfill_owner_fields.py            # dry run
    python scripts/maintenance/backfill_owner_fields.py --apply    # write
    python scripts/maintenance/backfill_owner_fields.py --tenant <cube_id>

Rollback: the backfill is additive. To undo, remove the ``owner_type`` /
``owner_id`` properties from the affected nodes/points (no data is deleted).
"""

from __future__ import annotations

import argparse
import os
import sys

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


BATCH_SIZE = 500


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


# ── Neo4j ────────────────────────────────────────────────────────────────────

_NEO4J_SELECT = """
MATCH (n:Memory)
WHERE n.owner_type IS NULL
  AND ($tenant IS NULL OR n.user_name = $tenant)
  AND ($last_id IS NULL OR n.id > $last_id)
RETURN n.id AS id, n.user_name AS user_name, n.user_id AS user_id
ORDER BY n.id
LIMIT $limit
"""

_NEO4J_UPDATE = """
UNWIND $rows AS row
MATCH (n:Memory {id: row.id})
WHERE n.owner_type IS NULL
SET n.owner_type = row.owner_type,
    n.owner_id = row.owner_id
"""


def plan_owner(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(owner_type, owner_id)`` for one legacy node/payload."""
    user_id = row.get("user_id")
    if user_id:
        return "user", str(user_id)
    tenant = row.get("user_name")
    if tenant:
        return "public", str(tenant)
    return None


def plan_neo4j_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    planned = []
    for row in rows:
        owner = plan_owner(row)
        if owner is None:
            continue
        owner_type, owner_id = owner
        planned.append({"id": row["id"], "owner_type": owner_type, "owner_id": owner_id})
    return planned


def backfill_neo4j(
    query_runner: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    *,
    apply: bool,
    tenant: str | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[str, int]:
    """Backfill Neo4j nodes in batches.

    ``query_runner(query, params)`` must return a list of record dicts.
    """
    scanned = 0
    planned = 0
    written = 0
    last_id: str | None = None
    while True:
        rows = query_runner(
            _NEO4J_SELECT, {"tenant": tenant, "limit": batch_size, "last_id": last_id}
        )
        if not rows:
            break
        scanned += len(rows)
        last_id = str(rows[-1]["id"])
        batch = plan_neo4j_rows(rows)
        planned += len(batch)
        if apply and batch:
            query_runner(_NEO4J_UPDATE, {"rows": batch})
            written += len(batch)
        if len(rows) < batch_size:
            break
    return {"scanned": scanned, "planned": planned, "written": written}


# ── Qdrant ───────────────────────────────────────────────────────────────────


def backfill_qdrant(
    client: Any,
    collection: str,
    *,
    apply: bool,
    tenant: str | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[str, int]:
    """Backfill vector payloads missing ``owner_type``.

    Points are filtered by ``user_name`` when ``tenant`` is given, so a
    single-tenant run never rewrites another tenant's payloads.
    """
    scanned = 0
    planned = 0
    written = 0

    from qdrant_client.http import models as qmodels

    must: list[Any] = [qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="owner_type"))]
    if tenant:
        must.append(qmodels.FieldCondition(key="user_name", match=qmodels.MatchValue(value=tenant)))

    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=qmodels.Filter(must=must),
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        scanned += len(points)
        planned += len(points)
        if apply:
            for point in points:
                payload = dict(point.payload or {})
                owner = plan_owner(payload)
                if owner is None:
                    continue
                owner_type, owner_id = owner
                client.set_payload(
                    collection_name=collection,
                    payload={"owner_type": owner_type, "owner_id": owner_id},
                    points=[point.id],
                )
                written += 1
        if offset is None:
            break
    return {"scanned": scanned, "planned": planned, "written": written}


# ── CLI ──────────────────────────────────────────────────────────────────────


def _run_neo4j(apply: bool, tenant: str | None) -> dict[str, int]:
    from neo4j import GraphDatabase

    uri = _env("NEO4J_URI", "bolt://localhost:7687")
    user = _env("NEO4J_USER", "neo4j")
    password = _env("NEO4J_PASSWORD", "")
    database = _env("NEO4J_DB_NAME")

    driver = GraphDatabase.driver(uri, auth=(user, password))

    def runner(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with driver.session(database=database) as session:
            return [dict(record) for record in session.run(query, **params)]

    try:
        return backfill_neo4j(runner, apply=apply, tenant=tenant)
    finally:
        driver.close()


def _run_qdrant(apply: bool, tenant: str | None) -> dict[str, int]:
    from qdrant_client import QdrantClient

    url = _env("QDRANT_URL")
    collection = _env("QDRANT_COLLECTION")
    if not url or not collection:
        return {"scanned": 0, "planned": 0, "written": 0}
    client = QdrantClient(url=url, api_key=_env("QDRANT_API_KEY"))
    return backfill_qdrant(client, collection, apply=apply, tenant=tenant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    parser.add_argument("--tenant", default=None, help="Only backfill this user_name/cube")
    args = parser.parse_args(argv)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[backfill-owner] mode={mode} tenant={args.tenant or '*'}")

    neo4j_stats = _run_neo4j(args.apply, args.tenant)
    print(f"[backfill-owner] neo4j: {neo4j_stats}")

    qdrant_stats = _run_qdrant(args.apply, args.tenant)
    print(f"[backfill-owner] qdrant: {qdrant_stats}")

    if not args.apply:
        print("[backfill-owner] dry run only; re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
