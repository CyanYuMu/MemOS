"""The standalone MCP endpoint acknowledges claims without writing trusted evidence."""

import json
import socket
import subprocess
import sys
import tempfile
import threading

from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from memos.skill_adapter.recording_server import run_fixed_check


SCRIPT = Path(__file__).resolve().parents[2] / "src/memos/skill_adapter/recording_server.py"


def run_server(tmp_path, requests, *, context=None):
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            context
            or {
                "run_id": "r1",
                "attempt_id": "a1",
                "skill_revision": "1",
                "rule_ids": ["R1"],
            }
        )
    )
    before = set(tmp_path.iterdir())
    process = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "--context", str(context_path)],
        input="\n".join(json.dumps(request) for request in requests) + "\n",
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert set(tmp_path.iterdir()) == before
    return [json.loads(line) for line in process.stdout.splitlines()]


def request(method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        payload["id"] = request_id
    return payload


def handshake():
    return [
        request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "unit-test", "version": "1"},
            },
        ),
        request("notifications/initialized", request_id=None),
    ]


def declaration(**changes):
    return {
        "rule_id": "R1",
        "phase": "observe",
        "observation": "The test failed",
        "decision": "Gather contract evidence",
        "evidence_refs": [],
        **changes,
    }


def test_initialize_lists_only_public_rule_recording_and_acknowledges_a_claim(tmp_path):
    messages = run_server(
        tmp_path,
        [
            *handshake(),
            request("tools/list", request_id=2),
            request("tools/call", {"name": "record_rule", "arguments": declaration()}, 3),
        ],
    )
    assert messages[0]["result"]["protocolVersion"] == "2025-06-18"
    tools = messages[1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["record_rule"]
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    receipt = json.loads(messages[2]["result"]["content"][0]["text"])
    assert receipt["status"] == "declaration_received"
    assert receipt["execution_verified"] is False
    assert receipt["run_id"] == "r1"
    assert receipt["attempt_id"] == "a1"
    assert receipt["skill_revision"] == "1"


def test_server_rejects_unknown_rule_authority_override_private_fields_and_oversized_claim(
    tmp_path,
):
    invalid = [
        declaration(rule_id="R2"),
        declaration(run_id="foreign"),
        declaration(thinking="private"),
        declaration(observation="x" * 4001),
        declaration(related_tool_events=[5]),
        declaration(phase="reason"),
    ]
    requests = [
        *handshake(),
        *(
            request("tools/call", {"name": "record_rule", "arguments": claim}, index + 2)
            for index, claim in enumerate(invalid)
        ),
    ]
    messages = run_server(tmp_path, requests)
    assert all(message["result"]["isError"] for message in messages[1:])
    assert "foreign" not in json.dumps(messages)
    assert "private" not in json.dumps(messages)


def test_server_does_not_claim_to_validate_cross_run_event_references(tmp_path):
    messages = run_server(
        tmp_path,
        [
            *handshake(),
            request(
                "tools/call",
                {
                    "name": "record_rule",
                    "arguments": declaration(
                        evidence_refs=["foreign:1"], related_tool_events=["foreign:2"]
                    ),
                },
                2,
            ),
        ],
    )
    receipt = json.loads(messages[-1]["result"]["content"][0]["text"])
    assert receipt["evidence_validation"] == "pending_host_validation"
    assert receipt["execution_verified"] is False


def test_protocol_rejects_requests_before_initialization_and_unknown_method(tmp_path):
    messages = run_server(
        tmp_path, [request("tools/list"), *handshake(), request("not/a/method", request_id=3)]
    )
    assert messages[0]["error"]["code"] == -32002
    assert messages[-1]["error"]["code"] == -32601


def _test_context(tmp_path, code, timeout=2):
    return {
        "run_id": "r1",
        "attempt_id": "a1",
        "skill_revision": None,
        "rule_ids": [],
        "test_command": [str(Path(sys.executable).resolve()), "-I", "-B", "-c", code],
        "test_cwd": str(tmp_path),
        "test_timeout_seconds": timeout,
    }


def execute_test(tmp_path, code, timeout=2):
    return run_fixed_check(_test_context(tmp_path, code, timeout))


def test_no_skill_lists_only_fixed_visible_check_tool(tmp_path):
    messages = run_server(
        tmp_path,
        [*handshake(), request("tools/list", request_id=2)],
        context={
            **_test_context(tmp_path, "print('ok')"),
            "test_broker_socket": "/tmp/not-connected-for-list",
        },
    )
    assert [tool["name"] for tool in messages[-1]["result"]["tools"]] == ["run_visible_checks"]
    schema = messages[-1]["result"]["tools"][0]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"] == {}


def test_fixed_visible_check_executes_and_preserves_real_exit_code(tmp_path):
    outcome = execute_test(
        tmp_path, "import sys; print('test failed'); print('detail', file=sys.stderr); sys.exit(1)"
    )
    assert outcome["exit_code"] == 1
    assert outcome["stdout"] == "test failed\n"
    assert outcome["stderr"] == "detail\n"
    assert outcome["timed_out"] is False
    assert outcome["truncated"] is False


def test_agent_cannot_replace_fixed_command_or_arguments(tmp_path):
    messages = run_server(
        tmp_path,
        [
            *handshake(),
            request(
                "tools/call",
                {"name": "run_visible_checks", "arguments": {"command": "anything"}},
                2,
            ),
        ],
        context={
            **_test_context(tmp_path, "print('MUST-NOT-RUN')"),
            "test_broker_socket": "/tmp/must-not-connect",
        },
    )
    assert messages[-1]["result"]["isError"] is True
    assert "MUST-NOT-RUN" not in json.dumps(messages)


def test_fixed_visible_check_timeout_terminates_direct_child(tmp_path):
    outcome = execute_test(tmp_path, "import time; time.sleep(30)", timeout=0.1)
    assert outcome["timed_out"] is True
    assert outcome["exit_code"] != 0


def test_fixed_visible_check_output_is_bounded_and_marked_truncated(tmp_path):
    outcome = execute_test(tmp_path, "import sys; sys.stdout.write('x' * 500000)")
    assert outcome["truncated"] is True
    assert len(outcome["stdout"].encode()) + len(outcome["stderr"].encode()) <= 128 * 1024


def test_fixed_visible_check_does_not_inherit_model_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "PRIVATE-KEY")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "PRIVATE-ENDPOINT")
    outcome = execute_test(
        tmp_path,
        "import os; print(os.environ.get('ANTHROPIC_API_KEY')); print(os.environ.get('ANTHROPIC_BASE_URL'))",
    )
    assert outcome["stdout"] == "None\nNone\n"


