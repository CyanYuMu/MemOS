"""Host-only P4.1 verifier and guarded task-asset qualification commands."""

from __future__ import annotations

import argparse
import json

from pathlib import Path

from memos.skill_verification.selftest import audit_tasks
from memos.skill_verification.verifier import check_receipt, verify_candidate


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="P4.1 受信隔离验收；不运行模型或自动发布")
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit-tasks", help="七任务 × 三变体隔离自检及可见检查")
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--timeout", type=float, default=10)
    verify = commands.add_parser("verify", help="使用新验证器封存并验收受信宿主候选")
    verify.add_argument("case_id")
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--artifacts", type=Path, required=True)
    verify.add_argument("--timeout", type=float, default=10)
    check = commands.add_parser("check", help="复验新凭条与当前最终补丁的绑定")
    check.add_argument("receipt", type=Path)
    check.add_argument("--candidate", type=Path, required=True)
    check.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "audit-tasks":
            result = audit_tasks(args.output, timeout=args.timeout, progress=_emit)
            _emit(
                {
                    "success": result["success"],
                    "qualification_id": result["qualification_id"],
                    "report": str(args.output.resolve() / "summary.json"),
                    "purpose": "task_asset_self_test",
                    "new_model_calls": 0,
                }
            )
            return 0 if result["success"] else 1
        if args.command == "verify":
            result = verify_candidate(
                args.case_id, args.candidate, args.artifacts, timeout=args.timeout
            )
            _emit({"status": result.status, "receipt": str(result.receipt_path)})
            return {"passed": 0, "failed": 1, "error": 2, "needs_review": 3}[result.status]
        result = check_receipt(args.receipt, args.candidate, args.artifacts)
        _emit(
            {
                "matches": result.matches,
                "verification_status": result.verification_status,
                "reasons": result.reasons,
                "invalid": result.invalid,
            }
        )
        return 0 if result.matches else 2 if result.invalid else 1
    except KeyboardInterrupt:
        _emit({"error": "verification_interrupted", "artifacts_retained": True})
        return 130
    except (OSError, ValueError, TypeError) as error:
        _emit({"error": "verification_command_failed", "error_type": type(error).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
