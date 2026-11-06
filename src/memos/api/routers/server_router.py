"""
Server API Router for MemOS (Class-based handlers version).

This router demonstrates the improved architecture using class-based handlers
with dependency injection, providing better modularity and maintainability.

Comparison with function-based approach:
- Cleaner code: No need to pass dependencies in every endpoint
- Better testability: Easy to mock handler dependencies
- Improved extensibility: Add new handlers or modify existing ones easily
- Clear separation of concerns: Router focuses on routing, handlers handle business logic
"""

import os
import random as _random
import socket

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from memos.api import handlers
from memos.api.cloud_routes import router as cloud_router
from memos.api.cloud_store import build_metadata_store
from memos.api.contract import contract_document
from memos.api.handlers import cloud_handler
from memos.api.handlers.add_handler import AddHandler
from memos.api.handlers.base_handler import HandlerDependencies
from memos.api.handlers.chat_handler import ChatHandler
from memos.api.handlers.cube_handler import CubeHandler
from memos.api.handlers.feedback_handler import FeedbackHandler
from memos.api.handlers.formatters_handler import strip_memory_sources
from memos.api.handlers.prompt_handler import PromptHandler
from memos.api.handlers.search_handler import SearchHandler
from memos.api.knowledge_base import KnowledgeBaseService
from memos.api.message_store import build_message_store
from memos.api.product_models import (
    AddStatusRequestV2,
    AddStatusResponseV2,
    AllStatusResponse,
    APIADDRequest,
    APIChatCompleteRequest,
    APIExtractRequest,
    APIFeedbackRequest,
    APISearchRequest,
    ChatBusinessRequest,
    ChatPlaygroundRequest,
    ChatRequest,
    CreateCubeRequest,
    CreateCubeResponse,
    DeleteMemoryByRecordIdRequest,
    DeleteMemoryByRecordIdResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    ExistMemCubeIdRequest,
    ExistMemCubeIdResponse,
    GetEventMemoriesRequest,
    GetEventMemoriesResponse,
    GetMemoryByIdsRequest,
    GetMemoryDashboardRequest,
    GetMemoryGraphRequest,
    GetMemoryGraphResponse,
    GetMemoryPlaygroundRequest,
    GetMemoryRequest,
    GetMemoryResponse,
    GetMessageRequest,
    GetMessageResponse,
    GetUserNamesByMemoryIdsRequest,
    GetUserNamesByMemoryIdsResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDeleteRequest,
    KnowledgeBaseFileAddRequest,
    KnowledgeBaseFileDeleteRequest,
    KnowledgeBaseFileDownloadRequest,
    KnowledgeBaseFileListRequest,
    KnowledgeBaseListRequest,
    KnowledgeBaseUpdateRequest,
    MemoryResponse,
    ProfileBindRequest,
    ProfileDeleteRequest,
    ProfileEditRequest,
    ProfileQueryRequest,
    ProfileTemplateCreateRequest,
    ProfileTemplateDeleteRequest,
    ProfileTemplateQueryRequest,
    ProfileTemplateUpdateRequest,
    RecoverMemoryByRecordIdRequest,
    RecoverMemoryByRecordIdResponse,
    RegisterCubeRequest,
    RegisterCubeResponse,
    RerankRequest,
    RerankResponse,
    SearchResponse,
    StatusResponse,
    SuggestionRequest,
    SuggestionResponse,
    TaskQueueResponse,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)
from memos.api.profile import ProfileService
from memos.api.routers.prompt_router import build_router
from memos.log import get_logger
from memos.mem_scheduler.base_scheduler import BaseScheduler
from memos.mem_scheduler.utils.status_tracker import TaskStatusTracker
from memos.time_basis import normalize_payload_times


logger = get_logger(__name__)

router = APIRouter(prefix="/product", tags=["Server API"])

# Instance ID for identifying this server instance in logs and responses
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{_random.randint(1000, 9999)}"

# Initialize all server components
components = handlers.init_server()

# Create dependency container
dependencies = HandlerDependencies.from_init_server(components)

