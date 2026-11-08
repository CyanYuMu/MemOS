"""Explicit source collection and Memory bridge commands with safe diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import shutil

from pathlib import Path

from memos.skill_adapter.runner import RunConfig, validate_config
from memos.skill_experience.collection import collect_source


_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Unknown arguments can contain credentials; never echo argparse input.
        raise ValueError("invalid_arguments")


def _emit(value: dict | list) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _bridge(args):
    from memos.skill_experience.bridge import MemoryBridge
    from memos.skill_experience.client import ProductMemoryClient

    endpoint = os.environ.get("MEMOS_SKILL_API_URL")
    if not endpoint:
        raise ValueError("missing_service_configuration")
    token = os.environ.get("MEMOS_SKILL_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return MemoryBridge(
        ProductMemoryClient(endpoint, timeout=args.timeout, headers=headers),
        args.ledger,
        args.snapshots,
        cube_id=args.cube,
        user_id=args.user,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="采集真实来源证据并通过 MemOS 实际回读保存快照")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect-source", help="无 Skill 执行固定来源任务")
    collect.add_argument("case")
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--cli-path", type=Path)
    collect.add_argument("--model", required=True)
    collect.add_argument("--timeout", type=float, default=300)
    collect.add_argument("--max-turns", type=int, default=20)
    collect.add_argument("--max-budget", type=float, default=2)
    verify = commands.add_parser("verify-evidence", help="重验证据及全部关键引用")
    verify.add_argument("evidence", type=Path)
    for name in ("ingest", "refresh", "list-sources"):
        command = commands.add_parser(name)
        if name == "ingest":
            command.add_argument("evidence", type=Path)
        if name == "refresh":
            command.add_argument("snapshot", type=Path)
        command.add_argument("--ledger", type=Path, required=True)
        command.add_argument("--snapshots", type=Path, required=True)
        command.add_argument("--cube", required=True)
        command.add_argument("--user", required=True)
        command.add_argument("--timeout", type=float, default=30)
    try:
        args = parser.parse_args(argv)
        if args.command == "collect-source":
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
            provider = {key: os.environ[key] for key in _PROVIDER_KEYS if key in os.environ}
            path = collect_source(args.case, args.output, config, provider_environment=provider)
            result = json.loads(path.read_text(encoding="utf-8"))
            _emit(
                {
                    "status": result["status"],
                    "qualification": result["qualification"],
                    "collection": str(path),
                    "evidence": result.get("evidence"),
                }
            )
            # A complete failed attempt is useful evidence, but is not a repaired task.
            return (
                0
                if result["status"] == "collected"
                and result["qualification"] == "qualified_success"
                else 1
            )
        if args.command == "verify-evidence":
            from memos.skill_experience.evidence import verify_evidence

            result = verify_evidence(args.evidence)
            _emit(
                {
                    "status": "verified",
                    "qualification": result["qualification"],
                    "evidence": str(args.evidence.resolve()),
                }
            )
            return 0
        bridge = _bridge(args)
        if args.command == "list-sources":
            sources = bridge.list_sources()
            _emit({"status": "completed", "sources": sources})
            return 0
        result = (
            bridge.ingest(args.evidence)
            if args.command == "ingest"
            else bridge.refresh(args.snapshot)
        )
        _emit(result)
        return 0 if result["status"] in {"completed", "unchanged"} else 1
    except Exception:
        # Exceptions from providers, paths or HTTP libraries can contain private
        # deployment details. Detailed source artifacts are inspected locally.
        _emit({"status": "error", "error_code": "invalid_input_or_operation"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
