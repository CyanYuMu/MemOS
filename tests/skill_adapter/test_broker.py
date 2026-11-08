"""Exercise the host broker with real isolated Python, without a model service."""

import json
import socket
import sys

import pytest

from memos.skill_adapter.broker import VisibleCheckBroker
from memos.skill_evaluation.tasks import materialize_case


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolation backend")


def request(path, value):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(str(path))
        client.sendall(json.dumps(value).encode() + b"\n")
        result = b""
        while b"\n" not in result:
            result += client.recv(8192)
    return json.loads(result)


def test_broker_runs_sealed_fixed_checks_and_rejects_command_override(tmp_path):
    workspace = materialize_case("dev-strict-signature", tmp_path / "workspace", "reference")
    with VisibleCheckBroker("dev-strict-signature", workspace, tmp_path / "evidence") as broker:
        outcome = request(broker.socket_path, {"action": "run_visible_checks"})
        assert outcome["exit_code"] == 0, outcome
        assert len(broker.records) == 1
        assert broker.records[0]["candidate_snapshot"]
        assert broker.records[0]["profile_sha256"]
        invalid = request(
            broker.socket_path, {"action": "run_visible_checks", "command": "echo bad"}
        )
        assert invalid["execution_error"] == "invalid_broker_request"
        assert len(broker.records) == 1
    assert not broker.socket_path.exists()


def test_changed_protected_check_is_never_executed(tmp_path):
    workspace = materialize_case("dev-strict-signature", tmp_path / "workspace")
    (workspace / "visible_checks.py").write_text("raise RuntimeError('tampered')")
    with VisibleCheckBroker("dev-strict-signature", workspace, tmp_path / "evidence") as broker:
        result = request(broker.socket_path, {"action": "run_visible_checks"})
        assert result["execution_error"] == "protected_task_changed"
        assert not broker.records


def test_candidate_cannot_read_host_or_fork_from_visible_checks(tmp_path):
    workspace = materialize_case("dev-strict-signature", tmp_path / "workspace", "reference")
    hidden = tmp_path / "host-secret"
    hidden.write_text("host canary")
    caller = workspace / "caller.py"
    prefix = (
        "import os\nfrom pathlib import Path\n"
        f"try:\n    Path({str(hidden)!r}).read_text()\n"
        "except PermissionError:\n    pass\nelse:\n    raise RuntimeError('host read escaped')\n"
        "try:\n    os.fork()\nexcept PermissionError:\n    pass\n"
        "else:\n    raise RuntimeError('fork escaped')\n"
    )
    caller.write_text(prefix + caller.read_text())
    with VisibleCheckBroker("dev-strict-signature", workspace, tmp_path / "evidence") as broker:
        outcome = request(broker.socket_path, {"action": "run_visible_checks"})
        assert outcome["exit_code"] == 0, outcome


def test_per_run_budget_limits_repeated_execution(tmp_path):
    workspace = materialize_case("dev-strict-signature", tmp_path / "workspace", "reference")
    with VisibleCheckBroker(
        "dev-strict-signature", workspace, tmp_path / "evidence", max_calls=1
    ) as broker:
        assert request(broker.socket_path, {"action": "run_visible_checks"})["exit_code"] == 0
        assert (
            request(broker.socket_path, {"action": "run_visible_checks"})["execution_error"]
            == "test_budget_exhausted"
        )


def test_public_check_output_is_redacted_before_evidence_is_saved(tmp_path):
    workspace = materialize_case("dev-strict-signature", tmp_path / "workspace", "reference")
    caller = workspace / "caller.py"
    secret = "synthetic-broker-credential"
    caller.write_text(f"print('Authorization: Bearer {secret}')\n" + caller.read_text())
    evidence = tmp_path / "evidence"
    with VisibleCheckBroker(
        "dev-strict-signature", workspace, evidence, secret_values=(secret,)
    ) as broker:
        outcome = request(broker.socket_path, {"action": "run_visible_checks"})
        assert outcome["exit_code"] == 0
        assert secret not in json.dumps(outcome)
        saved = (evidence / "check-1.json").read_text()
        assert secret not in saved
        assert (
            outcome["evidence_ref"]["candidate_snapshot"] == json.loads(saved)["candidate_snapshot"]
        )
