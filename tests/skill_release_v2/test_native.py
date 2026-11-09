"""Native protocol fixtures never contain private experiment material."""

import copy
import json

import pytest

from memos.skill_release_v2.gate import NativeRequest, RunIdentity, digest_json
from memos.skill_release_v2.native import (
    NativeCaptureError,
    NativeStream,
    verify_denied,
    verify_native,
)


IDENTITY = RunIdentity("run", "attempt", "session")
TOOLS = ("Read", "Edit", "Write", "mcp__memos_v2__record_rule", "mcp__memos_v2__run_visible_checks")
MODEL = "claude-opus-5-5"


def fixture(tmp_path, tool="Read", *, error=False):
    path = tmp_path / "TASK.md"
    path.write_text("First line\nSecond line\n")
    value = {"file_path": str(path)} if tool in {"Read", "Edit", "Write"} else {}
    if tool == "Edit":
        value.update(old_string="First", new_string="Changed", replace_all=False)
    if tool == "Write":
        value["content"] = "Replacement\n"
    full = tool if tool in {"Read", "Edit", "Write"} else "mcp__memos_v2__" + tool
    request = NativeRequest(
        IDENTITY,
        "tool-1",
        tool,
        json.dumps(value),
        digest_json(value),
        "TASK.md" if "file_path" in value else None,
        "a" * 64,
        "b" * 64,
    )
    pre = {
        "hook_event_name": "PreToolUse",
        "session_id": "session",
        "cwd": str(tmp_path),
        "tool_use_id": "tool-1",
        "tool_name": full,
        "tool_input": value,
    }
    post = dict(pre, hook_event_name="PostToolUse")
    before = path.read_bytes()
    after = before
    host = None
    if tool == "Read":
        structured = {
            "type": "text",
            "file": {
                "filePath": str(path),
                "content": before.decode(),
                "numLines": 3,
                "startLine": 1,
                "totalLines": 3,
            },
        }
        content = "1\tFirst line\n2\tSecond line\n3\t"
    elif tool == "Edit":
        after = before.replace(b"First", b"Changed")
        structured = {
            "filePath": str(path),
            "oldString": "First",
            "newString": "Changed",
            "originalFile": before.decode(),
            "replaceAll": False,
            "userModified": False,
            "structuredPatch": [],
        }
        content = f"The file {path} has been updated successfully. (file state is current in your context — no need to Read it back)"
    elif tool == "Write":
        after = value["content"].encode()
        structured = {
            "type": "update",
            "filePath": str(path),
            "content": value["content"],
            "structuredPatch": [],
            "originalFile": before.decode(),
        }
        content = f"The file {path} has been updated successfully. (file state is current in your context — no need to Read it back)"
    else:
        host = (
            {"status": "error", "execution_error": "unavailable"}
            if error
            else {"status": "failed", "exit_code": 1}
        )
        content = json.dumps(host) if error else [{"type": "text", "text": json.dumps(host)}]
        structured = {"content": [{"type": "text", "text": json.dumps(host)}], "isError": error}
    post["tool_response"] = structured
    native = {
        "session_id": "session",
        "tool_use_id": "tool-1",
        "is_error": error,
        "tool_result": content,
        "structured_result": structured,
    }
    material = {
        "request": request,
        "identity": IDENTITY,
        "full_name": full,
        "pre_hook": pre,
        "post_hook": post,
        "host_result": host,
        "host_is_error": error if host else None,
        "file_before": before if request.path else None,
        "file_after": after if request.path else None,
        "workspace": tmp_path,
        "denied": None,
    }
    return request, native, material


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "record_rule", "run_visible_checks"])
def test_three_way_matching_results_are_bound_to_request(tmp_path, tool):
    request, native, material = fixture(tmp_path, tool)
    verified = verify_native(request, native, material)
    assert verified.request_digest == request.digest
    assert verified.is_error is False
    assert len(verified.result_sha256) == 64