# Initialize all handlers with dependency injection
search_handler = SearchHandler(dependencies)
add_handler = AddHandler(dependencies)
chat_handler = (
    ChatHandler(
        dependencies=dependencies,
        chat_llms=components["chat_llms"],
        playground_chat_llms=components.get("playground_chat_llms"),
        search_handler=search_handler,
        add_handler=add_handler,
        online_bot=components.get("online_bot"),
    )
    if os.getenv("ENABLE_CHAT_API", "false") == "true"
    else None
)
feedback_handler = FeedbackHandler(dependencies)
cube_handler = CubeHandler(dependencies)
prompt_handler = PromptHandler(dependencies)
# Extract commonly used components for function-based handlers
# (These can be accessed from the components dict without unpacking all of them)
mem_scheduler: BaseScheduler = components["mem_scheduler"]
llm = components["llm"]
suggestion_llm = components.get("suggestion_llm", llm)
naive_mem_cube = components["naive_mem_cube"]
redis_client = components["redis_client"]
status_tracker = TaskStatusTracker(redis_client=redis_client)
graph_db = components["graph_db"]

# Cloud-parity services are built lazily: router import happens under patched
# test fixtures and must not touch the filesystem or the driver.
_cloud_store = None


def get_cloud_store():
    global _cloud_store
    if _cloud_store is None:
        _cloud_store = build_metadata_store(graph_db)
    return _cloud_store


_message_store = None


def get_message_store():
    global _message_store
    if _message_store is None:
        _message_store = build_message_store(graph_db)
    return _message_store


def get_kb_service() -> KnowledgeBaseService:
    return KnowledgeBaseService.from_env(get_cloud_store())


def get_profile_service() -> ProfileService:
    return ProfileService(get_cloud_store())


# =============================================================================
# Contract
# =============================================================================


@router.get("/contract", summary="Describe the wire contract")
def get_contract():
    """Machine-readable contract for consumers (console, MCP gateway).

    Consumers check ``contract_version`` before taking traffic instead of
    hard-coding the quirks of one deployment.
    """
    return contract_document()


# =============================================================================
# Search API Endpoints
# =============================================================================


def _shape_response(resp, include_sources: bool):
    """Apply the response contract.

    * every timestamp is real UTC (see ``memos.time_basis``);
    * ``metadata.sources`` is dropped unless the caller asked for it - it can be
      hundreds of megabytes for a single preference node and no console
      consumer reads it.
    """
    normalize_payload_times(resp.data)
    if not include_sources:
        strip_memory_sources(resp.data)
    return resp


@router.post("/search", summary="Search memories", response_model=SearchResponse)
def search_memories(search_req: APISearchRequest):
    """
    Search memories for a specific user.

    This endpoint uses the class-based SearchHandler for better code organization.
    """
    search_results = search_handler.handle_search_memories(search_req)
    return _shape_response(search_results, search_req.include_sources)


# =============================================================================
# Add API Endpoints
# =============================================================================


@router.post("/add", summary="Add memories", response_model=MemoryResponse)
def add_memories(add_req: APIADDRequest):
    """
    Add memories for a specific user.

    This endpoint uses the class-based AddHandler for better code organization.
    """
    return add_handler.handle_add_memories(add_req)


@router.post(
    "/extract_memory",
    summary="Extract memories without persisting (dry run)",
    response_model=MemoryResponse,
)
def extract_memories(extract_req: APIExtractRequest):
    """Return what the reader would extract from the messages, without writing.

    Mirrors the cloud ``extract/memory`` contract: facts/preferences only, no
    graph or vector-store writes, no scheduler task.
    """
    return add_handler.handle_extract_memories(extract_req)


# =============================================================================
# Cube Management API Endpoints
# =============================================================================


