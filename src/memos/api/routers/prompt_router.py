"""Routes for project prompt configuration.

One place for the prompt endpoints, so the full server and the dev-only prompt
service (``scripts/dev/prompt_service.py``) serve exactly the same routes and
cannot drift apart. ``build_router`` takes the handler so each app can inject
its own dependencies.

The five ``*_extract_prompt`` / ``debug_extract`` endpoints are the shape the
console used before the registry existed; they map onto the ``extract.chat``
slot.

Every route is a plain ``def``. The handlers are synchronous — SQLite plus, for
``test``/``generate``, a blocking model call joined with a 90s timeout — so an
``async def`` would run them on the event loop and stall the whole process. As
``def`` FastAPI puts them on the worker thread pool, where they can wait without
blocking anything else.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from memos.api.exceptions import PromptAPIError
from memos.api.handlers.prompt_handler import model_list
from memos.api.product_models import (
    DebugExtractRequest,
    ExtractDebugResponse,
    ExtractPromptRequest,
    ExtractPromptResponse,
    GenerateExtractPromptRequest,
    ModelListResponse,
    PromptDetailRequest,
    PromptDetailResponse,
    PromptGenerateRequest,
    PromptGenerateResponse,
    PromptListRequest,
    PromptListResponse,
    PromptResetRequest,
    PromptResetResponse,
    PromptResponse,
    PromptSaveRequest,
    PromptSaveResponse,
    PromptTestRequest,
    PromptTestResponse,
    PromptValidateRequest,
    PromptValidateResponse,
    PromptVersionsRequest,
    PromptVersionsResponse,
    SaveExtractPromptRequest,
)
from memos.exceptions import PromptBusyError
from memos.log import get_logger
from memos.mem_reader.prompt_registry import MAX_REQUEST_BYTES, MAX_TEST_REQUEST_BYTES


if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from memos.api.handlers.prompt_handler import PromptHandler


logger = get_logger(__name__)


class PromptAPIRoute(APIRoute):
    """Turn unexpected failures into the prompt contract's own error shape.

    Every route maps the errors it knows about; this is the backstop for the
    rest, so a bug cannot answer with a debug traceback or a bare 500, and a
    locked SQLite file does not look like a crash.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except (HTTPException, RequestValidationError):
                raise
            except PromptBusyError as exc:
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": "busy",
                        "message": str(exc),
                        "data": {"retry_after_sec": exc.retry_after_sec},
                    },
                    headers={"retry-after": str(exc.retry_after_sec)},
                )
            except Exception:
                logger.exception("[PromptAPI] unhandled error on %s", request.url.path)
                return JSONResponse(
                    status_code=500,
                    content={
                        "code": "internal_error",
                        "message": "服务内部错误，请稍后重试。",
                        "data": None,
                    },
                )

        return handler


PROMPT_PATH_PREFIX = "/product/prompts/"


