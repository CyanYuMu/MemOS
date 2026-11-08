"""List or audit fixed branch assets; this command never runs a Coding Agent."""

import argparse
import json

from pathlib import Path

from memos.skill_branch_validation.selftest import check_variant
from memos.skill_branch_validation.tasks import list_tasks, task_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    audit = commands.add_parser("audit")
    audit.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
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
