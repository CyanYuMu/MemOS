import json
import os

from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt

from memos.configs.memory import GeneralTextMemoryConfig
from memos.embedders.factory import ArkEmbedder, EmbedderFactory, OllamaEmbedder
from memos.exceptions import ConfigurationError
from memos.llms.factory import AzureLLM, LLMFactory, OllamaLLM, OpenAILLM
from memos.log import get_logger
from memos.memories.textual.base import BaseTextMemory
from memos.memories.textual.item import TextualMemoryItem
from memos.search.hybrid import CorpusDocument, HybridHit, HybridSearcher
from memos.templates.mem_reader_prompts import SIMPLE_STRUCT_MEM_READER_PROMPT
from memos.types import MessageList
from memos.utils import utc_now_iso
from memos.vec_dbs.factory import QdrantVecDB, VecDBFactory
from memos.vec_dbs.item import VecDBItem


logger = get_logger(__name__)


class GeneralTextMemory(BaseTextMemory):
    """General textual memory implementation for storing and retrieving memories."""

    def __init__(self, config: GeneralTextMemoryConfig):
        """Initialize memory with the given configuration."""
        # Set mode from class default or override if needed
        self.mode = getattr(self.__class__, "mode", "sync")
        self.config: GeneralTextMemoryConfig = config
        self.extractor_llm: OpenAILLM | OllamaLLM | AzureLLM = LLMFactory.from_config(
            config.extractor_llm
        )
        self.vector_db: QdrantVecDB = VecDBFactory.from_config(config.vector_db)
        self.embedder: OllamaEmbedder | ArkEmbedder = EmbedderFactory.from_config(config.embedder)
        self.hybrid_searcher = None
        if config.hybrid_search is not None:
            # The lexical side needs every item's text but none of its vectors.
            # Refuse the configuration instead of silently indexing an empty
            # corpus on a backend that cannot snapshot payloads.
            if not getattr(self.vector_db, "supports_text_snapshot", False):
                raise ConfigurationError(
                    "hybrid_search requires a vector_db backend that can snapshot item text "
                    f"without vectors; {type(self.vector_db).__name__} does not support it. "
                    "Use the qdrant backend or leave hybrid_search unset."
                )
            self.hybrid_searcher = HybridSearcher(config.hybrid_search)

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(json.JSONDecodeError),
        before_sleep=lambda retry_state: logger.warning(
            f"Extracting memory failed due to JSON decode error: {retry_state.outcome.exception()}, Attempt retry: {retry_state.attempt_number} / {3}"
        ),
    )
    def extract(self, messages: MessageList) -> list[TextualMemoryItem]:
        """Extract memories based on the messages.

        Args:
            messages: List of message dictionaries to extract memories from.

        Returns:
            List of TextualMemoryItem objects representing the extracted memories.
        """

        str_messages = "\n".join(
            [message["role"] + ":" + message["content"] for message in messages]
        )

        prompt = SIMPLE_STRUCT_MEM_READER_PROMPT.replace("${conversation}", str_messages).replace(
            "${custom_tags_prompt}", ""
        )
        messages = [{"role": "user", "content": prompt}]
        response_text = self.extractor_llm.generate(messages)
        response_json = self.parse_json_result(response_text)

        extracted_memories = [
            TextualMemoryItem(
                memory=memory_dict["value"],
                metadata={
                    "key": memory_dict["key"],
                    "source": "conversation",
                    "tags": memory_dict["tags"],
                    "updated_at": utc_now_iso(),
                },
            )
            for memory_dict in response_json["memory list"]
        ]

        return extracted_memories

    def add(self, memories: list[TextualMemoryItem | dict[str, Any]]) -> None:
        """Add memories.

        Args:
            memories: List of TextualMemoryItem objects or dictionaries to add.
        """
        memory_items = [TextualMemoryItem(**m) if isinstance(m, dict) else m for m in memories]

        # Memory encode
        embed_memories = self.embedder.embed([m.memory for m in memory_items])

        # Create vector db items
        vec_db_items = []
        for item, emb in zip(memory_items, embed_memories, strict=True):
            vec_db_items.append(
                VecDBItem(
                    id=item.id,
                    payload=item.model_dump(),
                    vector=emb,
                )
            )

        # Add to vector db
        self.vector_db.add(vec_db_items)
        self._invalidate_hybrid_index()

    def update(self, memory_id: str, new_memory: TextualMemoryItem | dict[str, Any]) -> None:
        """Update a memory by memory_id."""
        memory_item = (
            TextualMemoryItem(**new_memory) if isinstance(new_memory, dict) else new_memory
        )
        memory_item.id = memory_id

        vec_db_item = VecDBItem(
            id=memory_item.id,
            payload=memory_item.model_dump(),
            vector=self._embed_one_sentence(memory_item.memory),
        )

        self.vector_db.update(memory_id, vec_db_item)
        self._invalidate_hybrid_index()

    def search(self, query: str, top_k: int, info=None, **kwargs) -> list[TextualMemoryItem]:
        """Search for memories based on a query.

        When ``hybrid_search`` is configured and available, the vector results
        are fused with BM25 lexical results over the whole collection.  In every
        other case this is exactly the previous vector-only search.
        Args:
            query (str): The query to search for.
            top_k (int): The number of top results to return.
        Returns:
            list[TextualMemoryItem]: List of matching memories.
        """
        query_vector = self._embed_one_sentence(query)
        hybrid = self.hybrid_searcher
        use_hybrid = hybrid is not None and hybrid.enabled
        candidate_top_k = max(top_k, hybrid.candidate_top_k) if use_hybrid else top_k

        search_results = self.vector_db.search(query_vector, candidate_top_k)
        search_results = sorted(  # make higher score first
            search_results, key=lambda x: x.score, reverse=True
        )
        if not use_hybrid:
            return [TextualMemoryItem(**search_item.payload) for search_item in search_results]

        hits = self._fuse_with_lexical(query, search_results, top_k)
        if hits is None:
            # Hybrid retrieval failed or was skipped: preserve vector-only results.
            return [
                TextualMemoryItem(**search_item.payload) for search_item in search_results[:top_k]
            ]

        results: list[TextualMemoryItem] = []
        seen: set[str] = set()
        vector_items = {search_item.id: search_item for search_item in search_results}
        for hit in hits:
            if hit.item_id in seen or len(results) >= top_k:
                continue
            search_item = vector_items.get(hit.item_id)
            payload = (
                search_item.payload
                if search_item is not None and search_item.payload
                else hit.payload
            )
            if not payload:
                continue
            seen.add(hit.item_id)
            results.append(TextualMemoryItem(**payload))
        return results

    def _fuse_with_lexical(
        self, query: str, search_results: list[VecDBItem], top_k: int
    ) -> list[HybridHit] | None:
        """Run the hybrid searcher, returning ``None`` to signal fallback."""
        if top_k <= 0:
            return []
        try:
            return self.hybrid_searcher.fuse(
                query,
                [(item.id, float(item.score or 0.0)) for item in search_results],
                self._hybrid_documents,
                limit=top_k,
            )
        except Exception as exc:
            logger.warning("Hybrid retrieval failed, falling back to vector-only: %s", exc)
            return None

    def _hybrid_documents(self) -> list[CorpusDocument]:
        """Snapshot the collection for the BM25 index.

        Payload-only: embeddings are never needed for lexical scoring and are
        expensive to pull for a large collection.
        """
        documents: list[CorpusDocument] = []
        for db_item in self.vector_db.get_all(with_vectors=False):
            payload = db_item.payload or {}
            text = (
                payload.get("memory")
                or payload.get("original_text")
                or getattr(db_item, "memory", None)
                or getattr(db_item, "original_text", None)
                or ""
            )
            documents.append(CorpusDocument(item_id=db_item.id, text=str(text), payload=payload))
        return documents

    def _invalidate_hybrid_index(self) -> None:
        if self.hybrid_searcher is not None:
            self.hybrid_searcher.invalidate()

    def get(self, memory_id: str, user_name: str | None = None) -> TextualMemoryItem:
        """Get a memory by its ID."""
        result = self.vector_db.get_by_id(memory_id)
        if result is None:
            raise ValueError(f"Memory with ID {memory_id} not found")
        return TextualMemoryItem(**result.payload)

    def get_by_ids(self, memory_ids: list[str]) -> list[TextualMemoryItem]:
        """Get memories by their IDs.
        Args:
            memory_ids (list[str]): List of memory IDs to retrieve.
        Returns:
            list[TextualMemoryItem]: List of memories with the specified IDs.
        """
        db_items = self.vector_db.get_by_ids(memory_ids)
        memories = [TextualMemoryItem(**db_item.payload) for db_item in db_items]
        return memories

    def get_all(self) -> list[TextualMemoryItem]:
        """Get all memories.
        Returns:
            list[TextualMemoryItem]: List of all memories.
        """
        all_items = self.vector_db.get_all()
        all_memories = [TextualMemoryItem(**memo.payload) for memo in all_items]
        return all_memories

    def delete(self, memory_ids: list[str]) -> None:
        """Delete a memory."""
        self.vector_db.delete(memory_ids)
        self._invalidate_hybrid_index()

    def delete_all(self) -> None:
        """Delete all memories."""
        self.vector_db.delete_collection(self.vector_db.config.collection_name)
        self.vector_db.create_collection()
        self._invalidate_hybrid_index()

    def load(self, dir: str) -> None:
        try:
            memory_file = os.path.join(dir, self.config.memory_filename)

            if not os.path.exists(memory_file):
                logger.warning(f"Memory file not found: {memory_file}")
                return

            with open(memory_file, encoding="utf-8") as f:
                memories = json.load(f)

            vec_db_items = [VecDBItem.from_dict(m) for m in memories]
            self.vector_db.add(vec_db_items)
            self._invalidate_hybrid_index()
            logger.info(f"Loaded {len(memories)} memories from {memory_file}")

        except FileNotFoundError:
            logger.error(f"Memory file not found in directory: {dir}")
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from memory file: {e}")
        except Exception as e:
            logger.error(f"An error occurred while loading memories: {e}")

    def dump(self, dir: str) -> None:
        """Dump memories to os.path.join(dir, self.config.memory_filename)"""
        try:
            all_vec_db_items = self.vector_db.get_all()
            json_memories = [memory.to_dict() for memory in all_vec_db_items]

            os.makedirs(dir, exist_ok=True)
            memory_file = os.path.join(dir, self.config.memory_filename)
            with open(memory_file, "w", encoding="utf-8") as f:
                json.dump(json_memories, f, indent=4, ensure_ascii=False)

            logger.info(f"Dumped {len(all_vec_db_items)} memories to {memory_file}")

        except Exception as e:
            logger.error(f"An error occurred while dumping memories: {e}")
            raise

    def drop(
        self,
    ) -> None:
        pass

    def _embed_one_sentence(self, sentence: str) -> list[float]:
        """Embed a single sentence."""
        return self.embedder.embed([sentence])[0]

    def parse_json_result(self, response_text):
        try:
            json_start = response_text.find("{")
            response_text = response_text[json_start:]
            response_text = response_text.replace("```", "").strip()
            if response_text[-1] != "}":
                response_text += "}"
            response_json = json.loads(response_text)
            return response_json
        except json.JSONDecodeError as e:
            logger.warning(
                f"Failed to parse LLM response as JSON: {e}\nRaw response:\n{response_text}"
            )
            return {}
