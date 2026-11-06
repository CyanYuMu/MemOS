"""Handlers for cloud-parity endpoints.

Covers:

* ``update/memory`` (T2.1)
* ``get/status`` / ``add_status`` (T2.3)
* ``get/message`` over the raw message store
* knowledge base CRUD and file operations (T3.1)
* profile templates / instances / recall (T3.2)

All handlers are thin: validation and storage live in
:mod:`memos.api.knowledge_base` and :mod:`memos.api.profile`; lifecycle rules
live in :mod:`memos.api.lifecycle_rules`.
"""

from __future__ import annotations

import uuid

from typing import Any

from memos.api.knowledge_base import KnowledgeBaseService, public_file_view
from memos.api.lifecycle_rules import (
    lifecycle_guard,
)
from memos.api.product_models import (
    AddStatusRequestV2,
    AddStatusResponseV2,
    GetMessageRequest,
    GetMessageResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDeleteRequest,
    KnowledgeBaseFileAddRequest,
    KnowledgeBaseFileDeleteRequest,
    KnowledgeBaseFileListRequest,
    KnowledgeBaseListRequest,
    KnowledgeBaseUpdateRequest,
    ProfileBindRequest,
    ProfileDeleteRequest,
    ProfileEditRequest,
    ProfileQueryRequest,
    ProfileTemplateCreateRequest,
    ProfileTemplateDeleteRequest,
    ProfileTemplateQueryRequest,
    ProfileTemplateUpdateRequest,
    RerankRequest,
    RerankResponse,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)
from memos.api.profile import ProfileService, ProfileValidationError
from memos.api.rerank import RerankUnavailableError, rerank_model_name, score_documents
from memos.log import get_logger
from memos.utils import utc_now_iso


logger = get_logger(__name__)

# Cloud memory-view names for graph memory types.
_TYPE_TO_VIEW = {
    "WorkingMemory": "detail_factual",
    "LongTermMemory": "detail_factual",
    "UserMemory": "detail_factual",
    "RawFileMemory": "detail_factual",
    "PreferenceMemory": "preference",
    "EventMemory": "event",
    "ToolSchemaMemory": "tool_memory",
    "ToolTrajectoryMemory": "tool_memory",
    "ProfileMemory": "profile",
}

_STATUS_MAP = {
    "completed": "completed",
    "failed": "failed",
    "in_progress": "running",
    "waiting": "running",
    "pending": "running",
    "cancelled": "failed",
}


# ── T2.1 update_memory ───────────────────────────────────────────────────────


#: Fields a caller may not set through ``UpdateMemoryRequest.metadata``.
#: Content/status/title have dedicated fields; the rest are graph/vector
#: identity or lifecycle fields owned by the server.
RESERVED_UPDATE_METADATA_KEYS = frozenset(
    {
        "id",
        "memory",
        "user_name",
        "owner_type",
        "owner_id",
        "embedding",
        "embedding_1024",
        "embedding_3072",
        "embedding_768",
        "memory_type",
        "created_at",
        "updated_at",
        "version",
        "history",
        "status",
        "key",
        "task_id",
        "stage",
        "content_hash",
    }
)


def _embed(embedder: Any, content: str) -> list[float] | None:
    if embedder is None or not hasattr(embedder, "embed"):
        return None
    try:
        vectors = embedder.embed([content])
        return vectors[0] if vectors else None
    except Exception:
        logger.warning("Failed to embed updated memory content", exc_info=True)
        return None


def _resolve_tenant(graph_db: Any, memory_id: str, requested: str | None) -> str | None:
    if requested:
        return requested
    getter = getattr(graph_db, "get_user_names_by_memory_ids", None)
    if callable(getter):
        try:
            mapping = getter([memory_id])
            if isinstance(mapping, dict):
                value = mapping.get(memory_id)
                if isinstance(value, str):
                    return value
        except Exception:
            logger.warning("Failed to resolve memory tenant", exc_info=True)
    return None


