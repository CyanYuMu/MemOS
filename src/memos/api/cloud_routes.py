"""Cloud-contract facade: the cloud paths and envelope on top of the existing handlers.

``/product/*`` stays exactly as it is (the MCP gateway and the console depend on
it). These routes take the cloud field names, convert to the internal models in
:mod:`memos.api.cloud_models`, call the same handlers, and shape the result with
:mod:`memos.api.cloud_contract`: ``{code:0,data,message:"ok"}``, HTTP 400 plus a
4xxxx code on invalid input, epoch-millisecond timestamps, view-split lists.

No authentication is added here: like ``/product/*`` it relies on the gateway,
which must keep injecting ``user_id`` / ``writable_cube_ids``.
"""

import json
import uuid

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from memos.api import cloud_contract
from memos.api.cloud_models import (
    AddMessageRequest,
    ChatCloudRequest,
    DeleteMemoryCloudRequest,
    ExtractMemoryCloudRequest,
    FeedbackCloudRequest,
    GetMemoryCloudRequest,
    SearchMemoryRequest,
)
from memos.api.handlers import cloud_handler, memory_handler
from memos.api.product_models import (
    AddStatusRequestV2,
    GetMessageRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDeleteRequest,
    KnowledgeBaseFileDeleteRequest,
    KnowledgeBaseFileListRequest,
    KnowledgeBaseListRequest,
    ProfileBindRequest,
    ProfileDeleteRequest,
    ProfileEditRequest,
    ProfileQueryRequest,
    ProfileTemplateCreateRequest,
    ProfileTemplateDeleteRequest,
    ProfileTemplateQueryRequest,
    ProfileTemplateUpdateRequest,
    RerankRequest,
    UpdateMemoryRequest,
)
from memos.log import get_logger


logger = get_logger(__name__)


