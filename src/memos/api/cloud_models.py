"""Request models for the cloud-contract facade (``/add/message`` and friends).

These accept the cloud field names and convert to the internal models in
:meth:`to_internal`, so the handlers keep seeing one shape.
"""

from __future__ import annotations

import os

from typing import Any, Literal

from pydantic import Field, model_validator

from memos.api.product_models import (
    APIADDRequest,
    APIChatCompleteRequest,
    APIExtractRequest,
    APIFeedbackRequest,
    APISearchRequest,
    BaseRequest,
    DeleteMemoryRequest,
    GetMemoryRequest,
    MessagesType,
)


def facade_tenant() -> str | None:
    """Tenant for cloud requests that name no user, e.g. agent-only or group chat."""
    return (os.getenv("MEMOS_CLOUD_TENANT") or "").strip() or None


def _require_tenant(reason: str) -> str:
    tenant = facade_tenant()
    if not tenant:
        raise ValueError(f"{reason} requires MEMOS_CLOUD_TENANT")
    return tenant


class AddMessageRequest(BaseRequest):
    """Cloud ``add/message``."""

    user_id: str | list[str] = Field(..., description="User (or list for group chat)")
    conversation_id: str | None = Field(None, description="Conversation the messages belong to")
    messages: MessagesType = Field(..., description="Messages to extract memories from")
    agent_id: str | list[str] | None = None
    group_id: str | None = None
    app_id: str | None = None
    tags: list[str] | None = Field(None, description="Cloud name for `custom_tags`")
    info: dict[str, Any] | None = None
    allow_memory_view: list[str] | None = None
    allow_public: bool = False
    custom_extract_prompt: dict[str, str] | None = None
    async_mode: bool = Field(True, description="Cloud boolean; maps to sync/async")

    @model_validator(mode="after")
    def _resolve_group_tenant(self) -> AddMessageRequest:
        if isinstance(self.user_id, list) and not facade_tenant():
            raise ValueError("group chat (user_id list) requires MEMOS_CLOUD_TENANT")
        return self

    def to_internal(self) -> APIADDRequest:
        cubes = [facade_tenant()] if isinstance(self.user_id, list) else None
        return APIADDRequest(
            writable_cube_ids=cubes,
            user_id=self.user_id,
            session_id=self.conversation_id,
            messages=self.messages,
            agent_id=self.agent_id,
            group_id=self.group_id,
            app_id=self.app_id,
            custom_tags=self.tags,
            info=self.info,
            allow_memory_view=self.allow_memory_view,
            allow_public=self.allow_public,
            custom_extract_prompt=self.custom_extract_prompt,
            async_mode="async" if self.async_mode else "sync",
        )


class SearchMemoryRequest(BaseRequest):
    """Cloud ``search/memory``."""

    query: str = Field(..., min_length=1)
    user_id: str | None = None
    agent_id: str | list[str] | None = None
    conversation_id: str | None = None
    app_id: str | None = None
    filter: dict[str, Any] | None = None
    include_memory_view: (
        list[Literal["detail_factual", "preference", "profile", "event", "tool_memory"]]
        | None
    ) = None
    memory_limit_number: int | None = Field(None, ge=1, le=25, description="Cloud default 9")
    relativity: float | None = Field(None, ge=0, le=1)

    def to_internal(self) -> APISearchRequest:
        optional = {} if self.relativity is None else {"relativity": self.relativity}
        internal = APISearchRequest(
            query=self.query,
            user_id=self.user_id,
            agent_id=self.agent_id,
            app_id=self.app_id,
            filter=self.filter,
            include_memory_view=self.include_memory_view,
            top_k=self.memory_limit_number or 9,
            **optional,
            session_id=self.conversation_id,
        )
        internal._budget_across_views = True
        return internal


class GetMemoryCloudRequest(BaseRequest):
    """Cloud ``get/memory``."""

    user_id: str | None = None
    agent_id: str | list[str] | None = None
    mem_cube_id: str | None = None
    include_memory_view: (
        list[Literal["detail_factual", "preference", "profile", "event", "tool_memory"]]
        | None
    ) = None
    filter: dict[str, Any] | None = None
    page: int = Field(1, ge=1)
    size: int = Field(10, ge=1, le=50, description="Cloud name for `page_size`")

    @model_validator(mode="after")
    def _require_subject(self) -> GetMemoryCloudRequest:
        if not (self.user_id or self.agent_id or self.mem_cube_id):
            raise ValueError("user_id or agent_id is required")
        if self.agent_id and not (self.user_id or self.mem_cube_id):
            _require_tenant("agent-only read")
        return self

    def to_internal(self) -> GetMemoryRequest:
        return GetMemoryRequest(
            user_id=self.user_id,
            agent_id=self.agent_id,
            mem_cube_id=self.mem_cube_id or self.user_id or facade_tenant(),
            include_memory_view=self.include_memory_view,
            filter=self.filter,
            page=self.page,
            page_size=self.size,
        )


