"""Backend-neutral vector index adapters used by graph providers."""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from memos.configs.vec_db import VectorDBConfigFactory
from memos.vec_dbs.base import BaseVecDB
from memos.vec_dbs.factory import VecDBFactory
from memos.vec_dbs.item import MilvusVecDBItem, VecDBItem


class BaseVectorIndex(ABC):
    """Small vector-index contract exposed to graph database providers."""

    @classmethod
    @abstractmethod
    def from_config(cls, config: VectorDBConfigFactory) -> "BaseVectorIndex":
        """Build an index from a registered vector database configuration."""

    @abstractmethod
    def check_health(self) -> None:
        """Raise when the configured vector index cannot serve requests."""

    @abstractmethod
    def upsert(self, items: list[VecDBItem]) -> None:
        """Insert or update vectors."""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """Delete vectors by their memory IDs."""

    @abstractmethod
    def search(
        self,
        vector: list[float],
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[VecDBItem]:
        """Search vectors and return backend-neutral items."""

    @property
    def vec_db(self) -> BaseVecDB | None:
        """The wrapped vector database, when this adapter has one.

        The Neo4j graph backends expose their raw VecDB as ``vec_db``, and
        ``tree_text_memory/organize/handler.py`` reaches for that name to sync a
        merged loser's payload status. Graph providers built on this adapter
        expose the same attribute so that sync is not a silent no-op.
        """
        return None


class _ConfiguredVectorIndex(BaseVectorIndex):
    """Common collection health handling for configured vector backends."""

    def __init__(self, vector_db: BaseVecDB, collection_name: str):
        self._vector_db = vector_db
        self._collection_name = collection_name

    @property
    def vec_db(self) -> BaseVecDB:
        return self._vector_db

    def check_health(self) -> None:
        if not self._vector_db.collection_exists(self._collection_name):
            raise ConnectionError(f"Vector collection {self._collection_name!r} is unavailable")


class QdrantVectorIndex(_ConfiguredVectorIndex):
    """Adapt the standard VecDB API to the graph provider contract."""

    @classmethod
    def from_config(cls, config: VectorDBConfigFactory) -> "QdrantVectorIndex":
        return cls(
            VecDBFactory.from_config(config),
            str(config.config.collection_name),
        )

    def upsert(self, items: list[VecDBItem]) -> None:
        self._vector_db.upsert(items)

    def delete(self, ids: list[str]) -> None:
        self._vector_db.delete(ids)

    def search(
        self,
        vector: list[float],
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[VecDBItem]:
        return self._vector_db.search(query_vector=vector, top_k=top_k, filter=filter)


class MilvusVectorIndex(_ConfiguredVectorIndex):
    """Adapt Milvus' collection-aware API to the graph provider contract."""

    @classmethod
    def from_config(cls, config: VectorDBConfigFactory) -> "MilvusVectorIndex":
        collection_names = config.config.collection_name
        if not collection_names:
            raise ValueError("Milvus vector index requires a collection name")
        return cls(
            VecDBFactory.from_config(config),
            str(collection_names[0]),
        )

    def _milvus_items(self, items: list[VecDBItem]) -> list[MilvusVecDBItem]:
        return [
            MilvusVecDBItem(
                id=item.id,
                memory="",
                original_text="",
                vector=item.vector,
                payload=item.payload,
            )
            for item in items
        ]

    def upsert(self, items: list[VecDBItem]) -> None:
        self._vector_db.upsert(self._collection_name, self._milvus_items(items))

    def delete(self, ids: list[str]) -> None:
        self._vector_db.delete(self._collection_name, ids)

    def search(
        self,
        vector: list[float],
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[VecDBItem]:
        return self._vector_db.search(
            query_vector=vector,
            query="",
            collection_name=self._collection_name,
            top_k=top_k,
            filter=filter,
        )


class VectorIndexFactory:
    """Build a backend-neutral vector index from a vector DB config."""

    backend_to_class: ClassVar[dict[str, type[BaseVectorIndex]]] = {
        "qdrant": QdrantVectorIndex,
        "milvus": MilvusVectorIndex,
    }

    @classmethod
    def from_config(cls, config: VectorDBConfigFactory | dict[str, Any]) -> BaseVectorIndex:
        config_factory = (
            config
            if isinstance(config, VectorDBConfigFactory)
            else VectorDBConfigFactory.model_validate(config)
        )
        index_class = cls.backend_to_class.get(config_factory.backend)
        if index_class is None:
            raise ValueError(f"Invalid vector index backend: {config_factory.backend}")
        return index_class.from_config(config_factory)
