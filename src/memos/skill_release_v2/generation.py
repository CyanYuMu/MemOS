"""单次、无工具的公开 JSON 生成；模型响应不授予候选任何发布资格。"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import stat
import threading
import time

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from memos.exceptions import MemOSError
from memos.skill_drafting.generation import _usage, _valid_usage, assert_public_json
from memos.skill_release_v2.candidate import proposal_schema
from memos.skill_release_v2.capture import _parse_object
from memos.skill_release_v2.inputs import validate_generation, validate_input_manifest


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_CODE = re.compile(r"generation_v2_[a-z0-9_]{1,100}\Z")
_STAGE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FINISH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_IDENTITIES = (
    "input_manifest_id",
    "request_sha256",
    "producer_digest",
    "service_identity",
    "requested_model",
)
_DIAGNOSTIC_FIELDS = {
    "schema_version",
    "kind",
    *_IDENTITIES,
    "reported_model",
    "model_identity_match",
    "finish_reason",
    "usage",
    "usage_status",
    "elapsed_seconds",
    "stage",
    "code",
    "request_attempts",
    "provider_response_received",
}


class GenerationError(MemOSError, ValueError):
    """只暴露固定错误码；public_result 仅可包含已验证的完整公开结果。"""

    def __init__(
        self,
        code: str,
        *,
        diagnostic: dict | None = None,
        public_result=None,
        retention_failed=False,
    ):
        if type(code) is not str or not _CODE.fullmatch(code):
            code = "generation_v2_failed"
        self.code = code
        self.diagnostic = (
            validate_failure_diagnostic(diagnostic) if diagnostic is not None else None
        )
        self.public_result = copy.deepcopy(public_result)
        # 仅供调用宿主观察写盘失败；不扩展持久化诊断字段白名单。
        self._retention_failed = retention_failed is True
        super().__init__(code)

    @property
    def retention_failed(self):
        return self._retention_failed


def validate_failure_diagnostic(value: dict) -> dict:
    """失败只保留选定的元数据，绝不把异常或供应商对象序列化。"""
    try:
        valid = (
            type(value) is dict
            and set(value) == _DIAGNOSTIC_FIELDS
            and type(value["schema_version"]) is int
            and value["schema_version"] == 1
            and value["kind"] == "candidate_generation_failure"
            and all(type(value[k]) is str and _DIGEST.fullmatch(value[k]) for k in _IDENTITIES[:-1])
            and type(value["requested_model"]) is str
            and _MODEL.fullmatch(value["requested_model"])
            and (
                value["reported_model"] is None
                or (
                    type(value["reported_model"]) is str
                    and _MODEL.fullmatch(value["reported_model"])
                )
            )
            and value["model_identity_match"]
            is (
                None
                if value["reported_model"] is None
                else value["reported_model"] == value["requested_model"]
            )
            and (
                value["finish_reason"] is None
                or (
                    type(value["finish_reason"]) is str
                    and _FINISH.fullmatch(value["finish_reason"])
                )
            )
            and type(value["usage"]) is dict
            and _valid_usage(value["usage"])
            and value["usage_status"] in {"reported", "missing", "invalid"}
            and bool(value["usage"]) == (value["usage_status"] == "reported")
            and type(value["elapsed_seconds"]) in (int, float)
            and math.isfinite(value["elapsed_seconds"])
            and value["elapsed_seconds"] >= 0
            and type(value["stage"]) is str
            and _STAGE.fullmatch(value["stage"])
            and type(value["code"]) is str
            and _CODE.fullmatch(value["code"])
            and type(value["request_attempts"]) is int
            and value["request_attempts"] in (0, 1)
            and type(value["provider_response_received"]) is bool
            and (not value["provider_response_received"] or value["request_attempts"] == 1)
        )
        if not valid:
            raise ValueError("shape")
        assert_public_json(value)
        return copy.deepcopy(value)
    except Exception:
        raise GenerationError("generation_v2_invalid_diagnostic") from None


def current_producer() -> dict:
    """回读当前四个明确的生产文件；不跟随文件链接或接受共享硬链接。"""
    files = {}
    try:
        for name in ("authority.py", "candidate.py", "inputs.py", "generation.py"):
            path = Path(__file__).parent / name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("file")
                raw = stream.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("size")
            files[name] = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        raise GenerationError("generation_v2_producer_unavailable") from None
    return {"version": "memos-candidate-generator-v2", "files": files}


def _new_client(*, api_key, base_url, timeout, max_retries, follow_redirects):
    # 可选依赖延迟导入；SDK 构造失败也要释放先创建的 HTTP 客户端。
    try:
        import httpx

        from openai import OpenAI
    except ImportError:
        raise GenerationError("generation_v2_dependency_missing") from None
    http_client = httpx.Client(timeout=timeout, follow_redirects=follow_redirects)
    try:
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
    except BaseException:
        try:
            http_client.close()
        finally:
            raise


def _metadata(response, secrets):
    reported, finish, usage, usage_status = None, None, {}, "missing"
    if response is not None:
        try:
            name = response.model
            if type(name) is str and _MODEL.fullmatch(name):
                assert_public_json(name, secrets)
                reported = name
        except Exception:
            reported = None
        try:
            choices = response.choices
            if len(choices) == 1:
                reason = choices[0].finish_reason
                if type(reason) is str and _FINISH.fullmatch(reason):
                    assert_public_json(reason, secrets)
                    finish = reason
        except Exception:
            finish = None
        try:
            usage, usage_status = _usage(response)
        except Exception:
            usage, usage_status = {}, "invalid"
    return reported, finish, usage, usage_status


def failure_diagnostic(
    manifest, *, stage, code, elapsed_seconds, request_attempts, response_received, metadata=None
):
    """仅从已认证输入和公开响应元数据构造诊断；调用次数不表示计费次数。"""
    reported, finish, usage, status = metadata or (None, None, {}, "missing")
    return validate_failure_diagnostic(
        {
            "schema_version": 1,
            "kind": "candidate_generation_failure",
            **{key: manifest[key] for key in _IDENTITIES},
            "reported_model": reported,
            "model_identity_match": None
            if reported is None
            else reported == manifest["requested_model"],
            "finish_reason": finish,
            "usage": usage,
            "usage_status": status,
            "elapsed_seconds": elapsed_seconds,
            "stage": stage,
            "code": code,
            "request_attempts": request_attempts,
            "provider_response_received": response_received,
        }
    )


class OpenAIJSONGeneratorV2:
    """每实例至多一次请求尝试；timeout 只约束 SDK/HTTP I/O。"""

    def __init__(self, api_key, base_url, client_factory=None):
        try:
            if (
                type(api_key) is not str
                or not api_key.strip()
                or len(api_key) > 4096
                or "\x00" in api_key
            ):
                raise ValueError("key")
            if (
                type(base_url) is not str
                or len(base_url) > 4096
                or any(c.isspace() or ord(c) < 32 for c in base_url)
            ):
                raise ValueError("url")
            parts = urlsplit(base_url)
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError("url")
            _ = parts.port
            normalized = urlunsplit(
                (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
            )
            if client_factory is not None and not callable(client_factory):
                raise ValueError("factory")
        except (ValueError, TypeError, AttributeError):
            raise GenerationError("generation_v2_configuration_invalid") from None
        self._service_identity = hashlib.sha256(normalized.encode()).hexdigest()
        self._api_key = api_key
        self._base_url = normalized
        self._secrets = (api_key, base_url, normalized)
        self._factory = client_factory or _new_client
        self._lock = threading.Lock()
        self._claimed = False
        self._request_attempts = 0
        self._provider_response_received = False

    @property
    def service_identity(self):
        return self._service_identity

    @property
    def request_attempts(self):
        return self._request_attempts

    @property
    def provider_response_received(self):
        return self._provider_response_received

    def assert_public(self, value):
        """宿主落盘前复用同一凭据红线；不对内容做静默删改。"""
        try:
            assert_public_json(value, self._secrets)
        except Exception:
            raise GenerationError("generation_v2_private_content") from None

    def generate(self, packet, *, contexts, parent, input_manifest, exposure_manifest):
        if not self._lock.acquire(blocking=False):
            raise GenerationError("generation_v2_busy")
        try:
            if self._claimed:
                raise GenerationError("generation_v2_already_attempted")
            try:
                packet, contexts, parent, manifest, exposure = copy.deepcopy(
                    (packet, contexts, parent, input_manifest, exposure_manifest)
                )
                validate_input_manifest(
                    manifest,
                    packet,
                    contexts=contexts,
                    parent=parent,
                    exposure_manifest=exposure,
                    output_schema=proposal_schema(),
                )
                self.assert_public(manifest)
                if manifest["service_identity"] != self.service_identity:
                    raise GenerationError("generation_v2_service_changed")
                if current_producer() != manifest["producer"]:
                    raise GenerationError("generation_v2_producer_changed")
            except GenerationError:
                raise
            except Exception:
                raise GenerationError("generation_v2_input_invalid") from None
            self._claimed = True
            return self._request(manifest)
        finally:
            self._lock.release()

    def _request(self, manifest):
        start, client, response, result, failure = time.monotonic(), None, None, None, None
        stage = "client"
        try:
            client = self._factory(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=manifest["parameters"]["timeout_seconds"],
                max_retries=0,
                follow_redirects=False,
            )
            stage = "precheck"
            if current_producer() != manifest["producer"]:
                raise GenerationError("generation_v2_producer_changed")
            stage = "request"
            self._request_attempts = 1
            response = client.chat.completions.create(
                model=manifest["requested_model"],
                messages=copy.deepcopy(manifest["messages"]),
                temperature=0,
                max_tokens=manifest["parameters"]["max_tokens"],
                response_format={"type": "json_object"},
            )
            self._provider_response_received = True
            stage = "response"
            result = self._parse_response(response, manifest, start)
        except Exception as error:
            failure = (
                error.code
                if isinstance(error, GenerationError)
                else "generation_v2_" + stage + "_failed"
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    if failure is None:
                        failure, stage = "generation_v2_client_close_failed", "close"
        if failure is not None:
            diagnostic = failure_diagnostic(
                manifest,
                stage=stage,
                code=failure,
                elapsed_seconds=round(time.monotonic() - start, 6),
                request_attempts=self.request_attempts,
                response_received=self.provider_response_received,
                metadata=_metadata(response, self._secrets),
            )
            raise GenerationError(failure, diagnostic=diagnostic, public_result=result) from None
        return result

    def _parse_response(self, response, manifest, start):
        if len(response.choices) != 1:
            raise GenerationError("generation_v2_invalid_response")
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise GenerationError("generation_v2_incomplete_response")
        message = choice.message
        if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
            raise GenerationError("generation_v2_unexpected_tools")
        if getattr(message, "refusal", None):
            raise GenerationError("generation_v2_refused")
        content = message.content
        if type(content) is not str or not content.strip() or len(content.encode()) > 131072:
            raise GenerationError("generation_v2_invalid_response")
        self.assert_public(content)
        proposal = _parse_object(content)
        self.assert_public(proposal)
        reported = response.model
        if type(reported) is not str or not _MODEL.fullmatch(reported):
            raise GenerationError("generation_v2_model_identity_missing")
        usage, status = _metadata(response, self._secrets)[2:]
        try:
            sdk_version = version("openai")
        except PackageNotFoundError:
            sdk_version = "unavailable"
        record = {
            "schema_version": 2,
            "kind": "candidate_generation",
            "provider": "openai_compatible_json",
            **{
                key: copy.deepcopy(manifest[key])
                for key in (*_IDENTITIES, "producer", "parameters", "prompt_sha256", "messages")
            },
            "sdk_version": sdk_version,
            "reported_model": reported,
            "model_identity_match": reported == manifest["requested_model"],
            "schema_sha256": manifest["output_schema_sha256"],
            "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "response_content": content,
            "finish_reason": "stop",
            "usage": usage,
            "usage_status": status,
            "elapsed_seconds": round(time.monotonic() - start, 6),
        }
        self.assert_public(record)
        validate_generation(record, proposal, input_manifest=manifest)
        # 不验证 proposal 的候选语义结构；完整公开响应要先交给宿主留存。
        return proposal, record
