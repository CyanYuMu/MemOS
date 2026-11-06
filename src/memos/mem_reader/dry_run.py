"""Build a memory reader that needs only an LLM and SQLite.

The prompt trial run and the live prompt tests must exercise the real reader —
same class, same model configuration, same parsing — without a graph database,
a vector store, or Redis. Two dependencies are filled in:

- the embedder: memory items carry a vector, but a trial run never stores or
  compares one, so a deterministic placeholder is enough;
- nothing else: the reader is built from the same config the data plane uses
  for its LLM and chunker.

Each call returns a private reader: a trial wraps its models to capture raw
output, so concurrent runs must not share one instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memos.configs.mem_reader import MemReaderConfigFactory
from memos.mem_reader.multi_modal_struct import MultiModalStructMemReader
from memos.mem_reader.simple_struct import SimpleStructMemReader


if TYPE_CHECKING:
    from memos.mem_reader.base import BaseMemReader


DRY_RUN_BACKENDS: tuple[str, ...] = ("simple_struct", "multimodal_struct")
PLACEHOLDER_EMBEDDER: dict[str, Any] = {"backend": "placeholder", "config": {"embedding_dims": 8}}


def dry_run_reader_config(backend: str = "simple_struct") -> dict[str, Any]:
    """The reader config used by dry runs, shaped like ``mem_reader`` config."""
    from memos.api.config import APIConfig

    if backend not in DRY_RUN_BACKENDS:
        raise ValueError(f"dry runs support {', '.join(DRY_RUN_BACKENDS)}; got {backend!r}")
    return {
        "backend": backend,
        "config": {
            "llm": APIConfig.get_memreader_config(),
            "general_llm": APIConfig.get_memreader_general_llm_config(),
            "embedder": dict(PLACEHOLDER_EMBEDDER),
            "chunker": {
                "backend": "sentence",
                "config": {
                    "save_rawfile": False,
                    "tokenizer_or_token_counter": "gpt2",
                    "chunk_size": 512,
                    "chunk_overlap": 128,
                    "min_sentences_per_chunk": 1,
                },
            },
            "chat_chunker": APIConfig.get_reader_config(),
            "remove_prompt_example": False,
            "oss_config": None,
        },
    }


def build_dry_run_llm():
    """The model trial runs call directly (Dream stages take one explicitly)."""
    from memos.api.config import APIConfig
    from memos.configs.llm import LLMConfigFactory
    from memos.llms.factory import LLMFactory

    return LLMFactory.from_config(LLMConfigFactory.model_validate(APIConfig.get_memreader_config()))


_READER_CLASSES = {
    "simple_struct": SimpleStructMemReader,
    "multimodal_struct": MultiModalStructMemReader,
}


def build_dry_run_reader(backend: str = "simple_struct") -> BaseMemReader:
    """Return a private reader for one trial run.

    Deliberately not through ``MemReaderFactory``: that factory caches by
    config, and a trial wraps the reader's models to record raw output, so two
    concurrent runs must not share an instance.
    """
    config = MemReaderConfigFactory.model_validate(dry_run_reader_config(backend))
    return _READER_CLASSES[config.backend](config.config)
