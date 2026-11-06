from __future__ import annotations

import logging

from typing import Any


logger = logging.getLogger(__name__)


def embed_text(context: dict[str, Any] | None, text: str) -> list[float] | None:
    """Embed text for a Dream-owned node, or None when no embedder is usable.

    ``add_node`` only writes to the vector DB when node metadata carries an
    embedding, so a None here leaves the node stored but unsearchable instead
    of failing the write.
    """
    shared = (context or {}).get("shared") or {}
    embedder = shared.get("embedder") or getattr(shared.get("text_mem"), "embedder", None)
    if embedder is None or not text:
        return None
    try:
        return embedder.embed([text])[0]
    except Exception:
        logger.warning(
            "[Dream] embedding failed; node stays out of the vector index.", exc_info=True
        )
        return None