class PromptBodyLimitMiddleware:
    """Cap request bodies on the prompt routes before anything parses them.

    FastAPI reads and decodes the whole body before a dependency runs, and a
    missing ``Content-Length`` (chunked upload) skips the header check entirely.
    This sits in front of the app, counts the bytes actually received and stops
    as soon as the body is too large, so the rest never enters memory.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        prefix: str = PROMPT_PATH_PREFIX,
        max_bytes: int = MAX_REQUEST_BYTES,
        test_max_bytes: int = MAX_TEST_REQUEST_BYTES,
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.max_bytes = max_bytes
        self.test_max_bytes = test_max_bytes

    def _limit(self, path: str) -> int:
        return self.test_max_bytes if path.endswith("/test") else self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        limit = self._limit(path)
        body = bytearray()
        more = True
        while more:
            message: Message = await receive()
            if message["type"] != "http.request":
                # The client went away mid-body; nothing to answer.
                return
            body += message.get("body", b"")
            more = bool(message.get("more_body", False))
            if len(body) > limit:
                await self._reject(send, limit)
                return

        replayed = {"done": False}

        async def replay() -> Message:
            if not replayed["done"]:
                replayed["done"] = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send, limit: int) -> None:
        payload = json.dumps(
            {
                "code": "input_too_large",
                "message": f"请求体超过 {limit // 1024} KiB，请减少输入内容后重试。",
                "data": None,
            },
            ensure_ascii=False,
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def install_body_limits(app, *, prefix: str = PROMPT_PATH_PREFIX) -> None:
    """Add :class:`PromptBodyLimitMiddleware` to an app serving the prompt routes."""
    app.add_middleware(PromptBodyLimitMiddleware, prefix=prefix)


def _body_size_limit(max_bytes: int):
    """Cheap header check; the real guard is :class:`PromptBodyLimitMiddleware`."""

    async def dependency(request: Request) -> None:
        raw_length = request.headers.get("content-length")
        if raw_length and raw_length.isdigit() and int(raw_length) > max_bytes:
            raise PromptAPIError(
                413,
                "input_too_large",
                f"请求体超过 {max_bytes // 1024} KiB，请减少输入内容后重试。",
            )

    return dependency


_body_too_large = _body_size_limit(MAX_REQUEST_BYTES)
_test_body_too_large = _body_size_limit(MAX_TEST_REQUEST_BYTES)


def build_router(handler: PromptHandler, tags: list[str] | None = None) -> APIRouter:
    """Return the prompt routes bound to ``handler``."""
    router = APIRouter(tags=list(tags or []), route_class=PromptAPIRoute)

    @router.post(
        "/get_extract_prompt",
        summary="Read a project's extract prompt",
        response_model=ExtractPromptResponse,
    )
    def get_extract_prompt(request: ExtractPromptRequest) -> ExtractPromptResponse:
        """Return the prompt saved for ``mem_cube_id``, or the built-in default."""
        return handler.get_prompt(request)

    @router.post(
        "/save_extract_prompt",
        summary="Save a project's extract prompt",
        response_model=PromptResponse,
    )
    def save_extract_prompt(request: SaveExtractPromptRequest) -> PromptResponse:
        """Persist the full extraction prompt for one project.

        The prompt must keep the ``${conversation}`` placeholder.
        """
        return handler.save(request)

    @router.post(
        "/reset_extract_prompt",
        summary="Reset a project's extract prompt",
        response_model=PromptResponse,
    )
    def reset_extract_prompt(request: ExtractPromptRequest) -> PromptResponse:
        """Delete the saved prompt and return the built-in default."""
        return handler.reset(request)

    @router.post(
        "/generate_extract_prompt",
        summary="Rewrite a project's extract prompt from an instruction",
        response_model=PromptResponse,
    )
    def generate_extract_prompt(request: GenerateExtractPromptRequest) -> PromptResponse:
        """Ask the configured chat model to rewrite the prompt. Not persisted."""
        return handler.generate(request)

    @router.post(
        "/debug_extract",
        summary="Run extraction once with a project's prompt",
        response_model=ExtractDebugResponse,
    )
    def debug_extract(request: DebugExtractRequest) -> ExtractDebugResponse:
        """Dry run: no memory or graph writes."""
        return handler.debug_extract(request)

    @router.get("/models", summary="List models in use", response_model=ModelListResponse)
    def list_models() -> ModelListResponse:
        """Models actually configured in this process, read from the environment."""
        return model_list()

    @router.post(
        "/prompts/list",
        summary="List every project-overridable prompt with its state",
        response_model=PromptListResponse,
    )
    def prompts_list(
        request: PromptListRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptListResponse:
        """Names, languages and per-language state; no prompt bodies."""
        return handler.list_prompts(request)

    @router.post(
        "/prompts/detail",
        summary="One prompt: registration, defaults and effective bodies",
        response_model=PromptDetailResponse,
    )
    def prompts_detail(
        request: PromptDetailRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptDetailResponse:
        """Full registration plus the built-in default and live body per language."""
        return handler.detail(request)

    @router.post(
        "/prompts/versions",
        summary="Version history of one prompt and language",
        response_model=PromptVersionsResponse,
    )
    def prompts_versions(
        request: PromptVersionsRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptVersionsResponse:
        """Newest first; ``before_version_id`` pages deeper into the history."""
        return handler.versions(request)

    @router.post(
        "/prompts/validate",
        summary="Statically validate a prompt draft",
        response_model=PromptValidateResponse,
    )
    def prompts_validate(
        request: PromptValidateRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptValidateResponse:
        """Always HTTP 200: errors block saving, warnings do not."""
        return handler.validate(request)

    @router.post(
        "/prompts/save",
        summary="Save a new prompt version or roll back to an old one",
        response_model=PromptSaveResponse,
    )
    def prompts_save(
        request: PromptSaveRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptSaveResponse:
        """Appends to the version table; ``base_version_id`` guards concurrent edits."""
        return handler.save_version(request)

    @router.post(
        "/prompts/test",
        summary="Run one prompt with a real model, without saving anything",
        response_model=PromptTestResponse,
    )
    def prompts_test(
        request: PromptTestRequest, _limit: None = Depends(_test_body_too_large)
    ) -> PromptTestResponse:
        """Draft or effective body; ``compare_default`` adds a run of the default."""
        return handler.test_prompt(request)

    @router.post(
        "/prompts/generate",
        summary="Rewrite a prompt from a one-line instruction",
        response_model=PromptGenerateResponse,
    )
    def prompts_generate(
        request: PromptGenerateRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptGenerateResponse:
        """Returns the rewritten body plus its static validation; not saved."""
        return handler.generate_prompt(request)

    @router.post(
        "/prompts/reset",
        summary="Reset one slot, one prompt, or a whole project to defaults",
        response_model=PromptResetResponse,
    )
    def prompts_reset(
        request: PromptResetRequest, _limit: None = Depends(_body_too_large)
    ) -> PromptResetResponse:
        """Only slots that are currently overridden are reset."""
        return handler.reset_prompts(request)

    return router
