"""The v2 visible broker exercises locked plans inside real no-fork sandboxes."""

import json
import socket
import sys
import threading
import time

import pytest

from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_evaluation.tasks import materialize_case
from memos.skill_verification.broker import VisibleCheckBroker, check_visible_record


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


@pytest.mark.parametrize("variant,expected", [("initial", 1), ("reference", 0)])
def test_fixture_visible_check_uses_real_pytest_without_candidate_fork(tmp_path, variant, expected):
    workspace = materialize_case("holdout-fixture", tmp_path / "workspace", variant)
    evidence = tmp_path / "evidence"
    with VisibleCheckBroker("holdout-fixture", workspace, evidence) as broker:
        outcome = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
        assert outcome["exit_code"] == expected, outcome
        assert not outcome.get("execution_error")
        assert (
            outcome["evidence_ref"]["candidate_snapshot"] == broker.records[0]["candidate_snapshot"]
        )
        assert broker.records[0]["plan_digest"]
        assert check_visible_record(evidence / "check-1.json", workspace)
        invalid = request(
            broker.socket_path,
            {"action": "run_visible_checks", "path": "/tmp/arbitrary.py"},
        )
        assert invalid["execution_error"] == "invalid_broker_request"
        assert len(broker.records) == 1
    assert not broker.socket_path.exists()


def test_protected_visible_script_is_rejected_before_execution(tmp_path):
    workspace = materialize_case("holdout-fixture", tmp_path / "workspace")
    (workspace / "visible_checks.py").write_text("raise RuntimeError('modified')")
    with VisibleCheckBroker("holdout-fixture", workspace, tmp_path / "evidence") as broker:
        outcome = request(broker.socket_path, {"action": "run_visible_checks"})
        assert outcome["execution_error"] == "protected_task_changed"
        assert not broker.records


def test_broker_budget_and_result_tampering_are_rejected(tmp_path):
    workspace = materialize_case("holdout-record", tmp_path / "workspace", "reference")
    evidence = tmp_path / "evidence"
    with VisibleCheckBroker("holdout-record", workspace, evidence, max_calls=1) as broker:
        assert request(broker.socket_path, {"action": "run_visible_checks"})["exit_code"] == 0
        assert (
            request(broker.socket_path, {"action": "run_visible_checks"})["execution_error"]
            == "test_budget_exhausted"
        )
    record = evidence / "check-1.json"
    value = json.loads(record.read_text())
    value["result"]["stdout"] = "invented success"
    record.write_text(json.dumps(value))
    assert not check_visible_record(record, workspace)


def test_broker_exit_cancels_current_candidate_and_retains_record(tmp_path):
    workspace = materialize_case("holdout-record", tmp_path / "workspace", "reference")
    caller = workspace / "caller.py"
    caller.write_text("import time\ntime.sleep(30)\n" + caller.read_text())
    broker = VisibleCheckBroker("holdout-record", workspace, tmp_path / "evidence")
    broker.__enter__()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(request(broker.socket_path, {"action": "run_visible_checks"}))
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not broker.active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert broker.active
    started = time.monotonic()
    broker.__exit__(None, None, None)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert time.monotonic() - started < 3
    assert results[0]["execution_error"] == "test_cancelled"
    assert broker.records[0]["execution"]["cancelled"]


def test_visible_result_does_not_expose_credential_or_accept_changed_candidate(tmp_path):
    workspace = materialize_case("holdout-record", tmp_path / "workspace", "reference")
    secret = "synthetic-visible-private-value"
    caller = workspace / "caller.py"
    caller.write_text(f"print('Authorization: Bearer {secret}')\n" + caller.read_text())
    evidence = tmp_path / "evidence"
    with VisibleCheckBroker(
        "holdout-record", workspace, evidence, secret_values=(secret,)
    ) as broker:
        outcome = request(broker.socket_path, {"action": "run_visible_checks"})
        assert outcome["exit_code"] == 0
        assert secret not in json.dumps(outcome)
    assert secret not in (evidence / "check-1.json").read_text()
    caller.write_text(caller.read_text() + "\n# changed after check\n")
    assert not check_visible_record(evidence / "check-1.json", workspace)
