"""Project-level prompt configuration: legacy extract endpoints plus the registry.

The five ``*_extract_prompt`` / ``debug_extract`` endpoints are what the console's
"custom extraction" page has always called. They now read and write the
``extract.chat`` slot of the prompt registry, so the older console and the newer
prompt workbench share one history.

The ``/product/prompts/*`` endpoints expose the whole registry: every prompt,
every language, with defaults, effective bodies, version history, validation,
save, rollback and reset. They never touch graph, vector, or memory tables.
"""

from __future__ import annotations

import os
import re

from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from memos.api.exceptions import PromptAPIError
from memos.api.handlers.base_handler import BaseHandler
from memos.api.product_models import (
    DebugExtractRequest,
    ExtractDebugData,
    ExtractDebugResponse,
    ExtractDebugResult,
    ExtractPromptData,
    ExtractPromptRequest,
    ExtractPromptResponse,
    GenerateExtractPromptRequest,
    ModelListData,
    ModelListResponse,
    ModelRecord,
    PromptData,
    PromptDetailData,
    PromptDetailRequest,
    PromptDetailResponse,
    PromptEffective,
    PromptGenerateData,
    PromptGenerateRequest,
    PromptGenerateResponse,
    PromptListData,
    PromptListItem,
    PromptListRequest,
    PromptListResponse,
    PromptResetData,
    PromptResetItem,
    PromptResetRequest,
    PromptResetResponse,
    PromptResponse,
    PromptRun,
    PromptSaveData,
    PromptSaveRequest,
    PromptSaveResponse,
    PromptTestData,
    PromptTestRequest,
    PromptTestResponse,
    PromptValidateData,
    PromptValidateRequest,
    PromptValidateResponse,
    PromptVersionItem,
    PromptVersionsData,
    PromptVersionsRequest,
    PromptVersionsResponse,
    SaveExtractPromptRequest,
)
from memos.exceptions import (
    PromptBusyError,
    PromptInputError,
    PromptInputTooLargeError,
    PromptNotFoundError,
    PromptValidationError,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
)
from memos.log import get_logger
from memos.mem_reader import prompt_store
from memos.mem_reader.prompt_config import (
    EXTRACTION_TYPES,
    PROMPT_PLACEHOLDER,
    default_extract_prompt,
    delete_prompt,
    get_saved_prompt,
    normalize_extract_prompt,
    save_prompt,
)
from memos.mem_reader.prompt_registry import (
    MAX_INSTRUCTION_CHARS,
    MAX_NOTE_CHARS,
    MAX_PROMPT_CHARS,
    get_spec,
    validate_body,
)
from memos.mem_reader.trial import (
    LLM_TIMEOUT_SEC,
    MAX_INPUT_CHARS,
    MAX_MESSAGES,
    TrialRun,
    call_with_timeout,
    default_limiter,
    run_trial_with_timeout,
    validate_input,
    with_request_timeout,
)
from memos.mem_reader.utils import parse_json_result


logger = get_logger(__name__)


_REWRITE_SYSTEM = (
    "你是提示词工程专家。用户会给你一份「记忆抽取器」的提示词，以及一句修改要求。"
    "请按修改要求重写这份提示词，保持它原有的输出 JSON 结构（memory list / summary）不变，"
    "只调整抽取规则部分。必须保留 ${conversation} 占位符。"
    "直接输出重写后的完整提示词正文，不要任何解释、不要 markdown 代码块。"
)

_MESSAGES = {
    "invalid_prompt": "提示词没有通过校验，请按错误列表修改后再保存。",
    "invalid_input": "请求参数不完整或格式不对，请检查后重试。",
    "unknown_prompt_key": "没有找到这条提示词，请刷新列表后重试。",
    "version_conflict": "这条提示词已被其他人更新，请刷新后再保存。",
    "unknown_version": "这个历史版本不存在，请刷新版本历史后重试。",
}

