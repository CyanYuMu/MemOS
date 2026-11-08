"""Maintainer CLI handles registered source/holdout tasks without relabeling them."""

import json

from pathlib import Path

from memos.skill_evaluation.cli import main
from memos.skill_evaluation.tasks import get_case, materialize_case


def test_list_and_prepare_use_registered_split(tmp_path, capsys):
    assert main(["list", "--split", "source"]) == 0
    listed = json.loads(capsys.readouterr().out)["cases"]
    assert {row["case_id"] for row in listed} == {
        "source-dream-update",
        "source-scoped-update",
    }
    assert all(row["split"] == "source" for row in listed)
    candidate = tmp_path / "candidate"
    assert main(["prepare", "source-dream-update", "--workspace", str(candidate)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["split"] == "source"
    assert prepared["asset_digest"]
    assert {
        path.relative_to(candidate).as_posix() for path in candidate.rglob("*") if path.is_file()
    } == set(get_case("source-dream-update").initial_files)


def test_h6_cli_distinguishes_pending_review_from_passed(tmp_path, capsys):
    candidate = materialize_case("holdout-ambiguous", tmp_path / "candidate", "reference")
    assert (
        main(
            [
                "verify",
                "holdout-ambiguous",
                "--candidate",
                str(candidate),
                "--artifacts",
                str(tmp_path / "artifacts"),
            ]
        )
        == 3
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "needs_review"


def test_asset_audit_is_not_an_effectiveness_experiment(tmp_path, capsys):
    assert main(["audit-tasks", "--output", str(tmp_path / "audit")]) == 0
    result = json.loads(capsys.readouterr().out)
    summary = json.loads(Path(result["report"]).read_text())
    assert summary["purpose"] == "task_asset_self_test"
    assert summary["qualified_agent_experience"] is False
    assert summary["effectiveness_evaluation"] is False
    assert len(summary["cases"]) == 9
    h6 = next(case for case in summary["cases"] if case["case_id"] == "holdout-ambiguous")
    assert {row["status"] for row in h6["results"]} == {"failed", "needs_review"}
    assert all(
        row["receipt_matches"] and row["expected_checks_satisfied"]
        for case in summary["cases"]
        for row in case["results"]
    )