@pytest.mark.parametrize("string", [False, True])
def test_actual_mcp_errors_preserve_error_identity(tmp_path, string):
    request, native, material = fixture(tmp_path, "run_visible_checks", error=True)
    if not string:
        native["tool_result"] = [{"type": "text", "text": native["tool_result"]}]
    assert verify_native(request, native, material).is_error is True


@pytest.mark.parametrize(
    "change",
    [
        lambda n, m: n.update(session_id="other"),
        lambda n, m: n.update(tool_use_id="other"),
        lambda n, m: n.update(is_error=0),
        lambda n, m: m.update(post_hook=None),
        lambda n, m: m["post_hook"].update(session_id="other"),
        lambda n, m: m["pre_hook"]["tool_input"].update(offset=2),
        lambda n, m: m.update(identity=RunIdentity("other", "attempt", "session")),
        lambda n, m: m.update(full_name="mcp__evil__Read"),
        lambda n, m: m["post_hook"].update(cwd="/elsewhere"),
        lambda n, m: m["post_hook"].update(tool_use_id="other"),
    ],
)
def test_identity_hook_input_and_native_mismatches_are_rejected(tmp_path, change):
    request, native, material = fixture(tmp_path)
    change(native, material)
    with pytest.raises(NativeCaptureError):
        verify_native(request, native, material)


@pytest.mark.parametrize(
    "change",
    [
        lambda n, m: n.update(tool_result="1\tFirst line"),
        lambda n, m: n.update(tool_result="1\tFirst line\n2\tFORGED"),
        lambda n, m: n["structured_result"]["file"].update(content="First line\n"),
        lambda n, m: n["structured_result"]["file"].update(numLines=1),
        lambda n, m: n["structured_result"]["file"].update(totalLines=1),
        lambda n, m: n["structured_result"]["file"].update(startLine=2),
        lambda n, m: m.update(file_after=b"Changed\n"),
        lambda n, m: n.update(tool_result="1\tFirst line\n2\tSecond line\n[truncated]"),
    ],
)
def test_read_checks_delivered_text_and_file_not_only_metadata(tmp_path, change):
    request, native, material = fixture(tmp_path)
    change(native, material)
    with pytest.raises(NativeCaptureError):
        verify_native(request, native, material)


@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize(
    "change",
    [
        lambda n, m: m.update(file_after=m["file_before"]),
        lambda n, m: n.update(tool_result="Success"),
        lambda n, m: n["structured_result"].update(filePath="/wrong"),
        lambda n, m: n["structured_result"].update(originalFile="forged"),
    ],
)
def test_write_success_requires_exact_requested_change_and_native_result(tmp_path, tool, change):
    request, native, material = fixture(tmp_path, tool)
    change(native, material)
    with pytest.raises(NativeCaptureError):
        verify_native(request, native, material)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "error"),
        ("execution_error", "cannot_execute"),
        ("timed_out", True),
        ("exit_code", None),
    ],
)
def test_check_execution_error_cannot_be_reported_as_nonerror(tmp_path, field, value):
    request, native, material = fixture(tmp_path, "run_visible_checks")
    host = dict(material["host_result"], **{field: value})
    material["host_result"] = host
    content = [{"type": "text", "text": json.dumps(host)}]
    native["tool_result"] = content
    native["structured_result"]["content"] = content
    with pytest.raises(NativeCaptureError, match="check_error_identity"):
        verify_native(request, native, material)


def test_mcp_hook_native_and_host_must_all_match(tmp_path):
    request, native, material = fixture(tmp_path, "run_visible_checks")
    material["post_hook"] = copy.deepcopy(material["post_hook"])
    material["post_hook"]["tool_response"]["content"][0]["text"] = '{"status":"passed"}'
    with pytest.raises(NativeCaptureError):
        verify_native(request, native, material)


