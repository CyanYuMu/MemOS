"""Offline G1 transport proof: host state changes precede externally visible grants."""

import json
import os
import socket
import threading
import time

from pathlib import Path

import pytest

from memos.skill_release_v2.gate import HostGate, RunIdentity, VerifiedResult, digest_json
from memos.skill_release_v2.transport import HostTransport, TransportError


IDENTITY = RunIdentity("run", "attempt", "session")


def hook(tool_id="read", name="Read", value=None, event="PreToolUse", **extra):
    if name in {"mcp__memos_v2__record_rule", "mcp__memos_v2__run_visible_checks"}:
        extra.setdefault("mcp_server", {"name": "memos_v2", "source": "dynamic"})
    return {
        "session_id": "session",
        "cwd": str(extra.pop("workspace")),
        "hook_event_name": event,
        "tool_use_id": tool_id,
        "tool_name": name,
        "tool_input": value if value is not None else {"file_path": "TASK.md"},
        **extra,
    }


@pytest.fixture
def host(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "TASK.md").write_text("Repair caller.\n")
    (workspace / "caller.py").write_text("wrong()\n")
    gate = HostGate(
        workspace=workspace,
        identity=IDENTITY,
        rules={"entry": ("repair",), "repair": ("exit:success",)},
        entry_rule="entry",
        allowed_paths=("caller.py",),
        request_path="TASK.md",
        verify_result=lambda request, payload: VerifiedResult(
            request.digest, digest_json(payload["result"]), payload["is_error"]
        ),
    )
    # Unix sockaddr paths on macOS have a small platform limit.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="mv2-") as directory:
        result = HostTransport(
            gate,
            IDENTITY,
            directory=Path(directory),
            check_runner=lambda request: ({"status": "failed", "failed": 1}, False),
            completion_wait=0.05,
        )
        yield result
        result.close()


def pre(host, tool_id="read", name="Read", value=None):
    return host.handle_hook(hook(tool_id, name, value, workspace=host.gate.workspace))


def post(host, tool_id="read", name="Read", value=None, **extra):
    return host.handle_hook(
        hook(tool_id, name, value, "PostToolUse", workspace=host.gate.workspace, **extra)
    )


def finish(host, tool_id, result=None, is_error=False):
    return host.complete_native(tool_id, {"result": result or {}, "is_error": is_error})


def declare(host, tool_id="declaration"):
    return pre(
        host,
        tool_id,
        "mcp__memos_v2__record_rule",
        {
            "rule_id": "entry",
            "phase": "select",
            "branch_index": 0,
            "observation": "Observed.",
            "decision": "Repair.",
            "evidence_refs": ["unknown"],
        },
    )


def test_pre_is_durable_and_post_is_pending_until_native_completion(host):
    assert pre(host)["hookSpecificOutput"]["permissionDecision"] == "allow"
    material = host.delivery_material("read")
    assert material["full_name"] == "Read"
    assert material["file_before"] == b"Repair caller.\n"
    rows = [json.loads(row) for row in host.journal_path.read_text().splitlines()]
    assert any(row["event"] == "pre_allowed" for row in rows)
    ack = post(host, tool_response={"content": "Repair caller.\n"})
    assert ack["hookSpecificOutput"]["additionalContext"] == (
        "observation:" + material["request"].digest
    )
    assert not host.gate.state().observations
    finish(host, "read")
    assert len(host.gate.state().observations) == 1


def test_fixed_name_mapping_and_no_host_completion_socket_operation(host):
    denied = pre(host, "alias", "evil__run_visible_checks", {})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    config = host.credentials("mcp")
    message = {
        **{k: config[k] for k in ("role", "token", "run_id", "attempt_id", "session_id")},
        "nonce": "fresh",
        "operation": "complete_native",
        "payload": {},
    }
    with pytest.raises(TransportError, match="operation_forbidden"):
        host.dispatch(message)


