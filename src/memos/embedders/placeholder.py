"""Deterministic embedder for dry runs and tests.

The prompt trial run (``/product/prompts/test``) must not need a real embedding
model: the vectors it computes are never stored, compared, or returned. This
backend turns text into a stable vector so the real extraction code can run
unchanged with only an LLM configured.

It is not a semantic embedding. Never use it where similarity matters: set
``MOS_EMBEDDER_BACKEND=placeholder`` only for dry runs and tests.
"""

import hashlib
import struct

from memos.configs.embedder import PlaceholderEmbedderConfig
from memos.embedders.base import BaseEmbedder


DEFAULT_DIMENSION = 8


class PlaceholderEmbedder(BaseEmbedder):
    """Map each text to a stable pseudo-random unit vector."""

    def __init__(self, config: PlaceholderEmbedderConfig):
        self.config = config
        self.dimension = config.embedding_dims or DEFAULT_DIMENSION

    def _vector(self, text: str) -> list[float]:
        values: list[float] = []
        seed = (text or "").encode("utf-8")
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(digest), 4):
                if len(values) >= self.dimension:
                    break
                (raw,) = struct.unpack(">i", digest[offset : offset + 4])
                values.append(raw / 2**31)
            counter += 1
        return values

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one stable vector per text."""
        return [self._vector(text) for text in texts]
