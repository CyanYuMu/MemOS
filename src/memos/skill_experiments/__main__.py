"""Explicit host preparation; browser requests cannot create experiment recipes."""

from __future__ import annotations

import argparse
import json

from pathlib import Path

from memos.skill_adapter.runner import RunConfig
from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_experiments.manifest import (
    GROUPS,
    freeze_experiment,
    raw_context,
    runtime_binding,
    write_json,
)
from memos.skill_experiments.runner import run_experiment_case
from memos.skill_experiments.scoring import score_run
from memos.skill_management.config import read_config
from memos.skill_management.runtime import native_skill


def runtime(config):
    settings = config["runtime"]
    return RunConfig(
        cli_path=Path(settings["cliPath"]),
        model=settings["model"],
        timeout_seconds=settings["timeoutSeconds"],
        max_turns=settings["maxTurns"],
        max_budget_usd=settings["maxBudgetUsd"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="P4 固定三组实验准备；不自动发布")
    parser.add_argument("command", choices=["calibrate", "freeze"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--project")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        run_config = runtime(config)
        if args.command == "freeze":
            if args.calibration is None or not args.project:
                raise ValueError("missing_calibration_or_project")
            result = freeze_experiment(
                args.candidate,
                args.output,
                run_config,
                project_id=args.project,
                calibration_reports=[args.calibration / group / "report.json" for group in GROUPS],
            )
            print(
                json.dumps(
                    {"status": "frozen", "experimentId": result["experimentId"], "planned": 21}
                )
            )
            return 0
        loaded = load_candidate(args.candidate)
        raw, selection = raw_context(loaded["packet"], 32768)
        args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        case = get_case("dev-tenant-isolation")
        identity = hash_json(
            {
                "purpose": "dev_calibration",
                "revision": loaded["candidate"]["revision"],
                "runtime": runtime_binding(run_config),
                "raw": selection,
            }
        )
        results = []
        for group in GROUPS:
            print(json.dumps({"calibration": "started", "group": group}), flush=True)
            report = run_experiment_case(
                case.case_id,
                args.output / group,
                run_config,
                group=group,
                experiment_id=identity,
                expected_case_digest=case_digest(case),
                skill=native_skill(loaded["candidate"]) if group == "skill" else None,
                raw_memory=raw if group == "raw_memory" else None,
                provider_environment=config["runtime"]["providerEnvironment"],
            )
            score = score_run(report, group=group, expected_case_digest=case_digest(case))
            results.append(score)
            print(
                json.dumps(
                    {
                        "calibration": "finished",
                        "group": group,
                        "status": score["status"],
                        "loading": score["loading"],
                        "capture": score["capture"],
                        "taskAccepted": score["taskAccepted"],
                    }
                ),
                flush=True,
            )
        write_json(args.output / "summary.json", {"purpose": "dev_calibration", "results": results})
        return 0 if all(score["status"] == "passed" for score in results) else 1
    except Exception as error:
        # Configuration/provider errors may carry credentials. A fixed category
        # and retained output directory are sufficient for host investigation.
        print(
            json.dumps(
                {"error": "experiment_preparation_failed", "errorType": type(error).__name__}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
