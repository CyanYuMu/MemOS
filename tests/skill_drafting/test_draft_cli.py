"""CLI contracts exercise boundaries without model, Memory or graph services."""

import builtins
import importlib.util
import json

from pathlib import Path
from types import SimpleNamespace

import pytest

from memos.skill_drafting import __main__ as cli


def plan():
    return {
        "schema_version": 1,
        "project_id": "p2-project",
        "scope": {
            "service_identity": "a" * 64,
            "cube_id": "cube",
            "user_id": "user",
            "namespace": "source",
        },
        "sources": [
            {
                "evidence_path": f"/source-{i}.json",
                "evidence_revision": str(i) * 64,
                "memory_ids": [f"memory-{i}"],
            }
            for i in (1, 2)
        ],
    }


@pytest.fixture
def args(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()))
    return [
        "draft",
        "--plan",
        str(path),
        "--output",
        str(tmp_path / "job"),
        "--skill-id",
        "repair-contract",
        "--ledger",
        str(tmp_path / "ledger.sqlite"),
        "--snapshots",
        str(tmp_path / "snapshots"),
    ]


def output(capsys):
    return json.loads(capsys.readouterr().out)


def test_help_loads_no_workflow_or_optional_modules(monkeypatch, capsys):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in {"openai", "neo4j", "httpx"} or name.startswith(
            ("memos.skill_drafting.", "memos.skill_experience.")
        ):
            raise AssertionError("help must not import workflow dependencies")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    spec = importlib.util.spec_from_file_location("isolated_draft_cli", Path(cli.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as caught:
        module.main(["--help"])
    assert caught.value.code == 0
    assert "graph-import" in capsys.readouterr().out


def test_unknown_credential_argument_is_never_echoed(capsys):
    assert cli.main(["--api-key", "synthetic-private-key"]) == 2
    result = output(capsys)
    assert result == {"status": "error", "error_code": "cli_invalid_arguments"}


def test_duplicate_plan_keys_fail_before_bridge(args, monkeypatch, capsys):
    Path(args[2]).write_text('{"schema_version":1,"schema_version":1}')
    monkeypatch.setattr(cli, "_bridge", lambda *a: pytest.fail("must not build bridge"))
    assert cli.main(args) == 2
    assert output(capsys)["error_code"] == "cli_plan_failed"


@pytest.mark.parametrize("identity", ["../other", "", "wrong project"])
def test_invalid_project_fails_before_graph_connection(identity, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repository", lambda *a: pytest.fail("must not connect"))
    assert cli.main(["graph-inspect", "--project", identity]) == 2
    assert output(capsys)["error_code"] == "cli_configuration_failed"


def test_draft_routes_frozen_plan_and_closes_model(args, tmp_path, monkeypatch, capsys):
    calls = []
    expected = plan()
    generator = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(
        cli, "_bridge", lambda options, value: calls.append(("bridge", value)) or "bridge"
    )
    monkeypatch.setattr(
        cli,
        "_generator",
        lambda options: calls.append(("generator", options.timeout, options.max_tokens))
        or generator,
    )

    def draft(bridge, value, provider, destination, *, skill_id):
        assert bridge == "bridge" and value == expected and provider is generator
        assert skill_id == "repair-contract"
        calls.append("draft")
        destination.mkdir()
        path = destination / "job.json"
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "candidate_directory": str(destination / "candidate"),
                    "qualification": "unpublished_candidate",
                }
            )
        )
        return path

    monkeypatch.setattr(cli, "_draft", draft)
    assert cli.main([*args, "--timeout", "42", "--max-tokens", "2100"]) == 0
    result = output(capsys)
    assert result["qualification"] == "unpublished_candidate"
    assert calls == [("bridge", expected), ("generator", 42, 2100), "draft", "close"]


