"""Exercise the runnable offline verification workflow."""

import json
import os
import subprocess
import sys

from pathlib import Path

from memos.skill_evaluation.cli import main


def test_cli_prepares_only_visible_task_material(tmp_path, capsys):
    workspace = tmp_path / "candidate"
    assert main(["prepare", "dev-strict-signature", "--workspace", str(workspace)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["split"] == "dev"
    assert (workspace / "caller.py").is_file()
    assert not any("oracle" in p.name or "reference" in p.name for p in workspace.rglob("*"))
    assert main(["prepare", "dev-strict-signature", "--workspace", str(workspace)]) == 2


def test_cli_reports_candidate_failure_with_nonzero_exit(tmp_path, capsys):
    workspace = tmp_path / "candidate"
    main(["prepare", "dev-strict-signature", "--workspace", str(workspace)])
    capsys.readouterr()
    status = main(
        [
            "verify",
            "dev-strict-signature",
            "--candidate",
            str(workspace),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert status == 1
    assert result["status"] == "failed"
    assert Path(result["receipt"]).is_file()


def test_cli_demo_completes_all_expected_outcomes(tmp_path, capsys):
    destination = tmp_path / "demo"
    assert main(["demo", "--output", str(destination)]) == 0
    response = json.loads(capsys.readouterr().out)
    summary = json.loads(Path(response["report"]).read_text())
    assert summary["success"] is True
    assert summary["qualified_agent_experience"] is False
    assert len(summary["cases"]) == 2
    for case in summary["cases"]:
        assert len(case["results"]) == 4
        assert all(row["status"] == row["expected_status"] for row in case["results"])
        assert case["changed_candidate_rejected"] is True
    assert main(["demo", "--output", str(destination)]) == 2


def test_module_entrypoint_is_runnable_from_the_project_environment():
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "memos.skill_evaluation", "list"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    # Existing memos package initialization can emit diagnostics before the command.
    payload = json.loads(result.stdout.splitlines()[-1])
    assert {case["case_id"] for case in payload["cases"]} == {
        "dev-strict-signature",
        "dev-tenant-isolation",
    }


def test_missing_receipt_has_infrastructure_error_exit(tmp_path, capsys):
    code = main(
        [
            "check-receipt",
            str(tmp_path / "artifacts" / "missing.json"),
            "--candidate",
            str(tmp_path / "candidate"),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["matches"] is False