def test_trusted_pre_denial_closes_without_postfailure(tmp_path):
    _, native, material = fixture(tmp_path, "Edit")
    material.update(
        request=None,
        denied="declaration_required",
        post_hook=None,
        file_after=material["file_before"],
    )
    native.update(
        is_error=True,
        tool_result="declaration_required",
        structured_result="Error: declaration_required",
    )
    verify_denied(native, material)
    native["is_error"] = False
    with pytest.raises(NativeCaptureError):
        verify_denied(native, material)


def stream(tmp_path):
    request, native, material = fixture(tmp_path)
    calls = []
    materials = {}

    def complete(tool_id, result):
        calls.append(verify_native(request, result, materials[tool_id]))

    consumer = NativeStream(
        identity=IDENTITY,
        expected_model=MODEL,
        expected_tools=TOOLS,
        material=materials.__getitem__,
        complete=complete,
    )
    init = {
        "type": "system",
        "subtype": "init",
        "session_id": "session",
        "model": MODEL,
        "tools": list(TOOLS),
        "claude_code_version": "2.1.296",
        "permissionMode": "dontAsk",
        "mcp_servers": [{"name": "memos_v2", "source": "dynamic", "status": "connected"}],
        "skills": ["doctor", "plugin-authoring"],
        "plugins": [
            {"name": name, "path": "builtin", "source": name + "@builtin"}
            for name in ("cc-plugin-agents-md", "cc-plugin-plugin-authoring")
        ],
    }
    start = {
        "type": "assistant",
        "session_id": "session",
        "message": {
            "model": MODEL,
            "content": [
                {"type": "tool_use", "id": "tool-1", "name": "Read", "input": request.input}
            ],
        },
    }
    result = {
        "type": "user",
        "session_id": "session",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": native["tool_result"]}
            ]
        },
        "tool_use_result": native["structured_result"],
    }
    end = {
        "type": "result",
        "subtype": "success",
        "session_id": "session",
        "is_error": False,
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "modelUsage": {MODEL: {"inputTokens": 1, "outputTokens": 2, "costUSD": 0.01}},
    }
    return consumer, materials, material, calls, init, start, result, end


def feed(consumer, *rows):
    for row in rows:
        consumer.feed(json.dumps(row) + "\n")


def test_start_can_arrive_before_pre_and_closes_only_after_native_result(tmp_path):
    consumer, materials, material, calls, init, start, result, end = stream(tmp_path)
    feed(consumer, init, start)
    assert calls == []
    materials["tool-1"] = material
    feed(consumer, result, end)
    summary = consumer.finish()
    assert summary["complete"] is True
    assert summary["completed_tools"] == 1
    assert summary["total_cost_usd"] == 0.01
    assert "First line" not in str(summary)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "case",
    [
        "wrong_model",
        "wrong_tools",
        "wrong_session",
        "unknown_tool",
        "input_mismatch",
        "duplicate_start",
        "duplicate_result",
        "unknown_id",
        "truncated",
        "duplicate_json",
        "result_missing",
        "pending",
        "unknown_event",
        "wrong_role",
        "wrong_result_model",
    ],
)
def test_stream_rejects_unbound_duplicate_truncated_or_unknown_events(tmp_path, case):
    consumer, materials, material, calls, init, start, result, end = stream(tmp_path)
    materials["tool-1"] = material
    rows = [init, start, result, end]
    if case == "wrong_model":
        init["model"] = "other"
    if case == "wrong_tools":
        init["tools"].append("Bash")
    if case == "wrong_session":
        result["session_id"] = "other"
    if case == "unknown_tool":
        start["message"]["content"][0]["name"] = "mcp__other__Read"
    if case == "input_mismatch":
        start["message"]["content"][0]["input"]["limit"] = 1
    if case == "duplicate_start":
        rows.insert(2, copy.deepcopy(start))
    if case == "duplicate_result":
        rows.insert(3, copy.deepcopy(result))
    if case == "unknown_id":
        result["message"]["content"][0]["tool_use_id"] = "unknown"
    if case == "result_missing":
        rows.pop()
    if case == "pending":
        rows.remove(result)
    if case == "unknown_event":
        rows.insert(1, {"type": "mystery", "session_id": "session"})
    if case == "wrong_role":
        start["type"] = "user"
    if case == "wrong_result_model":
        end["modelUsage"] = {"other": {}}
    with pytest.raises(NativeCaptureError):
        if case == "truncated":
            consumer.feed('{"type":"system"')
        elif case == "duplicate_json":
            consumer.feed('{"type":"system","type":"user"}\n')
        else:
            feed(consumer, *rows)
            consumer.finish()
    assert consumer.summary()["complete"] is False


