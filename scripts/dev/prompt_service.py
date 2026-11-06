"""只跑提示词接口的开发服务：只依赖 SQLite 与 LLM，监听 18101，供控制面联调。

启动：

    MEMOS_PROMPT_CONFIG_DB=/tmp/prompts.db python scripts/dev/prompt_service.py

它挂的路由与线上 `server_api` 完全同源（`memos.api.routers.prompt_router`），所以
控制面按 `/product/prompts/*` 与旧 `prompt-config/*` 写的代理可以直接联调；以后新加
提示词路由会自动带上。这里不初始化图库、向量库、调度器与 Redis。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from memos.api.exceptions import APIExceptionHandler
from memos.api.handlers.base_handler import HandlerDependencies
from memos.api.handlers.prompt_handler import PromptHandler
from memos.api.routers.prompt_router import build_router, install_body_limits
from memos.log import get_logger


DEFAULT_PORT = 18101
DEFAULT_HOST = "127.0.0.1"

logger = get_logger(__name__)


def build_llm():
    """The extraction model, configured exactly like the data plane.

    Returns ``None`` when no LLM key is configured: the registry endpoints do
    not need a model, and ``generate`` / ``debug_extract`` then answer 502
    instead of hanging on a request to the public OpenAI endpoint.
    """
    if not (os.getenv("MEMRADER_API_KEY") or os.getenv("OPENAI_API_KEY")):
        logger.warning("[PromptService] no LLM key in env; generate/debug will answer 502")
        return None
    try:
        from memos.api.config import APIConfig
        from memos.configs.llm import LLMConfigFactory
        from memos.llms.factory import LLMFactory

        config = LLMConfigFactory.model_validate(APIConfig.get_memreader_config())
        return LLMFactory.from_config(config)
    except Exception:
        logger.warning("[PromptService] LLM config is unusable; generate/debug will answer 502")
        return None


def create_app(llm=None) -> FastAPI:
    """Build the prompt-only app; ``llm`` is injected so tests can pass a stub."""
    app = FastAPI(
        title="MemOS prompt service (dev)",
        description="Prompt endpoints only: SQLite storage plus one LLM.",
        version="0.1.0",
    )
    handler = PromptHandler(HandlerDependencies(llm=llm))
    app.include_router(build_router(handler, tags=["Prompt API"]), prefix="/product")
    install_body_limits(app)

    app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)
    app.exception_handler(ValueError)(APIExceptionHandler.value_error_handler)
    app.exception_handler(HTTPException)(APIExceptionHandler.http_error_handler)
    app.exception_handler(Exception)(APIExceptionHandler.global_exception_handler)

    @app.get("/health")
    def health_check() -> dict:
        """Liveness plus whether a model is wired in."""
        return {
            "status": "healthy",
            "service": "memos-prompt-service",
            "llm_configured": llm is not None,
        }

    return app


app = create_app(llm=build_llm())


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the prompt endpoints on their own.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PROMPT_SERVICE_PORT", str(DEFAULT_PORT)))
    )
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
