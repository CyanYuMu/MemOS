#!/usr/bin/env python3
"""Prepare the fixed experiment visibly, then run the existing service console."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from pathlib import Path


def ensure_calibration(config: Path, candidate: Path, directory: Path) -> None:
    if directory.exists():
        summary = directory / "summary.json"
        if not summary.is_file():
            raise ValueError("calibration_needs_review")
        results = json.loads(summary.read_text()).get("results", [])
        if (
            len(results) != 3
            or {r.get("group") for r in results} != {"none", "raw_memory", "skill"}
            or any(r.get("status") != "passed" for r in results)
        ):
            raise ValueError("calibration_needs_review")
        print("[preparation] 复用已完成的三组 dev 校准；随后重新核验凭条。", flush=True)
        return
    print("[preparation] 开始三组 dev 校准；不会启动正式 holdout。", flush=True)
    result = subprocess.run(
        [
            sys.executable,
            "-u",
            "-m",
            "memos.skill_experiments",
            "calibrate",
            "--config",
            str(config),
            "--candidate",
            str(candidate),
            "--output",
            str(directory),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )
    if result.returncode:
        raise ValueError("calibration_needs_review")


def main():
    from memos.skill_experiments.__main__ import runtime
    from memos.skill_experiments.manifest import freeze_experiment, load_experiment, write_json
    from memos.skill_management.config import read_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve()
    if config_path.stat().st_mode & 0o077:
        raise ValueError("private_config_required")
    console = json.loads(config_path.read_text())
    prep = console["preparation"]
    service_path = Path(console["serviceConfig"])
    service = read_config(service_path)
    candidate, calibration, experiment = (
        Path(prep[k]).resolve() for k in ("candidate", "calibration", "experiment")
    )
    ensure_calibration(service_path, candidate, calibration)
    if experiment.exists():
        manifest = load_experiment(experiment, runtime(service))
    else:
        manifest = freeze_experiment(
            candidate,
            experiment,
            runtime(service),
            project_id=console["projectId"],
            calibration_reports=[
                calibration / group / "report.json" for group in ("none", "raw_memory", "skill")
            ],
        )
    if manifest["projectId"] != console["projectId"]:
        raise ValueError("experiment_project_mismatch")
    project = next(p for p in service["projects"] if p["projectId"] == console["projectId"])
    if not any(Path(c["directory"]).resolve() == candidate for c in project["candidates"]):
        project["candidates"].append(
            {
                "directory": str(candidate),
                "challenges": [
                    "来源约束修订已提出；健康路径、迁移范围及规则使用仍需人工语义审阅，不能因实验完成自动批准。"
                ],
            }
        )
    project["experiments"] = list(dict.fromkeys([*project.get("experiments", []), str(experiment)]))
    write_json(service_path, service)
    print(
        "[preparation] 已冻结实验 "
        + manifest["experimentId"]
        + "；正式 21 单元等待浏览器明确启动。",
        flush=True,
    )
    legacy = Path(__file__).with_name("skill_p3_console.py")
    os.execv(sys.executable, [sys.executable, "-u", str(legacy), "--config", str(config_path)])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        category = (
            str(error)
            if type(error) is ValueError
            and str(error)
            in {
                "private_config_required",
                "calibration_needs_review",
                "experiment_project_mismatch",
            }
            else "preparation_failed"
        )
        print("[preparation] " + category + "；既有记录保留，未自动重跑或批准。", flush=True)
        raise SystemExit(1) from None
