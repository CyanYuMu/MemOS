"""Explicit P2 commands; help stays independent of provider and graph SDKs."""

from __future__ import annotations

import argparse
import json
import math
import os
import re

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally repeats input, including accidentally supplied keys.
        raise ValueError("invalid_arguments")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nonfinite_json")


def _read_json(path: Path, *, maximum: int = 1_000_000) -> dict:
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("invalid_json_file")
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("invalid_json_document")
    return value


def _project(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value) is None:
        raise ValueError("invalid_project")


def _validated_plan(plan: dict) -> dict:
    from memos.skill_drafting.sources import validate_plan

    _project(plan.get("project_id"))
    scope = plan.get("scope")
    if (
        not isinstance(scope, dict)
        or set(scope) != {"service_identity", "cube_id", "user_id", "namespace"}
        or any(
            not isinstance(value, str) or not value.strip() or len(value) > 300 or "\x00" in value
            for value in scope.values()
        )
        or re.fullmatch(r"[a-f0-9]{64}", scope["service_identity"]) is None
    ):
        raise ValueError("invalid_plan_scope")
    # Check the complete shape before constructing a bridge that owns a ledger.
    declared = SimpleNamespace(
        client=SimpleNamespace(service_identity=scope["service_identity"]),
        cube_id=scope["cube_id"],
        user_id=scope["user_id"],
        namespace=scope["namespace"],
    )
    validate_plan(plan, declared)
    return plan


def _load_plan(path: Path) -> dict:
    return _validated_plan(_read_json(path))


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise ValueError("missing_configuration")
    return value


def _bridge(args, plan: dict):
    from memos.skill_experience.bridge import MemoryBridge
    from memos.skill_experience.client import ProductMemoryClient

    plan = _validated_plan(plan)
    if not args.ledger.is_file() or not args.snapshots.is_dir():
        raise ValueError("existing_memory_registry_required")
    token = os.environ.get("MEMOS_SKILL_API_TOKEN")
    if token is not None and (not token.strip() or any(c.isspace() for c in token)):
        raise ValueError("invalid_memory_token")
    client = ProductMemoryClient(
        _required("MEMOS_SKILL_API_URL"),
        timeout=args.memory_timeout,
        headers={"Authorization": f"Bearer {token}"} if token else None,
    )
    scope = plan["scope"]
    if client.service_identity != scope["service_identity"]:
        raise ValueError("source_scope_mismatch")
    return MemoryBridge(
        client,
        args.ledger,
        args.snapshots,
        cube_id=scope["cube_id"],
        user_id=scope["user_id"],
        namespace=scope["namespace"],
    )


def _generator(args):
    from memos.skill_drafting.generation import OpenAIJSONGenerator

    return OpenAIJSONGenerator(
        model=_required("MEMOS_SKILL_LLM_MODEL"),
        api_key=_required("MEMOS_SKILL_LLM_API_KEY"),
        base_url=_required("MEMOS_SKILL_LLM_BASE_URL"),
        timeout=args.timeout,
        max_tokens=args.max_tokens,
    )


def _repository(args):
    from memos.skill_drafting.graph import SkillGraphRepository

    _project(args.project)
    return SkillGraphRepository(
        _required("MEMOS_SKILL_GRAPH_URI"),
        user=_required("MEMOS_SKILL_GRAPH_USER"),
        password=_required("MEMOS_SKILL_GRAPH_PASSWORD"),
        project_id=args.project,
        database=_required("MEMOS_SKILL_GRAPH_DATABASE"),
        memory_database_id=_required("MEMOS_SKILL_GRAPH_MEMORY_DB_ID"),
    )


def _draft(*args, **kwargs):
    from memos.skill_drafting.pipeline import draft

    return draft(*args, **kwargs)


def _load_candidate(path):
    from memos.skill_drafting.artifacts import load_candidate

    return load_candidate(path)


def _import_candidate(*args):
    from memos.skill_drafting.graph_bundle import import_candidate

    return import_candidate(*args)


