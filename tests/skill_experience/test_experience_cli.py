"""CLI tests never start an Agent or a network request."""

import json
import sys

from memos.skill_experience import __main__ as cli


def test_collect_command_passes_explicit_budget_and_credentials_only_in_memory(
    tmp_path, monkeypatch, capsys
):
    calls = []
    secret = "synthetic-cli-provider-secret"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", secret)
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "not-forwarded")

    def collect(case, output, config, *, provider_environment):
        calls.append((case, output, config, provider_environment))
        output.mkdir()
        path = output / "collection.json"
        path.write_text(json.dumps({"status": "collected", "qualification": "qualified_success"}))
        return path

    monkeypatch.setattr(cli, "collect_source", collect)
    code = cli.main(
        [
            "collect-source",
            "source-dream-update",
            "--output",
            str(tmp_path / "run"),
            "--cli-path",
            sys.executable,
            "--model",
            "fixed-model",
            "--timeout",
            "240",
            "--max-turns",
            "20",
            "--max-budget",
            "2",
        ]
    )
    assert code == 0
    assert calls[0][2].timeout_seconds == 240
    assert calls[0][2].max_turns == 20
    assert calls[0][3] == {"ANTHROPIC_AUTH_TOKEN": secret}
    assert secret not in capsys.readouterr().out


def test_failed_collection_is_not_a_successful_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "collection.json"
    path.write_text(json.dumps({"status": "collection_failed", "qualification": "needs_evidence"}))
    monkeypatch.setattr(cli, "collect_source", lambda *a, **k: path)
    assert (
        cli.main(
            [
                "collect-source",
                "source-dream-update",
                "--output",
                str(tmp_path / "run"),
                "--cli-path",
                sys.executable,
                "--model",
                "fixed-model",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["qualification"] == "needs_evidence"


def test_input_error_does_not_echo_secrets(capsys):
    assert cli.main(["--synthetic-private-token", "secret-value"]) == 2
    output = capsys.readouterr().out
    assert "secret-value" not in output
    assert "synthetic-private-token" not in output


def test_exception_text_is_not_echoed(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("secret-private-endpoint")

    monkeypatch.setattr(cli, "collect_source", fail)
    assert (
        cli.main(
            [
                "collect-source",
                "source-dream-update",
                "--output",
                str(tmp_path),
                "--cli-path",
                sys.executable,
                "--model",
                "fixed-model",
            ]
        )
        == 2
    )
    assert "secret-private-endpoint" not in capsys.readouterr().out


def test_ingest_keeps_an_uncertain_write_pending(tmp_path, monkeypatch, capsys):
    class Bridge:
        def ingest(self, evidence):
            assert evidence == tmp_path / "evidence.json"
            return {"status": "pending_reconcile", "memory_ids": [], "snapshots": []}

    monkeypatch.setattr(cli, "_bridge", lambda args: Bridge())
    args = [
        "ingest",
        str(tmp_path / "evidence.json"),
        "--ledger",
        str(tmp_path / "ledger"),
        "--snapshots",
        str(tmp_path / "snapshots"),
        "--cube",
        "source",
        "--user",
        "owner",
    ]
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "pending_reconcile"


def test_service_configuration_is_not_persisted_or_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MEMOS_SKILL_API_URL", raising=False)
    args = [
        "list-sources",
        "--ledger",
        str(tmp_path / "ledger"),
        "--snapshots",
        str(tmp_path / "snapshots"),
        "--cube",
        "source",
        "--user",
        "owner",
    ]
    assert cli.main(args) == 2
    assert not (tmp_path / "ledger").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "error"