def handle_update_memory(
    req: UpdateMemoryRequest,
    naive_mem_cube: Any,
    embedder: Any = None,
) -> UpdateMemoryResponse:
    """Rewrite a memory in the graph and keep the vector payload in sync."""
    text_mem = getattr(naive_mem_cube, "text_mem", None)
    graph_db = getattr(text_mem, "graph_store", None)
    if graph_db is None:
        return UpdateMemoryResponse(
            message="memory store unavailable", code=500, data={"success": False}
        )

    requested_tenant = (req.mem_cube_id or "").strip() or None
    if not requested_tenant and not req.user_id:
        # Neither the tenant nor a subject: the id alone is not an
        # authorization boundary, so refuse instead of reverse-looking-up the
        # node's own tenant and updating across callers.
        return UpdateMemoryResponse(
            message="mem_cube_id or user_id is required to update a memory",
            code=400,
            data={"success": False},
        )
    tenant = _resolve_tenant(graph_db, req.memory_id, requested_tenant)
    if not tenant:
        # Refuse to fall back to a global lookup: an unscoped get_node() +
        # add_node() would move another tenant's node into this deployment's
        # default cube.
        return UpdateMemoryResponse(
            message="mem_cube_id is required to update this memory",
            code=400,
            data={"success": False},
        )

    try:
        node = graph_db.get_node(req.memory_id, user_name=tenant)
    except Exception:
        logger.warning("get_node failed for %s", req.memory_id, exc_info=True)
        node = None

    if node is None:
        return UpdateMemoryResponse(
            message=f"memory not found: {req.memory_id}",
            code=404,
            data={"success": False},
        )

    metadata = dict(node.get("metadata") or {})
    current_status = metadata.get("status") or "activated"

    # Ownership is checked against the node itself (fail-closed on mismatch).
    # ``mem_cube_id`` is the tenant scope; ``user_id`` is the subject.
    node_user = metadata.get("user_id")
    owner_type = metadata.get("owner_type")
    owner_id = metadata.get("owner_id")
    if req.user_id:
        if owner_type == "user":
            if not owner_id or str(owner_id) != req.user_id:
                return UpdateMemoryResponse(
                    message="memory does not belong to this user",
                    code=403,
                    data={"success": False},
                )
        elif owner_type in ("agent", "group"):
            # Agent/group memory is not a user's private memory; the cloud
            # update endpoint has no corresponding subject field.
            return UpdateMemoryResponse(
                message="memory is not a user-owned memory",
                code=403,
                data={"success": False},
            )
        elif node_user and str(node_user) != req.user_id:
            return UpdateMemoryResponse(
                message="memory does not belong to this user",
                code=403,
                data={"success": False},
            )

    if req.metadata:
        reserved = sorted(set(req.metadata) & RESERVED_UPDATE_METADATA_KEYS)
        if reserved:
            return UpdateMemoryResponse(
                message=(
                    "reserved metadata fields cannot be set here: "
                    f"{', '.join(reserved)}; use content/title/status instead"
                ),
                code=400,
                data={"success": False},
            )

    violation = lifecycle_guard(current_status, "update", target_status=req.status)
    if violation:
        return UpdateMemoryResponse(message=violation, code=409, data={"success": False})

    new_content = req.content if req.content is not None else None
    content_changed = new_content is not None and new_content != node.get("memory")
    status_changed = req.status is not None and req.status != current_status
    title_changed = req.title is not None and req.title != metadata.get("key")
    # Reserved keys were rejected above, so anything left is safe to merge.
    extra_metadata = dict(req.metadata or {})
    metadata_changed = bool(extra_metadata)

    if not any((content_changed, status_changed, title_changed, metadata_changed)):
        return UpdateMemoryResponse(
            message="no changes requested",
            code=400,
            data={"success": False, "memory_id": req.memory_id},
        )

    now = utc_now_iso()
    old_version = int(metadata.get("version") or 1)

    if content_changed:
        history = list(metadata.get("history") or [])
        history.append(
            {
                "version": old_version,
                "memory": node.get("memory") or "",
                "update_type": "update",
                "created_at": metadata.get("updated_at") or now,
            }
        )
        metadata["history"] = history
        metadata["version"] = old_version + 1

    if title_changed:
        metadata["key"] = req.title
    if status_changed:
        metadata["status"] = req.status
    if metadata_changed:
        metadata.update(extra_metadata)

    metadata["updated_at"] = now
    metadata.pop("embedding", None)
    metadata.pop("embedding_1024", None)
    metadata.pop("embedding_3072", None)
    metadata.pop("embedding_768", None)
    # `created_at` must survive: add_node requires it and the MERGE would
    # otherwise reset the original creation time.
    metadata.setdefault("created_at", now)

    try:
        embedding = None
        if content_changed:
            embedder_obj = embedder or getattr(text_mem, "embedder", None)
            if embedder_obj is None:
                # Without an embedder the graph would hold the new text while
                # the vector index keeps the old one, so recall silently
                # returns stale content. Fail instead of a false success.
                return UpdateMemoryResponse(
                    message="content update requires a configured embedder",
                    code=500,
                    data={"success": False},
                )
            embedding = _embed(embedder_obj, new_content)
            if embedding is None:
                # Do not write the graph with a stale vector: the caller
                # would get a successful update that recall cannot see.
                return UpdateMemoryResponse(
                    message="failed to re-embed memory content",
                    code=500,
                    data={"success": False},
                )
            metadata["embedding"] = embedding
            # `add_node` MERGEs the node, so it updates the graph *and* (for the
            # community backend) upserts the vector point with the new vector.
            graph_db.add_node(req.memory_id, new_content, metadata, user_name=tenant)
            sync = getattr(graph_db, "_sync_vector_payload", None)
            if sync is not None and embedding is not None:
                sync(req.memory_id, {"memory": new_content, **metadata})
        else:
            update_fields = {
                key: value
                for key, value in metadata.items()
                if key in ("status", "key", "updated_at", "version", "history")
                or key in extra_metadata
            }
            graph_db.update_node(req.memory_id, update_fields, user_name=tenant)
    except Exception as e:
        logger.exception("Failed to update memory %s", req.memory_id)
        return UpdateMemoryResponse(
            message=f"failed to update memory: {e}",
            code=500,
            data={"success": False},
        )

    return UpdateMemoryResponse(
        message="ok",
        code=200,
        data={
            "success": True,
            "memory_id": req.memory_id,
            "memory": new_content if content_changed else node.get("memory"),
            "status": metadata.get("status"),
            "title": metadata.get("key"),
            "updated_at": now,
            "reembedded": bool(content_changed and embedding is not None),
        },
    )