def test_failed_job_returns_one_without_retry(args, monkeypatch, capsys):
    counts = []
    monkeypatch.setattr(cli, "_bridge", lambda *a: "bridge")
    monkeypatch.setattr(
        cli, "_generator", lambda *a: SimpleNamespace(close=lambda: counts.append("close"))
    )

    def draft(*arguments, **kwargs):
        counts.append("draft")
        path = arguments[3] / "job.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "qualification": "not_created",
                    "failure_stage": "generate",
                    "error_code": "generation_provider_failed",
                }
            )
        )
        return path

    monkeypatch.setattr(cli, "_draft", draft)
    assert cli.main(args) == 1
    assert output(capsys)["status"] == "failed"
    assert counts == ["draft", "close"]


def test_exception_does_not_print_provider_details(args, monkeypatch, capsys):
    closed = []
    monkeypatch.setattr(cli, "_bridge", lambda *a: "bridge")
    monkeypatch.setattr(
        cli, "_generator", lambda *a: SimpleNamespace(close=lambda: closed.append(True))
    )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-private-key https://private.invalid")

    monkeypatch.setattr(cli, "_draft", fail)
    assert cli.main(args) == 2
    assert output(capsys) == {"status": "error", "error_code": "cli_draft_failed"}
    assert closed == [True]


def test_graph_import_rejects_project_mismatch_before_services(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli, "_load_candidate", lambda *a: {"packet": {"project_id": "other", "plan": plan()}}
    )
    monkeypatch.setattr(cli, "_bridge", lambda *a: pytest.fail("must not build bridge"))
    monkeypatch.setattr(cli, "_repository", lambda *a: pytest.fail("must not build graph"))
    arguments = [
        "graph-import",
        "--candidate",
        str(tmp_path),
        "--project",
        "p2-project",
        "--ledger",
        str(tmp_path / "ledger"),
        "--snapshots",
        str(tmp_path / "snapshots"),
    ]
    assert cli.main(arguments) == 2
    assert output(capsys)["error_code"] == "cli_candidate_failed"


def test_graph_import_uses_live_bridge_and_closes_repository(monkeypatch, tmp_path, capsys):
    calls = []
    repository = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(
        cli, "_load_candidate", lambda *a: {"packet": {"project_id": "p2-project", "plan": plan()}}
    )
    monkeypatch.setattr(
        cli, "_bridge", lambda options, value: calls.append(("bridge", value)) or "bridge"
    )
    monkeypatch.setattr(cli, "_repository", lambda options: repository)

    def import_candidate(directory, bridge, repo):
        assert directory == tmp_path and bridge == "bridge" and repo is repository
        calls.append("import")
        return {"nodes": 2, "edges": 1}

    monkeypatch.setattr(cli, "_import_candidate", import_candidate)
    arguments = [
        "graph-import",
        "--candidate",
        str(tmp_path),
        "--project",
        "p2-project",
        "--ledger",
        str(tmp_path / "ledger"),
        "--snapshots",
        str(tmp_path / "snapshots"),
    ]
    assert cli.main(arguments) == 0
    assert output(capsys)["result"] == {"nodes": 2, "edges": 1}
    assert calls == [("bridge", plan()), "import", "close"]


def test_inspect_is_offline_and_returns_review_status(monkeypatch, tmp_path, capsys):
    loaded = {
        "candidate": {"status": "candidate"},
        "validation": {"semantic_review": "pending"},
        "manifest": {"manifest_id": "manifest"},
    }
    monkeypatch.setattr(cli, "_load_candidate", lambda *a: loaded)
    monkeypatch.setattr(cli, "_bridge", lambda *a: pytest.fail("offline inspection"))
    monkeypatch.setattr(cli, "_repository", lambda *a: pytest.fail("offline inspection"))
    assert cli.main(["inspect", "--candidate", str(tmp_path)]) == 0
    result = output(capsys)
    assert result["candidate"]["status"] == "candidate"
    assert result["validation"]["semantic_review"] == "pending"


@pytest.mark.parametrize(
    "command, extra, method, value",
    [
        ("graph-inspect", [], "inspect", None),
        (
            "trace",
            ["--rule-key", "repair-contract@" + "b" * 64 + "#R1"],
            "trace_rule",
            "repair-contract@" + "b" * 64 + "#R1",
        ),
        ("affected", ["--memory-id", "memory-1"], "affected_by_memory", "memory-1"),
    ],
)
def test_graph_queries_route_explicit_project(command, extra, method, value, monkeypatch, capsys):
    calls = []

    def operation(*values):
        calls.append(values)
        return {"found": True}

    repository = SimpleNamespace(**{method: operation}, close=lambda: calls.append("close"))

    def make(options):
        assert options.project == "p2-project"
        return repository

    monkeypatch.setattr(cli, "_repository", make)
    assert cli.main([command, "--project", "p2-project", *extra]) == 0
    assert output(capsys)["result"] == {"found": True}
    assert calls == ([()] if value is None else [(value,)]) + ["close"]