def test_mcp_requires_one_pending_matching_native_request_and_is_one_shot(host):
    with pytest.raises(TransportError, match="native_request_missing"):
        host.handle_mcp("run_visible_checks", {})
    assert (
        pre(host, "check", "mcp__memos_v2__run_visible_checks", {})["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "allow"
    )
    body, is_error = host.handle_mcp("run_visible_checks", {})
    assert body["status"] == "failed" and is_error is False
    assert body["observation_ref"].startswith("observation:")
    assert host.delivery_material("check")["host_result"] == body
    with pytest.raises(TransportError, match="native_request_missing"):
        host.handle_mcp("run_visible_checks", {})


@pytest.mark.parametrize("body", [{"status": "error"}, {"execution_error": "no executable"}])
def test_unexecutable_check_cannot_be_claimed_as_non_error(host, body):
    host.check_runner = lambda request: (body, False)
    pre(host, "check", "mcp__memos_v2__run_visible_checks", {})
    result, is_error = host.handle_mcp("run_visible_checks", {})
    assert is_error is True
    assert result["status"] == "error"


def test_next_pre_waits_without_holding_state_lock(host):
    pre(host)
    post(host)
    result = []
    waiter = threading.Thread(target=lambda: result.append(declare(host)))
    waiter.start()
    time.sleep(0.01)
    finish(host, "read")
    waiter.join(1)
    assert not waiter.is_alive()
    assert result[0]["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_pending_previous_call_cannot_authorize_next_pre(host):
    pre(host)
    post(host)
    result = declare(host)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "previous_completion_timeout" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_parallel_reads_and_check_do_not_wait_for_read_stdout(host):
    assert pre(host)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert pre(host, "read2")["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        pre(host, "check", "mcp__memos_v2__run_visible_checks", {})["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "allow"
    )


def test_failed_declaration_is_verified_and_aborted_without_commit(host):
    declare(host)
    material = host.delivery_material("declaration")
    result, is_error = host.handle_mcp("record_rule", material["request"].input)
    assert is_error is True
    post(
        host,
        "declaration",
        "mcp__memos_v2__record_rule",
        material["request"].input,
        tool_response=result,
    )
    verified = []

    def verifier(request, envelope, retained):
        verified.append(retained["host_result"])
        return VerifiedResult(request.digest, digest_json(result), True)

    host.verify_result = verifier
    assert finish(host, "declaration", result, is_error=True) is None
    assert verified == [result]
    assert not host.gate.state().committed_declarations
    assert not host.closed


@pytest.mark.parametrize("changed", [{"session_id": "other"}, {"cwd": "/"}, {"tool_use_id": True}])
def test_hook_identity_and_shape_fail_closed(host, changed):
    value = hook(workspace=host.gate.workspace)
    value.update(changed)
    result = host.handle_hook(value)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not host.gate.state().observations


def test_post_must_match_pre_and_is_not_a_completion_authority(host):
    pre(host)
    result = post(host, value={"file_path": "caller.py"})
    assert result["continue"] is False
    assert host.gate.state().invalidated


def test_fsync_failure_revokes_admission(host, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    result = pre(host)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert host.gate.state().invalidated
    assert host.closed


def test_role_tokens_nonce_and_journal_chain(host):
    config = host.credentials("hook")
    assert config["token"] != host.credentials("mcp")["token"]
    message = {
        **{k: config[k] for k in ("role", "token", "run_id", "attempt_id", "session_id")},
        "nonce": "once",
        "operation": "hook",
        "payload": hook(workspace=host.gate.workspace),
    }
    assert host.dispatch(message)["hookSpecificOutput"]["permissionDecision"] == "allow"
    with pytest.raises(TransportError, match="nonce_replayed"):
        host.dispatch(message)
    message["nonce"] = "new"
    message["role"] = "mcp"
    with pytest.raises(TransportError, match="authentication_failed"):
        host.dispatch(message)
    rows = [json.loads(row) for row in host.journal_path.read_text().splitlines()]
    previous = "0" * 64
    for index, row in enumerate(rows, 1):
        digest = row.pop("digest")
        assert row["sequence"] == index
        assert row["previous_hash"] == previous
        assert digest == digest_json(row)
        previous = digest
    assert config["token"] not in host.journal_path.read_text()


def test_socket_permissions_and_real_request(host):
    host.start()
    assert host.directory.stat().st_mode & 0o777 == 0o700
    assert host.socket_path.stat().st_mode & 0o777 == 0o600
    config = host.credentials("hook")
    message = {
        **{k: config[k] for k in ("role", "token", "run_id", "attempt_id", "session_id")},
        "nonce": "socket-once",
        "operation": "hook",
        "payload": hook(workspace=host.gate.workspace),
    }
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(host.socket_path))
        client.sendall(json.dumps(message).encode() + b"\n")
        response = json.loads(client.makefile("rb").readline())
    assert response["ok"] is True
    assert response["result"]["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_edit_permit_is_consumed_and_fsynced_before_allow(host):
    pre(host)
    post(host)
    read = finish(host, "read")
    pre(host, "check", "mcp__memos_v2__run_visible_checks", {})
    check_result, _ = host.handle_mcp("run_visible_checks", {})
    post(host, "check", "mcp__memos_v2__run_visible_checks", {}, tool_response=check_result)
    check = finish(host, "check", check_result)
    for tool_id, rule, phase, branch in (
        ("entry", "entry", "select", 0),
        ("target", "repair", "select", None),
        ("act", "repair", "act", None),
    ):
        value = {
            "rule_id": rule,
            "phase": phase,
            "branch_index": branch,
            "observation": "Current evidence.",
            "decision": "Apply repair.",
            "evidence_refs": [read.reference, check.reference],
        }
        assert (
            pre(host, tool_id, "mcp__memos_v2__record_rule", value)["hookSpecificOutput"][
                "permissionDecision"
            ]
            == "allow"
        )
        result, is_error = host.handle_mcp("record_rule", value)
        assert is_error is False and result["status"] == "pending"
        assert tool_id not in host.gate.state().committed_declarations
        post(host, tool_id, "mcp__memos_v2__record_rule", value, tool_response=result)
        finish(host, tool_id, result)
    value = {"file_path": "caller.py", "old_string": "wrong()", "new_string": "right()"}
    assert pre(host, "edit", "Edit", value)["hookSpecificOutput"]["permissionDecision"] == "allow"
    rows = [json.loads(line) for line in host.journal_path.read_text().splitlines()]
    consume_index = next(
        index
        for index, row in enumerate(rows)
        if row["event"] == "gate" and row["entry"]["event"] == "consumed"
    )
    allow_index = next(
        index
        for index, row in enumerate(rows)
        if row["event"] == "pre_allowed" and row["tool_use_id"] == "edit"
    )
    assert consume_index < allow_index
    assert (host.gate.workspace / "caller.py").read_text() == "wrong()\n"


def test_denied_write_retains_real_file_before_and_after(host):
    value = {"file_path": "caller.py", "content": "wrong replacement"}
    assert pre(host, "write", "Write", value)["hookSpecificOutput"]["permissionDecision"] == "deny"
    material = host.delivery_material("write")
    assert material["file_before"] == material["file_after"] == b"wrong()\n"
    (host.gate.workspace / "caller.py").write_text("unauthorized\n")
    assert host.delivery_material("write")["file_after"] != material["file_before"]


def test_check_timeout_is_retained_as_real_error_without_blocking_post(host):
    stopped = threading.Event()
    host.check_timeout = 0.02
    host.check_runner = lambda request: (stopped.wait(0.2), False)
    pre(host, "check", "mcp__memos_v2__run_visible_checks", {})
    started = time.monotonic()
    result, is_error = host.handle_mcp("run_visible_checks", {})
    assert time.monotonic() - started < 0.1
    assert is_error is True and result["execution_error"] == "check_timeout"
    assert post(host, "check", "mcp__memos_v2__run_visible_checks", {}, tool_response=result) == {}
    stopped.set()


def test_socket_rejects_duplicate_json_keys_without_executing(host):
    host.start()
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(host.socket_path))
        client.sendall(b'{"role":"hook","role":"mcp"}\n')
        response = json.loads(client.makefile("rb").readline())
    assert response == {"ok": False, "error": "duplicate_json_key"}
    assert not host.gate.audit()


def test_close_does_not_wait_forever_for_a_blocked_state_lock(host):
    held = threading.Event()
    release = threading.Event()

    def holder():
        with host._condition:
            held.set()
            release.wait(2)

    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(1)
    started = time.monotonic()
    try:
        assert host.close(timeout=0.03) is False
        assert time.monotonic() - started < 0.15
        assert host.closed
    finally:
        release.set()
        worker.join(1)
    assert host.close(timeout=0.03) is True


def test_fixed_cli_common_hook_metadata_and_post_duration_are_accepted(host):
    metadata = {
        "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
        "transcript_path": "/private/session/transcript.jsonl",
        "scratchpad_dir": "/private/session/scratchpad",
        "permission_mode": "dontAsk",
        "effort": {"level": "low"},
    }
    payload = hook(workspace=host.gate.workspace, **metadata)
    assert host.handle_hook(payload)["hookSpecificOutput"]["permissionDecision"] == "allow"
    response = host.handle_hook(
        {
            **payload,
            "hook_event_name": "PostToolUse",
            "duration_ms": 1.5,
            "tool_response": {"content": "Repair caller.\n"},
        }
    )
    assert "additionalContext" in response["hookSpecificOutput"]
    retained = host.delivery_material("read")
    assert retained["pre_hook"]["prompt_id"] == metadata["prompt_id"]
    assert retained["post_hook"]["duration_ms"] == 1.5


@pytest.mark.parametrize(
    "metadata",
    [
        {"prompt_id": None},
        {"prompt_id": 1},
        {"prompt_id": "not-a-uuid"},
        {"prompt_id": "550e8400e29b41d4a716446655440000"},
        {"scratchpad_dir": None},
        {"scratchpad_dir": True},
        {"scratchpad_dir": "relative/path"},
        {"scratchpad_dir": "/private/../escape"},
        {"scratchpad_dir": "/" + "x" * 4096},
        {"transcript_path": []},
        {"transcript_path": "relative.jsonl"},
        {"effort": None},
        {"effort": "low"},
        {"effort": {}},
        {"effort": {"level": True}},
        {"effort": {"level": "medium"}},
        {"effort": {"level": "high"}},
        {"effort": {"level": "low", "other": 1}},
        {"agent_id": "child"},
        {"agent_id": None},
        {"agent_type": "main"},
        {"permission_mode": None},
        {"permission_mode": "manual"},
        {"duration_ms": 1},
        {"mcp_server": {"name": "memos_v2", "source": "dynamic"}},
        {"unknown_runtime_key": "private metadata value"},
    ],
)
def test_invalid_or_unbound_metadata_is_denied_with_value_free_diagnostics(host, metadata):
    payload = hook(workspace=host.gate.workspace, **metadata)
    result = host.handle_hook(payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    rows = [json.loads(line) for line in host.journal_path.read_text().splitlines()]
    diagnostic = next(row for row in rows if row["event"] == "hook_rejected_shape")
    assert diagnostic["payload_sha256"] == digest_json(payload)
    assert "payload" not in diagnostic and "pre_hook" not in diagnostic
    assert "private metadata value" not in json.dumps(diagnostic)
    assert not host.gate.audit()


@pytest.mark.parametrize("duration", [None, True, -1, "1", [], float("inf"), float("nan")])
def test_post_duration_requires_finite_nonnegative_number(host, duration):
    pre(host)
    result = post(host, tool_response={}, duration_ms=duration)
    assert result["continue"] is False
    assert host.gate.state().invalidated


def test_mcp_provenance_must_match_the_fixed_dynamic_stdio_source(host):
    payload = hook(
        "check",
        "mcp__memos_v2__run_visible_checks",
        {},
        workspace=host.gate.workspace,
        mcp_server={"name": "memos_v2", "source": "dynamic"},
    )
    result = host.handle_hook(payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize(
    "provenance",
    [
        None,
        {},
        {"name": "other", "source": "dynamic"},
        {"name": "memos_v2", "source": "sdk"},
        {"name": "memos_v2", "source": "plugin"},
        {"name": "memos_v2", "source": "project"},
        {"name": "memos_v2", "source": "dynamic", "extra": True},
    ],
)
def test_mcp_other_provenance_never_registers_native_request(host, provenance):
    payload = hook(
        "check",
        "mcp__memos_v2__run_visible_checks",
        {},
        workspace=host.gate.workspace,
        mcp_server=provenance,
    )
    result = host.handle_hook(payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not host.gate.audit()


def test_fixed_cli_mcp_requires_provenance_field(host):
    payload = hook("check", "mcp__memos_v2__run_visible_checks", {}, workspace=host.gate.workspace)
    payload.pop("mcp_server", None)
    assert host.handle_hook(payload)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not host.gate.audit()


def test_rejected_hook_diagnostics_are_bounded_and_exclude_values(host):
    private = "private diagnostic sentinel"
    payload = hook(workspace=host.gate.workspace)
    payload.update({f"extra_{i}": private for i in range(200)})
    payload[private + "\n" + "x" * 200] = private
    result = host.handle_hook(payload)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    row = json.loads(host.journal_path.read_text().splitlines()[-1])
    assert row["event"] == "hook_rejected_shape"
    assert row["field_count"] == len(payload)
    assert len(row["fields"]) <= 64 and row["fields_truncated"] is True
    assert row["payload_sha256"] == digest_json(payload)
    assert private not in json.dumps(row)


def test_rejected_unauthenticated_socket_input_has_no_hook_diagnostic(host):
    config = host.credentials("hook")
    message = {
        **{k: config[k] for k in ("role", "token", "run_id", "attempt_id", "session_id")},
        "nonce": "unauthenticated",
        "operation": "hook",
        "payload": hook(workspace=host.gate.workspace, unknown="secret"),
    }
    message["token"] = "wrong"
    with pytest.raises(TransportError, match="authentication_failed"):
        host.dispatch(message)
    assert [json.loads(line)["event"] for line in host.journal_path.read_text().splitlines()] == [
        "opened"
    ]


@pytest.mark.parametrize("duration", [0, 0.0, 10**400])
def test_finite_nonnegative_duration_does_not_require_float_coercion(host, duration):
    pre(host)
    result = post(host, tool_response={}, duration_ms=duration)
    assert "additionalContext" in result["hookSpecificOutput"]


def test_diagnostic_write_failure_revokes_admission(host, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    result = host.handle_hook(hook(workspace=host.gate.workspace, unexpected="private"))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert host.closed and host.gate.state().invalidated