# ── T2.3 add_status ──────────────────────────────────────────────────────────


def _collect_memory_views(naive_mem_cube: Any, tenant: str | None, task_id: str) -> dict:
    text_mem = getattr(naive_mem_cube, "text_mem", None)
    views: dict[str, Any] = {"added": 0, "updated": 0, "deleted": 0}
    if text_mem is None or not tenant:
        return views

    try:
        exported = text_mem.get_all(
            user_name=tenant,
            filter={"task_id": task_id},
            page_size=None,
        )
    except Exception:
        logger.warning("Failed to collect memory views for task %s", task_id, exc_info=True)
        return views

    nodes = exported.get("nodes", []) if isinstance(exported, dict) else []
    by_view: dict[str, list[str]] = {}
    for node in nodes:
        metadata = node.get("metadata") or {}
        view = _TYPE_TO_VIEW.get(metadata.get("memory_type"), "detail_factual")
        by_view.setdefault(view, []).append(node.get("id"))

    for view, ids in by_view.items():
        views[view] = {"memory_ids": ids}
    views["added"] = len(nodes)
    return views


def handle_add_status(
    req: AddStatusRequestV2,
    status_tracker: Any,
    naive_mem_cube: Any,
) -> AddStatusResponseV2:
    """Return the task status plus the memory-view summary once complete."""
    tenant = req.mem_cube_id or req.user_id
    status_data: dict[str, Any] | None = None
    if status_tracker is not None:
        try:
            status_data = status_tracker.get_task_status_by_business_id(req.task_id, req.user_id)
            if not status_data:
                status_data = status_tracker.get_task_status(req.task_id, req.user_id)
        except Exception:
            logger.warning("Failed to read task status for %s", req.task_id, exc_info=True)

    raw_status = (status_data or {}).get("status")
    status = _STATUS_MAP.get(
        str(raw_status), "not_found" if raw_status is None else str(raw_status)
    )

    data: dict[str, Any] = {
        "task_id": req.task_id,
        "status": status,
        "memory_views": _collect_memory_views(naive_mem_cube, tenant, req.task_id),
    }
    if status == "failed":
        errors = (status_data or {}).get("errors")
        if errors is None:
            errors = (status_data or {}).get("error")
        if isinstance(errors, str):
            errors = [errors]
        data["errors"] = errors or []
    return AddStatusResponseV2(
        code=404 if status == "not_found" else 200,
        message="ok" if status != "not_found" else f"task not found: {req.task_id}",
        data=data,
    )


# ── raw messages (cloud get/message) ─────────────────────────────────────────


def handle_get_message(req: GetMessageRequest, store: Any) -> GetMessageResponse:
    """Return the latest ``message_limit_number`` stored messages of a conversation, oldest first."""
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    messages = store.list(
        user_name=tenant,
        user_id=req.user_id,
        conversation_id=req.conversation_id,
        limit=req.message_limit_number,
    )
    return GetMessageResponse(code=0, message="ok", data={"message_detail_list": messages})


