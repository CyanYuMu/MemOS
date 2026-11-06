from unittest.mock import MagicMock

import pytest

from memos.vec_dbs import BaseVectorIndex as ExportedBaseVectorIndex
from memos.vec_dbs.base import BaseVecDB
from memos.vec_dbs.index import BaseVectorIndex, MilvusVectorIndex, QdrantVectorIndex
from memos.vec_dbs.item import MilvusVecDBItem, VecDBItem
from memos.vec_dbs.milvus import MilvusVecDB


VECTOR_ID = "11111111-1111-4111-8111-111111111111"


def test_base_vector_index_is_exported_from_vec_dbs_package():
    assert ExportedBaseVectorIndex is BaseVectorIndex


def test_qdrant_adapter_implements_backend_neutral_vector_index():
    vector_db = MagicMock(spec=BaseVecDB)
    vector_db.list_collections.return_value = ["memories"]
    index = QdrantVectorIndex(vector_db, "memories")

    assert isinstance(index, BaseVectorIndex)
    item = VecDBItem(id=VECTOR_ID, vector=[0.1, 0.2], payload={"tenant": "one"})

    index.check_health()
    index.upsert([item])
    index.delete([VECTOR_ID])
    index.search([0.1, 0.2], top_k=3, filter={"tenant": "one"})

    vector_db.collection_exists.assert_called_once_with("memories")
    vector_db.upsert.assert_called_once_with([item])
    vector_db.delete.assert_called_once_with([VECTOR_ID])
    vector_db.search.assert_called_once_with(
        query_vector=[0.1, 0.2], top_k=3, filter={"tenant": "one"}
    )


def test_vector_index_rejects_missing_collection():
    vector_db = MagicMock(spec=BaseVecDB)
    vector_db.collection_exists.return_value = False
    index = QdrantVectorIndex(vector_db, "memories")

    with pytest.raises(ConnectionError, match="memories"):
        index.check_health()


def test_milvus_adapter_normalizes_collection_aware_operations():
    vector_db = MagicMock(spec=BaseVecDB)
    vector_db.collection_exists.return_value = True
    index = MilvusVectorIndex(vector_db, "memories")
    item = VecDBItem(id=VECTOR_ID, vector=[0.1, 0.2], payload={"tenant": "one"})

    index.upsert([item])
    index.delete([VECTOR_ID])
    index.search([0.1, 0.2], top_k=3, filter={"tenant": "one"})

    stored_item = vector_db.upsert.call_args.args[1][0]
    assert isinstance(stored_item, MilvusVecDBItem)
    assert stored_item.id == VECTOR_ID
    assert stored_item.memory == ""
    assert stored_item.original_text == ""
    assert stored_item.payload == {"tenant": "one"}
    vector_db.upsert.assert_called_once_with("memories", [stored_item])
    vector_db.delete.assert_called_once_with("memories", [VECTOR_ID])
    vector_db.search.assert_called_once_with(
        query_vector=[0.1, 0.2],
        query="",
        collection_name="memories",
        top_k=3,
        filter={"tenant": "one"},
    )

    milvus_db = MilvusVecDB.__new__(MilvusVecDB)
    milvus_db.client = MagicMock()
    milvus_db.add("memories", [stored_item])

    milvus_db.client.upsert.assert_called_once_with(
        collection_name="memories",
        data=[
            {
                "id": VECTOR_ID,
                "memory": "",
                "original_text": "",
                "vector": [0.1, 0.2],
                "payload": {"tenant": "one"},
            }
        ],
    )
