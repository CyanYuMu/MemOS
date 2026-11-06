from types import SimpleNamespace

from memos.api.handlers import cloud_handler
from memos.api.handlers.add_handler import AddHandler
from memos.api.handlers.feedback_handler import FeedbackHandler
from memos.api.message_store import JsonlMessageStore, message_id_for
from memos.api.product_models import (
    AddStatusRequestV2,
    APIADDRequest,
    APIExtractRequest,
    APIFeedbackRequest,
    APISearchRequest,
    KnowledgeBaseDeleteRequest,
    MemoryResponse,
)


def test_extraction_types_keeps_only_the_requested_kind():
    handler = object.__new__(AddHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None)
    handler._build_cube_view = lambda req: SimpleNamespace(
        extract_memories=lambda _: [
            {"memory_type": "LongTermMemory", "memory": "fact"},
            {"memory_type": "PreferenceMemory", "memory": "pref"},
        ]
    )
    request = APIExtractRequest(
        user_id="u1", messages=[{"role": "user", "content": "hi"}], extraction_types=["preference"]
    )

    only_pref = handler.handle_extract_memories(request).data
    request.extraction_types = ["memory", "preference"]

    assert [item["memory"] for item in only_pref] == ["pref"]
    assert len(handler.handle_extract_memories(request).data) == 2


def test_status_no_longer_needs_user_id_and_kb_delete_accepts_delete_all():
    captured = {}
    tracker = SimpleNamespace(
        get_task_status_by_business_id=lambda task, user: (
            captured.update(user=user) or {"status": "running"}
        ),
        get_task_status=lambda task, user: None,
    )

    resp = cloud_handler.handle_add_status(
        AddStatusRequestV2(task_id="t1"), tracker, SimpleNamespace(text_mem=None)
    )
    kb = KnowledgeBaseDeleteRequest(knowledgebase_id="kb1", delete_all=True, mem_cube_id="c1")
    calls = []
    result = cloud_handler.handle_kb_delete(
        kb, SimpleNamespace(delete=lambda **kwargs: calls.append(kwargs) or True)
    )

    assert resp.code == 200 and resp.data["status"] == "running"
    assert captured["user"] is None
    assert result["data"]["success"] is True
    assert calls == [{"user_name": "c1", "knowledgebase_id": "kb1"}]


def test_feedback_agent_and_app_ids_reach_info():
    captured = {}

    class View:
        def feedback_memories(self, req):
            captured.update(req.info)
            return {"record": []}

    handler = object.__new__(FeedbackHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None)
    handler._build_cube_view = lambda req: View()
    request = APIFeedbackRequest(
        user_id="u1",
        history=[],
        feedback_content="fix",
        agent_id="a1",
        app_id="app1",
        info={"agent_id": "explicit"},
    )

    response = handler.handle_feedback_memories(request)

    assert isinstance(response, MemoryResponse)
    assert captured == {"agent_id": "explicit", "app_id": "app1"}


def test_app_id_reaches_the_filter_and_the_source_fields(tmp_path):
    from memos.memories.textual.item import SourceMessage, TextualMemoryItem
    from memos.multi_mem_cube.single_cube import SingleCubeView

    added = APIADDRequest(user_id="u1", messages=[{"role": "user", "content": "hi"}], app_id="app1")
    searched = APISearchRequest(query="q", user_id="u1", app_id="app1", filter={"tags": "x"})
    memory = TextualMemoryItem(
        memory="你好世界",
        metadata={"sources": [SourceMessage(type="chat", role="user", content="你好世界")]},
    )
    SingleCubeView._fill_source_fields([memory], "s1")

    store = JsonlMessageStore(tmp_path)
    store.add(
        user_name="u1",
        user_ids=["u1"],
        conversation_id="s1",
        messages=[{"role": "user", "content": "你好世界"}],
    )
    stored = store.list(user_name="u1", user_id="u1", conversation_id="s1", limit=1)[0]
    source = memory.metadata.sources[0]

    assert added.info == {"app_id": "app1"}
    assert searched.filter == {"and": [{"tags": "x"}, {"app_id": "app1"}]}
    assert source.lang == "zh"
    assert source.message_id == message_id_for("s1", "user", "你好世界") == stored["message_id"]
