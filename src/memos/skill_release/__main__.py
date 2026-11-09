"""Host commands retain qualification and batch evidence outside the repository."""

from __future__ import annotations

import argparse
import json

from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="固定候选发布资格与恢复回归")
    commands = parser.add_subparsers(dest="command", required=True)
    qualify = commands.add_parser("qualify")
    qualify.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    for name in ("config", "candidate", "qualification", "branch-plan", "output"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--plan-purpose", choices=("release", "recovery"), default="release")
    run.add_argument("--project", required=True)
    ingest = commands.add_parser("import")
    for name in ("config", "batch", "qualification", "allowed-root", "output"):
        ingest.add_argument("--" + name, type=Path, required=True)
    for name in ("project", "skill"):
        ingest.add_argument("--" + name, required=True)
    args = parser.parse_args()

    def progress(value):
        print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)

    if args.command == "qualify":
        from memos.skill_release.qualification import audit_protocol
        from memos.skill_release.tasks import SUITES

        args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        results = []
        for suite in SUITES:
            result = audit_protocol(suite, args.output / suite, progress=progress)
            results.append(
                {
                    "suiteId": suite,
                    "success": result["success"],
                    "qualificationId": result["qualificationId"],
                }
            )
        progress(
            {"results": results, "success": all(r["success"] for r in results), "modelCalls": 0}
        )
        return 0 if all(r["success"] for r in results) else 1
    if args.command == "run":
        from memos.skill_release.batch import run_batch
        from memos.skill_release.host import execution_guard

        with execution_guard(args.config, args.project, args.candidate) as guard:
            result = run_batch(
                args.config,
                args.candidate,
                args.qualification,
                args.output,
                branch_intents=json.loads(args.branch_plan.read_text()),
                plan_purpose=args.plan_purpose,
                progress=progress,
                execution_guard=guard,
            )
        progress(result)
        return 0 if result["success"] else 1
    from memos.skill_release.importer import import_batch

    progress(
        import_batch(
            args.config,
            args.project,
            args.skill,
            args.batch,
            args.qualification,
            args.allowed_root,
            args.output,
            progress=progress,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
