"""Freeze and run six nonpublishing transfer units with no retries."""

import argparse
import json
import time

from pathlib import Path

from memos.skill_adapter.runner import RunConfig, _file_digest, validate_config
from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_experiments.manifest import raw_context
from memos.skill_management.config import read_config
from memos.skill_management.domain import require
from memos.skill_management.runtime import export_identity, native_skill
from memos.skill_transfer.runner import adapter_digest, run_transfer_case
from memos.skill_transfer.tasks import TASKS, case_digest, get_case
from memos.skill_transfer.verification import (
    audit,
    check_receipt,
    require_qualification,
    sha,
    write,
)


UNITS = [(TASKS[0], g) for g in ("none", "raw_memory", "skill")] + [
    (TASKS[1], g) for g in ("skill", "raw_memory", "none")
]


def runtime(config):
    c = config["runtime"]
    result = RunConfig(
        cli_path=Path(c["cliPath"]),
        model=c["model"],
        max_turns=20,
        timeout_seconds=240,
        max_budget_usd=3,
    )
    validate_config(result)
    identity = {
        "cliHash": _file_digest(result.cli_path),
        "cliVersion": result.expected_version,
        "model": result.model,
        "tools": list(result.tools),
        "effort": result.effort,
        "maxTurns": 20,
        "timeoutSeconds": 240,
        "maxBudgetUsd": 3,
    }
    return result, identity


def binding(config, candidate_directory, qualification):
    loaded = load_candidate(candidate_directory)
    candidate = loaded["candidate"]
    _, runtime_id = runtime(config)
    raw, selection = raw_context(loaded["packet"], 32768)
    identity = {
        "protocol": "transfer-six-units-1",
        "skillId": candidate["skill_id"],
        "revision": candidate["revision"],
        "projection": export_identity(candidate),
        "adapterDigest": adapter_digest(),
        "runtime": runtime_id,
        "qualification": require_qualification(qualification),
        "rawSelection": selection,
        "units": [
            {"id": t + "-" + g, "caseId": t, "group": g, "taskDigest": case_digest(get_case(t))}
            for t, g in UNITS
        ],
        "maxAgentRuns": 6,
        "newProblemFamilies": 0,
        "publicationEvidence": False,
    }
    return {**identity, "experimentId": hash_json(identity)}, raw, candidate


def score(report, output, group, elapsed):
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    native = [e["payload"] for e in events if e["kind"] == "native_result"]
    receipt = report.get("receipt")
    bound = bool(
        receipt
        and check_receipt(Path(receipt), output / "candidate", output / "validation").matches
    )
    capture = report.get("capture", {}).get("status", "unknown")
    loaded = report.get("loading", {}).get("status", "unknown")
    execution = report.get("execution", {})
    valid = (
        capture == "complete"
        and report.get("context_matches") is True
        and execution.get("returncode") == 0
        and not execution.get("timed_out")
        and len(native) == 1
        and not native[0].get("is_error")
        and native[0].get("subtype") == "success"
        and (group != "skill" or loaded == "verified")
        and (group != "raw_memory" or report.get("experience", {}).get("readObserved") is True)
    )
    return {
        "status": "unknown"
        if not valid or not bound or report.get("task_status") == "error"
        else "passed"
        if report.get("task_accepted")
        else "failed",
        "taskAccepted": report.get("task_accepted", False),
        "receiptVerified": bound,
        "loading": loaded,
        "capture": capture,
        "contextMatches": report.get("context_matches", False),
        "experienceRead": report.get("experience", {}).get("readObserved"),
        "elapsedSeconds": round(elapsed, 3),
        "reportedCostUsd": native[0].get("total_cost_usd") if len(native) == 1 else None,
        "ruleUsageReview": "unreviewed",
        "branchCoverage": [],
        "reportSha256": sha(output / "report.json"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("qualify", "freeze", "run"))
    parser.add_argument("--output", type=Path, required=True)
    for name in ("config", "candidate", "qualification"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.command == "qualify":
        result = audit(args.output)
        print(
            json.dumps({"passed": result["passed"], "units": len(result["rows"]), "modelCalls": 0})
        )
        return 0 if result["passed"] else 1
    require(all((args.config, args.candidate, args.qualification)))
    config = read_config(args.config)
    manifest, raw, candidate = binding(config, args.candidate, args.qualification)
    if args.command == "freeze":
        args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        write(args.output / "manifest.json", manifest)
        with (args.output / "raw-memory.txt").open("x") as stream:
            stream.write(raw)
        (args.output / "raw-memory.txt").chmod(0o600)
        print(json.dumps({"experimentId": manifest["experimentId"], "maxAgentRuns": 6}))
        return 0
    require(
        json.loads((args.output / "manifest.json").read_text()) == manifest,
        "transfer_manifest_changed",
    )
    require((args.output / "raw-memory.txt").read_text() == raw, "transfer_memory_changed")
    # Exclusive marker prohibits a second call after partial failure or interruption.
    write(args.output / "started.json", {"experimentId": manifest["experimentId"]})
    run_config, _ = runtime(config)
    rows = []
    for unit in manifest["units"]:
        require(
            binding(config, args.candidate, args.qualification)[0] == manifest,
            "transfer_inputs_changed",
        )
        root = args.output / "runs" / unit["id"]
        print(json.dumps({"stage": "started", "unit": unit["id"]}), flush=True)
        started = time.monotonic()
        try:
            report_path = run_transfer_case(
                unit["caseId"],
                root,
                run_config,
                qualification_directory=args.qualification,
                group=unit["group"],
                experiment_id=manifest["experimentId"],
                expected_case_digest=unit["taskDigest"],
                skill=native_skill(candidate) if unit["group"] == "skill" else None,
                raw_memory=raw if unit["group"] == "raw_memory" else None,
                provider_environment=config["runtime"]["providerEnvironment"],
            )
            result = score(
                json.loads(report_path.read_text()), root, unit["group"], time.monotonic() - started
            )
        except Exception as error:
            result = {
                "status": "unknown",
                "failureType": type(error).__name__,
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "reportedCostUsd": None,
            }
        row = {**unit, **result}
        rows.append(row)
        write(args.output / (unit["id"] + "-result.json"), row)
        print(json.dumps({"stage": "finished", **row}), flush=True)
    write(
        args.output / "results.json",
        {
            "experimentId": manifest["experimentId"],
            "rows": rows,
            "complete": True,
            "newProblemFamilies": 0,
            "publicationEvidence": False,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