def test_raw_size_errors_are_bounded_and_do_not_echo_payload(tmp_path):
    consumer, *_ = stream(tmp_path)
    with pytest.raises(NativeCaptureError) as err:
        consumer.feed("PRIVATE_SECRET" * 100000)
    assert "PRIVATE_SECRET" not in str(err.value)


def test_pinned_mcp_success_uses_raw_content_array_for_hook_and_sidecar(tmp_path):
    request, native, material = fixture(tmp_path, "run_visible_checks")
    native["structured_result"] = copy.deepcopy(native["tool_result"])
    material["post_hook"]["tool_response"] = copy.deepcopy(native["tool_result"])
    assert verify_native(request, native, material).is_error is False


def test_pinned_mcp_failure_uses_hook_error_and_error_prefixed_sidecar(tmp_path):
    request, native, material = fixture(tmp_path, "run_visible_checks", error=True)
    material["post_hook"].update(hook_event_name="PostToolUseFailure", error=native["tool_result"])
    del material["post_hook"]["tool_response"]
    native["structured_result"] = "Error: " + native["tool_result"]
    assert verify_native(request, native, material).is_error is True


def test_forged_error_sidecar_is_rejected(tmp_path):
    request, native, material = fixture(tmp_path, "run_visible_checks", error=True)
    material["post_hook"].update(hook_event_name="PostToolUseFailure", error=native["tool_result"])
    del material["post_hook"]["tool_response"]
    native["structured_result"] = 'Error: {"status":"passed"}'
    with pytest.raises(NativeCaptureError):
        verify_native(request, native, material)


def test_partial_read_is_verified_as_partial_not_misrepresented_full_file(tmp_path):
    request, native, material = fixture(tmp_path)
    from dataclasses import replace

    value = dict(request.input, offset=2, limit=1)
    request = replace(request, input_json=json.dumps(value), input_sha256=digest_json(value))
    material["request"] = request
    material["pre_hook"]["tool_input"] = value
    material["post_hook"]["tool_input"] = value
    native["structured_result"]["file"].update(content="Second line", startLine=2, numLines=1)
    native["tool_result"] = "2\tSecond line"
    assert verify_native(request, native, material).is_error is False


def test_auto_truncated_read_never_produces_request_evidence(tmp_path):
    request, native, material = fixture(tmp_path)
    native["structured_result"]["file"]["truncatedByTokenCap"] = True
    with pytest.raises(NativeCaptureError, match="read_truncated"):
        verify_native(request, native, material)


