"""受托宿主的单次候选生成编排；来源漂移留下失败，不改写历史结果。"""

from __future__ import annotations

import copy
import logging
import os
import time

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate as load_parent
from memos.skill_drafting.domain import json_bytes
from memos.skill_release_v2 import generation as provider
from memos.skill_release_v2.artifacts import write_candidate
from memos.skill_release_v2.authority import recheck_contract_context
from memos.skill_release_v2.candidate import make_candidate, proposal_schema
from memos.skill_release_v2.generation import GenerationError
from memos.skill_release_v2.inputs import validate_generation, validate_input_manifest


logger = logging.getLogger(__name__)


def _safe_output(value):
    try:
        path = Path(value).absolute()
        if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("symlink")
        if path.exists():
            raise ValueError("exists")
        return path
    except (OSError, TypeError, ValueError):
        raise GenerationError("generation_v2_output_unavailable") from None


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, value, generator):
    # 独占写入并刷盘，不覆盖开始记录、成功公共响应或先前失败。
    generator.assert_public(value)
    try:
        raw = json_bytes(value) + b"\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except (OSError, ValueError, TypeError):
        raise GenerationError("generation_v2_storage_failed") from None


def generate_revision(
    parent_directory,
    output,
    generator,
    *,
    contexts,
    input_manifest,
    exposure_manifest,
    authority_bindings,
    source_recheck,
) -> Path:
    """成功返回新候选 manifest；失败返回追加的白名单诊断。

    source_recheck 是宿主绑定真实 Memory 回读的能力，不接受序列化的信任标记。
    曝光清单只做既定结构和输入绑定校验，不声称认证完整曝光历史。
    """
    output = _safe_output(output)
    if not isinstance(generator, provider.OpenAIJSONGeneratorV2) or not callable(source_recheck):
        raise GenerationError("generation_v2_host_capability_missing")
    if generator.request_attempts:
        raise GenerationError("generation_v2_already_attempted")
    try:
        parent = load_parent(parent_directory)
        packet = copy.deepcopy(parent["packet"])
        contexts, manifest, exposure, bindings = copy.deepcopy(
            (contexts, input_manifest, exposure_manifest, authority_bindings)
        )
        validate_input_manifest(
            manifest,
            packet,
            contexts=contexts,
            parent=parent,
            exposure_manifest=exposure,
            output_schema=proposal_schema(),
        )
        generator.assert_public(manifest)
        if manifest["service_identity"] != generator.service_identity:
            raise GenerationError("generation_v2_service_changed")
    except GenerationError:
        raise
    except Exception:
        raise GenerationError("generation_v2_input_invalid") from None
    try:
        output.mkdir(parents=True, mode=0o700)
        _fsync_directory(output.parent)
    except OSError:
        raise GenerationError("generation_v2_output_unavailable") from None

    start = time.monotonic()
    record = None

    def diagnostic(error, stage):
        if isinstance(error, GenerationError) and error.diagnostic is not None:
            return provider.validate_failure_diagnostic(error.diagnostic)
        code = (
            error.code
            if isinstance(error, GenerationError)
            else "generation_v2_" + stage + "_failed"
        )
        metadata = (
            None
            if record is None
            else (
                record["reported_model"],
                record["finish_reason"],
                record["usage"],
                record["usage_status"],
            )
        )
        return provider.failure_diagnostic(
            manifest,
            stage=stage,
            code=code,
            elapsed_seconds=round(time.monotonic() - start, 6),
            request_attempts=generator.request_attempts,
            response_received=generator.provider_response_received,
            metadata=metadata,
        )

    def failed(error, stage, *, filename="failure.json"):
        value = diagnostic(error, stage)
        try:
            _write(output / filename, value, generator)
        except Exception:
            # 记录写入失败不能覆盖原始失败；只把安全诊断及布尔事实交还宿主。
            raise GenerationError(value["code"], diagnostic=value, retention_failed=True) from None
        return output / filename

    def recheck(supplied_packet):
        if json_bytes(supplied_packet) != json_bytes(packet):
            raise GenerationError("generation_v2_packet_changed")
        try:
            source_recheck(copy.deepcopy(packet))
        except Exception:
            raise GenerationError("generation_v2_source_changed") from None
        try:
            if json_bytes(load_parent(parent_directory)) != json_bytes(parent):
                raise ValueError("parent")
        except Exception:
            raise GenerationError("generation_v2_parent_changed") from None
        try:
            for context in contexts:
                recheck_contract_context(context, authority_bindings=bindings)
        except Exception:
            raise GenerationError("generation_v2_context_changed") from None
        if provider.current_producer() != manifest["producer"]:
            raise GenerationError("generation_v2_producer_changed")

    # 输入重推通过后才允许留存；未开始请求的预检失败也明确记为零次尝试。
    _write(output / "input-manifest.json", manifest, generator)
    try:
        recheck(packet)
    except Exception as error:
        return failed(error, "precheck")
    _write(
        output / "request-started.json",
        {
            "schema_version": 1,
            "kind": "candidate_generation_attempt",
            "status": "unknown",
            **{
                key: manifest[key]
                for key in (
                    "input_manifest_id",
                    "request_sha256",
                    "producer_digest",
                    "service_identity",
                    "requested_model",
                )
            },
        },
        generator,
    )

    primary, result = None, None
    stage = "generate"
    try:
        result = generator.generate(
            packet,
            contexts=contexts,
            parent=parent,
            input_manifest=manifest,
            exposure_manifest=exposure,
        )
    except GenerationError as error:
        primary, result = error, error.public_result
    except Exception:
        primary = GenerationError("generation_v2_generate_failed")
    if result is not None:
        try:
            proposal, record = result
            validate_generation(record, proposal, input_manifest=manifest)
            generator.assert_public(proposal)
            generator.assert_public(record)
            _write(output / "proposal.json", proposal, generator)
            _write(output / "generation.json", record, generator)
        except Exception as error:
            if primary is None:
                primary, stage = error, "retain_response"

    # 已知的主要失败先落盘；后续回读失败另追加，绝不替换主要错误码。
    primary_path, retention_error = None, None
    if primary is not None:
        try:
            primary_path = failed(primary, stage)
        except GenerationError as error:
            retention_error = error
    try:
        recheck(packet)
    except Exception as error:
        if primary is None:
            return failed(error, "postcheck")
        try:
            failed(error, "postcheck", filename="postcheck-failure.json")
        except GenerationError:
            # 主要记录仍是终态；日志不包含路径、异常文本或任何供应商对象。
            logger.warning("generation_v2_secondary_retention_failed")
    if retention_error is not None:
        raise retention_error from None
    if primary_path is not None:
        return primary_path

    try:
        candidate = make_candidate(
            proposal,
            packet,
            skill_id=parent["candidate"]["skill_id"],
            contexts=contexts,
            parent=parent,
            input_manifest=manifest,
            exposure_manifest=exposure,
            generation=record,
        )
    except Exception as error:
        return failed(error, "validate")
    try:
        # writer 在发布前后调用同一回读闭包，覆盖整个封存窗口。
        return write_candidate(
            output / "candidate",
            packet,
            proposal,
            candidate,
            record,
            contexts=contexts,
            parent=parent,
            input_manifest=manifest,
            exposure_manifest=exposure,
            authority_bindings=bindings,
            source_recheck=recheck,
        )
    except Exception as error:
        return failed(error, "seal")
