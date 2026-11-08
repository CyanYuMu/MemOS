"""Explicit, credential-safe command-line entry for the fixed Claude adapter."""

from __future__ import annotations

import argparse
import json
import os
import shutil

from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, runtime_alias
from memos.skill_adapter.runner import RunConfig, run_case, validate_config


_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
_ERROR_CODES = {
    "capability_missing",
    "artifact_mismatch",
    "alias_conflict",
    "unknown_command",
    "load_unverified",
    "capture_incomplete",
    "invalid_arguments",
    "invalid_skill_json",
    "invalid_report",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default errors can echo arbitrary unknown arguments, including credentials.
        raise ValueError("invalid_arguments")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_skill_json")
        result[key] = value
    return result


def _load_skill(path: Path) -> SkillRevision:
    if path.stat().st_size > 1_000_000:
        raise ValueError("invalid_skill_json")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    required = {"skill_id", "revision", "body", "rule_ids"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - (required | {"requires"})
        or not isinstance(value["rule_ids"], list)
        or not isinstance(value.get("requires", []), list)
    ):
        raise ValueError("invalid_skill_json")
    skill = SkillRevision(
        skill_id=value["skill_id"],
        revision=value["revision"],
        body=value["body"],
        rule_ids=tuple(value["rule_ids"]),
        requires=tuple(value.get("requires", [])),
    )
    runtime_alias(skill)  # Validate the exact revision before any Agent process starts.
    return skill


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="运行固定版本 Claude Code 并保存独立验收报告")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="使用明确任务、模型和可选固定 Skill 运行")
    run.add_argument("case")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--cli-path", type=Path)
    run.add_argument("--model", required=True)
    run.add_argument("--skill-json", type=Path)
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--max-turns", type=int, default=8)
    run.add_argument("--max-budget", type=float, default=1)
    try:
        args = parser.parse_args(argv)
        executable = args.cli_path or shutil.which("claude")
        if executable is None:
            raise ValueError("capability_missing")
        config = RunConfig(
            cli_path=Path(executable).resolve(),
            model=args.model,
            timeout_seconds=args.timeout,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget,
        )
        validate_config(config)
        skill = _load_skill(args.skill_json) if args.skill_json is not None else None
        provider = {key: os.environ[key] for key in _PROVIDER_KEYS if key in os.environ}
        report_path = run_case(
            args.case,
            args.output,
            config,
            skill=skill,
            provider_environment=provider,
        )
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("status"), str):
            raise ValueError("invalid_report")
        _emit({"status": report["status"], "report": str(report_path)})
        return 0 if report["status"] == "passed" else 1
    except (OSError, ValueError, TypeError) as error:
        prefix = str(error).split(":", 1)[0]
        code = prefix if prefix in _ERROR_CODES else "invalid_input"
        _emit({"status": "error", "error_code": code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
