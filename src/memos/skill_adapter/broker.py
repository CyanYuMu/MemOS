"""Host-side fixed checks; candidate code never runs inside the credentialed Agent."""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import tempfile
import threading

from contextlib import suppress
from pathlib import Path

from memos.skill_adapter.events import sanitize_public
from memos.skill_adapter.isolation import IsolationSpec, runtime_paths, sandbox_test_command
from memos.skill_adapter.recording_server import run_fixed_check
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_evaluation.tasks import candidate_policy_violations, get_case


def _failure(reason: str) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": reason,
    }


class VisibleCheckBroker:
    """One run, one fixed test entry point, sealed input and bounded execution.

    The short Unix endpoint is outside all Agent-writable paths. The caller must
    grant only this socket in its Agent profile. Requests cannot name commands or
    paths. Each check executes a snapshot copy in a separate host-created sandbox.
    """

    def __init__(
        self,
        case_id: str,
        workspace: Path,
        evidence: Path,
        *,
        max_calls: int = 20,
        secret_values: tuple[str, ...] = (),
    ):
        self.case = get_case(case_id)
        self.workspace = workspace.resolve()
        self.evidence = evidence.resolve()
        self.store = SnapshotStore(self.evidence / "snapshots")
        self.max_calls = max_calls
        self.secret_values = secret_values
        self.records: list[dict] = []
        self._stop = threading.Event()
        self._temporary = None
        self._thread = None
        self._server = None

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="memos-broker-", dir="/tmp")
        self.socket_path = Path(self._temporary.name).resolve() / "checks.sock"
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self._server.listen(2)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, name="memos-visible-check", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=13)
        self._server.close()
        self._temporary.cleanup()
        if self._thread.is_alive():
            raise RuntimeError("test_broker_cleanup_failed")

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    connection.settimeout(1)
                    data = bytearray()
                    while b"\n" not in data and len(data) <= 1024:
                        chunk = connection.recv(1025 - len(data))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if not data.endswith(b"\n") or len(data) > 1024:
                        raise ValueError("invalid request")
                    request = json.loads(data)
                    if request != {"action": "run_visible_checks"}:
                        raise ValueError("invalid request")
                    result = self._run()
                except (ValueError, TimeoutError, OSError):
                    result = _failure("invalid_broker_request")
                with suppress(OSError):
                    connection.sendall(json.dumps(result, ensure_ascii=False).encode() + b"\n")

    def _run(self) -> dict:
        if len(self.records) >= self.max_calls:
            return _failure("test_budget_exhausted")
        try:
            snapshot = self.store.capture(self.workspace)
            with tempfile.TemporaryDirectory(prefix="memos-fixed-check-", dir="/tmp") as temporary:
                root = Path(temporary).resolve()
                candidate = self.store.restore(snapshot.snapshot_id, root / "candidate")
                if (
                    candidate_policy_violations(self.case, candidate)
                    or (candidate / "visible_checks.py").read_bytes()
                    != self.case.initial_files["visible_checks.py"].encode()
                ):
                    return _failure("protected_task_changed")
                control, config, scratch = (
                    root / name for name in ("control", "config", "scratch")
                )
                for directory in (control, config, scratch):
                    directory.mkdir()
                repository = Path(__file__).resolve().parents[3]
                spec = IsolationSpec(
                    workspace=candidate,
                    control_root=control,
                    config_root=config,
                    runtime_read_roots=runtime_paths(Path(sys.executable)),
                    forbidden_roots=(repository, self.workspace, self.evidence),
                    writable_scratch=(scratch,),
                )
                command = sandbox_test_command(
                    [sys.executable, "-I", "-B", str(candidate / "visible_checks.py")], spec
                )
                result = run_fixed_check(
                    {
                        "test_command": command,
                        "test_cwd": str(candidate),
                        "test_timeout_seconds": 10,
                    }
                )
                if result["truncated"]:
                    result["stdout"] = result["stderr"] = "[OMITTED_TRUNCATED_OUTPUT]"
                result = sanitize_public(result, self.secret_values)
                result["evidence_ref"] = {
                    "check_index": len(self.records) + 1,
                    "candidate_snapshot": snapshot.snapshot_id,
                }
                record = {
                    "check_index": len(self.records) + 1,
                    "candidate_snapshot": snapshot.snapshot_id,
                    "profile_sha256": hashlib.sha256(command[2].encode()).hexdigest(),
                    "result": result,
                }
                self.records.append(record)
                self.evidence.mkdir(parents=True, exist_ok=True)
                with (self.evidence / f"check-{len(self.records)}.json").open("x") as stream:
                    json.dump(record, stream, ensure_ascii=False, sort_keys=True)
                return result
        except (ValueError, OSError):
            return _failure("test_input_or_isolation_invalid")