@contextmanager
def broker(response):
    observed = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="mcp-broker-", dir="/tmp") as directory:
        path = str(Path(directory).resolve() / "test.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            server.settimeout(4)

            def respond():
                try:
                    connection, _ = server.accept()
                    with connection:
                        line = bytearray()
                        while b"\n" not in line:
                            line.extend(connection.recv(1024))
                        observed.append(json.loads(line))
                        with suppress(BrokenPipeError):
                            connection.sendall(response)
                except OSError as error:
                    errors.append(error)

            worker = threading.Thread(target=respond, daemon=True)
            worker.start()
            yield path, observed
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert not errors


def test_mcp_visible_check_sends_only_the_fixed_action_to_host_broker(tmp_path):
    outcome = {
        "exit_code": 0,
        "stdout": "checks passed\n",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": None,
    }
    with broker(json.dumps(outcome).encode() + b"\n") as (path, observed):
        messages = run_server(
            tmp_path,
            [
                *handshake(),
                request("tools/call", {"name": "run_visible_checks", "arguments": {}}, 2),
            ],
            context={
                "run_id": "r1",
                "attempt_id": "a1",
                "rule_ids": [],
                "skill_revision": None,
                "test_broker_socket": path,
            },
        )
    assert observed == [{"action": "run_visible_checks"}]
    reply = messages[-1]["result"]
    assert reply["isError"] is False
    assert json.loads(reply["content"][0]["text"]) == outcome


@pytest.mark.parametrize("response", [b"{}\n", b"x" * (512 * 1024 + 1) + b"\n"])
def test_mcp_rejects_malformed_or_oversized_broker_responses(tmp_path, response):
    with broker(response) as (path, _):
        messages = run_server(
            tmp_path,
            [
                *handshake(),
                request("tools/call", {"name": "run_visible_checks", "arguments": {}}, 2),
            ],
            context={
                "run_id": "r1",
                "attempt_id": "a1",
                "rule_ids": [],
                "test_broker_socket": path,
            },
        )
    assert messages[-1]["result"]["isError"] is True
    outcome = json.loads(messages[-1]["result"]["content"][0]["text"])
    assert outcome["execution_error"] == "test_broker_failed"


def test_host_executor_bounds_text_size_for_non_utf8_output(tmp_path):
    outcome = execute_test(tmp_path, "import os; os.write(1, b'\\xff' * 100000)")
    assert outcome["truncated"] is True
    assert len(outcome["stdout"].encode()) <= 128 * 1024


@pytest.mark.parametrize(
    "evidence_ref",
    [
        {"check_index": 1, "candidate_snapshot": "a" * 64},
        {"check_index": True, "candidate_snapshot": "a" * 64},
        {"check_index": 1, "candidate_snapshot": []},
        {"check_index": 1, "candidate_snapshot": "a" * 64, "extra": "forbidden"},
    ],
)
def test_mcp_accepts_only_well_formed_host_evidence_reference(tmp_path, evidence_ref):
    outcome = {
        "exit_code": 0,
        "stdout": "done",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": None,
        "evidence_ref": evidence_ref,
    }
    with broker(json.dumps(outcome).encode() + b"\n") as (path, _):
        messages = run_server(
            tmp_path,
            [
                *handshake(),
                request("tools/call", {"name": "run_visible_checks", "arguments": {}}, 2),
            ],
            context={
                "run_id": "r1",
                "attempt_id": "a1",
                "rule_ids": [],
                "test_broker_socket": path,
            },
        )
    valid = (
        type(evidence_ref["check_index"]) is int
        and type(evidence_ref["candidate_snapshot"]) is str
        and set(evidence_ref) == {"check_index", "candidate_snapshot"}
    )
    assert messages[-1]["result"]["isError"] is not valid
    if valid:
        assert (
            json.loads(messages[-1]["result"]["content"][0]["text"])["evidence_ref"] == evidence_ref
        )
