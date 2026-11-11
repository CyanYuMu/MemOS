"""Public orchestration must retain failures and never select only good sources."""

import json

from pathlib import Path

import pytest

from memos.skill_demo.workflow import SOURCES, init_demo, load_config, source_entries


@pytest.fixture
def config(tmp_path):
    value = {
        "schemaVersion": 1,
        "projectId": "demo-one",
        "skillId": "demo-dream-contract",
        "cliPath": "/opt/demo/claude",
        "agentModel": "test-model",
        "cubeId": "example-cube",
        "userId": "example-user",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return path


def test_config_rejects_secrets_unknown_keys_and_wrong_types(config):
    assert load_config(config)["projectId"] == "demo-one"
    original = config.read_text()
    for key, value in (
        ("apiKey", "do-not-echo"),
        ("schemaVersion", True),
        ("projectId", "../other"),
    ):
        data = json.loads(original)
        data[key] = value
        config.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            load_config(config)


def test_new_demo_does_not_reuse_old_directory(config, tmp_path):
    output = tmp_path / "demo"
    init_demo(load_config(config), output)
    before = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        init_demo(load_config(config), output)
    assert (output / "manifest.json").read_bytes() == before


def test_missing_source_blocks_before_memory_or_model(config, tmp_path, monkeypatch):
    output = tmp_path / "demo"
    init_demo(load_config(config), output)
    called = []
    monkeypatch.setattr("memos.skill_demo.workflow.verify_evidence", lambda p: called.append(p))
    with pytest.raises((ValueError, FileNotFoundError)):
        source_entries(output)
    assert called == []


def test_failed_source_is_retained_and_unqualified_source_blocks(tmp_path, monkeypatch):
    def evidence(path):
        case = Path(path).parents[1].name
        return {
            "qualification": "qualified_success" if case == SOURCES[0] else "qualified_failure",
            "evidence_revision": case,
            "case": {"case_id": case, "split": "source"},
        }

    monkeypatch.setattr("memos.skill_demo.workflow.verify_evidence", evidence)
    for case in SOURCES:
        p = tmp_path / "sources" / case
        (p / "evidence").mkdir(parents=True)
        (p / "evidence/evidence.json").write_text("{}")
        (p / "collection.json").write_text(
            json.dumps(
                {
                    "case_id": case,
                    "status": "collected",
                    "evidence": str(p / "evidence/evidence.json"),
                }
            )
        )
    assert len(source_entries(tmp_path)) == 2
    path = tmp_path / "sources" / SOURCES[1] / "collection.json"
    data = json.loads(path.read_text())
    data["status"] = "collection_failed"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="demo_source_incomplete"):
        source_entries(tmp_path)


def test_endpoint_change_refused_before_new_memory_claim(tmp_path, monkeypatch):
    from memos.skill_demo.workflow import bridge

    config = {"cubeId": "cube", "userId": "user", "projectId": "demo"}
    monkeypatch.setenv("MEMOS_SKILL_API_URL", "http://127.0.0.1:11111")
    first = bridge(config, tmp_path)
    assert bridge(config, tmp_path).client.service_identity == first.client.service_identity
    monkeypatch.setenv("MEMOS_SKILL_API_URL", "http://127.0.0.1:22222")
    with pytest.raises(ValueError, match="demo_binding_changed"):
        bridge(config, tmp_path)


def test_repeated_collection_never_invokes_model(config, tmp_path, monkeypatch):
    from memos.skill_demo import workflow

    data = load_config(config)
    output = tmp_path / "demo"
    init_demo(data, output)
    (output / "sources").mkdir()
    monkeypatch.setattr(workflow, "run_config", lambda c: object())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(
        "memos.skill_experience.collection.collect_source",
        lambda *a, **kw: pytest.fail("model was invoked twice"),
    )
    with pytest.raises(FileExistsError):
        workflow.collect(data, output, lambda value: None)


def test_cli_error_does_not_echo_private_input(tmp_path, capsys):
    from memos.skill_demo.__main__ import main

    assert main(["collect", "--secret", "do-not-echo"]) == 2
    assert "do-not-echo" not in capsys.readouterr().out
