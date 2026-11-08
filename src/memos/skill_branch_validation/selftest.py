"""Asset qualification only: reference runs never establish Agent branch coverage."""

from __future__ import annotations

import json

from pathlib import Path

from memos.skill_branch_validation.tasks import get_task, materialize, task_digest
from memos.skill_evaluation.snapshots import snapshot_fingerprint
from memos.skill_verification.executor import execute_program, implementation_digest


def check_variant(task_id: str, variant: str, output: Path) -> dict:
    task = get_task(task_id)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    candidate = materialize(task_id, output / "candidate", variant)
    before = snapshot_fingerprint(candidate)
    execution = execute_program(task.oracle, candidate, output / "execution", 15)
    path = output / "execution/report.json"
    report = json.loads(path.read_text()) if path.is_file() else {}
    checks = report.get("checks", [])
    intact = snapshot_fingerprint(candidate) == before
    names = [row.get("name") for row in checks if isinstance(row, dict)]
    valid = (
        intact
        and names == list(task.required_checks)
        and all(row.get("status") in {"passed", "failed"} for row in checks)
        and not execution.get("timed_out")
        and not execution.get("output_limited")
        and not execution.get("cancelled")
        and execution.get("returncode") in (0, 1)
        and report.get("imported")
        == {name: str(candidate / (name + ".py")) for name in ("caller", "fake_graph")}
    )
    failed = [row["name"] for row in checks if row.get("status") == "failed"]
    if valid and execution["returncode"] != (1 if failed else 0):
        valid = False
    status = ("failed" if failed else "passed") if valid else "error"
    expected = task.expected_failures[variant]
    result = {
        "schemaVersion": 1,
        "purpose": "branch_asset_selftest",
        "taskId": task.task_id,
        "taskDigest": task_digest(task),
        "executorDigest": implementation_digest(),
        "variant": variant,
        "candidateSnapshot": before,
        "candidateUnchanged": intact,
        "status": status,
        "failedChecks": failed,
        "matchesExpectation": valid and set(failed) == set(expected),
        "qualifiedAgentExperience": False,
        "branchCoverage": [],
    }
    (output / "selftest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result
