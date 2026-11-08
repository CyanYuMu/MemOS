"""Configuration and single-owner recovery boundaries."""

import json

import pytest

from memos.skill_management.config import read_config, service_lock
from memos.skill_management.domain import ManagementError


@pytest.fixture(autouse=True)
def isolated_lock(monkeypatch, tmp_path):
    monkeypatch.setattr("memos.skill_management.config.tempfile.gettempdir", lambda: str(tmp_path))


def config(tmp_path):
    return {
        "serviceToken": "x" * 32,
        "listen": {"host": "127.0.0.1", "port": 5281},
        "graph": {
            "uri": "bolt://localhost:7687",
            "user": "neo4j",
            "password": "test",
            "database": "neo4j",
            "memory_database_id": "different-id",
        },
        "memory": {
            "url": "http://localhost:8000",
            "ledger": "/tmp/ledger",
            "snapshots": "/tmp/snapshots",
        },
        "projects": [
            {
                "projectId": "review",
                "graphProject": "graph-review",
                "candidates": [{"directory": "/tmp/candidate", "challenges": []}],
            }
        ],
        "runtime": {
            "cliPath": "/tmp/claude",
            "model": "fixed-model-1",
            "timeoutSeconds": 120,
            "maxTurns": 8,
            "maxBudgetUsd": 1,
            "providerEnvironment": {},
        },
        "artifactsRoot": str(tmp_path),
    }


def test_private_permissions_required(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config(tmp_path)))
    path.chmod(0o644)
    with pytest.raises(ManagementError, match="private_config_required"):
        read_config(path)
    path.chmod(0o600)
    assert read_config(path)["projects"][0]["projectId"] == "review"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["listen"].update(host="0.0.0.0"),
        lambda c: c["runtime"]["providerEnvironment"].update(CLAUDE_CONFIG_DIR="/private"),
        lambda c: c["runtime"]["providerEnvironment"].update(
            ANTHROPIC_BASE_URL="https://example.invalid/v1/messages"
        ),
        lambda c: c["projects"].append(c["projects"][0]),
        lambda c: c["projects"].append({**c["projects"][0], "projectId": "other-project"}),
        lambda c: c.update(serviceToken="short"),
    ],
)
def test_configuration_rejects_unsafe_boundary(tmp_path, mutation):
    value = config(tmp_path)
    mutation(value)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(ManagementError):
        read_config(path)


def test_same_database_cannot_recover_live_jobs(tmp_path):
    value = config(tmp_path)
    value["graph"]["uri"] += "/" + tmp_path.name
    with service_lock(value):  # noqa: SIM117 - owner and contender have different lifetimes
        with pytest.raises(ValueError, match="service_already_running"), service_lock(value):
            pass
    with service_lock(value):
        pass


def test_database_uri_alias_cannot_bypass_owner(tmp_path):
    value = config(tmp_path)
    with service_lock(value):
        alias = config(tmp_path)
        alias["graph"]["uri"] = "neo4j://127.0.0.1:7687"
        with pytest.raises(ValueError, match="service_already_running"), service_lock(alias):
            pass
