"""Public branch checks have fixed inputs and independently rechecked evidence."""

import hashlib
import json
import socket
import sys
import threading
import time

import pytest

from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_branch_validation.broker import VisibleCheckBroker, check_visible_record
from memos.skill_branch_validation.tasks import materialize
from memos.skill_drafting.domain import hash_json


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")


def request(broker):
    return _request_visible_checks({"test_broker_socket": str(broker.socket_path)})


def run_once(tmp_path, task="branch-scoped-table", variant="reference", **kwargs):
    candidate = materialize(task, tmp_path / "candidate", variant)
    evidence = tmp_path / "evidence"
    with VisibleCheckBroker(task, candidate, evidence, **kwargs) as broker:
        result = request(broker)
    return candidate, evidence / "check-1.json", result


@pytest.mark.parametrize("task", ["branch-scoped-table", "branch-dream-journal"])
@pytest.mark.parametrize("variant,code", [("initial", 1), ("reference", 0)])
def test_fixed_public_checks_match_expected_results_and_recheck(tmp_path, task, variant, code):
    candidate, path, result = run_once(tmp_path, task, variant)
    assert result["exit_code"] == code and result["execution_error"] is None
    assert check_visible_record(path, candidate)
    record = json.loads(path.read_text())
    program = path.parent / record["phase_job"] / "program.py"
    assert "oracle" not in program.read_text()
    assert "reference_files" not in program.read_text()


def test_invalid_request_does_not_spend_budget_and_second_real_request_exhausts_it(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    with VisibleCheckBroker(
        "branch-scoped-table", candidate, tmp_path / "evidence", max_calls=1
    ) as broker:
        with socket.socket(socket.AF_UNIX) as stream:
            stream.connect(str(broker.socket_path))
            stream.sendall(b'{"action":"run_visible_checks","program":"print(1)"}\n')
            assert json.loads(stream.recv(8192))["execution_error"] == "invalid_broker_request"
        assert request(broker)["exit_code"] == 0
        assert request(broker)["execution_error"] == "test_budget_exhausted"
        assert len(broker.records) == 1
    assert not broker.socket_path.exists()


def test_public_file_change_is_rejected_before_execution(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    (candidate / "visible_checks.py").write_text("raise SystemExit(0)\n")
    with VisibleCheckBroker("branch-scoped-table", candidate, tmp_path / "evidence") as broker:
        assert request(broker)["execution_error"] == "protected_task_changed"
        assert not broker.records


@pytest.mark.parametrize(
    "source", ["raise SystemExit(0)\n", "print('caller_contract: passed')\nraise SystemExit(0)\n"]
)
def test_early_zero_exit_cannot_claim_complete_checks(tmp_path, source):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    (candidate / "fake_graph.py").write_text(source)
    with VisibleCheckBroker("branch-scoped-table", candidate, tmp_path / "evidence") as broker:
        result = request(broker)
        assert result["exit_code"] == 0
        assert result["execution_error"] == "test_execution_audit_invalid"
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)


def test_timeout_is_retained_as_error_not_success(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    (candidate / "fake_graph.py").write_text("import time\ntime.sleep(5)\n")
    with VisibleCheckBroker(
        "branch-scoped-table", candidate, tmp_path / "evidence", timeout=0.15
    ) as broker:
        result = request(broker)
        assert result["timed_out"] and result["execution_error"] == "test_timeout"
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)


def test_output_limit_is_retained_without_unbounded_response(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    (candidate / "fake_graph.py").write_text("while True: print('x' * 4096)\n")
    with VisibleCheckBroker(
        "branch-scoped-table", candidate, tmp_path / "evidence", timeout=2
    ) as broker:
        result = request(broker)
        assert result["truncated"] and result["execution_error"] == "test_resource_limit"
        assert len(result["stdout"]) < 1000
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)


def test_actual_public_executor_denies_private_read_candidate_write_and_network(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    private = tmp_path / "host-only.txt"
    private.write_text("not visible to candidate")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        preamble = (
            "import socket\nfrom pathlib import Path\n"
            f"try: Path({str(private)!r}).read_text()\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('private read allowed')\n"
            "try: Path(__file__).write_text('changed')\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('candidate write allowed')\n"
            f"try: socket.create_connection({listener.getsockname()!r}, timeout=1)\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('network allowed')\n"
        )
        target = candidate / "fake_graph.py"
        target.write_text(preamble + target.read_text())
        with VisibleCheckBroker("branch-scoped-table", candidate, tmp_path / "evidence") as broker:
            result = request(broker)
            assert result["exit_code"] == 0 and result["execution_error"] is None
    assert check_visible_record(tmp_path / "evidence/check-1.json", candidate)


def test_exit_cancels_active_check_and_removes_socket(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    (candidate / "fake_graph.py").write_text("import time\ntime.sleep(10)\n")
    results = []
    with VisibleCheckBroker(
        "branch-scoped-table", candidate, tmp_path / "evidence", timeout=15
    ) as broker:
        client = threading.Thread(target=lambda: results.append(request(broker)))
        client.start()
        deadline = time.monotonic() + 3
        while not broker.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert broker.active
    client.join(timeout=3)
    assert not client.is_alive() and not broker.active and not broker.socket_path.exists()
    assert results[0]["execution_error"] == "test_cancelled"


@pytest.mark.parametrize(
    "change", ["result", "program", "profile", "execution", "output", "task", "patch", "index"]
)
def test_rehashed_changed_evidence_is_rejected(tmp_path, change):
    candidate, path, _ = run_once(tmp_path)
    record = json.loads(path.read_text())
    job = path.parent / record["phase_job"]
    if change == "result":
        record["result"]["stdout"] = "forged"
    elif change == "task":
        record["task_id"] = "branch-dream-journal"
    elif change == "patch":
        (candidate / "fake_graph.py").write_text("# changed\n")
    elif change == "index":
        record["check_index"] = 2
    else:
        name = {
            "program": "program.py",
            "profile": "isolation.sb",
            "execution": "execution.json",
            "output": "stdout.txt",
        }[change]
        artifact = job / name
        if change == "execution":
            value = json.loads(artifact.read_text())
            value["limits"]["fork_allowed"] = True
            artifact.write_text(json.dumps(value))
            record["execution"] = value
        else:
            artifact.write_text(
                {
                    "program": "print('forged')\n",
                    "profile": "(version 1)\n(allow default)\n",
                    "output": "",
                }[change]
            )
        record["phase_artifacts"][name].update(
            sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(), size=artifact.stat().st_size
        )
    record["record_digest"] = hash_json({k: v for k, v in record.items() if k != "record_digest"})
    path.write_text(json.dumps(record))
    assert not check_visible_record(path, candidate)


def test_overlapping_roots_and_linked_evidence_are_rejected(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate")
    with pytest.raises(ValueError):
        VisibleCheckBroker("branch-scoped-table", candidate, candidate / "evidence")
    with pytest.raises(ValueError):
        VisibleCheckBroker("branch-scoped-table", candidate, tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        VisibleCheckBroker("branch-scoped-table", candidate, link)


def test_snapshot_store_symlink_is_rejected_on_readback(tmp_path):
    candidate, path, _ = run_once(tmp_path)
    store = path.parent / "snapshots"
    moved = tmp_path / "moved"
    store.rename(moved)
    store.symlink_to(moved, target_is_directory=True)
    assert not check_visible_record(path, candidate)
