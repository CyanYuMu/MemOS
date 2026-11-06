"""The public API contract, versioned.

Consumers (the console, the MCP gateway) should not have to remember how a
given deployment happens to serialise its data. Everything they rely on is
declared here and served from ``GET /product/contract``; a consumer refuses to
serve traffic when it sees a version it does not know.

Bump ``CONTRACT_VERSION`` whenever a change here would break an existing
consumer. Additive changes do not need a bump, but they should still show up in
``contract_document`` so the consumer can discover them.
"""

from __future__ import annotations

from typing import Any

from memos.time_basis import TIME_FIELDS


#: Version of the wire contract. See the module docstring.
CONTRACT_VERSION = 2

#: Cloud recall cap; the request model rejects anything larger.
TOP_K_MAX = 25


def contract_document() -> dict[str, Any]:
    """Machine-readable description of the contract, for startup self-checks."""
    return {
        "contract_version": CONTRACT_VERSION,
        # Every timestamp in every response is RFC 3339 real UTC. Nodes written
        # before the T1.3 fix are converted at the API boundary from
        # ``metadata.time_basis``; consumers never apply an offset.
        "time": {
            "basis": "utc",
            "format": "rfc3339",
            "fields": list(TIME_FIELDS),
            "legacy_marker": "metadata.time_basis",
        },
        # `metadata.sources` may be hundreds of megabytes; it is omitted unless
        # the caller sets `include_sources`.
        "sources": {"default": "omitted", "include_with": "include_sources"},
        # Reads accept the object shape; the legacy bare array still works.
        "get_memory_by_ids": {
            "body": "object",
            "fields": ["memory_ids", "include_sources"],
            "accepts_legacy_array": True,
            "mem_cube_id": "query",
        },
        # Parameter errors are HTTP 4xx, never `200` with `data.status=failure`.
        "errors": {"business_failures": "http_4xx"},
        "top_k_max": TOP_K_MAX,
    }
