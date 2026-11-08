"""Host CLI reports behavior and binding as separate outcomes."""

import json

from pathlib import Path
from types import SimpleNamespace

import pytest

from memos.skill_verification import __main__ as cli


@pytest.mark.parametrize(
    "status,code", [("passed", 0), ("failed", 1), ("error", 2), ("needs_review", 3)]
)
def test_verify_keeps_pending_and_failed_statuses(monkeypatch, capsys, status, code):
    monkeypatch.setattr(
        cli,
        "verify_candidate",
        lambda *_a, **_k: SimpleNamespace(status=status, receipt_path=Path("receipt.json")),
    )
    assert (
        cli.main(["verify", "holdout-fixture", "--candidate", "/tmp/c", "--artifacts", "/tmp/a"])
        == code
    )
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_check_exposes_mismatch_without_treating_it_as_passed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "check_receipt",
        lambda *_a, **_k: SimpleNamespace(
            matches=False,
            verification_status="passed",
            reasons=("candidate_mismatch",),
            invalid=True,
        ),
    )
    assert cli.main(["check", "/tmp/r", "--candidate", "/tmp/c", "--artifacts", "/tmp/a"]) == 2
    assert json.loads(capsys.readouterr().out)["matches"] is False


def test_audit_rejection_is_safe_and_never_reuses_output(monkeypatch, capsys):
    def existing(*_a, **_k):
        raise FileExistsError("private secret path must not be printed")

    monkeypatch.setattr(cli, "audit_tasks", existing)
    assert cli.main(["audit-tasks", "--output", "/tmp/audit"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["error"] == "verification_command_failed"
    assert "private secret" not in str(value)


def test_audit_exit_depends_on_real_selftest_success(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "audit_tasks", lambda *_a, **_k: {"success": False, "qualification_id": "test"}
    )
    assert cli.main(["audit-tasks", "--output", "/tmp/audit"]) == 1
    assert json.loads(capsys.readouterr().out)["success"] is False
