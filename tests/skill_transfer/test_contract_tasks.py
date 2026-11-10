"""New instances of the same Dream contract, not new problem families."""

import pytest


@pytest.mark.parametrize("task", ["transfer-batch-update", "transfer-version-update"])
@pytest.mark.parametrize(
    "variant,expected",
    [
        ("initial", "failed"),
        ("reference", "passed"),
        ("skip", "failed"),
        ("lenient", "failed"),
        ("weaken_checks", "failed"),
    ],
)
def test_isolated_acceptance_distinguishes_correct_behavior(tmp_path, task, variant, expected):
    from memos.skill_transfer.tasks import materialize_case
    from memos.skill_transfer.verification import check_receipt, verify_candidate

    candidate = materialize_case(task, tmp_path / "candidate", variant)
    result = verify_candidate(
        task, candidate, tmp_path / "evidence", run_id="test-run", attempt_id="test-attempt"
    )
    assert result.status == expected
    assert check_receipt(result.receipt_path, candidate, tmp_path / "evidence").matches
    (candidate / "extra.py").write_text("pass\n")
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "evidence").matches


def test_visible_broker_uses_sealed_fixed_checks_and_refuses_weakened_input(tmp_path):
    from memos.skill_adapter.recording_server import _request_visible_checks
    from memos.skill_transfer.broker import VisibleCheckBroker
    from memos.skill_transfer.tasks import materialize_case

    candidate = materialize_case("transfer-batch-update", tmp_path / "candidate", "reference")
    with VisibleCheckBroker("transfer-batch-update", candidate, tmp_path / "checks") as broker:
        context = {"test_broker_socket": str(broker.socket_path)}
        assert _request_visible_checks(context)["exit_code"] == 0
        (candidate / "visible_checks.py").write_text("pass\n")
        assert _request_visible_checks(context)["execution_error"] == "protected_task_changed"
        assert len(broker.records) == 1


def test_qualification_rejects_changed_hidden_artifacts(tmp_path):
    from memos.skill_transfer.verification import audit, require_qualification

    root = tmp_path / "qualification"
    assert audit(root)["passed"]
    assert require_qualification(root)["qualificationId"]
    (root / "transfer-batch-update-reference/hidden/report.json").write_text("{}")
    with pytest.raises(ValueError):
        require_qualification(root)