@router.post("/create_cube", summary="Create a new memory cube", response_model=CreateCubeResponse)
async def create_cube(request: CreateCubeRequest) -> CreateCubeResponse:
    """
    Create a new memory cube for a user.

    Memory cubes are containers that store different types of memories (textual, activation, parametric).
    Each cube can be owned by a user and shared with other users.

    **Note on cube_id vs mem_cube_id:**
    These terms are used interchangeably throughout the API:
    - `cube_id` is the canonical identifier for a cube
    - `mem_cube_id` appears in many legacy endpoints and means the same thing
    - When using other endpoints (search, add, chat), you can reference this cube using either term

    **Semantic Clarification:**
    - **Single mem_cube_id** (deprecated): Used in older endpoints to identify a single cube.
      New code should use `readable_cube_ids` / `writable_cube_ids` lists instead.
    - **readable_cube_ids**: List of cube IDs the user can read from (used in search/chat)
    - **writable_cube_ids**: List of cube IDs the user can write to (used in add/chat)
    """
    return await cube_handler.create_cube(request)


# Prompt endpoints live in their own module so the dev-only prompt service can
# serve the exact same routes.
router.include_router(build_router(prompt_handler))


@router.post(
    "/register_cube",
    summary="Register an existing memory cube",
    response_model=RegisterCubeResponse,
)
async def register_cube(request: RegisterCubeRequest) -> RegisterCubeResponse:
    """
    Register an existing memory cube with the MOS system.

    This method loads and registers a memory cube from a file path or creates a new one
    if the path doesn't exist. The cube becomes available for memory operations.

    **Note on cube_id vs mem_cube_id:**
    These terms are used interchangeably throughout the API. The registered cube can then
    be referenced by its cube_id/mem_cube_id in other endpoints.

    **Current Status:**
    This endpoint validates the registration request. Full registration functionality
    requires architectural integration with MOSCore, which will be completed in a future update.
    """
    return await cube_handler.register_cube(request)


# =============================================================================
# Scheduler API Endpoints
# =============================================================================


@router.get(  # Changed from post to get
    "/scheduler/allstatus",
    summary="Get detailed scheduler status",
    response_model=AllStatusResponse,
)
def scheduler_allstatus():
    """Get detailed scheduler status including running tasks and queue metrics."""
    return handlers.scheduler_handler.handle_scheduler_allstatus(
        mem_scheduler=mem_scheduler, status_tracker=status_tracker
    )


@router.get(  # Changed from post to get
    "/scheduler/status", summary="Get scheduler running status", response_model=StatusResponse
)
def scheduler_status(
    user_id: str = Query(..., description="User ID"),
    task_id: str | None = Query(None, description="Optional Task ID to query a specific task"),
):
    """Get scheduler running status."""
    return handlers.scheduler_handler.handle_scheduler_status(
        user_id=user_id,
        task_id=task_id,
        status_tracker=status_tracker,
    )


@router.get(  # Changed from post to get
    "/scheduler/task_queue_status",
    summary="Get scheduler task queue status",
    response_model=TaskQueueResponse,
)
def scheduler_task_queue_status(
    user_id: str = Query(..., description="User ID whose queue status is requested"),
):
    """Get scheduler task queue backlog/pending status for a user."""
    return handlers.scheduler_handler.handle_task_queue_status(
        user_id=user_id, mem_scheduler=mem_scheduler
    )


@router.post("/scheduler/wait", summary="Wait until scheduler is idle for a specific user")
def scheduler_wait(
    user_name: str,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.5,
):
    """Wait until scheduler is idle for a specific user."""
    return handlers.scheduler_handler.handle_scheduler_wait(
        user_name=user_name,
        status_tracker=status_tracker,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )


@router.get("/scheduler/wait/stream", summary="Stream scheduler progress for a user")
def scheduler_wait_stream(
    user_name: str,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.5,
):
    """Stream scheduler progress via Server-Sent Events (SSE)."""
    return handlers.scheduler_handler.handle_scheduler_wait_stream(
        user_name=user_name,
        status_tracker=status_tracker,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        instance_id=INSTANCE_ID,
    )


# =============================================================================
# Chat API Endpoints
# =============================================================================


