"""
Add handler for memory addition functionality (Class-based version).

This module provides a class-based implementation of add handlers,
using dependency injection for better modularity and testability.
"""

from typing import Any

from pydantic import validate_call

from memos.api.handlers.base_handler import BaseHandler, HandlerDependencies
from memos.api.message_store import (
    build_message_store,
    normalize_messages,
    raw_message_storage_enabled,
)
from memos.api.product_models import (
    APIADDRequest,
    APIExtractRequest,
    APIFeedbackRequest,
    MemoryResponse,
)
from memos.llms import usage as llm_usage
from memos.log import summarize_add_request
from memos.mem_reader.custom_extract import use_custom_extract_prompt
from memos.mem_reader.prompt_runtime import prompt_scope
from memos.memories.textual.item import (
    list_all_fields,
)
from memos.multi_mem_cube.composite_cube import CompositeCubeView
from memos.multi_mem_cube.single_cube import SingleCubeView
from memos.multi_mem_cube.views import MemCubeView
from memos.plugins.hooks import hookable
from memos.types import MessageList


class AddHandler(BaseHandler):
    """
    Handler for memory addition operations.

    Handles text memory additions with sync/async support.
    """

    def __init__(self, dependencies: HandlerDependencies):
        """
        Initialize add handler.

        Args:
            dependencies: HandlerDependencies instance
        """
        super().__init__(dependencies)
        self._validate_dependencies(
            "naive_mem_cube", "mem_reader", "mem_scheduler", "feedback_server"
        )

    @hookable("add")
    def handle_add_memories(self, add_req: APIADDRequest) -> MemoryResponse:
        """
        Main handler for add memories endpoint.

        Orchestrates the addition of text memories,
        supporting concurrent processing.

        Args:
            add_req: Add memory request (deprecated fields are converted in model validator)

        Returns:
            MemoryResponse with added memory information
        """
        self.logger.info(
            f"[DIAGNOSTIC] server_router -> add_handler.handle_add_memories called "
            f"(Modified at 2025-11-29 18:46). request={summarize_add_request(add_req)}"
        )
        # 本次请求的模型 token 计数从 0 开始；抽取里所有 chat.completions 都累计到它上面
        # （见 memos/llms/usage.py）。返回时挂到响应的 usage 字段。
        llm_usage.start()

        if add_req.info:
            add_req.info = self._sanitize_info(add_req.info)

        try:
            cube_view = self._build_cube_view(add_req)
        except ValueError as e:
            # A group-chat user_id list is not a tenant. Silently enqueueing it
            # used to fail inside the scheduler and still return success.
            return MemoryResponse(message=str(e), code=400, data=[])

        @validate_call
        def _check_messages(messages: MessageList) -> None:
            pass

        if add_req.is_feedback:
            try:
                messages = add_req.messages
                _check_messages(messages)

                chat_history = add_req.chat_history if add_req.chat_history else []
                concatenate_chat = chat_history + messages

                last_user_index = max(
                    i for i, d in enumerate(concatenate_chat) if d["role"] == "user"
                )
                feedback_content = concatenate_chat[last_user_index]["content"]
                feedback_history = concatenate_chat[:last_user_index]

                feedback_req = APIFeedbackRequest(
                    user_id=add_req.user_id,
                    session_id=add_req.session_id,
                    task_id=add_req.task_id,
                    history=feedback_history,
                    feedback_content=feedback_content,
                    writable_cube_ids=add_req.writable_cube_ids,
                    async_mode=add_req.async_mode,
                    info=add_req.info,
                )
                process_record = cube_view.feedback_memories(feedback_req)

                self.logger.info(
                    f"[ADDFeedbackHandler] Final feedback results count={len(process_record)}"
                )

                return MemoryResponse(
                    message="Memory feedback successfully",
                    data=[process_record],
                    usage=llm_usage.snapshot(),
                )
            except Exception as e:
                self.logger.warning(f"[ADDFeedbackHandler] Running error: {e}")

        cube_ids = self._resolve_cube_ids(add_req)
        with (
            prompt_scope(cube_ids[0] if cube_ids else None),
            use_custom_extract_prompt(add_req.custom_extract_prompt),
        ):
            results = cube_view.add_memories(add_req)
            self._update_profiles(add_req)

        self._store_raw_messages(add_req)

        self.logger.info(f"[AddHandler] Final add results count={len(results)}")

        return MemoryResponse(
            message="Memory added successfully",
            data=results,
            usage=llm_usage.snapshot(),
        )

    def _store_raw_messages(self, add_req: APIADDRequest) -> None:
        """Keep the messages as sent so ``get_message`` can read the conversation back."""
        if not raw_message_storage_enabled():
            return
        raw_messages = getattr(add_req, "raw_messages", None)
        messages = normalize_messages(
            raw_messages if raw_messages is not None else add_req.messages
        )
        user_id = add_req.user_id
        user_ids = [str(u) for u in user_id] if isinstance(user_id, list | tuple) else [user_id]
        user_ids = [u for u in user_ids if u]
        if not messages or not user_ids or not add_req.session_id:
            return
        try:
            if getattr(self, "_message_store", None) is None:
                self._message_store = build_message_store(self.graph_db)
            for cube_id in self._resolve_cube_ids(add_req):
                self._message_store.add(
                    user_name=cube_id,
                    user_ids=user_ids,
                    conversation_id=add_req.session_id,
                    messages=messages,
                    task_id=add_req.task_id,
                )
        except Exception:
            self.logger.warning("[AddHandler] Storing raw messages failed", exc_info=True)

    def _update_profiles(self, add_req: APIADDRequest) -> None:
        """Map conversation facts onto bound profile fields (cloud `profile` view).

        Opt-in: only runs when ``allow_memory_view`` explicitly contains
        ``"profile"``, so existing traffic pays no extra LLM call.
        """
        allow_views = getattr(add_req, "allow_memory_view", None)
        if not allow_views or "profile" not in allow_views:
            return

        if not _flatten_messages(add_req.messages):
            return

        from memos.api.cloud_store import build_metadata_store
        from memos.api.profile import ProfileService
        from memos.memories.textual.owner import build_write_views

        try:
            service = ProfileService(build_metadata_store(self.graph_db))
        except Exception:
            self.logger.warning("[AddHandler] Profile update unavailable", exc_info=True)
            return

        cube_ids = self._resolve_cube_ids(add_req)
        raw_messages = getattr(add_req, "raw_messages", None)
        owner_views = build_write_views(
            user_id=add_req.user_id,
            agent_id=getattr(add_req, "agent_id", None),
            group_id=getattr(add_req, "group_id", None),
            messages=(
                raw_messages
                if raw_messages is not None
                else (add_req.messages if isinstance(add_req.messages, list) else None)
            ),
            info=add_req.info,
            allow_public=bool(getattr(add_req, "allow_public", False)),
            tenant=cube_ids[0],
        )
        for cube_id in cube_ids:
            for view in owner_views:
                # Group / public views are not profile subjects; their profiles
                # would duplicate participant facts.
                if view.owner.owner_type not in ("user", "agent"):
                    continue
                conversation = _flatten_messages(view.messages)
                if not conversation:
                    continue
                subject = (
                    {"user_id": view.owner.owner_id}
                    if view.owner.owner_type == "user"
                    else {"agent_id": view.owner.owner_id}
                )
                try:
                    service.extract_and_update(
                        user_name=cube_id,
                        llm=self.llm,
                        conversation=conversation,
                        **subject,
                    )
                except Exception:
                    self.logger.warning(
                        "[AddHandler] Profile update failed for cube %s", cube_id, exc_info=True
                    )

    def handle_extract_memories(self, extract_req: APIExtractRequest) -> MemoryResponse:
        """Run extraction without persisting anything (dry run)."""
        self.logger.info(
            "[ExtractHandler] extract_memory called (user_id=%s, cube_ids=%s, mode=%s)",
            extract_req.user_id,
            extract_req.writable_cube_ids,
            extract_req.mode,
        )
        llm_usage.start()

        if extract_req.info:
            extract_req.info = self._sanitize_info(extract_req.info)

        cube_view = self._build_cube_view(extract_req)
        cube_ids = self._resolve_cube_ids(extract_req)
        with (
            prompt_scope(cube_ids[0] if cube_ids else None),
            use_custom_extract_prompt(extract_req.custom_extract_prompt),
        ):
            results = cube_view.extract_memories(extract_req)

        wanted = extract_req.extraction_types
        if wanted is not None:
            results = [
                item
                for item in results
                if ("preference" if item.get("memory_type") == "PreferenceMemory" else "memory")
                in wanted
            ]

        return MemoryResponse(
            message="Memory extraction completed successfully",
            data=results,
            usage=llm_usage.snapshot(),
        )

    def _sanitize_info(self, info: dict[str, Any]) -> dict[str, Any]:
        """Drop keys that collide with reserved metadata fields."""
        exclude_fields = list_all_fields()
        info_len = len(info)
        cleaned = {k: v for k, v in info.items() if k not in exclude_fields}
        if len(cleaned) < info_len:
            self.logger.warning(f"[AddHandler] info fields can not contain {exclude_fields}.")
        return cleaned

    def _resolve_cube_ids(self, add_req: APIADDRequest | APIExtractRequest) -> list[str]:
        """
        Normalize target cube ids from add_req.
        Priority:
        1) writable_cube_ids (deprecated mem_cube_id is converted to this in model validator)
        2) fallback to user_id
        """
        if add_req.writable_cube_ids:
            return list(dict.fromkeys(add_req.writable_cube_ids))

        if isinstance(add_req.user_id, list | tuple):
            # `user_id` lists are group-chat participants, not tenants. Without
            # an explicit cube there is no tenant to write to (and passing the
            # list through breaks scheduler validation).
            raise ValueError(  # noqa: TRY004 - contract error, not a type error
                "writable_cube_ids is required when user_id is a list (group chat)"
            )
        if not add_req.user_id:
            raise ValueError("user_id or writable_cube_ids is required")
        return [add_req.user_id]

    def _build_cube_view(self, add_req: APIADDRequest | APIExtractRequest) -> MemCubeView:
        cube_ids = self._resolve_cube_ids(add_req)

        if len(cube_ids) == 1:
            cube_id = cube_ids[0]
            return SingleCubeView(
                cube_id=cube_id,
                naive_mem_cube=self.naive_mem_cube,
                mem_reader=self.mem_reader,
                mem_scheduler=self.mem_scheduler,
                logger=self.logger,
                feedback_server=self.feedback_server,
                searcher=None,
            )
        else:
            single_views = [
                SingleCubeView(
                    cube_id=cube_id,
                    naive_mem_cube=self.naive_mem_cube,
                    mem_reader=self.mem_reader,
                    mem_scheduler=self.mem_scheduler,
                    logger=self.logger,
                    feedback_server=self.feedback_server,
                    searcher=None,
                )
                for cube_id in cube_ids
            ]
            return CompositeCubeView(
                cube_views=single_views,
                logger=self.logger,
            )


def _flatten_messages(messages: Any) -> str:
    """Render add messages into plain text for profile extraction."""
    if isinstance(messages, str):
        return messages
    if not isinstance(messages, list):
        return ""

    lines: list[str] = []
    for message in messages:
        if isinstance(message, str):
            lines.append(message)
            continue
        if not isinstance(message, dict):
            continue
        role = message.get("role") or "user"
        role_name = message.get("role_name")
        speaker = f"{role_name} ({role})" if role_name else str(role)
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in content
            )
        if content:
            lines.append(f"{speaker}: {content}")
    return "\n".join(lines)
