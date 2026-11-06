from abc import ABC, abstractmethod
from typing import Any, ClassVar

from memos.configs.vec_db import BaseVecDBConfig
from memos.vec_dbs.item import VecDBItem


class BaseVecDB(ABC):
    """Base class for all vector databases."""

    #: Whether ``get_all(with_vectors=False)`` is implemented and returns every
    #: item's payload/text. Lexical indexes (e.g. hybrid BM25) use this to build
    #: a corpus without pulling embeddings over the wire. Backends that cannot
    #: honour the flag must leave it False so callers fail fast instead of
    #: silently indexing an empty corpus.
    supports_text_snapshot: ClassVar[bool] = False

    @abstractmethod
    def __init__(self, config: BaseVecDBConfig):
        """Initialize the vector database with the given configuration."""

    # Collection management methods

    @abstractmethod
    def create_collection(self) -> None:
        """Create a new collection/index with specified parameters."""

    @abstractmethod
    def list_collections(self) -> list[str]:
        """List all collections/indexes."""

    @abstractmethod
    def delete_collection(self, name: str) -> None:
        """Delete a collection/index."""

    @abstractmethod
    def collection_exists(self, name: str) -> bool:
        """Check if a collection/index exists."""

    # Vector management methods

    @abstractmethod
    def search(
        self,
        query_vector: list[float],
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[VecDBItem]:
        """
        Search for similar items in the vector database.

        Args:
            query_vector: Single vector to search
            top_k: Number of results to return
            filter: payload filters (may not be supported by all implementations)

        Returns:
            List of search results with distance scores and payloads.
        """

    @abstractmethod
    def get_by_id(self, id: str) -> VecDBItem | None:
        """Get an item from the vector database."""

    @abstractmethod
    def get_by_ids(self, ids: list[str]) -> list[VecDBItem]:
        """Get multiple items by their IDs."""

    @abstractmethod
    def get_by_filter(self, filter: dict[str, Any], with_vectors: bool = True) -> list[VecDBItem]:
        """
        Retrieve all items that match the given filter criteria.

        Args:
            filter: Payload filters to match against stored items
            with_vectors: When False, omit embeddings from the result. Backends
                advertising ``supports_text_snapshot`` must honour it.

        Returns:
            List of items including vectors and payloads that match the filter
        """

    @abstractmethod
    def get_all(self, with_vectors: bool = True) -> list[VecDBItem]:
        """Retrieve all items in the vector database.

        Args:
            with_vectors: When False, omit embeddings from the result.
        """

    @abstractmethod
    def count(self, filter: dict[str, Any] | None = None) -> int:
        """Count items in the database, optionally with filter."""

    @abstractmethod
    def add(self, data: list[VecDBItem | dict[str, Any]]) -> None:
        """
        Add data to the vector database.

        Args:
            data: List of VecDBItem objects or dictionaries containing:
                - 'id': unique identifier
                - 'vector': embedding vector
                - 'payload': additional fields for filtering/retrieval
        """

    @abstractmethod
    def update(self, id: str, data: VecDBItem | dict[str, Any]) -> None:
        """Update an item in the vector database."""

    @abstractmethod
    def upsert(self, data: list[VecDBItem | dict[str, Any]]) -> None:
        """
        Add or update data in the vector database.

        If an item with the same ID exists, it will be updated.
        Otherwise, it will be added as a new item.
        """

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """Delete items from the vector database."""

    @abstractmethod
    def ensure_payload_indexes(self, fields: list[str]) -> None:
        """
        Create payload indexes for specified fields in the collection.
        Args:
            fields (list[str]): List of field names to index (as keyword).
        """