class CloudRoute(APIRoute):
    """Turn pydantic validation errors into the cloud's HTTP 400 + 4xxxx envelope."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapper(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=400,
                    content=cloud_contract.fail(
                        cloud_contract.INVALID_REQUEST,
                        "invalid request: "
                        + "; ".join(
                            f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
                            for error in exc.errors()
                        ),
                    ),
                )
            except ValueError as exc:
                return JSONResponse(
                    status_code=400,
                    content=cloud_contract.fail(cloud_contract.INVALID_REQUEST, str(exc)),
                )
            except HTTPException as exc:
                # Handlers raise HTTPException; keep the cloud envelope instead of {detail}.
                status = int(exc.status_code or 500)
                if status >= 500:
                    # Handler details can carry tracebacks; never echo them to callers.
                    logger.warning("cloud facade error: %s", exc.detail)
                    body = cloud_contract.fail(cloud_contract.SERVER_ERROR, "internal error")
                else:
                    detail = exc.detail
                    if not isinstance(detail, str):
                        detail = json.dumps(
                            jsonable_encoder(detail), ensure_ascii=False, default=str
                        )
                    body = cloud_contract.fail(cloud_contract.INVALID_REQUEST, str(detail))
                return JSONResponse(status_code=status, content=body, headers=exc.headers)

        return wrapper


router = APIRouter(route_class=CloudRoute)


def _server():
    from memos.api.routers import server_router

    return server_router


@router.post("/add/message", summary="Add conversation messages (cloud contract)")
def add_message(req: AddMessageRequest) -> dict[str, Any]:
    server = _server()
    internal = req.to_internal()
    internal.task_id = internal.task_id or uuid.uuid4().hex
    response = server.add_handler.handle_add_memories(internal)
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    status = "done" if internal.async_mode == "sync" else "running"
    return cloud_contract.ok({"success": True, "task_id": internal.task_id, "status": status})


@router.post("/get/message", summary="Get raw conversation messages (cloud contract)")
def get_message_cloud(req: GetMessageRequest) -> dict[str, Any]:
    return cloud_handler.handle_get_message(req, _server().get_message_store()).model_dump()


@router.post("/search/memory", summary="Search memories (cloud contract)")
def search_memory(req: SearchMemoryRequest) -> dict[str, Any]:
    response = _server().search_handler.handle_search_memories(req.to_internal())
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    views = cloud_contract.split_views(response.data)
    return cloud_contract.ok(views)


@router.post("/get/memory", summary="List memories (cloud contract)")
def get_memory(req: GetMemoryCloudRequest) -> dict[str, Any]:
    internal = req.to_internal()
    response = memory_handler.handle_get_memories(internal, _server().naive_mem_cube)
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    views = cloud_contract.split_views(response.data or {})
    total = cloud_contract.view_total(response.data or {})
    return cloud_contract.ok(
        cloud_contract.paged(views, internal.page or 1, internal.page_size or 10, total=total)
    )


@router.post("/delete/memory", summary="Delete memories (cloud contract)")
def delete_memory(req: DeleteMemoryCloudRequest) -> dict[str, Any]:
    response = memory_handler.handle_delete_memories(
        req.to_internal(),
        _server().naive_mem_cube,
        message_store=_server().get_message_store(),
    )
    success = (response.data or {}).get("status") == "success"
    if not success:
        return cloud_contract.fail(cloud_contract.SERVER_ERROR, response.message)
    return cloud_contract.ok({"success": True})


@router.post("/update/memory", summary="Update one memory (cloud contract)")
def update_memory_cloud(req: UpdateMemoryRequest) -> dict[str, Any]:
    server = _server()
    response = cloud_handler.handle_update_memory(
        req, naive_mem_cube=server.naive_mem_cube, embedder=server.components.get("embedder")
    )
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    return cloud_contract.ok(response.data or {"success": True})


@router.post("/add/feedback", summary="Feedback memories (cloud contract)")
def add_feedback(req: FeedbackCloudRequest) -> dict[str, Any]:
    internal = req.to_internal()
    internal.task_id = internal.task_id or uuid.uuid4().hex
    response = _server().feedback_handler.handle_feedback_memories(internal)
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    status = "done" if internal.async_mode == "sync" else "running"
    return cloud_contract.ok({"success": True, "task_id": internal.task_id, "status": status})


@router.post("/extract/memory", summary="Extract memories without writing (cloud contract)")
def extract_memory(req: ExtractMemoryCloudRequest) -> dict[str, Any]:
    response = _server().add_handler.handle_extract_memories(req.to_internal())
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    views = cloud_contract.split_views({"memories": response.data or []})
    return cloud_contract.ok(views)


@router.post("/get/status", summary="Async add task status (cloud contract)")
def get_status(req: AddStatusRequestV2) -> dict[str, Any]:
    response = cloud_handler.handle_add_status(
        req, status_tracker=_server().status_tracker, naive_mem_cube=_server().naive_mem_cube
    )
    if response.code not in (0, 200):
        return cloud_contract.fail(cloud_contract.INVALID_REQUEST, response.message)
    return cloud_contract.ok(response.data or {})


@router.post("/chat", summary="Chat with memory (cloud contract, non-streaming)")
def chat(req: ChatCloudRequest) -> dict[str, Any]:
    if req.stream:
        return cloud_contract.fail(
            cloud_contract.INVALID_REQUEST,
            "streaming is not supported on /chat; use /product/chat/stream",
        )
    server = _server()
    if server.chat_handler is None:
        return cloud_contract.fail(cloud_contract.SERVER_ERROR, "chat service is not available")
    response = server.chat_handler.handle_chat_complete(req.to_internal())
    data = response.get("data") if isinstance(response, dict) else None
    return cloud_contract.ok({"response": (data or {}).get("response", "")})


@router.post("/rerank", summary="Rerank documents (cloud contract)")
def rerank_cloud(req: RerankRequest) -> dict[str, Any]:
    return cloud_handler.handle_rerank(req, _server().components.get("reranker")).model_dump()


# ── knowledge base / profile path aliases ────────────────────────────────────
# Those endpoints already return the cloud envelope under /product/*; these aliases
# only add the cloud paths. Request models are reused as-is.


def _alias_kb_create(req: KnowledgeBaseCreateRequest) -> dict[str, Any]:
    return _server().create_knowledgebase(req)


def _alias_kb_delete(req: KnowledgeBaseDeleteRequest) -> dict[str, Any]:
    return _server().delete_knowledgebase(req)


def _alias_kb_get(req: KnowledgeBaseListRequest) -> dict[str, Any]:
    return _server().get_knowledgebase(req)


def _alias_kb_file_delete(req: KnowledgeBaseFileDeleteRequest) -> dict[str, Any]:
    return _server().delete_knowledgebase_file(req)


def _alias_kb_file_get(req: KnowledgeBaseFileListRequest) -> dict[str, Any]:
    return _server().get_knowledgebase_file(req)


async def _alias_kb_file_add(request: Request) -> dict[str, Any]:
    return await _server().add_knowledgebase_file(request)


def _alias_template_add(req: ProfileTemplateCreateRequest) -> dict[str, Any]:
    return _server().add_profile_template(req)


def _alias_template_list(req: ProfileTemplateQueryRequest) -> dict[str, Any]:
    return _server().list_profile_template(req)


def _alias_template_get(req: ProfileTemplateQueryRequest) -> dict[str, Any]:
    return _server().get_profile_template(req)


def _alias_template_update(req: ProfileTemplateUpdateRequest) -> dict[str, Any]:
    return _server().update_profile_template(req)


def _alias_template_delete(req: ProfileTemplateDeleteRequest) -> dict[str, Any]:
    return _server().delete_profile_template(req)


def _alias_template_bind(req: ProfileBindRequest) -> dict[str, Any]:
    return _server().bind_profile_template(req)


def _alias_profile_edit(req: ProfileEditRequest) -> dict[str, Any]:
    return _server().edit_profile(req)


def _alias_profile_delete(req: ProfileDeleteRequest) -> dict[str, Any]:
    return _server().delete_profile(req)


def _alias_profile_get(req: ProfileQueryRequest) -> dict[str, Any]:
    return _server().get_profile(req)


for _path, _endpoint, _name in (
    ("/create/knowledgebase", _alias_kb_create, "cloud_create_kb"),
    ("/delete/knowledgebase", _alias_kb_delete, "cloud_delete_kb"),
    ("/get/knowledgebase", _alias_kb_get, "cloud_get_kb"),
    ("/add/knowledgebase-file", _alias_kb_file_add, "cloud_add_kb_file"),
    ("/delete/knowledgebase-file", _alias_kb_file_delete, "cloud_delete_kb_file"),
    ("/get/knowledgebase-file", _alias_kb_file_get, "cloud_get_kb_file"),
    ("/add/profile_template", _alias_template_add, "cloud_add_template"),
    ("/list/profile_template", _alias_template_list, "cloud_list_template"),
    ("/get/profile_template", _alias_template_get, "cloud_get_template"),
    ("/update/profile_template", _alias_template_update, "cloud_update_template"),
    ("/delete/profile_template", _alias_template_delete, "cloud_delete_template"),
    ("/bind/profile_template", _alias_template_bind, "cloud_bind_template"),
    ("/edit/profile", _alias_profile_edit, "cloud_edit_profile"),
    ("/delete/profile", _alias_profile_delete, "cloud_delete_profile"),
    ("/get/profile", _alias_profile_get, "cloud_get_profile"),
):
    router.add_api_route(_path, _endpoint, methods=["POST"], name=_name, summary=_name)