def handle_rerank(req: RerankRequest, reranker: Any) -> RerankResponse:
    """Score ``req.documents`` against ``req.query``, highest score first."""
    try:
        scored = score_documents(reranker, req.query, req.documents)
    except RerankUnavailableError as exc:
        return RerankResponse(code=501, message=str(exc), data=None)
    except Exception:
        logger.exception("Rerank failed")
        return RerankResponse(code=500, message="rerank failed", data=None)

    scored.sort(key=lambda pair: pair[1], reverse=True)
    if req.top_n is not None:
        scored = scored[: req.top_n]
    results = [
        {
            "index": index,
            "document": {"text": req.documents[index]},
            "relevance_score": score,
        }
        for index, score in scored
    ]
    return RerankResponse(
        code=0,
        message="ok",
        data={"id": uuid.uuid4().hex, "model": rerank_model_name(reranker), "results": results},
    )


# ── T3.1 knowledge base ──────────────────────────────────────────────────────


def _tenant_of(mem_cube_id: str | None, user_id: str | None) -> str:
    tenant = (mem_cube_id or user_id or "").strip()
    if not tenant:
        raise ValueError("mem_cube_id or user_id is required")
    return tenant


def handle_kb_create(req: KnowledgeBaseCreateRequest, service: KnowledgeBaseService) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    document = service.create(
        user_name=tenant,
        name=req.knowledgebase_name,
        description=req.knowledgebase_description,
        owner_user_id=req.user_id,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "id": document["entity_id"],
            "knowledgebase_id": document["entity_id"],
            "knowledgebase_name": document["name"],
            "knowledgebase_description": document.get("description"),
        },
    }


def handle_kb_update(req: KnowledgeBaseUpdateRequest, service: KnowledgeBaseService) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    document = service.update(
        user_name=tenant,
        knowledgebase_id=req.knowledgebase_id,
        name=req.knowledgebase_name,
        description=req.knowledgebase_description,
    )
    if document is None:
        return {"code": 404, "message": "knowledgebase not found", "data": None}
    return {"code": 0, "message": "ok", "data": _kb_view(document)}


def handle_kb_list(req: KnowledgeBaseListRequest, service: KnowledgeBaseService) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    if req.knowledgebase_id:
        document = service.get(user_name=tenant, knowledgebase_id=req.knowledgebase_id)
        if document is None:
            return {"code": 404, "message": "knowledgebase not found", "data": None}
        return {"code": 0, "message": "ok", "data": _kb_view(document)}

    documents, total = service.list(user_name=tenant, page=req.page, page_size=req.page_size)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "knowledgebase_list": [_kb_view(document) for document in documents],
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        },
    }


def handle_kb_delete(req: KnowledgeBaseDeleteRequest, service: KnowledgeBaseService) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    # Our knowledge bases belong to exactly one tenant and carry no project link,
    # so `delete_all=false` and `true` both mean "remove it with its files". Honouring
    # `true` across tenants would let anyone delete another tenant's base by id.
    deleted = service.delete(user_name=tenant, knowledgebase_id=req.knowledgebase_id)
    return {
        "code": 0 if deleted else 404,
        "message": "ok" if deleted else "knowledgebase not found",
        "data": {"success": deleted},
    }


def handle_kb_file_add(
    req: KnowledgeBaseFileAddRequest,
    service: KnowledgeBaseService,
    uploads: list[dict[str, Any]] | None = None,
) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    files = list(req.file or [])
    files.extend(uploads or [])
    documents = service.add_files(
        user_name=tenant,
        knowledgebase_id=req.knowledgebase_id,
        files=files,
        file_type=req.type,
        owner_user_id=req.user_id,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": [public_file_view(document) for document in documents],
    }


def handle_kb_file_list(req: KnowledgeBaseFileListRequest, service: KnowledgeBaseService) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    if not req.knowledgebase_id and not req.file_ids:
        raise ValueError("exactly one of knowledgebase_id or file_ids is required")
    documents, total = service.list_files(
        user_name=tenant,
        knowledgebase_id=req.knowledgebase_id,
        file_ids=req.file_ids,
        file_type=req.type,
        page=req.page,
        page_size=req.page_size,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "file_detail_list": [public_file_view(document) for document in documents],
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        },
    }


def handle_kb_file_delete(
    req: KnowledgeBaseFileDeleteRequest, service: KnowledgeBaseService
) -> dict:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    deleted = service.delete_files(user_name=tenant, file_ids=req.file_ids)
    return {
        "code": 0,
        "message": "ok",
        "data": {"success": deleted > 0, "deleted": deleted},
    }


def handle_kb_file_download(req: Any, service: KnowledgeBaseService) -> tuple[bytes, str]:
    tenant = _tenant_of(req.mem_cube_id, req.user_id)
    result = service.read_file(user_name=tenant, file_id=req.file_id)
    if result is None:
        raise ValueError(f"file not found: {req.file_id}")
    content, document = result
    return content, document.get("name") or f"{req.file_id}.bin"