class DeleteMemoryCloudRequest(BaseRequest):
    """Cloud ``delete/memory``."""

    memory_ids: list[str] | None = None
    user_id: str | None = None
    agent_id: str | None = None

    @model_validator(mode="after")
    def _one_scope(self) -> DeleteMemoryCloudRequest:
        if sum(bool(value) for value in (self.memory_ids, self.user_id, self.agent_id)) != 1:
            raise ValueError("exactly one of memory_ids, user_id or agent_id is required")
        return self

    def to_internal(self) -> DeleteMemoryRequest:
        if self.agent_id and not self.user_id:
            # The handler deletes agent memories inside a tenant, so an agent-only
            # request needs the deployment's tenant.
            return DeleteMemoryRequest(
                agent_id=self.agent_id, writable_cube_ids=[_require_tenant("agent-only delete")]
            )
        return DeleteMemoryRequest(
            memory_ids=self.memory_ids, user_id=self.user_id, agent_id=self.agent_id
        )


class FeedbackCloudRequest(BaseRequest):
    """Cloud ``add/feedback``."""

    user_id: str = Field(..., min_length=1)
    conversation_id: str | None = None
    feedback_content: str = Field(..., min_length=1)
    feedback_time: str | None = None
    history: list[dict[str, Any]] | None = None
    agent_id: str | None = None
    app_id: str | None = None
    allow_public: bool = False
    async_mode: bool = True

    def to_internal(self) -> APIFeedbackRequest:
        return APIFeedbackRequest(
            user_id=self.user_id,
            session_id=self.conversation_id or "default_session",
            feedback_content=self.feedback_content,
            feedback_time=self.feedback_time,
            history=self.history or [],
            agent_id=self.agent_id,
            app_id=self.app_id,
            allow_public=self.allow_public,
            async_mode="async" if self.async_mode else "sync",
        )


class ExtractMemoryCloudRequest(BaseRequest):
    """Cloud ``extract/memory``."""

    user_id: str = Field(..., min_length=1)
    conversation_id: str | None = None
    messages: MessagesType = Field(..., description="Messages to extract from")
    extraction_types: list[Literal["memory", "preference"]] | None = None
    custom_extract_prompt: dict[str, str] | None = None
    tags: list[str] | None = None

    def to_internal(self) -> APIExtractRequest:
        return APIExtractRequest(
            user_id=self.user_id,
            session_id=self.conversation_id,
            messages=self.messages,
            extraction_types=self.extraction_types,
            custom_extract_prompt=self.custom_extract_prompt,
            custom_tags=self.tags,
        )


class ChatCloudRequest(BaseRequest):
    """Cloud ``chat`` (non-streaming; ``stream=true`` is refused on this path)."""

    user_id: str = Field(..., min_length=1)
    conversation_id: str | None = None
    query: str = Field(..., min_length=1)
    filter: dict[str, Any] | None = None
    memory_limit_number: int = Field(9, ge=1, le=25)
    include_preference: bool = True
    preference_limit_number: int = Field(6, ge=0)
    relativity: float | None = Field(None, ge=0, le=1)
    model_name: str | None = None
    system_prompt: str | None = None
    tags: list[str] | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    add_message_on_answer: bool = True
    app_id: str | None = None
    agent_id: str | None = None
    info: dict[str, Any] | None = None
    allow_public: bool = False

    def to_internal(self) -> APIChatCompleteRequest:
        optional = {} if self.relativity is None else {"relativity": self.relativity}
        return APIChatCompleteRequest(
            user_id=self.user_id,
            query=self.query,
            session_id=self.conversation_id,
            filter=self.filter,
            top_k=self.memory_limit_number,
            include_preference=self.include_preference,
            pref_top_k=self.preference_limit_number,
            **optional,
            model_name_or_path=self.model_name,
            system_prompt=self.system_prompt,
            tags=self.tags,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            add_message_on_answer=self.add_message_on_answer,
            app_id=self.app_id,
            agent_id=self.agent_id,
            info=self.info,
            allow_public=self.allow_public,
        )
