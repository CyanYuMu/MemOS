"""The adapter CLI fixes inputs without selecting Skill versions or exposing credentials."""

import json

from pathlib import Path

import pytest

from memos.skill_adapter import __main__ as cli


def arguments(tmp_path, *, status="passed"):
    executable = tmp_path / "claude"
    executable.write_text("fake executable for argument validation only")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"status": status}))
    argv = [
        "run",
        "dev-strict-signature",
        "--output",
        str(tmp_path / "run"),
        "--cli-path",
        str(executable),
        "--model",
        "claude-fixed-20260901",
    ]
    return argv, report


def test_cli_passes_only_explicit_configuration_and_provider_environment(
    tmp_path, monkeypatch, capsys
):
    argv, report = arguments(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "PRIVATE-KEY")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://private-endpoint.example")
    monkeypatch.setenv("UNRELATED_SECRET", "DO-NOT-PASS")
    received = []

    def run(case, output, config, **kwargs):
        received.append((case, output, config, kwargs))
        return report

    monkeypatch.setattr(cli, "run_case", run)
    assert cli.main([*argv, "--timeout", "23", "--max-turns", "5", "--max-budget", "0.5"]) == 0
    case, _, config, kwargs = received[0]
    assert case == "dev-strict-signature"
    assert config.model == "claude-fixed-20260901"
    assert config.timeout_seconds == 23
    assert config.max_turns == 5
    assert config.max_budget_usd == 0.5
    assert kwargs["skill"] is None
    assert kwargs["provider_environment"]["ANTHROPIC_API_KEY"] == "PRIVATE-KEY"
    assert "UNRELATED_SECRET" not in kwargs["provider_environment"]
    output = capsys.readouterr().out
    assert "PRIVATE-KEY" not in output and "private-endpoint" not in output
    assert json.loads(output) == {"status": "passed", "report": str(report)}


def test_cli_loads_only_the_exact_skill_json_and_defaults_to_discovered_cli(
    tmp_path, monkeypatch, capsys
):
    argv, report = arguments(tmp_path)
    executable = argv[argv.index("--cli-path") + 1]
    index = argv.index("--cli-path")
    del argv[index : index + 2]
    monkeypatch.setattr(cli.shutil, "which", lambda name: executable if name == "claude" else None)
    skill_path = tmp_path / "skill.json"
    skill_path.write_text(
        json.dumps(
            {
                "skill_id": "contract-check",
                "revision": "7",
                "body": "Observe the public contract.\n",
                "rule_ids": ["R1"],
                "requires": [],
            }
        )
    )
    received = []
    monkeypatch.setattr(
        cli, "run_case", lambda *args, **kwargs: received.append((args, kwargs)) or report
    )
    assert cli.main([*argv, "--skill-json", str(skill_path)]) == 0
    spec = received[0][1]["skill"]
    assert spec.revision == "7" and spec.rule_ids == ("R1",) and spec.requires == ()
    assert received[0][0][2].cli_path == Path(executable).resolve()
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


@pytest.mark.parametrize(
    "value",
    [
        {"skill_id": "x", "revision": "1", "body": "Read", "rule_ids": "R1"},
        {"skill_id": "x", "revision": "latest", "body": "Read", "rule_ids": ["R1"]},
        {"skill_id": "x", "revision": "1", "body": "Read", "rule_ids": ["R1"], "unknown": 1},
        {"skill_id": "x", "revision": "1", "rule_ids": ["R1"]},
    ],
)
def test_cli_rejects_invalid_skill_shape_before_starting_agent(
    tmp_path, monkeypatch, capsys, value
):
    argv, _ = arguments(tmp_path)
    skill = tmp_path / "skill.json"
    skill.write_text(json.dumps(value))
    monkeypatch.setattr(
        cli, "run_case", lambda *args, **kwargs: pytest.fail("Agent must not start")
    )
    assert cli.main([*argv, "--skill-json", str(skill)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_cli_rejects_duplicate_skill_keys(tmp_path, monkeypatch):
    argv, _ = arguments(tmp_path)
    skill = tmp_path / "skill.json"
    skill.write_text(
        '{"skill_id":"x","revision":"1","revision":"2","body":"Read","rule_ids":["R1"]}'
    )
    monkeypatch.setattr(
        cli, "run_case", lambda *args, **kwargs: pytest.fail("Agent must not start")
    )
    assert cli.main([*argv, "--skill-json", str(skill)]) == 2


@pytest.mark.parametrize("status", ["incomplete", "failed", "unknown"])
def test_cli_only_returns_zero_for_passed_adapter_report(tmp_path, monkeypatch, status):
    argv, report = arguments(tmp_path, status=status)
    monkeypatch.setattr(cli, "run_case", lambda *args, **kwargs: report)
    assert cli.main(argv) == 1


def test_cli_does_not_echo_sensitive_runtime_exception(tmp_path, monkeypatch, capsys):
    argv, _ = arguments(tmp_path)

    def fail(*args, **kwargs):
        raise ValueError("capability_missing: PRIVATE-KEY at https://private-endpoint.example")

    monkeypatch.setattr(cli, "run_case", fail)
    assert cli.main(argv) == 2
    output = capsys.readouterr().out
    assert "PRIVATE" not in output and "private-endpoint" not in output
    assert json.loads(output)["error_code"] == "capability_missing"


def test_cli_requires_explicit_model_and_rejects_unknown_secret_arguments(
    tmp_path, monkeypatch, capsys
):
    argv, _ = arguments(tmp_path)
    index = argv.index("--model")
    del argv[index : index + 2]
    monkeypatch.setattr(
        cli, "run_case", lambda *args, **kwargs: pytest.fail("Agent must not start")
    )
    assert cli.main(argv) == 2
    assert cli.main([*argv, "--api-key", "PRIVATE-KEY"]) == 2
    captured = capsys.readouterr()
    assert "PRIVATE-KEY" not in captured.out + captured.err


@pytest.mark.parametrize(
    "extra",
    [["--timeout", "nan"], ["--max-turns", "0"], ["--max-budget", "-1"], ["--model", "sonnet"]],
)
def test_cli_rejects_invalid_limits_or_floating_model_before_run(tmp_path, monkeypatch, extra):
    argv, _ = arguments(tmp_path)
    monkeypatch.setattr(
        cli, "run_case", lambda *args, **kwargs: pytest.fail("Agent must not start")
    )
    assert cli.main([*argv, *extra]) == 2