# 与部署里真正生效的配置键一一对应。只列能指到具体键的模型；缺失的整行不出现。
_MODEL_ROWS: tuple[dict[str, Any], ...] = (
    {
        "model": "MEMRADER_MODEL",
        "role": "记忆读取",
        "group": "memory",
        "provider": "MEMRADER_API_BASE",
    },
    {
        "model": "MOS_CHAT_MODEL",
        "role": "记忆写入 / 对话",
        "group": "chat",
        "provider": "OPENAI_API_BASE",
    },
    {
        "model": "MOS_EMBEDDER_MODEL",
        "role": "向量化",
        "group": "memory",
        "extra": lambda: _embedding_spec(),
    },
    {
        "model": "MOS_RERANKER_MODEL",
        "role": "重排",
        "group": "memory",
        "extra": lambda: _rerank_spec(),
    },
)


def _env(key: str) -> str:
    return os.getenv(key, "").strip()


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""


def _embedding_spec() -> str:
    dimension = _env("EMBEDDING_DIMENSION")
    return f"{dimension} 维" if dimension else ""


def _rerank_spec() -> str:
    backend = _env("MOS_RERANKER_BACKEND")
    return f"后端 {backend}" if backend else ""


def list_models() -> ModelListData:
    records: list[ModelRecord] = []
    for row in _MODEL_ROWS:
        model = _env(row["model"])
        if not model:
            continue
        parts: list[str] = []
        provider = _host_of(_env(row.get("provider", "")))
        if provider:
            parts.append(provider)
        extra = row["extra"]() if "extra" in row else ""
        if extra:
            parts.append(extra)
        records.append(
            ModelRecord(
                role=row["role"],
                model=model,
                spec=" · ".join(parts),
                source=row["model"],
                group=row["group"],
            )
        )
    return ModelListData(records=records, total=len(records))


def _strip_fence(text: str) -> str:
    fenced = re.match(r"^```(?:\w+)?\s*([\s\S]*?)```$", text.strip())
    return (fenced.group(1) if fenced else text).strip()


def _unknown_key(request_key: str) -> PromptAPIError:
    return PromptAPIError(
        404,
        "unknown_prompt_key",
        f"{_MESSAGES['unknown_prompt_key']}（key={request_key}）",
        {"key": request_key},
    )


def _unknown_version() -> PromptAPIError:
    return PromptAPIError(404, "unknown_version", _MESSAGES["unknown_version"])


def _invalid_prompt(errors: list[dict[str, str]]) -> PromptAPIError:
    return PromptAPIError(
        400,
        "invalid_prompt",
        _MESSAGES["invalid_prompt"],
        {"errors": errors},
    )


