from types import SimpleNamespace

from memos.api.handlers import memory_handler
from memos.api.product_models import GetMemoryDashboardRequest, GetMemoryRequest


def _cube(calls):
    def get_all(**kwargs):
        calls.append(kwargs)
        return {"nodes": [{"id": kwargs["memory_type"][0]}], "total_nodes": 1}

    return SimpleNamespace(text_mem=SimpleNamespace(graph_store=None, get_all=get_all))


def test_cloud_paging_defaults_but_dashboard_still_exports_all():
    cloud = GetMemoryRequest(mem_cube_id="c1", user_id="u1")
    dashboard = GetMemoryDashboardRequest(mem_cube_id="c1", user_id="u1")

    assert (cloud.page, cloud.page_size) == (1, 10)
    assert (dashboard.page, dashboard.page_size) == (None, None)
    assert GetMemoryRequest(user_id="u1").mem_cube_id == "u1"


def test_memory_views_split_events_and_narrow_by_agent():
    calls = []
    request = GetMemoryRequest(
        mem_cube_id="c1",
        user_id="u1",
        agent_id=["a1", "a2"],
        include_memory_view=["detail_factual", "event"],
        filter={"tags": {"contains": "x"}},
    )

    data = memory_handler.handle_get_memories(request, _cube(calls)).data

    assert list(data) == ["text_mem", "event_mem"]
    assert "EventMemory" not in calls[0]["memory_type"]
    assert calls[1]["memory_type"] == ["EventMemory"]
    assert calls[0]["filter"] == {"tags": {"contains": "x"}, "agent_id": {"in": ["a1", "a2"]}}
    assert calls[0]["page"] == 1 and calls[0]["page_size"] == 10
