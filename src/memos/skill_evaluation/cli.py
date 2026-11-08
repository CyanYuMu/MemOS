"""Command-line interface for the offline Skill verification foundation."""

import argparse
import json
import shutil

from pathlib import Path

from memos.skill_evaluation.tasks import case_digest, get_case, list_cases, materialize_case
from memos.skill_evaluation.verifier import check_receipt, verify_candidate


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _demo(output: Path, *, registered_tasks: bool = False) -> tuple[int, dict]:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    artifacts = output / "artifacts"
    cases = []
    specs = list_cases("source") + list_cases("holdout") if registered_tasks else list_cases()
    for case in specs:
        results = []
        reference_receipt = None
        reference_candidate = None
        acceptable = "needs_review" if case.assessment_kind == "manual_boundary" else "passed"
        for label, variant, expected in (
            ("baseline", "baseline", acceptable),
            ("initial", "initial", "failed"),
            ("reference", "reference", acceptable),
            ("unsafe", "unsafe", "failed"),
        ):
            candidate = materialize_case(
                case.case_id, output / "candidates" / case.case_id / label, variant
            )
            result = verify_candidate(case.case_id, candidate, artifacts)
            receipt = json.loads(result.receipt_path.read_text())
            failures = {row["name"] for row in receipt["checks"] if row["status"] == "failed"}
            correct_failure = set(case.expected_failure_checks.get(variant, ())) <= failures
            binding = check_receipt(result.receipt_path, candidate, artifacts)
            results.append(
                {
                    "variant": label,
                    "status": result.status,
                    "expected_status": expected,
                    "expected_checks_satisfied": correct_failure,
                    "receipt_matches": binding.matches,
                    "receipt": str(result.receipt_path),
                    "scoring": receipt["scoring"],
                }
            )
            if label == "reference":
                reference_receipt = result.receipt_path
                reference_candidate = candidate
        changed = output / "candidates" / case.case_id / "changed-after-verification"
        shutil.copytree(reference_candidate, changed)
        with (changed / "caller.py").open("a") as stream:
            stream.write("\n# This edit must invalidate the old receipt binding.\n")
        stale = check_receipt(reference_receipt, changed, artifacts)
        cases.append(
            {
                "case_id": case.case_id,
                "split": case.split,
                "lineage_id": case.lineage_id,
                "asset_digest": case_digest(case),
                "results": results,
                "changed_candidate_rejected": not stale.matches,
            }
        )
    success = all(
        case["changed_candidate_rejected"]
        and all(
            row["status"] == row["expected_status"]
            and row["expected_checks_satisfied"]
            and row["receipt_matches"]
            for row in case["results"]
        )
        for case in cases
    )
    report = {
        "schema_version": 1,
        "purpose": "task_asset_self_test" if registered_tasks else "verifier_self_test",
        "effectiveness_evaluation": False,
        "qualified_agent_experience": False,
        "success": success,
        "cases": cases,
    }
    path = output / "summary.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    has_error = any(row["status"] == "error" for case in cases for row in case["results"])
    return (0 if success else 2 if has_error else 1), {
        "status": "passed" if success else "error" if has_error else "failed",
        "report": str(path),
        "qualified_agent_experience": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skill 原型的离线任务与补丁验收")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="维护者列出固定用途的任务")
    listing.add_argument("--split", choices=("dev", "source", "holdout", "all"), default="dev")
    prepare = commands.add_parser("prepare", help="创建初始故障任务目录")
    prepare.add_argument("case_id")
    prepare.add_argument("--workspace", type=Path, required=True)
    verify = commands.add_parser("verify", help="封存并独立验收候选补丁")
    verify.add_argument("case_id")
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--artifacts", type=Path, required=True)
    verify.add_argument("--timeout", type=float, default=10)
    receipt = commands.add_parser("check-receipt", help="检查回执与当前候选的绑定")
    receipt.add_argument("receipt", type=Path)
    receipt.add_argument("--candidate", type=Path, required=True)
    receipt.add_argument("--artifacts", type=Path, required=True)
    demo = commands.add_parser("demo", help="运行基线、故障、正确修复和错误修复自检")
    demo.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("audit-tasks", help="维护者检查九个固定任务资产，不作效果实验")
    audit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            _emit(
                {
                    "cases": [
                        {
                            "case_id": case.case_id,
                            "revision": case.revision,
                            "split": case.split,
                            "description": case.description,
                        }
                        for case in list_cases(args.split)
                    ]
                }
            )
        elif args.command == "prepare":
            workspace = materialize_case(args.case_id, args.workspace)
            case = get_case(args.case_id)
            _emit(
                {
                    "case_id": args.case_id,
                    "split": case.split,
                    "revision": case.revision,
                    "lineage_id": case.lineage_id,
                    "asset_digest": case_digest(case),
                    "workspace": str(workspace.resolve()),
                }
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.case_id, args.candidate, args.artifacts, timeout=args.timeout
            )
            _emit({"status": result.status, "receipt": str(result.receipt_path)})
            return {"passed": 0, "failed": 1, "error": 2, "needs_review": 3}[result.status]
        elif args.command == "check-receipt":
            check = check_receipt(args.receipt, args.candidate, args.artifacts)
            _emit(
                {
                    "matches": check.matches,
                    "verification_status": check.verification_status,
                    "reasons": check.reasons,
                    "invalid": check.invalid,
                }
            )
            return 0 if check.matches else 2 if check.invalid else 1
        elif args.command == "demo":
            code, summary = _demo(args.output)
            _emit(summary)
            return code
        elif args.command == "audit-tasks":
            code, summary = _demo(args.output, registered_tasks=True)
            _emit(summary)
            return code
    except (OSError, ValueError) as error:
        _emit({"status": "error", "message": str(error)})
        return 2
    return 0