@pytest.mark.parametrize(
    "subtype,is_error",
    [("success", True), ("error_during_execution", False), ("PRIVATE_SECRET", False)],
)
def test_final_result_subtype_cannot_conflict_with_error_or_leak_arbitrary_text(
    tmp_path, subtype, is_error
):
    consumer, materials, material, _, init, start, result, end = stream(tmp_path)
    materials["tool-1"] = material
    end.update(subtype=subtype, is_error=is_error)
    with pytest.raises(NativeCaptureError, match="native_final_shape"):
        feed(consumer, init, start, result, end)
    assert "PRIVATE_SECRET" not in str(consumer.summary())


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.update(claude_code_version="2.1.295"),
        lambda row: row.update(permissionMode="bypassPermissions"),
        lambda row: row["mcp_servers"][0].update(status="disconnected"),
        lambda row: row["mcp_servers"][0].update(name="memos_run"),
        lambda row: row["mcp_servers"].append({"name": "other", "status": "connected"}),
        lambda row: row["skills"].append("old-skill"),
        lambda row: row["skills"].append("doctor"),
        lambda row: row.update(skills=[]),
        lambda row: row["plugins"][0].update(path="/private/plugin"),
        lambda row: row["plugins"][0].update(source="external"),
        lambda row: row["plugins"].append(row["plugins"][0]),
        lambda row: row.update(plugins=[]),
    ],
)
def test_fixed_probe_init_context_rejects_unregistered_sources(tmp_path, change):
    consumer, _, _, _, init, *_ = stream(tmp_path)
    change(init)
    with pytest.raises(NativeCaptureError, match="native_context_mismatch"):
        feed(consumer, init)


@pytest.mark.parametrize(
    "content,sidecar",
    [
        ("Error reading file: EIO", {"status": "success"}),
        ("declaration_required", {"status": "success"}),
        ("declaration_required", None),
        ("declaration_required", "Error: protected_path"),
        ("protected_path", "Error: protected_path"),
        ("declaration_required trailing", "Error: declaration_required trailing"),
        (
            "Permission to use Edit has been denied.",
            "Error: Permission to use Edit has been denied.",
        ),
        ("<tool_use_error>declaration_required</tool_use_error>", "Error: declaration_required"),
    ],
)
def test_pre_denial_requires_pinned_refusal_reason_and_exact_error_sidecar(
    tmp_path, content, sidecar
):
    _, native, material = fixture(tmp_path, "Edit")
    material.update(
        request=None,
        denied="declaration_required",
        post_hook=None,
        file_after=material["file_before"],
    )
    native.update(is_error=True, tool_result=content, structured_result=sidecar)
    with pytest.raises(NativeCaptureError, match="denial_result_mismatch"):
        verify_denied(native, material)


def test_pre_denial_cannot_include_successful_post_tool_return(tmp_path):
    _, native, material = fixture(tmp_path, "Edit")
    material.update(request=None, denied="declaration_required", file_after=material["file_before"])
    native.update(
        is_error=True,
        tool_result="declaration_required",
        structured_result="Error: declaration_required",
    )
    with pytest.raises(NativeCaptureError):
        verify_denied(native, material)


def test_postfailure_on_denied_call_must_repeat_the_trusted_refusal(tmp_path):
    _, native, material = fixture(tmp_path, "Edit")
    material.update(request=None, denied="declaration_required", file_after=material["file_before"])
    material["post_hook"].update(
        hook_event_name="PostToolUseFailure", error="Error reading file: EIO"
    )
    del material["post_hook"]["tool_response"]
    native.update(
        is_error=True,
        tool_result="declaration_required",
        structured_result="Error: declaration_required",
    )
    with pytest.raises(NativeCaptureError, match="denial_result_mismatch"):
        verify_denied(native, material)


@pytest.mark.parametrize("status", ["passed", "failed"])
def test_business_check_outcome_cannot_be_transport_error(tmp_path, status):
    request, native, material = fixture(tmp_path, "run_visible_checks")
    host = {"status": status, "exit_code": 0 if status == "passed" else 1}
    content = [{"type": "text", "text": json.dumps(host)}]
    material.update(host_is_error=True, host_result=host)
    material["post_hook"]["tool_response"] = content
    native.update(is_error=True, tool_result=content, structured_result=content)
    with pytest.raises(NativeCaptureError, match="check_error_identity"):
        verify_native(request, native, material)


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
def test_failed_file_tool_cannot_have_successful_structured_result(tmp_path, tool):
    request, native, material = fixture(tmp_path, tool)
    error = "Error reading file: EIO"
    material.update(file_after=material["file_before"])
    material["post_hook"].update(hook_event_name="PostToolUseFailure", error=error)
    del material["post_hook"]["tool_response"]
    native.update(is_error=True, tool_result=error, structured_result={"status": "success"})
    with pytest.raises(NativeCaptureError, match="failure_delivery_mismatch"):
        verify_native(request, native, material)


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
def test_failed_file_tool_closes_with_exact_error_sidecar(tmp_path, tool):
    request, native, material = fixture(tmp_path, tool)
    error = "Error reading file: EIO"
    material.update(file_after=material["file_before"])
    material["post_hook"].update(hook_event_name="PostToolUseFailure", error=error)
    del material["post_hook"]["tool_response"]
    native.update(is_error=True, tool_result=error, structured_result="Error: " + error)
    assert verify_native(request, native, material).is_error is True