def test_model_configuration_uses_environment_only(monkeypatch):
    from memos.skill_drafting import generation

    captured = {}
    monkeypatch.setattr(
        generation, "OpenAIJSONGenerator", lambda **kwargs: captured.update(kwargs) or "generator"
    )
    for name, value in {
        "BASE_URL": "https://provider.invalid/v1",
        "API_KEY": "synthetic-key",
        "MODEL": "fixed-model",
    }.items():
        monkeypatch.setenv("MEMOS_SKILL_LLM_" + name, value)
    assert cli._generator(SimpleNamespace(timeout=40, max_tokens=3000)) == "generator"
    assert captured == {
        "base_url": "https://provider.invalid/v1",
        "api_key": "synthetic-key",
        "model": "fixed-model",
        "timeout": 40,
        "max_tokens": 3000,
    }


def test_graph_configuration_requires_all_explicit_environment(monkeypatch):
    from memos.skill_drafting import graph

    captured = {}
    monkeypatch.setattr(
        graph,
        "SkillGraphRepository",
        lambda *args, **kwargs: captured.update(uri=args[0], **kwargs) or "repository",
    )
    values = {
        "URI": "bolt://localhost:7689",
        "USER": "neo4j",
        "PASSWORD": "synthetic-password",
        "DATABASE": "neo4j",
        "MEMORY_DB_ID": "memory-database-id",
    }
    for name, value in values.items():
        monkeypatch.setenv("MEMOS_SKILL_GRAPH_" + name, value)
    assert cli._repository(SimpleNamespace(project="p2-project")) == "repository"
    assert captured["memory_database_id"] == "memory-database-id"
    assert captured["project_id"] == "p2-project"
    monkeypatch.delenv("MEMOS_SKILL_GRAPH_MEMORY_DB_ID")
    with pytest.raises(ValueError):
        cli._repository(SimpleNamespace(project="p2-project"))


def test_bridge_checks_service_identity_before_ledger_mutation(tmp_path, monkeypatch):
    from memos.skill_experience import bridge, client

    monkeypatch.setenv("MEMOS_SKILL_API_URL", "https://memory.invalid")
    monkeypatch.setattr(
        client, "ProductMemoryClient", lambda *a, **kw: SimpleNamespace(service_identity="b" * 64)
    )
    monkeypatch.setattr(
        bridge,
        "MemoryBridge",
        lambda *a, **kw: pytest.fail("scope mismatch must precede bridge creation"),
    )
    ledger = tmp_path / "ledger"
    ledger.write_bytes(b"")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    with pytest.raises(ValueError):
        cli._bridge(SimpleNamespace(ledger=ledger, snapshots=snapshots, memory_timeout=30), plan())


def test_trace_accepts_real_graph_rule_identity(monkeypatch, capsys):
    from memos.skill_drafting.graph import node_key

    key = node_key("Rule", {"skill_id": "repair-contract", "revision": "c" * 64, "rule_id": "R1"})
    captured = []
    repository = SimpleNamespace(
        trace_rule=lambda value: captured.append(value) or {}, close=lambda: None
    )
    monkeypatch.setattr(cli, "_repository", lambda *a: repository)
    assert cli.main(["trace", "--project", "p2-project", "--rule-key", key]) == 0
    assert captured == [key]
    assert output(capsys)["status"] == "completed"


def test_trace_rejects_a_digest_instead_of_logical_rule_identity(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repository", lambda *a: pytest.fail("must not connect"))
    assert cli.main(["trace", "--project", "p2-project", "--rule-key", "b" * 64]) == 2
    assert output(capsys)["error_code"] == "cli_configuration_failed"
