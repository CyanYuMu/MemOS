"""List assets or qualify the new execution protocol without calling a model."""

import argparse
import json

from pathlib import Path

from memos.skill_branch_validation.selftest import check_variant
from memos.skill_branch_validation.tasks import list_tasks, task_digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    audit = commands.add_parser("audit")
    audit.add_argument("--output", required=True, type=Path)
    qualify = commands.add_parser("qualify")
    qualify.add_argument("--output", required=True, type=Path)
    qualify.add_argument("--timeout", type=float, default=15)
    check = commands.add_parser("check-qualification")
    check.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.command in {"qualify", "check-qualification"}:
        from memos.skill_branch_validation.qualification import (
            audit_protocol,
            require_qualification,
        )

        def emit(value):
            print(json.dumps(value, ensure_ascii=False), flush=True)

        try:
            if args.command == "check-qualification":
                emit(require_qualification(args.directory))
                return 0
            result = audit_protocol(args.output, timeout=args.timeout, progress=emit)
            emit(
                {
                    "success": result["success"],
                    "qualificationId": result["qualificationId"],
                    "modelCalls": 0,
                    "branchCoverage": [],
                }
            )
            return 0 if result["success"] else 1
        except (OSError, ValueError, TypeError) as error:
            emit({"error": "branch_qualification_failed", "errorType": type(error).__name__})
            return 2
    tasks = list_tasks()
    if args.command == "list":
        print(
            json.dumps(
                [
                    {"taskId": t.task_id, "digest": task_digest(t), "purpose": t.purpose}
                    for t in tasks
                ],
                indent=2,
            )
        )
        return 0
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    results = []
    for task in tasks:
        for variant in ("initial", "reference", "unsafe"):
            result = check_variant(task.task_id, variant, output / task.task_id / variant)
            results.append(result)
            print(
                json.dumps(
                    {
                        "taskId": task.task_id,
                        "variant": variant,
                        "status": result["status"],
                        "matchesExpectation": result["matchesExpectation"],
                    }
                ),
                flush=True,
            )
    summary = {
        "purpose": "branch_asset_selftest",
        "passed": all(r["matchesExpectation"] for r in results),
        "results": results,
        "modelCalls": 0,
        "branchCoverage": [],
        "humanApprovals": 0,
    }
    path = output / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
