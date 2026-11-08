"""The collector must observe the failure before launching the real source run."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.runner import RunConfig
from memos.skill_evaluation.tasks import get_case
from memos.skill_evaluation.verifier import check_receipt
from memos.skill_experience.collection import collect_source, prepare_preflight


@pytest.mark.skipif(sys.platform != "darwin", reason="P1b uses the tested macOS executor")
@pytest.mark.parametrize("case_id", ["source-dream-update", "source-scoped-update"])
def test_source_preflight_executes_healthy_and_broken_states(case_id, tmp_path):
    path = prepare_preflight(case_id, tmp_path / "preflight")
    data = json.loads(path.read_text())
    assert data["case_id"] == case_id
    for variant, expected in (("baseline", "passed"), ("initial", "failed")):
        record = data["variants"][variant]
        checked = check_receipt(
            Path(record["receipt"]), Path(record["candidate"]), Path(record["artifact_root"])
        )
        assert checked.matches
        assert checked.verification_status == expected
        receipt = json.loads(Path(record["receipt"]).read_text())
        if variant == "initial":
            assert set(get_case(case_id).expected_failure_checks["initial"]) <= {
                row["name"] for row in receipt["checks"] if row["status"] == "failed"
            }
        assert receipt["qualified_agent_experience"] is False


@pytest.mark.parametrize("case_id", ["dev-strict-signature", "holdout-record"])
def test_non_source_cannot_enter_collection(case_id, tmp_path):
    with pytest.raises(ValueError, match="source_required"):
        collect_source(
            case_id,
            tmp_path / "collection",
            RunConfig(cli_path=Path(sys.executable), model="fixed-test-model"),
        )
    assert not (tmp_path / "collection").exists()


def test_preflight_failure_never_launches_agent_and_preserves_attempt(tmp_path, monkeypatch):
    from memos.skill_experience import collection

    def broken_preflight(*args, **kwargs):
        raise ValueError("synthetic-private-detail")

    def forbidden_launch(*args, **kwargs):
        pytest.fail("Agent must not launch after an invalid baseline")

    monkeypatch.setattr(collection, "prepare_preflight", broken_preflight)
    monkeypatch.setattr(collection, "run_case", forbidden_launch)
    path = collect_source(
        "source-dream-update",
        tmp_path / "collection",
        RunConfig(cli_path=Path(sys.executable), model="fixed-test-model"),
    )
    report = json.loads(path.read_text())
    assert report["status"] == "preflight_failed"
    assert report["evidence"] is None
    assert "synthetic-private-detail" not in path.read_text()
    with pytest.raises(ValueError, match="output_exists"):
        collect_source(
            "source-dream-update",
            path.parent,
            RunConfig(cli_path=Path(sys.executable), model="fixed-test-model"),
        )
