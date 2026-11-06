from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memos.dream.routers.diary_router import create_diary_router


class FakeGraphDB:
    def __init__(self, nodes=None):
        self.nodes = nodes or []
        self.calls = []

    def get_by_metadata(self, filters, user_name=None, status=None):
        self.calls.append({"filters": filters, "user_name": user_name, "status": status})
        ids = []
        for node in self.nodes:
            metadata = node.get("metadata") or {}
            if status and metadata.get("status") != status:
                continue
            if not all(metadata.get(f.get("field")) == f.get("value") for f in filters):
                continue
            ids.append(node["id"])
        return ids

    def get_nodes(self, ids, user_name=None):
        return [node for node in self.nodes if node["id"] in ids]

    def get_node(self, id, user_name=None):
        for node in self.nodes:
            if node["id"] == id:
                return node
        return None


class FakePlugin:
    name = "dream"
    version = "0.1.0"

    def __init__(self, graph_db):
        self.context = {"shared": {"graph_db": graph_db}}
        self.signal_store = type("SignalStore", (), {"trigger_threshold": 100})()


def _client(graph_db) -> TestClient:
    app = FastAPI()
    app.include_router(create_diary_router(FakePlugin(graph_db)))
    return TestClient(app)


def test_diary_query_does_not_filter_by_activated_status_in_graph_db_call():
    node = {
        "id": "dream_diary_1",
        "memory": "Diary content",
        "metadata": {
            "memory_type": "DreamDiary",
            "status": "completed",
            "created_at": "2026-05-19T10:00:00",
            "title": "Dream title",
            "summary": "Dream summary",
            "dream_entry": "Dream entry",
            "motive": {"type": "newness"},
            "context_events": [{"context_id": "ctx_1", "label": "Context label"}],
            "themes": ["dream"],
        },
    }
    graph_db = FakeGraphDB(nodes=[node])

    response = _client(graph_db).post("/dream/diary", json={"cube_id": "cube-a"})

    assert response.status_code == 200
    assert graph_db.calls == [
        {
            "filters": [{"field": "memory_type", "op": "=", "value": "DreamDiary"}],
            "user_name": "cube-a",
            "status": None,
        }
    ]
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["task_id"] == "dream_diary_1"
    assert data[0]["title"] == "Dream title"
    assert data[0]["context_events"] == [{"context_id": "ctx_1", "label": "Context label"}]


def test_diary_query_returns_completed_and_skipped_entries():
    graph_db = FakeGraphDB(
        nodes=[
            {
                "id": "completed",
                "memory": "Completed",
                "metadata": {"memory_type": "DreamDiary", "status": "completed"},
            },
            {
                "id": "skipped",
                "memory": "Skipped",
                "metadata": {"memory_type": "DreamDiary", "status": "skipped"},
            },
        ]
    )

    response = _client(graph_db).post("/dream/diary", json={"cube_id": "cube-a"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["task_id"] for item in data] == ["completed", "skipped"]


def test_diary_query_reports_the_readable_task_id_not_the_node_id():
    """The node id must be a bare UUID for the vector DB; the API keeps
    exposing the human-readable `dream_diary_<hex>` id."""
    graph_db = FakeGraphDB(
        nodes=[
            {
                "id": "9c1f0b7a-2f4e-4a1d-9b6c-5d8e7f0a1b2c",
                "memory": "Diary content",
                "metadata": {
                    "memory_type": "DreamDiary",
                    "status": "completed",
                    "task_id": "dream_diary_abc123",
                    "title": "Dream title",
                    "summary": "Dream summary",
                },
            }
        ]
    )

    response = _client(graph_db).post("/dream/diary", json={"cube_id": "cube-a"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["task_id"] for item in data] == ["dream_diary_abc123"]


def test_diary_task_id_filter_accepts_the_readable_id():
    graph_db = FakeGraphDB(
        nodes=[
            {
                "id": "9c1f0b7a-2f4e-4a1d-9b6c-5d8e7f0a1b2c",
                "memory": "Diary content",
                "metadata": {
                    "memory_type": "DreamDiary",
                    "status": "completed",
                    "task_id": "dream_diary_abc123",
                    "title": "Dream title",
                    "summary": "Dream summary",
                },
            }
        ]
    )

    response = _client(graph_db).post(
        "/dream/diary", json={"cube_id": "cube-a", "filter": {"task_id": "dream_diary_abc123"}}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["task_id"] == "dream_diary_abc123"
    assert data[0]["title"] == "Dream title"


def test_diary_task_id_filter_still_accepts_a_node_id():
    graph_db = FakeGraphDB(
        nodes=[
            {
                "id": "9c1f0b7a-2f4e-4a1d-9b6c-5d8e7f0a1b2c",
                "memory": "Diary content",
                "metadata": {
                    "memory_type": "DreamDiary",
                    "status": "completed",
                    "task_id": "dream_diary_abc123",
                    "title": "Dream title",
                    "summary": "Dream summary",
                },
            }
        ]
    )

    response = _client(graph_db).post(
        "/dream/diary",
        json={
            "cube_id": "cube-a",
            "filter": {"task_id": "9c1f0b7a-2f4e-4a1d-9b6c-5d8e7f0a1b2c"},
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["task_id"] == "dream_diary_abc123"


def test_diary_task_id_filter_returns_empty_for_an_unknown_id():
    graph_db = FakeGraphDB(
        nodes=[
            {
                "id": "9c1f0b7a-2f4e-4a1d-9b6c-5d8e7f0a1b2c",
                "memory": "Diary content",
                "metadata": {"memory_type": "DreamDiary", "task_id": "dream_diary_abc123"},
            }
        ]
    )

    response = _client(graph_db).post(
        "/dream/diary", json={"cube_id": "cube-a", "filter": {"task_id": "dream_diary_nope"}}
    )

    assert response.status_code == 200
    assert response.json()["data"] == []