@router.post("/chat/complete", summary="Chat with MemOS (Complete Response)")
def chat_complete(chat_req: APIChatCompleteRequest):
    """
    Chat with MemOS for a specific user. Returns complete response (non-streaming).

    This endpoint uses the class-based ChatHandler.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_complete(chat_req)


@router.post("/chat/stream", summary="Chat with MemOS")
def chat_stream(chat_req: ChatRequest):
    """
    Chat with MemOS for a specific user. Returns SSE stream.

    This endpoint uses the class-based ChatHandler which internally
    composes SearchHandler and AddHandler for a clean architecture.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_stream(chat_req)


@router.post("/chat/stream/playground", summary="Chat with MemOS playground")
def chat_stream_playground(chat_req: ChatPlaygroundRequest):
    """
    Chat with MemOS for a specific user. Returns SSE stream.

    This endpoint uses the class-based ChatHandler which internally
    composes SearchHandler and AddHandler for a clean architecture.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_stream_playground(chat_req)


# =============================================================================
# Suggestion API Endpoints
# =============================================================================


@router.post(
    "/suggestions",
    summary="Get suggestion queries",
    response_model=SuggestionResponse,
)
def get_suggestion_queries(suggestion_req: SuggestionRequest):
    """Get suggestion queries for a specific user with language preference."""
    return handlers.suggestion_handler.handle_get_suggestion_queries(
        user_id=suggestion_req.mem_cube_id,
        language=suggestion_req.language,
        message=suggestion_req.message,
        llm=suggestion_llm,
        naive_mem_cube=naive_mem_cube,
    )


# =============================================================================
# Memory Retrieval Delete API Endpoints
# =============================================================================


@router.post("/get_all", summary="Get all memories for user", response_model=MemoryResponse)
def get_all_memories(memory_req: GetMemoryPlaygroundRequest):
    """
    Get all memories or subgraph for a specific user.

    If search_query is provided, returns a subgraph based on the query.
    Otherwise, returns all memories of the specified type.
    """
    if memory_req.search_query:
        resp = handlers.memory_handler.handle_get_subgraph(
            user_id=memory_req.user_id,
            mem_cube_id=(
                memory_req.mem_cube_ids[0] if memory_req.mem_cube_ids else memory_req.user_id
            ),
            query=memory_req.search_query,
            top_k=200,
            naive_mem_cube=naive_mem_cube,
            search_type=memory_req.search_type,
        )
        return _shape_response(resp, False)
    resp = handlers.memory_handler.handle_get_all_memories(
        user_id=memory_req.user_id,
        mem_cube_id=(memory_req.mem_cube_ids[0] if memory_req.mem_cube_ids else memory_req.user_id),
        memory_type=memory_req.memory_type or "text_mem",
        naive_mem_cube=naive_mem_cube,
    )
    return _shape_response(resp, False)


@router.post(
    "/get_memory_graph",
    summary="Get the dashboard memory graph",
    response_model=GetMemoryGraphResponse,
)
def get_memory_graph(memory_req: GetMemoryGraphRequest):
    """Return tenant-scoped nodes plus PARENT/MERGED_TO edges for the graph view."""
    resp = handlers.memory_handler.handle_get_memory_graph(
        get_graph_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )
    return _shape_response(resp, False)


@router.post(
    "/get_event_memories",
    summary="Get event and Dream memories",
    response_model=GetEventMemoriesResponse,
)
def get_event_memories(memory_req: GetEventMemoriesRequest):
    """Return paginated EventMemory / Context / InsightMemory / DreamDiary nodes."""
    resp = handlers.memory_handler.handle_get_event_memories(
        get_event_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )
    return _shape_response(resp, False)


@router.post("/get_memory", summary="Get memories for user", response_model=GetMemoryResponse)
def get_memories(memory_req: GetMemoryRequest):
    resp = handlers.memory_handler.handle_get_memories(
        get_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )
    return _shape_response(resp, memory_req.include_sources)


@router.get("/get_memory/{memory_id}", summary="Get memory by id", response_model=GetMemoryResponse)
def get_memory_by_id(
    memory_id: str,
    mem_cube_id: str | None = Query(
        None,
        description="Only return the memory when it belongs to this cube/tenant. "
        "Omitted: look the id up across tenants (owner lookup). "
        "Ignored in multi-database mode, where tenants are isolated physically.",
    ),
    include_sources: bool = Query(
        False,
        description="Include metadata.sources (the full conversation behind the memory).",
    ),
):
    resp = handlers.memory_handler.handle_get_memory(
        memory_id=memory_id,
        naive_mem_cube=naive_mem_cube,
        mem_cube_id=mem_cube_id,
    )
    return _shape_response(resp, include_sources)


@router.post("/get_memory_by_ids", summary="Get memory by ids", response_model=GetMemoryResponse)
def get_memory_by_ids(
    payload: GetMemoryByIdsRequest | list[str],
    mem_cube_id: str | None = Query(
        None,
        description="Only return memories that belong to this cube/tenant. "
        "Omitted: the service's configured default tenant. "
        "Ignored in multi-database mode, where tenants are isolated physically.",
    ),
):
    # Contract v2 is the object shape; the bare array is accepted for callers
    # that have not migrated yet.
    memory_ids = payload.memory_ids if isinstance(payload, GetMemoryByIdsRequest) else payload
    include_sources = (
        payload.include_sources if isinstance(payload, GetMemoryByIdsRequest) else False
    )
    resp = handlers.memory_handler.handle_get_memory_by_ids(
        memory_ids=memory_ids,
        naive_mem_cube=naive_mem_cube,
        mem_cube_id=mem_cube_id,
    )
    return _shape_response(resp, include_sources)


@router.post(
    "/delete_memory", summary="Delete memories for user", response_model=DeleteMemoryResponse
)
def delete_memories(memory_req: DeleteMemoryRequest):
    return handlers.memory_handler.handle_delete_memories(
        delete_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
        message_store=get_message_store(),
    )


# =============================================================================
# Feedback API Endpoints
# =============================================================================


@router.post("/feedback", summary="Feedback memories", response_model=MemoryResponse)
def feedback_memories(feedback_req: APIFeedbackRequest):
    """
    Feedback memories for a specific user.

    This endpoint uses the class-based FeedbackHandler for better code organization.
    """
    return feedback_handler.handle_feedback_memories(feedback_req)


# =============================================================================
# Other API Endpoints (for internal use)
# =============================================================================


@router.post(
    "/get_user_names_by_memory_ids",
    summary="Get user names by memory ids",
    response_model=GetUserNamesByMemoryIdsResponse,
)
def get_user_names_by_memory_ids(request: GetUserNamesByMemoryIdsRequest):
    """Get user names by memory ids. Now unified to query from graph_db only."""
    result = graph_db.get_user_names_by_memory_ids(memory_ids=request.memory_ids)

    return GetUserNamesByMemoryIdsResponse(
        code=200,
        message="Successfully",
        data=result,
    )


@router.post(
    "/exist_mem_cube_id",
    summary="Check if mem cube id exists",
    response_model=ExistMemCubeIdResponse,
)
def exist_mem_cube_id(request: ExistMemCubeIdRequest):
    """(inner) Check if mem cube id exists."""
    return ExistMemCubeIdResponse(
        code=200,
        message="Successfully",
        data=graph_db.exist_user_name(user_name=request.mem_cube_id),
    )


@router.post("/chat/stream/business_user", summary="Chat with MemOS for business user")
def chat_stream_business_user(chat_req: ChatBusinessRequest):
    """(inner) Chat with MemOS for a specific business user. Returns SSE stream."""
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )

    return chat_handler.handle_chat_stream_for_business_user(chat_req)


@router.post(
    "/delete_memory_by_record_id",
    summary="Delete memory by record id",
    response_model=DeleteMemoryByRecordIdResponse,
)
def delete_memory_by_record_id(memory_req: DeleteMemoryByRecordIdRequest):
    """(inner) Delete memory nodes by mem_cube_id (user_name) and delete_record_id. Record id is inner field, just for delete and recover memory, not for user to set."""
    graph_db.delete_node_by_mem_cube_id(
        mem_cube_id=memory_req.mem_cube_id,
        delete_record_id=memory_req.record_id,
        hard_delete=memory_req.hard_delete,
    )

    return DeleteMemoryByRecordIdResponse(
        code=200,
        message="Called Successfully",
        data={"status": "success"},
    )


@router.post(
    "/recover_memory_by_record_id",
    summary="Recover memory by record id",
    response_model=RecoverMemoryByRecordIdResponse,
)
def recover_memory_by_record_id(memory_req: RecoverMemoryByRecordIdRequest):
    """(inner) Recover memory nodes by mem_cube_id (user_name) and delete_record_id. Record id is inner field, just for delete and recover memory, not for user to set."""
    graph_db.recover_memory_by_mem_cube_id(
        mem_cube_id=memory_req.mem_cube_id,
        delete_record_id=memory_req.delete_record_id,
    )

    return RecoverMemoryByRecordIdResponse(
        code=200,
        message="Called Successfully",
        data={"status": "success"},
    )


@router.post(
    "/get_memory_dashboard", summary="Get memories for dashboard", response_model=GetMemoryResponse
)
def get_memories_dashboard(memory_req: GetMemoryDashboardRequest):
    resp = handlers.memory_handler.handle_get_memories_dashboard(
        get_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )
    return _shape_response(resp, memory_req.include_sources)


# =============================================================================
# Cloud parity endpoints (T2/T3)
# =============================================================================


@router.post("/update_memory", summary="Update one memory", response_model=UpdateMemoryResponse)
def update_memory(update_req: UpdateMemoryRequest):
    """Rewrite content/title/status of a memory.

    Content updates recompute the embedding and update both the graph node and
    the vector payload; frozen/expired lifecycle rules are enforced here.
    """
    return cloud_handler.handle_update_memory(
        update_req,
        naive_mem_cube=naive_mem_cube,
        embedder=components.get("embedder"),
    )


@router.post("/add_status", summary="Get async add task status", response_model=AddStatusResponseV2)
def add_status(status_req: AddStatusRequestV2):
    """Return the task status plus the `memory_views` summary."""
    return cloud_handler.handle_add_status(
        status_req,
        status_tracker=status_tracker,
        naive_mem_cube=naive_mem_cube,
    )


@router.post(
    "/get_message", summary="Get raw conversation messages", response_model=GetMessageResponse
)
def get_message(message_req: GetMessageRequest):
    """Return the stored messages of one conversation (cloud ``get/message``)."""
    return cloud_handler.handle_get_message(message_req, get_message_store())


@router.post("/rerank", summary="Rerank documents against a query", response_model=RerankResponse)
def rerank(rerank_req: RerankRequest):
    """Score arbitrary documents with the configured reranker (cloud ``rerank``)."""
    return cloud_handler.handle_rerank(rerank_req, components.get("reranker"))


# ── knowledge base ───────────────────────────────────────────────────────────


@router.post("/create_knowledgebase", summary="Create a knowledge base")
def create_knowledgebase(req: KnowledgeBaseCreateRequest):
    return cloud_handler.handle_kb_create(req, get_kb_service())


@router.post("/delete_knowledgebase", summary="Delete a knowledge base")
def delete_knowledgebase(req: KnowledgeBaseDeleteRequest):
    return cloud_handler.handle_kb_delete(req, get_kb_service())


@router.post("/update_knowledgebase", summary="Rename a knowledge base or update its description")
def update_knowledgebase(req: KnowledgeBaseUpdateRequest):
    return cloud_handler.handle_kb_update(req, get_kb_service())


@router.post("/get_knowledgebase", summary="List or get knowledge bases")
def get_knowledgebase(req: KnowledgeBaseListRequest):
    return cloud_handler.handle_kb_list(req, get_kb_service())


@router.post("/add_knowledgebase_file", summary="Add knowledge base files")
async def add_knowledgebase_file(request: Request):
    """Accept cloud's JSON `file` list or a multipart upload (`file` parts)."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        payload = {
            "user_id": form.get("user_id"),
            "mem_cube_id": form.get("mem_cube_id"),
            "knowledgebase_id": form.get("knowledgebase_id"),
            "type": form.get("type"),
        }
        uploads = []
        for upload in form.getlist("file"):
            filename = getattr(upload, "filename", None)
            if not filename:
                continue
            uploads.append(
                {
                    "name": filename,
                    "bytes": await upload.read(),
                    "content_type": getattr(upload, "content_type", None),
                }
            )
        req = KnowledgeBaseFileAddRequest(**payload)
        return cloud_handler.handle_kb_file_add(req, get_kb_service(), uploads=uploads)

    body = await request.json()
    req = KnowledgeBaseFileAddRequest(**body)
    return cloud_handler.handle_kb_file_add(req, get_kb_service())


