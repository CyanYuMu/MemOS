from memos.api.config import APIConfig
from memos.api.handlers.config_builders import build_graph_db_config


def _set_hugegraph_env(monkeypatch):
    monkeypatch.setenv("GRAPH_DB_BACKEND", "hugegraph")
    monkeypatch.setenv("HUGEGRAPH_URL", "http://hugegraph.internal:18080")
    monkeypatch.setenv("HUGEGRAPH_GRAPHSPACE", "example-space")
    monkeypatch.setenv("HUGEGRAPH_GRAPH", "mem_graph")
    monkeypatch.setenv("HUGEGRAPH_USER", "hugegraph-user")
    monkeypatch.setenv("HUGEGRAPH_PASSWORD", "hugegraph-password")
    monkeypatch.setenv("QDRANT_HOST", "qdrant")
    monkeypatch.setenv("QDRANT_PORT", "6333")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "memos_hugegraph_vectors")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1536")


def test_hugegraph_config_uses_documented_environment(monkeypatch):
    _set_hugegraph_env(monkeypatch)

    config = APIConfig.get_hugegraph_config(user_id="tenant-1")

    assert config == {
        "ip": "hugegraph.internal",
        "port": 18080,
        "graph": "mem_graph",
        "graphspace": "example-space",
        "user": "hugegraph-user",
        "password": "hugegraph-password",
        "user_name": "memostenant-1",
        "vec_config": {
            "backend": "qdrant",
            "config": {
                "collection_name": "memos_hugegraph_vectors",
                "vector_dimension": 1536,
                "distance_metric": "cosine",
                "host": "qdrant",
                "port": 6333,
                "path": None,
                "url": None,
                "api_key": None,
            },
        },
    }


def test_graph_db_builder_selects_hugegraph(monkeypatch):
    _set_hugegraph_env(monkeypatch)

    config = build_graph_db_config(user_id="tenant-1")

    assert config.backend == "hugegraph"
    assert config.config.graph == "mem_graph"
    assert config.config.graphspace == "example-space"
    assert config.config.user_name == "memostenant-1"
    assert config.config.vec_config["backend"] == "qdrant"


def test_hugegraph_config_selects_milvus_vector_backend(monkeypatch):
    _set_hugegraph_env(monkeypatch)
    monkeypatch.setenv("HUGEGRAPH_VECTOR_BACKEND", "milvus")
    monkeypatch.setenv("MILVUS_URI", "http://milvus:19530")
    monkeypatch.setenv("HUGEGRAPH_MILVUS_COLLECTION_NAME", "hugegraph_vectors")

    config = APIConfig.get_hugegraph_config(user_id="tenant-1")

    assert config["vec_config"] == {
        "backend": "milvus",
        "config": {
            "collection_name": ["hugegraph_vectors"],
            "vector_dimension": 1536,
            "distance_metric": "cosine",
            "uri": "http://milvus:19530",
            "user_name": "root",
            "password": "12345678",
        },
    }


def test_hugegraph_config_can_disable_vector_backend(monkeypatch):
    _set_hugegraph_env(monkeypatch)
    monkeypatch.setenv("HUGEGRAPH_VECTOR_BACKEND", "none")

    assert APIConfig.get_hugegraph_config(user_id="tenant-1")["vec_config"] is None


def test_hugegraph_config_rejects_unknown_vector_backend(monkeypatch):
    _set_hugegraph_env(monkeypatch)
    monkeypatch.setenv("HUGEGRAPH_VECTOR_BACKEND", "unknown")

    import pytest

    with pytest.raises(ValueError, match="HUGEGRAPH_VECTOR_BACKEND"):
        APIConfig.get_hugegraph_config(user_id="tenant-1")


def test_default_cube_config_selects_hugegraph(monkeypatch):
    _set_hugegraph_env(monkeypatch)
    monkeypatch.setenv("MOS_ENABLE_DEFAULT_CUBE_CONFIG", "true")

    config = APIConfig.get_default_cube_config()

    assert config is not None
    assert config.text_mem.config.graph_db.backend == "hugegraph"
    assert config.text_mem.config.graph_db.config.graph == "mem_graph"


def test_unselected_hugegraph_config_is_not_evaluated(monkeypatch):
    monkeypatch.setenv("GRAPH_DB_BACKEND", "postgres")
    monkeypatch.setenv("HUGEGRAPH_URL", "not-a-url")
    monkeypatch.setenv("POSTGRES_USER", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "postgres")
    monkeypatch.setenv("POSTGRES_DB", "memos")

    config = build_graph_db_config(user_id="tenant-1")

    assert config.backend == "postgres"


def test_hugegraph_tenant_mapping_does_not_collapse_hyphens(monkeypatch):
    _set_hugegraph_env(monkeypatch)

    assert (
        APIConfig.get_hugegraph_config("a-b")["user_name"]
        != APIConfig.get_hugegraph_config("ab")["user_name"]
    )


def test_hugegraph_rejects_https_until_client_supports_it(monkeypatch):
    _set_hugegraph_env(monkeypatch)
    monkeypatch.setenv("HUGEGRAPH_URL", "https://hugegraph.internal:8443")

    import pytest

    with pytest.raises(ValueError, match="only supports http"):
        APIConfig.get_hugegraph_config("tenant-1")
