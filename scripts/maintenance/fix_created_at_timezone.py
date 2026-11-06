"""Normalize historical ``created_at`` / ``updated_at`` to true UTC.

Before the 2026-10 timezone fix (``docs/cloud-能力补齐-难度分级.md`` T1.3) the
deployed image wrote **Beijing wall-clock values with a ``+00:00`` suffix** for
ordinary memory writes. Dream products were already true UTC, so the two
lineages now disagree by eight hours.

This script rewrites the ordinary lineage to true UTC (the new code baseline):

* only nodes with a ``+00:00`` timestamp are touched;
* Dream memory types are skipped (already true UTC);
* nodes carrying ``time_basis='utc'`` are skipped (idempotent);
* only nodes created before ``--before`` are touched, so it is safe to run after
  the new code has started writing real UTC values;
* the Qdrant payload copy is updated in the same pass (the payload is a copy of
  the Neo4j metadata, so a graph-only fix leaves search results stale).

Runs inside the memos pod so it reuses the live Neo4j / Qdrant config. Dry-run
unless ``--apply`` is passed.

Usage::

    python fix_created_at_timezone.py --before 2026-10-21T00:00:00+08:00
    python fix_created_at_timezone.py --before ... --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path

from memos.api.handlers.config_builders import build_graph_db_config
from memos.graph_dbs.factory import GraphStoreFactory


DREAM_TYPES = ["DreamDiary", "Context", "InsightMemory"]
BACKUP_PATH = Path("/tmp/timezone_fix/backup.jsonl")
BATCH_SIZE = 500
# Neo4j returns nanosecond precision; Python's fromisoformat stops at 6 digits.
_NANOSECOND_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        # neo4j.time.DateTime is not a datetime subclass but exposes to_native().
        to_native = getattr(value, "to_native", None)
        if callable(to_native):
            parsed = to_native()
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            text = _NANOSECOND_FRACTION.sub(r"\1", text)
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _shift(value, offset: timedelta) -> str | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return (parsed - offset).astimezone(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before",
        required=True,
        help=(
            "Only rewrite timestamps strictly before this instant (ISO 8601). "
            "Use the moment the new UTC-writing code was deployed."
        ),
    )
    parser.add_argument(
        "--offset-hours",
        type=float,
        default=8.0,
        help="Hours to subtract from the fake-UTC lineage (default: 8).",
    )
    parser.add_argument("--apply", action="store_true", help="Write the changes.")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N candidate nodes (0 = no limit, for sampling).",
    )
    args = parser.parse_args()

    cutoff = _parse_dt(args.before)
    if cutoff is None:
        print(f"invalid --before value: {args.before!r}", file=sys.stderr)
        return 2
    offset = timedelta(hours=args.offset_hours)

    graph_db = GraphStoreFactory.from_config(build_graph_db_config())
    session = graph_db.driver.session(database=graph_db.db_name)
    vec_db = getattr(graph_db, "vec_db", None)

    rows = list(
        session.run(
            """
            MATCH (n:Memory)
            WHERE n.created_at IS NOT NULL
              AND coalesce(n.memory_type, '') NOT IN $dream_types
              AND coalesce(n.time_basis, '') <> 'utc'
            RETURN n.id AS id, n.user_name AS user_name,
                   n.memory_type AS memory_type,
                   n.created_at AS created_at, n.updated_at AS updated_at
            """,
            dream_types=DREAM_TYPES,
        )
    )

    candidates = []
    skipped_no_suffix = 0
    skipped_after_cutoff = 0
    for row in rows:
        created = _parse_dt(row["created_at"])
        if created is None:
            skipped_no_suffix += 1
            continue
        if created >= cutoff:
            skipped_after_cutoff += 1
            continue
        new_created = _shift(row["created_at"], offset)
        new_updated = _shift(row["updated_at"], offset) if row["updated_at"] else None
        if new_created is None:
            skipped_no_suffix += 1
            continue
        candidates.append(
            {
                "id": row["id"],
                "user_name": row["user_name"],
                "memory_type": row["memory_type"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                "new_created_at": new_created,
                "new_updated_at": new_updated,
            }
        )
        if args.limit and len(candidates) >= args.limit:
            break

    by_type: dict[str, int] = {}
    for item in candidates:
        by_type[item["memory_type"] or "(none)"] = (
            by_type.get(item["memory_type"] or "(none)", 0) + 1
        )

    print(f"mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"cutoff: {cutoff.isoformat()}  offset: -{args.offset_hours}h")
    print(f"rows scanned: {len(rows)}")
    print(f"skipped (no parseable timestamp): {skipped_no_suffix}")
    print(f"skipped (created_at >= cutoff): {skipped_after_cutoff}")
    print(f"candidates: {len(candidates)}  by type: {by_type}")
    for item in candidates[:10]:
        print(
            f"  {item['memory_type']:<18}{item['id']}  "
            f"{item['created_at']} -> {item['new_created_at']}"
        )

    if not args.apply or not candidates:
        session.close()
        return 0

    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BACKUP_PATH.open("w", encoding="utf-8") as fh:
        for item in candidates:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"backup -> {BACKUP_PATH}")

    updated = 0
    vec_failed: list[str] = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        session.run(
            """
            UNWIND $rows AS row
            MATCH (n:Memory {id: row.id})
            SET n.created_at = datetime(row.new_created_at),
                n.updated_at = CASE
                    WHEN row.new_updated_at IS NULL THEN n.updated_at
                    ELSE datetime(row.new_updated_at)
                END,
                n.time_basis = 'utc'
            """,
            rows=batch,
        )

        if vec_db is not None:
            for item in batch:
                payload = {"time_basis": "utc", "created_at": item["new_created_at"]}
                if item["new_updated_at"]:
                    payload["updated_at"] = item["new_updated_at"]
                try:
                    vec_db.update(item["id"], {"id": item["id"], "payload": payload})
                except Exception as exc:
                    vec_failed.append(f"{item['id']}: {exc}")
        updated += len(batch)
        print(f"  rewritten {updated}/{len(candidates)}")

    print(f"\nneo4j updated: {updated}")
    if vec_db is None:
        print("vector payload: SKIPPED (backend has no vec_db)")
    else:
        print(f"vector payload failures: {len(vec_failed)}")
        for failure in vec_failed[:20]:
            print(f"  FAILED {failure}")
    session.close()
    return 1 if vec_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
