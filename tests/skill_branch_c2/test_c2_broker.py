"""Structured public reports use the unchanged MCP transport envelope."""

import json
import sys

import pytest

from memos.skill_branch_c2.broker import (
    VisibleCheckBroker,
    check_visible_record,
    decode_public_result,
)
from memos.skill_branch_c2.tasks import get_suite, materialize
from memos.skill_experiments.recording_server import _request_visible_checks


SUITE = "scoped-healthy-dream-c2-v1"
MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed no-fork executor")


@MACOS
@pytest.mark.parametrize("variant", ["initial", "unsafe_dream_caller", "reference"])
def test_public_json_reaches_the_frozen_client_and_rechecks(tmp_path, variant):
    candidate = materialize(SUITE, tmp_path / "candidate")
    for name, content in get_suite(SUITE).variants[variant].items():
        (candidate / name).write_text(content)
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence") as broker:
        result = _request_visible_checks(
            {"test_broker_socket": str(broker.socket_path), "test_broker_timeout_seconds": 20}
        )
    assert result == broker.records[0]["result"]
    report = decode_public_result(SUITE, result)
    assert report["status"] == ("passed" if variant == "reference" else "failed")
    assert len(report["checks"]) == 11
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)
    assert (tmp_path / "evidence/checks/check-1/extension_visible/stdout.txt").read_text() == ""


def test_plain_old_stdout_is_not_a_structured_extension_result():
    result = {
        "exit_code": 0,
        "stdout": "caller_contract: passed\n",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": None,
    }
    with pytest.raises(ValueError):
        decode_public_result(SUITE, result)


@MACOS
def test_modified_mapping_and_duplicate_json_fields_are_rejected(tmp_path):
    candidate = materialize(SUITE, tmp_path / "candidate")
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence") as broker:
        result = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    data = json.loads(result["stdout"])
    data["checkMapping"] = list(reversed(data["checkMapping"]))
    with pytest.raises(ValueError):
        decode_public_result(SUITE, {**result, "stdout": json.dumps(data)})
    with pytest.raises(ValueError):
        decode_public_result(
            SUITE, {**result, "stdout": result["stdout"].replace("{", '{"status":"passed",', 1)}
        )


@MACOS
@pytest.mark.parametrize(
    "source",
    ["raise SystemExit(0)\n", "while True: pass\n", "while True: print('x' * 8192, flush=True)\n"],
)
def test_public_execution_errors_are_delivered_and_never_green(tmp_path, source):
    candidate = materialize(SUITE, tmp_path / "candidate")
    (candidate / "caller.py").write_text(source)
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence", timeout=0.25) as broker:
        result = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    report = decode_public_result(SUITE, result)
    assert report["status"] == "error"
    assert report["groupStatus"] == {"scoped": "error", "dream": "error"}
    assert result["execution_error"]
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)


@MACOS
@pytest.mark.parametrize("field", ["checks", "group_status", "result", "suite_identity"])
def test_rehashed_public_record_changes_are_rejected(tmp_path, field):
    from memos.skill_drafting.domain import hash_json

    candidate = materialize(SUITE, tmp_path / "candidate")
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence") as broker:
        _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    path = tmp_path / "evidence/check-1.json"
    record = json.loads(path.read_text())
    record[field] = [] if field == "checks" else {}
    record["record_digest"] = hash_json({k: v for k, v in record.items() if k != "record_digest"})
    path.write_text(json.dumps(record))
    assert not check_visible_record(path, candidate)


@MACOS
def test_public_budget_and_argument_rejection_do_not_execute_additional_programs(tmp_path):
    import socket

    candidate = materialize(SUITE, tmp_path / "candidate")
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence", max_calls=1) as broker:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
            client.sendall(b'{"action":"run_visible_checks","program":"print(1)"}\n')
            rejected = json.loads(client.recv(4096))
        assert rejected["execution_error"] == "invalid_broker_request"
        assert not broker.records
        _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
        result = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
        assert result["execution_error"] == "test_budget_exhausted"
        assert len(broker.records) == 1
    assert not broker.socket_path.exists()


@pytest.mark.parametrize("value", [True, 0, -1, 101, None])
def test_invalid_public_budget_fails_before_creating_artifacts(tmp_path, value):
    with pytest.raises(ValueError):
        VisibleCheckBroker(SUITE, tmp_path / "missing", tmp_path / "evidence", max_calls=value)
    assert not (tmp_path / "evidence").exists()


@MACOS
def test_public_schema_rejects_true_in_place_of_numeric_version(tmp_path):
    candidate = materialize(SUITE, tmp_path / "candidate")
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence") as broker:
        result = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    report = json.loads(result["stdout"])
    report["schemaVersion"] = True
    with pytest.raises(ValueError):
        decode_public_result(SUITE, {**result, "stdout": json.dumps(report)})


@MACOS
def test_cancellation_interrupts_public_execution_and_retains_error(tmp_path):
    import threading
    import time

    from concurrent.futures import ThreadPoolExecutor

    candidate = materialize(SUITE, tmp_path / "candidate")
    (candidate / "caller.py").write_text("while True: pass\n")
    cancellation = threading.Event()
    with (
        VisibleCheckBroker(
            SUITE, candidate, tmp_path / "evidence", timeout=5, cancellation_event=cancellation
        ) as broker,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        response = pool.submit(
            _request_visible_checks, {"test_broker_socket": str(broker.socket_path)}
        )
        deadline = time.monotonic() + 3
        while not broker.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert broker.active
        cancellation.set()
        result = response.result(timeout=3)
    assert broker.records[0]["execution"]["cancelled"]
    assert decode_public_result(SUITE, result)["status"] == "error"
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)
    assert not broker.socket_path.exists()


def test_unsupported_isolation_is_returned_as_failure_without_fallback(tmp_path, monkeypatch):
    from memos.skill_adapter.isolation import IsolationUnavailableError
    from memos.skill_branch_c2 import broker as module

    candidate = materialize(SUITE, tmp_path / "candidate")
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise IsolationUnavailableError("unsupported")

    monkeypatch.setattr(module, "execute_program", unavailable)
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence") as broker:
        result = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    assert calls == [1]
    assert result["execution_error"] == "test_input_or_isolation_invalid"
    assert not broker.records


@MACOS
def test_rebinding_public_implementation_during_execution_rejects_result(tmp_path, monkeypatch):
    from memos.skill_branch_c2 import broker as module

    candidate = materialize(SUITE, tmp_path / "candidate")
    actual = module.execute_program

    def execute_and_change(*args, **kwargs):
        execution = actual(*args, **kwargs)
        monkeypatch.setattr(module, "implementation_digest", lambda: "new-implementation")
        return execution

    monkeypatch.setattr(module, "execute_program", execute_and_change)
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "evidence") as broker:
        result = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    assert result["execution_error"] == "extension_binding_changed"
    assert not broker.records
    assert list((tmp_path / "evidence").rglob("execution.json"))