@router.post("/delete_knowledgebase_file", summary="Delete knowledge base files")
def delete_knowledgebase_file(req: KnowledgeBaseFileDeleteRequest):
    return cloud_handler.handle_kb_file_delete(req, get_kb_service())


@router.post("/get_knowledgebase_file", summary="List or get knowledge base files")
def get_knowledgebase_file(req: KnowledgeBaseFileListRequest):
    return cloud_handler.handle_kb_file_list(req, get_kb_service())


@router.post("/download_knowledgebase_file", summary="Download a knowledge base file")
def download_knowledgebase_file(req: KnowledgeBaseFileDownloadRequest):
    content, filename = cloud_handler.handle_kb_file_download(req, get_kb_service())
    safe_name = filename.replace('"', "").replace("\n", "").replace("\r", "")
    fallback = safe_name if safe_name.isascii() and safe_name else "download"
    encoded = quote(safe_name, safe="")
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


# ── profile ──────────────────────────────────────────────────────────────────


@router.post("/add_profile_template", summary="Create a profile template")
def add_profile_template(req: ProfileTemplateCreateRequest):
    return cloud_handler.handle_profile_template_create(req, get_profile_service())


@router.post("/list_profile_template", summary="List profile templates")
def list_profile_template(req: ProfileTemplateQueryRequest):
    return cloud_handler.handle_profile_template_list(req, get_profile_service())


