"""Turn off hybrid thinking on the LLMs that run memory extraction.

Extraction only needs the structured answer, so thinking just adds tokens and latency.
``MEMOS_EXTRACT_DISABLE_THINKING=false`` restores the provider default.
"""

from __future__ import annotations

import os

from typing import Any

from memos.llms.openai import OpenAILLM
from memos.log import get_logger


logger = get_logger(__name__)

# Models that only run in thinking mode reject a "turn it off" flag.
_THINKING_ONLY_MARKERS = ("reasoner", "-r1", "thinking", "qwq", "qvq")


def extraction_thinking_disabled() -> bool:
    value = os.getenv("MEMOS_EXTRACT_DISABLE_THINKING", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def thinking_off_body(model_name: str) -> dict[str, Any] | None:
    """Request-body fragment that disables thinking for this model family, or ``None``."""
    name = model_name.strip().lower()
    if any(marker in name for marker in _THINKING_ONLY_MARKERS):
        return None
    if "qwen" in name:
        return {"enable_thinking": False}
    if "deepseek" in name:
        return {"thinking": {"type": "disabled"}}
    return None


def disable_thinking(llm: Any) -> None:
    """Merge the thinking-off flag into ``llm``'s ``extra_body``; keys already set win."""
    if llm is None or not isinstance(llm, OpenAILLM) or not extraction_thinking_disabled():
        return
    config = llm.config
    # A backup client shares this body but may serve another model family.
    if config.enable_thinking is not None or config.backup_client:
        return
    body = thinking_off_body(config.model_name_or_path)
    if body is None:
        logger.info(
            "[LLM] No known thinking switch for model %s; leaving it as is",
            config.model_name_or_path,
        )
        return
    if config.extra_body is not None and not isinstance(config.extra_body, dict):
        return
    config.extra_body = {**body, **(config.extra_body or {})}