def _emit(value: dict) -> None:
    from memos.skill_drafting.generation import assert_public_json

    names = (
        "MEMOS_SKILL_API_URL",
        "MEMOS_SKILL_API_TOKEN",
        "MEMOS_SKILL_LLM_BASE_URL",
        "MEMOS_SKILL_LLM_API_KEY",
        "MEMOS_SKILL_GRAPH_URI",
        "MEMOS_SKILL_GRAPH_PASSWORD",
    )
    assert_public_json(value, tuple(os.environ[name] for name in names if os.environ.get(name)))
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _parser() -> _Parser:
    parser = _Parser(
        description="从真实 Memory 提炼未发布候选 Skill，并管理独立 Skill Graph",
        epilog=(
            "连接配置只读环境变量：MEMOS_SKILL_API_URL / MEMOS_SKILL_API_TOKEN（可选）；"
            "MEMOS_SKILL_LLM_BASE_URL / API_KEY / MODEL；"
            "MEMOS_SKILL_GRAPH_URI / USER / PASSWORD / DATABASE / MEMORY_DB_ID。"
            "退出码：0 完成，1 生成任务失败，2 输入或操作错误。"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    drafting = commands.add_parser("draft", help="从固定来源计划生成一个未发布候选")
    drafting.add_argument("--plan", type=Path, required=True)
    drafting.add_argument("--output", type=Path, required=True)
    drafting.add_argument("--skill-id", required=True)
    drafting.add_argument("--timeout", type=float, default=90, help="单次模型请求超时秒数")
    drafting.add_argument("--max-tokens", type=int, default=6000)
    inspect = commands.add_parser("inspect", help="离线验证并查看候选及审查状态")
    inspect.add_argument("--candidate", type=Path, required=True)
    importing = commands.add_parser("graph-import", help="重读来源后原子导入未发布候选")
    importing.add_argument("--candidate", type=Path, required=True)
    for command in (drafting, importing):
        command.add_argument("--ledger", type=Path, required=True)
        command.add_argument("--snapshots", type=Path, required=True)
        command.add_argument("--memory-timeout", type=float, default=30)
    graph_inspect = commands.add_parser("graph-inspect", help="查看一个明确项目的 Skill Graph")
    trace = commands.add_parser("trace", help="沿固定来源路径追溯一条规则")
    trace.add_argument("--rule-key", required=True, help="skill_id@revision#rule_id")
    affected = commands.add_parser("affected", help="查找引用指定 Memory 的候选和规则")
    affected.add_argument("--memory-id", required=True)
    for command in (importing, graph_inspect, trace, affected):
        command.add_argument("--project", required=True)
    return parser


def _validate_options(args) -> None:
    if hasattr(args, "project"):
        _project(args.project)
    if hasattr(args, "memory_timeout") and (
        not math.isfinite(args.memory_timeout) or not 0 < args.memory_timeout <= 300
    ):
        raise ValueError("invalid_memory_timeout")
    if args.command == "draft":
        if (
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", args.skill_id) is None
            or not math.isfinite(args.timeout)
            or not 0 < args.timeout <= 300
            or not 1 <= args.max_tokens <= 16000
        ):
            raise ValueError("invalid_draft_configuration")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("draft_output_exists")
    if (
        args.command == "trace"
        and re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,79}@[a-f0-9]{64}#[A-Za-z0-9][A-Za-z0-9_-]{0,79}",
            args.rule_key,
        )
        is None
    ):
        raise ValueError("invalid_rule_key")
    if args.command == "affected" and (
        not args.memory_id.strip() or len(args.memory_id) > 300 or "\x00" in args.memory_id
    ):
        raise ValueError("invalid_memory_id")


def main(argv: list[str] | None = None) -> int:
    stage = "invalid_arguments"
    try:
        args = _parser().parse_args(argv)
        stage = "configuration_failed"
        _validate_options(args)
        code = 0
        with ExitStack() as resources:
            if args.command == "draft":
                stage = "plan_failed"
                plan = _load_plan(args.plan)
                stage = "configuration_failed"
                bridge = _bridge(args, plan)
                generator = _generator(args)
                resources.callback(generator.close)
                stage = "draft_failed"
                path = _draft(bridge, plan, generator, args.output, skill_id=args.skill_id)
                job = _read_json(path)
                if job.get("status") not in {"completed", "failed"}:
                    raise ValueError("invalid_job_result")
                result = {
                    "status": job["status"],
                    "job": str(path),
                    "candidate_directory": job.get("candidate_directory"),
                    "qualification": job.get("qualification"),
                }
                if job["status"] == "failed":
                    result.update(
                        failure_stage=job.get("failure_stage"), error_code=job.get("error_code")
                    )
                    code = 1
            elif args.command == "inspect":
                stage = "candidate_failed"
                loaded = _load_candidate(args.candidate)
                result = {
                    "status": "completed",
                    **{key: loaded[key] for key in ("candidate", "validation", "manifest")},
                }
            elif args.command == "graph-import":
                stage = "candidate_failed"
                loaded = _load_candidate(args.candidate)
                if loaded["packet"]["project_id"] != args.project:
                    raise ValueError("candidate_project_mismatch")
                plan = _validated_plan(loaded["packet"]["plan"])
                stage = "configuration_failed"
                bridge = _bridge(args, plan)
                repository = _repository(args)
                resources.callback(repository.close)
                stage = "graph_import_failed"
                result = {
                    "status": "completed",
                    "result": _import_candidate(args.candidate, bridge, repository),
                }
            else:
                repository = _repository(args)
                resources.callback(repository.close)
                stage = "graph_query_failed"
                if args.command == "graph-inspect":
                    value = repository.inspect()
                elif args.command == "trace":
                    value = repository.trace_rule(args.rule_key)
                else:
                    value = repository.affected_by_memory(args.memory_id)
                result = {"status": "completed", "result": value}
            stage = "cleanup_failed"
        stage = "output_failed"
        _emit(result)
        return code
    except Exception:
        # Never print raw SDK, HTTP, filesystem or parser exceptions. A failed
        # job is inspected locally; invoking the command again is a new action.
        print(json.dumps({"status": "error", "error_code": "cli_" + stage}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