@router.post("/get_profile_template", summary="Get a profile template")
def get_profile_template(req: ProfileTemplateQueryRequest):
    if not req.profile_template_id:
        raise ValueError("profile_template_id is required")
    return cloud_handler.handle_profile_template_list(req, get_profile_service())


@router.post("/update_profile_template", summary="Update a profile template")
def update_profile_template(req: ProfileTemplateUpdateRequest):
    return cloud_handler.handle_profile_template_update(req, get_profile_service())


@router.post("/delete_profile_template", summary="Delete a profile template")
def delete_profile_template(req: ProfileTemplateDeleteRequest):
    return cloud_handler.handle_profile_template_delete(req, get_profile_service())


@router.post("/bind_profile_template", summary="Bind subjects to profile templates")
def bind_profile_template(req: ProfileBindRequest):
    return cloud_handler.handle_profile_bind(req, get_profile_service())


@router.post("/edit_profile", summary="Edit a profile instance")
def edit_profile(req: ProfileEditRequest):
    return cloud_handler.handle_profile_edit(req, get_profile_service())


@router.post("/delete_profile", summary="Delete a profile instance")
def delete_profile(req: ProfileDeleteRequest):
    return cloud_handler.handle_profile_delete(req, get_profile_service())


@router.post("/get_profile", summary="Get profile instances of a subject")
def get_profile(req: ProfileQueryRequest):
    return cloud_handler.handle_profile_query(req, get_profile_service())


# Cloud-contract facade: cloud paths at the root, so they are not added under /product.
# `include_router` would prepend this router's `/product` prefix, hence the direct extend.
router.routes.extend(cloud_router.routes)