def _kb_view(document: dict) -> dict:
    return {
        "id": document["entity_id"],
        "knowledgebase_id": document["entity_id"],
        "knowledgebase_name": document.get("name"),
        "knowledgebase_description": document.get("description"),
        "file_count": document.get("file_count", 0),
        "total_size": document.get("total_size", 0),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
    }


# ── T3.2 profile ─────────────────────────────────────────────────────────────


def _profile_tenant(req: Any) -> str:
    return _tenant_of(getattr(req, "mem_cube_id", None), getattr(req, "user_id", None))


def handle_profile_template_create(
    req: ProfileTemplateCreateRequest, service: ProfileService
) -> dict:
    tenant = _profile_tenant(req)
    document = service.add_template(user_name=tenant, name=req.name, metadata=req.metadata)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "profile_template_id": document["entity_id"],
            "id": document["entity_id"],
        },
    }


def handle_profile_template_list(req: ProfileTemplateQueryRequest, service: ProfileService) -> dict:
    tenant = _profile_tenant(req)
    if req.profile_template_id:
        document = service.get_template(user_name=tenant, template_id=req.profile_template_id)
        if document is None:
            return {"code": 404, "message": "profile template not found", "data": None}
        return {"code": 0, "message": "ok", "data": {**document, "profile_template": document}}

    page_size = req.page_size or req.size or (10 if req.page else None)
    documents, total = service.list_templates(user_name=tenant, page=req.page, page_size=page_size)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "profile_template_list": documents,
            "profile_templates": documents,
            "total": total,
            "page": req.page or (1 if page_size else None),
            "page_size": page_size,
            "current": req.page or 1,
            "size": page_size or total,
            "pages": -(-total // page_size) if page_size else int(total > 0),
        },
    }


def handle_profile_template_update(
    req: ProfileTemplateUpdateRequest, service: ProfileService
) -> dict:
    tenant = _profile_tenant(req)
    if req.name is None and req.metadata is None:
        raise ProfileValidationError("name or metadata is required")
    document = service.update_template(
        user_name=tenant,
        template_id=req.profile_template_id,
        name=req.name,
        metadata=req.metadata,
    )
    if document is None:
        return {"code": 404, "message": "profile template not found", "data": None}
    return {"code": 0, "message": "ok", "data": {**document, "success": True}}


def handle_profile_template_delete(
    req: ProfileTemplateDeleteRequest, service: ProfileService
) -> dict:
    tenant = _profile_tenant(req)
    existing = service.get_template(user_name=tenant, template_id=req.profile_template_id)
    deleted = service.delete_template(user_name=tenant, template_id=req.profile_template_id)
    return {
        "code": 0 if deleted else 404,
        "message": "ok" if deleted else "profile template not found",
        "data": {
            "success": deleted,
            "profile_template_id": req.profile_template_id,
            "deleted_instances": (existing or {}).get("instance_count", 0) if deleted else 0,
        },
    }


def handle_profile_bind(req: ProfileBindRequest, service: ProfileService) -> dict:
    tenant = _profile_tenant(req)
    instances = service.bind(user_name=tenant, bind_list=req.bind_list)
    return {"code": 0, "message": "ok", "data": {"success": True, "profile_list": instances}}


def handle_profile_edit(req: ProfileEditRequest, service: ProfileService) -> dict:
    tenant = _profile_tenant(req)
    instance = service.edit(
        user_name=tenant,
        template_id=req.profile_template_id,
        user_id=req.user_id,
        agent_id=req.agent_id,
        metadata=req.metadata,
        remove_fields=req.remove_fields,
    )
    if instance is None:
        return {"code": 404, "message": "profile not found", "data": None}
    return {"code": 0, "message": "ok", "data": {**instance, "success": True}}


def handle_profile_delete(req: ProfileDeleteRequest, service: ProfileService) -> dict:
    tenant = _profile_tenant(req)
    deleted = service.delete_instance(
        user_name=tenant,
        template_id=req.profile_template_id,
        user_id=req.user_id,
        agent_id=req.agent_id,
    )
    return {
        "code": 0 if deleted else 404,
        "message": "ok" if deleted else "profile not found",
        "data": {"success": deleted},
    }


def handle_profile_query(req: ProfileQueryRequest, service: ProfileService) -> dict:
    tenant = _profile_tenant(req)
    instances = service.query(
        user_name=tenant,
        user_id=req.user_id,
        agent_id=req.agent_id,
        template_id=req.profile_template_id,
    )
    return {"code": 0, "message": "ok", "data": {"profile_list": instances}}