def thinking_tokens(**changes):
    """Sanitized shape observed in the pinned CLI, with SDK optional metadata."""
    return {
        "type": "system",
        "subtype": "thinking_tokens",
        "estimated_tokens": 3,
        "estimated_tokens_delta": 1,
        "session_id": IDENTITY.session_id,
        "uuid": "00000000-0000-4000-8000-000000000001",
        **changes,
    }


@pytest.mark.parametrize("count,delta", [(0, 0), (3, 1), (3.5, 0.5), (1e100, 1e10)])
@pytest.mark.parametrize("user_message_uuid", [None, "user-message-1"])
def test_thinking_counts_are_validated_without_creating_tool_evidence(
    tmp_path, count, delta, user_message_uuid
):
    consumer, materials, material, calls, init, start, result, end = stream(tmp_path)
    row = thinking_tokens(estimated_tokens=count, estimated_tokens_delta=delta)
    if user_message_uuid is not None:
        row["user_message_uuid"] = user_message_uuid
    feed(consumer, init)
    before = consumer.summary()
    feed(consumer, row)
    assert consumer.summary() == before
    assert calls == []
    materials["tool-1"] = material
    feed(consumer, start, row, result, row, end)
    summary = consumer.finish()
    assert summary["completed_tools"] == 1
    assert summary["total_cost_usd"] == 0.01
    assert len(calls) == 1
    assert "estimated_tokens" not in str(summary)
    assert "user-message-1" not in str(summary)


@pytest.mark.parametrize("key", ["estimated_tokens", "estimated_tokens_delta"])
@pytest.mark.parametrize(
    "value", [True, False, None, "1", -1, -0.5, float("inf"), float("nan"), 10**400]
)
def test_thinking_counts_reject_invalid_numbers(tmp_path, key, value):
    consumer, _, _, calls, init, *_ = stream(tmp_path)
    feed(consumer, init)
    with pytest.raises(NativeCaptureError):
        feed(consumer, thinking_tokens(**{key: value}))
    assert calls == []
    assert consumer.summary()["complete"] is False


@pytest.mark.parametrize(
    "changes,removed",
    [
        ({}, "estimated_tokens"),
        ({}, "estimated_tokens_delta"),
        ({}, "uuid"),
        ({}, "session_id"),
        ({"session_id": "other-session"}, None),
        ({"uuid": "not-a-uuid"}, None),
        ({"uuid": ""}, None),
        ({"uuid": 1}, None),
        ({"user_message_uuid": None}, None),
        ({"user_message_uuid": ""}, None),
        ({"user_message_uuid": 1}, None),
        ({"user_message_uuid": "x" * 301}, None),
        ({"parent_tool_use_id": "nested-call"}, None),
        ({"tool_use_id": "tool-1"}, None),
        ({"thinking": "unsupported-private-field"}, None),
    ],
)
def test_thinking_counts_reject_missing_unknown_or_unbound_fields(tmp_path, changes, removed):
    consumer, _, _, calls, init, *_ = stream(tmp_path)
    row = thinking_tokens(**changes)
    if removed:
        del row[removed]
    feed(consumer, init)
    with pytest.raises(NativeCaptureError):
        feed(consumer, row)
    assert calls == []