class PromptHandler(BaseHandler):
    """Read/write project prompts, run them, and list the models in use."""

    @property
    def _trial_limiter(self):
        """Process-wide trial limit, created on first use."""
        limiter = getattr(self, "_limiter", None)
        if limiter is None:
            limiter = default_limiter()
            self._limiter = limiter
        return limiter

    # ------------------------------------------------------------------
    # Legacy extract prompt endpoints (response shapes unchanged)
    # ------------------------------------------------------------------

    def get_prompt(self, request: ExtractPromptRequest) -> ExtractPromptResponse:
        saved = get_saved_prompt(request.mem_cube_id)
        prompt = saved[0] if saved else default_extract_prompt()
        return ExtractPromptResponse(
            code=200,
            message="Extract prompt loaded",
            data=ExtractPromptData(
                mem_cube_id=request.mem_cube_id,
                prompt=prompt,
                extraction_types=list(EXTRACTION_TYPES),
                updated_at=saved[1] if saved else None,
                source="saved" if saved else "default",
                is_default=saved is None,
            ),
        )

    def save(self, request: SaveExtractPromptRequest) -> PromptResponse:
        try:
            normalize_extract_prompt(request.prompt)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_prompt(request.mem_cube_id, request.prompt)
        return PromptResponse(
            code=200, message="Extract prompt saved", data=PromptData(prompt=request.prompt.strip())
        )

    def reset(self, request: ExtractPromptRequest) -> PromptResponse:
        delete_prompt(request.mem_cube_id)
        return PromptResponse(
            code=200,
            message="Extract prompt reset to default",
            data=PromptData(prompt=default_extract_prompt()),
        )

    def generate(self, request: GenerateExtractPromptRequest) -> PromptResponse:
        if len(request.description or "") > MAX_INSTRUCTION_CHARS:
            raise PromptAPIError(
                413,
                "input_too_large",
                f"修改要求 {len(request.description)} 字符，超过上限 {MAX_INSTRUCTION_CHARS}，请写短一点。",
            )
        prompt = self._current_prompt(request.mem_cube_id)
        try:
            text = self.llm.generate(
                [
                    {"role": "system", "content": _REWRITE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"修改要求：{request.description}\n\n原提示词：\n{prompt}",
                    },
                ]
            )
        except Exception as exc:
            logger.warning("[PromptHandler] legacy generate failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="改写没有成功，模型调用出错，请稍后重试。",
            ) from exc
        rewritten = _strip_fence(text or "")
        if PROMPT_PLACEHOLDER not in rewritten:
            raise HTTPException(
                status_code=502,
                detail=f"model response dropped {PROMPT_PLACEHOLDER}; try again",
            )
        return PromptResponse(
            code=200, message="Extract prompt generated", data=PromptData(prompt=rewritten)
        )

    def debug_extract(self, request: DebugExtractRequest) -> ExtractDebugResponse:
        total_chars = sum(len(message.content or "") for message in request.messages)
        if len(request.messages) > MAX_MESSAGES:
            raise PromptAPIError(
                413,
                "input_too_large",
                f"对话最多 {MAX_MESSAGES} 条消息，当前 {len(request.messages)} 条，请删减后重试。",
            )
        if total_chars > MAX_INPUT_CHARS:
            raise PromptAPIError(
                413,
                "input_too_large",
                f"对话总长度 {total_chars} 字符，超过上限 {MAX_INPUT_CHARS}，请删减后重试。",
            )
        prompt = self._current_prompt(request.mem_cube_id)
        conversation = "\n\n".join(
            f"{'助手' if message.role == 'assistant' else '用户'}：{message.content}"
            for message in request.messages
        )
        try:
            raw = self.llm.generate(
                [{"role": "user", "content": prompt.replace(PROMPT_PLACEHOLDER, conversation)}]
            )
        except Exception as exc:
            logger.warning("[PromptHandler] legacy debug_extract failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="这次抽取没有跑完，模型调用出错，请稍后重试。",
            ) from exc

        results: list[ExtractDebugResult] = []
        try:
            parsed = parse_json_result(raw or "")
        except Exception:
            parsed = None
        memory_list = (parsed or {}).get("memory list") or (parsed or {}).get("memory_list")
        if isinstance(memory_list, list):
            for item in memory_list:
                if not isinstance(item, dict):
                    continue
                content = item.get("value") or item.get("content") or item.get("key") or ""
                if content:
                    results.append(
                        ExtractDebugResult(
                            type=str(item.get("memory_type") or item.get("type") or "memory"),
                            content=str(content),
                        )
                    )
        else:
            # 模型没按结构返回时给一条原文，明确标成 raw，不假装抽取成功。
            results = [ExtractDebugResult(type="raw", content=(raw or "")[:2000])]

        return ExtractDebugResponse(
            code=200,
            message="Extraction completed",
            data=ExtractDebugData(
                extraction_types=request.extraction_types or list(EXTRACTION_TYPES),
                results=results,
            ),
        )

    def _current_prompt(self, mem_cube_id: str) -> str:
        saved = get_saved_prompt(mem_cube_id)
        return saved[0] if saved else default_extract_prompt()

    # ------------------------------------------------------------------
    # Prompt registry endpoints
    # ------------------------------------------------------------------

    def list_prompts(self, request: PromptListRequest) -> PromptListResponse:
        items = prompt_store.list_items(request.mem_cube_id)
        return PromptListResponse(
            code=200,
            message="Prompt list loaded",
            data=PromptListData(prompts=[PromptListItem.model_validate(item) for item in items]),
        )

    def detail(self, request: PromptDetailRequest) -> PromptDetailResponse:
        try:
            data = prompt_store.detail_item(request.mem_cube_id, request.key)
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key) from exc
        return PromptDetailResponse(
            code=200,
            message="Prompt detail loaded",
            data=PromptDetailData.model_validate(data),
        )

    def versions(self, request: PromptVersionsRequest) -> PromptVersionsResponse:
        try:
            versions, has_more = prompt_store.list_versions(
                request.mem_cube_id,
                request.key,
                request.lang,
                limit=request.limit,
                before_version_id=request.before_version_id,
            )
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key) from exc
        return PromptVersionsResponse(
            code=200,
            message="Prompt versions loaded",
            data=PromptVersionsData(
                versions=[PromptVersionItem.model_validate(v) for v in versions],
                has_more=has_more,
            ),
        )

    def validate(self, request: PromptValidateRequest) -> PromptValidateResponse:
        try:
            errors, warnings = validate_body(request.key, request.lang, request.body)
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key) from exc
        return PromptValidateResponse(
            code=200,
            message="Prompt validated",
            data=PromptValidateData(
                ok=not errors,
                errors=[issue.as_dict() for issue in errors],
                warnings=[issue.as_dict() for issue in warnings],
            ),
        )

    def save_version(self, request: PromptSaveRequest) -> PromptSaveResponse:
        if len(request.note or "") > MAX_NOTE_CHARS:
            raise PromptAPIError(
                400,
                "invalid_input",
                f"{_MESSAGES['invalid_input']}（备注最多 {MAX_NOTE_CHARS} 个字符）",
            )
        try:
            result = prompt_store.save_slot(
                request.mem_cube_id,
                request.key,
                request.lang,
                body=request.body,
                restore_from_version_id=request.restore_from_version_id,
                note=request.note or "",
                author=request.author or "",
                base_version_id=request.base_version_id,
            )
        except PromptVersionNotFoundError as exc:
            raise _unknown_version() from exc
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key) from exc
        except PromptValidationError as exc:
            raise _invalid_prompt(exc.errors) from exc
        except PromptVersionConflictError as exc:
            raise PromptAPIError(
                409,
                "version_conflict",
                _MESSAGES["version_conflict"],
                {"current": exc.current},
            ) from exc
        return PromptSaveResponse(
            code=200,
            message="Prompt saved",
            data=PromptSaveData(
                version_id=result["version_id"],
                version_no=result.get("version_no", 0),
                created_at=result["created_at"],
                effective=PromptEffective.model_validate(result["effective"]),
            ),
        )

    def reset_prompts(self, request: PromptResetRequest) -> PromptResetResponse:
        try:
            reset = prompt_store.reset_slots(
                request.mem_cube_id,
                key=request.key,
                lang=request.lang,
                author=request.author or "",
            )
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key or "") from exc
        except PromptValidationError as exc:
            raise PromptAPIError(400, "invalid_input", str(exc)) from exc
        return PromptResetResponse(
            code=200,
            message="Prompts reset to default",
            data=PromptResetData(reset=[PromptResetItem.model_validate(item) for item in reset]),
        )

    def test_prompt(self, request: PromptTestRequest) -> PromptTestResponse:
        try:
            spec = get_spec(request.key)
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key) from exc
        if request.lang not in spec.languages:
            raise PromptAPIError(
                400,
                "invalid_input",
                f"这条提示词没有 {request.lang} 这个语种，可选的是 {', '.join(spec.languages)}。",
            )

        # No body means "run whatever is live for this project": the prompt
        # scope resolves it, so ``used_slots`` reports project/default honestly.
        body = request.body
        if body is not None:
            errors, _warnings = validate_body(request.key, request.lang, body)
            if errors:
                raise _invalid_prompt([issue.as_dict() for issue in errors])

        try:
            input_data = validate_input(spec, request.input)
        except PromptInputTooLargeError as exc:
            raise PromptAPIError(413, "input_too_large", str(exc)) from exc
        except PromptInputError as exc:
            raise PromptAPIError(400, "invalid_input", str(exc)) from exc

        limiter = self._trial_limiter
        planned_runs = 2 if request.compare_default else 1
        try:
            lease = limiter.lease(request.mem_cube_id, runs=planned_runs)
        except PromptBusyError as exc:
            raise PromptAPIError(
                429,
                "busy",
                str(exc),
                {"retry_after_sec": exc.retry_after_sec},
            ) from exc

        # Workers release the lease when they finish; a timed-out run keeps its
        # slot taken until its model call really ends. If a run cannot even
        # start, the runs that never started have to give their share back.
        runs: list[TrialRun] = []
        try:
            runs.append(
                run_trial_with_timeout(
                    spec,
                    label="draft",
                    lang=request.lang,
                    body=body,
                    input_data=input_data,
                    cube_id=request.mem_cube_id,
                    lease=lease,
                    draft_provided=request.body is not None,
                )
            )
            if request.compare_default:
                # The default side must be the built-in text: no draft and no
                # project override may leak into it.
                runs.append(
                    run_trial_with_timeout(
                        spec,
                        label="default",
                        lang=request.lang,
                        body=None,
                        input_data=input_data,
                        cube_id=request.mem_cube_id,
                        lease=lease,
                        ignore_project=True,
                    )
                )
        except BaseException:
            lease.abandon()
            raise

        return PromptTestResponse(
            code=200,
            message="Prompt trial completed",
            data=PromptTestData(runs=[PromptRun.model_validate(run.as_dict()) for run in runs]),
        )

    def generate_prompt(self, request: PromptGenerateRequest) -> PromptGenerateResponse:
        if len(request.instruction or "") > MAX_INSTRUCTION_CHARS:
            raise PromptAPIError(
                413,
                "input_too_large",
                f"修改要求 {len(request.instruction)} 字符，超过上限 {MAX_INSTRUCTION_CHARS}，请写短一点。",
            )
        if request.base_body is not None and len(request.base_body) > MAX_PROMPT_CHARS:
            raise PromptAPIError(
                413,
                "input_too_large",
                f"原始提示词 {len(request.base_body)} 字符，超过上限 {MAX_PROMPT_CHARS}。",
            )
        try:
            spec = get_spec(request.key)
        except PromptNotFoundError as exc:
            raise _unknown_key(request.key) from exc
        if request.lang not in spec.languages:
            raise PromptAPIError(
                400,
                "invalid_input",
                f"这条提示词没有 {request.lang} 这个语种，可选的是 {', '.join(spec.languages)}。",
            )
        base = request.base_body
        if base is None:
            base = prompt_store.resolve(request.mem_cube_id, request.key, request.lang).body

        try:
            rewritten = call_with_timeout(
                lambda: self._rewrite(spec, request, base), seconds=LLM_TIMEOUT_SEC
            )
        except TimeoutError as exc:
            raise PromptAPIError(
                504, "llm_timeout", "模型在 90 秒内没有返回，请稍后重试。"
            ) from exc
        except Exception as exc:
            logger.warning("[PromptGenerate] rewrite failed: %s", exc)
            raise PromptAPIError(
                502, "llm_error", "改写没有成功，模型调用出错，请稍后重试。"
            ) from exc

        rewritten = _strip_fence(rewritten or "")
        errors, warnings = validate_body(request.key, request.lang, rewritten)
        if errors:
            # One automatic retry with the findings spelled out.
            try:
                retried = call_with_timeout(
                    lambda: self._rewrite(spec, request, rewritten, errors),
                    seconds=LLM_TIMEOUT_SEC,
                )
                rewritten = _strip_fence(retried or rewritten)
                errors, warnings = validate_body(request.key, request.lang, rewritten)
            except Exception:  # keep the first attempt if the retry fails
                logger.warning("[PromptGenerate] retry failed; returning the first attempt")

        return PromptGenerateResponse(
            code=200,
            message="Prompt generated",
            data=PromptGenerateData(
                body=rewritten,
                validation=PromptValidateData(
                    ok=not errors,
                    errors=[issue.as_dict() for issue in errors],
                    warnings=[issue.as_dict() for issue in warnings],
                ),
            ),
        )

    def _rewrite(
        self,
        spec,
        request: PromptGenerateRequest,
        base: str,
        errors: list | None = None,
    ) -> str:
        placeholders = "、".join(p.token for p in spec.placeholders)
        system = (
            f"你是提示词工程专家。用户会给你一份「{spec.title.get('zh', spec.key)}」提示词，"
            "以及一句修改要求。请按修改要求重写这份提示词，保持它原有的输出结构不变，只调整规则部分。"
            f"必须保留这些占位符：{placeholders}。"
            "语法是：" + ("用 ${name} 形式" if spec.syntax == "dollar" else "用 {name} 形式") + "。"
            "直接输出重写后的完整提示词正文，不要任何解释、不要 markdown 代码块。"
        )
        user = f"修改要求：{request.instruction}\n\n原提示词：\n{base}"
        if errors:
            problems = "；".join(f"{issue.code}: {issue.message}" for issue in errors)
            user += f"\n\n上一次结果没有通过校验：{problems}。请修正后重新输出完整正文。"
        return with_request_timeout(self.llm).generate(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )


def model_list() -> ModelListResponse:
    data = list_models()
    return ModelListResponse(code=200, message="Model list loaded", data=data)