@pytest.mark.parametrize("position", ["before_init", "after_result"])
def test_thinking_counts_require_an_active_initialized_invocation(tmp_path, position):
    consumer, _, _, _, init, _, _, end = stream(tmp_path)
    if position == "after_result":
        feed(consumer, init, end)
    with pytest.raises(NativeCaptureError):
        feed(consumer, thinking_tokens())


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "record_rule", "run_visible_checks"])
def test_pre_denial_accepts_exact_pinned_hook_error_template(tmp_path, tool):
    _, native, material = fixture(tmp_path, tool)
    material.update(
        request=None, denied="hook_shape", post_hook=None, file_after=material["file_before"]
    )
    refusal = f"PreToolUse:{material['full_name']} hook error: hook_shape"
    native.update(is_error=True, tool_result=refusal, structured_result="Error: " + refusal)
    verify_denied(native, material)


@pytest.mark.parametrize(
    "content,sidecar",
    [
        ("PreToolUse:Edit hook error: hook_shape", "Error: PreToolUse:Edit hook error: hook_shape"),
        (
            "PreToolUse:Read hook error: other_reason",
            "Error: PreToolUse:Read hook error: other_reason",
        ),
        (
            "prefix PreToolUse:Read hook error: hook_shape",
            "Error: prefix PreToolUse:Read hook error: hook_shape",
        ),
        (
            "PreToolUse:Read hook error: hook_shape suffix",
            "Error: PreToolUse:Read hook error: hook_shape suffix",
        ),
        (
            "PostToolUse:Read hook error: hook_shape",
            "Error: PostToolUse:Read hook error: hook_shape",
        ),
        ("PreToolUse:Read hook error: hook_shape", "Error: hook_shape"),
        ("PreToolUse:Read hook error: hook_shape", {"status": "success"}),
        ("PreToolUse:Read hook error: hook_shape", None),
    ],
)
def test_hook_error_denial_rejects_other_names_reasons_prefixes_and_sidecars(
    tmp_path, content, sidecar
):
    _, native, material = fixture(tmp_path)
    material.update(
        request=None, denied="hook_shape", post_hook=None, file_after=material["file_before"]
    )
    native.update(is_error=True, tool_result=content, structured_result=sidecar)
    with pytest.raises(NativeCaptureError, match="denial_result_mismatch"):
        verify_denied(native, material)


def test_hook_error_denial_postfailure_must_match_the_delivered_template(tmp_path):
    _, native, material = fixture(tmp_path)
    material.update(request=None, denied="hook_shape", file_after=material["file_before"])
    refusal = "PreToolUse:Read hook error: hook_shape"
    native.update(is_error=True, tool_result=refusal, structured_result="Error: " + refusal)
    material["post_hook"].update(hook_event_name="PostToolUseFailure", error=refusal)
    del material["post_hook"]["tool_response"]
    verify_denied(native, material)
    material["post_hook"]["error"] = "hook_shape"
    with pytest.raises(NativeCaptureError, match="denial_result_mismatch"):
        verify_denied(native, material)


def test_native_stream_binds_hook_error_denial_to_start_and_trusted_pre(tmp_path):
    _, _, material, _, init, start, result, end = stream(tmp_path)
    material.update(
        request=None, denied="hook_shape", post_hook=None, file_after=material["file_before"]
    )
    calls = []

    def complete(tool_id, native):
        verify_denied(native, material)
        calls.append(tool_id)

    consumer = NativeStream(
        identity=IDENTITY,
        expected_model=MODEL,
        expected_tools=TOOLS,
        material=lambda _: material,
        complete=complete,
    )
    refusal = "PreToolUse:Read hook error: hook_shape"
    result["message"]["content"][0].update(content=refusal, is_error=True)
    result["tool_use_result"] = "Error: " + refusal
    feed(consumer, init, start, result, end)
    assert consumer.finish()["completed_tools"] == 1
    assert calls == ["tool-1"]
