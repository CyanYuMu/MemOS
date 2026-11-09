"""Fail-closed verification for the frozen Claude stream-json protocol.

Only safe reason codes and counters leave this module. The transport retains
private Hook payloads; this consumer must never treat assistant prose as proof.
"""

import hashlib
import math

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from memos.exceptions import MemOSError
from memos.skill_release_v2.capture import (
    ToolResultError,
    _canonical_json,
    _parse_object,
    verify_tool_delivery,
)
from memos.skill_release_v2.gate import NativeRequest, RunIdentity, VerifiedResult


TOOL_NAMES = {
    "Read": "Read",
    "Edit": "Edit",
    "Write": "Write",
    "mcp__memos_v2__record_rule": "record_rule",
    "mcp__memos_v2__run_visible_checks": "run_visible_checks",
}
_MAX_BYTES = 1048576
_WRITE_SUFFIX = " (file state is current in your context — no need to Read it back)"
_UNCHANGED_READ = (
    "Wasted call — file unchanged since your last Read. Refer to that earlier tool_result instead."
)


class NativeCaptureError(MemOSError, ValueError):
    """A bounded reason code, never native content or private host material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NativeCaptureError(code)


def _canonical(value: object) -> str:
    try:
        result = _canonical_json(value, code="native_json_shape")
    except ToolResultError:
        raise NativeCaptureError("native_json_shape") from None
    _require(len(result.encode("utf-8")) <= _MAX_BYTES, "native_payload_too_large")
    return result


def _same(left: object, right: object, code: str) -> None:
    _require(_canonical(left) == _canonical(right), code)


def _text(content: object) -> str:
    if type(content) is str:
        return content
    if (
        type(content) is list
        and len(content) == 1
        and type(content[0]) is dict
        and set(content[0]) == {"type", "text"}
        and content[0]["type"] == "text"
        and type(content[0]["text"]) is str
    ):
        return content[0]["text"]
    raise NativeCaptureError("native_text_shape")


def _hook(hook: object, material: Mapping, tool_id: str, event: str) -> dict:
    _require(type(hook) is dict, "missing_hook")
    identity = material["identity"]
    _require(isinstance(identity, RunIdentity), "identity_shape")
    _require(hook.get("hook_event_name") == event, "hook_event_mismatch")
    _require(hook.get("session_id") == identity.session_id, "hook_session_mismatch")
    _require(hook.get("tool_use_id") == tool_id, "hook_tool_id_mismatch")
    _require(hook.get("tool_name") == material["full_name"], "hook_tool_name_mismatch")
    _require(hook.get("cwd") == str(material["workspace"]), "hook_cwd_mismatch")
    _require(type(hook.get("tool_input")) is dict, "hook_input_shape")
    return hook


def _bound(native: object, material: Mapping) -> dict:
    _require(
        type(native) is dict
        and set(native)
        == {"session_id", "tool_use_id", "is_error", "tool_result", "structured_result"},
        "native_result_shape",
    )
    _canonical(native)
    _require(isinstance(material.get("identity"), RunIdentity), "identity_shape")
    _require(native["session_id"] == material["identity"].session_id, "native_session_mismatch")
    _require(
        type(native["tool_use_id"]) is str and bool(native["tool_use_id"]), "native_tool_id_shape"
    )
    _require(type(native["is_error"]) is bool, "native_error_shape")
    _require(material.get("full_name") in TOOL_NAMES, "native_tool_name")
    return _hook(material.get("pre_hook"), material, native["tool_use_id"], "PreToolUse")


def _decoded(raw: object) -> str:
    _require(type(raw) is bytes and len(raw) <= _MAX_BYTES, "file_observation_missing")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise NativeCaptureError("file_encoding") from None


def _file_path(response: Mapping, request: NativeRequest, material: Mapping) -> None:
    _require(type(response.get("filePath")) is str, "file_path_shape")
    _require(
        response["filePath"] == str(Path(material["workspace"]) / request.path),
        "file_path_mismatch",
    )


def _verify_read(request: NativeRequest, native: dict, material: Mapping) -> bool:
    response = native["structured_result"]
    _require(
        type(response) is dict
        and set(response) == {"type", "file"}
        and type(response["type"]) is str
        and response["type"] in {"text", "file_unchanged"},
        "read_response_shape",
    )
    data = response["file"]
    if response["type"] == "file_unchanged":
        _require(type(data) is dict and set(data) == {"filePath"}, "read_file_shape")
        _file_path(data, request, material)
        _require(type(material.get("file_before")) is bytes, "read_file_missing")
        _require(material["file_before"] == material.get("file_after"), "read_file_changed")
        _require(_text(native["tool_result"]) == _UNCHANGED_READ, "read_delivery_mismatch")
        # This proves delivery of the marker, never delivery of source bytes.
        return False
    _require(
        type(data) is dict
        and {"filePath", "content", "numLines", "startLine", "totalLines"} <= set(data)
        and set(data)
        <= {"filePath", "content", "numLines", "startLine", "totalLines", "truncatedByTokenCap"},
        "read_file_shape",
    )
    _file_path(data, request, material)
    _require(data.get("truncatedByTokenCap", False) is False, "read_truncated")
    before = _decoded(material.get("file_before"))
    _require(material["file_before"] == material.get("file_after"), "read_file_changed")
    # Claude's frozen text reader strips a UTF-8 BOM and normalizes CRLF.
    text = before.removeprefix("\ufeff").replace("\r\n", "\n").removesuffix("\r")
    lines = text.split("\n")
    offset = request.input.get("offset", 1)
    limit = request.input.get("limit")
    _require(
        type(offset) is int
        and offset >= 1
        and (limit is None or (type(limit) is int and limit > 0)),
        "read_range",
    )
    selected = lines[offset - 1 : None if limit is None else offset - 1 + limit]
    expected = "\n".join(selected)
    for key, value in [
        ("numLines", len(selected)),
        ("startLine", offset),
        ("totalLines", len(lines)),
    ]:
        _require(type(data.get(key)) is int and data[key] == value, "read_metadata_mismatch")
    _same(data["content"], expected, "read_content_mismatch")
    if text == "":
        rendered = "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"
        _require(_text(native["tool_result"]) == rendered, "read_delivery_mismatch")
    else:
        # The pinned renderer supports ':' for tab-leading source lines.
        variants = {
            "\n".join(f"{offset + i}{sep}{line}" for i, line in enumerate(selected))
            for sep in ("\t", ":")
        }
        _require(_text(native["tool_result"]) in variants, "read_delivery_mismatch")
    return True


def _verify_write(request: NativeRequest, native: dict, material: Mapping) -> None:
    response = native["structured_result"]
    _require(type(response) is dict, "write_response_shape")
    _file_path(response, request, material)
    _require(
        response.get("staged", False) is False and response.get("userModified", False) is False,
        "write_not_executed",
    )
    before = None if material.get("file_before") is None else _decoded(material["file_before"])
    _same(response.get("originalFile"), before, "write_original_mismatch")
    value = request.input
    if request.tool_name == "Edit":
        _require(before is not None, "edit_original_missing")
        _same(response.get("oldString"), value["old_string"], "edit_input_mismatch")
        _same(response.get("newString"), value["new_string"], "edit_input_mismatch")
        _same(response.get("replaceAll"), value.get("replace_all", False), "edit_input_mismatch")
        count = before.count(value["old_string"]) if value["old_string"] else 0
        _require(count > 0 and (count == 1 or value.get("replace_all", False)), "edit_match_count")
        expected = before.replace(value["old_string"], value["new_string"])
        rendered = f"The file {response['filePath']} has been updated successfully."
    else:
        expected = value["content"]
        _same(response.get("content"), expected, "write_input_mismatch")
        _require(
            response.get("type") == ("create" if before is None else "update"),
            "write_type_mismatch",
        )
        rendered = (
            f"File created successfully at: {response['filePath']}"
            if before is None
            else f"The file {response['filePath']} has been updated successfully."
        )
    _require(material.get("file_after") == expected.encode("utf-8"), "write_file_mismatch")
    _require(_text(native["tool_result"]) == rendered + _WRITE_SUFFIX, "write_delivery_mismatch")


def _verify_probe_context(row: dict) -> None:
    """The G1 probe has a fixed context; it does not qualify a loaded Skill."""
    skills = row.get("skills")
    plugins = row.get("plugins")
    servers = row.get("mcp_servers")
    names = {"cc-plugin-agents-md", "cc-plugin-plugin-authoring"}
    _require(
        row.get("claude_code_version") == "2.1.296"
        and row.get("permissionMode") == "dontAsk"
        and type(skills) is list
        and len(skills) == 2
        and all(type(skill) is str for skill in skills)
        and set(skills) == {"doctor", "plugin-authoring"}
        and type(plugins) is list
        and len(plugins) == 2
        and all(
            type(plugin) is dict
            and plugin.get("name") in names
            and plugin.get("path") == "builtin"
            and plugin.get("source") == plugin["name"] + "@builtin"
            for plugin in plugins
        )
        and {plugin["name"] for plugin in plugins} == names
        and type(servers) is list
        and len(servers) == 1
        and type(servers[0]) is dict
        and servers[0].get("name") == "memos_v2"
        and servers[0].get("status") == "connected",
        "native_context_mismatch",
    )


def _check_execution_error(result: dict) -> bool:
    return (
        result.get("status") == "error"
        or result.get("execution_error") is not None
        or result.get("timed_out") is True
        or ("exit_code" in result and result["exit_code"] is None)
    )


def verify_native(request: NativeRequest, native: object, material: Mapping) -> VerifiedResult:
    """Bind native delivery, Hook return, and trusted host/file observations."""
    pre = _bound(native, material)
    _require(
        isinstance(request, NativeRequest) and request == material.get("request"),
        "request_mismatch",
    )
    _require(
        request.identity == material["identity"] and request.tool_use_id == native["tool_use_id"],
        "request_identity_mismatch",
    )
    _require(TOOL_NAMES[material["full_name"]] == request.tool_name, "request_tool_mismatch")
    _same(pre["tool_input"], request.input, "request_input_mismatch")
    _require(not material.get("denied"), "request_denied")
    post_value = material.get("post_hook")
    event = post_value.get("hook_event_name") if type(post_value) is dict else None
    _require(event in {"PostToolUse", "PostToolUseFailure"}, "missing_hook")
    post = _hook(post_value, material, request.tool_use_id, event)
    _same(post["tool_input"], request.input, "post_input_mismatch")
    is_error = native["is_error"]
    read_content_delivered = not (request.tool_name == "Read" and is_error)
    if request.tool_name in {"record_rule", "run_visible_checks"}:
        try:
            normalized = verify_tool_delivery(
                {key: native[key] for key in ("tool_use_id", "is_error", "tool_result")},
                expected_tool_use_id=request.tool_use_id,
                expected_is_error=material.get("host_is_error"),
                host_result=material.get("host_result"),
            )
            hook_content = (
                post.get("error") if event == "PostToolUseFailure" else post.get("tool_response")
            )
            # Success data is the native MCP content array. Error throws preserve
            # the JSON error string in PostToolUseFailure.error.
            if type(hook_content) is dict and set(hook_content) == {"content", "isError"}:
                _require(hook_content["isError"] is is_error, "hook_error_mismatch")
                hook_content = hook_content["content"]
            verify_tool_delivery(
                {
                    "tool_use_id": request.tool_use_id,
                    "is_error": is_error,
                    "tool_result": hook_content,
                },
                expected_tool_use_id=request.tool_use_id,
                expected_is_error=is_error,
                host_result=material["host_result"],
            )
            sidecar = native["structured_result"]
            if sidecar is not None:
                if is_error and type(sidecar) is str and sidecar.startswith("Error: "):
                    sidecar = sidecar[len("Error: ") :]
                if type(sidecar) is dict and set(sidecar) == {"content", "isError"}:
                    _require(sidecar["isError"] is is_error, "sidecar_error_mismatch")
                    sidecar = sidecar["content"]
                verify_tool_delivery(
                    {
                        "tool_use_id": request.tool_use_id,
                        "is_error": is_error,
                        "tool_result": sidecar,
                    },
                    expected_tool_use_id=request.tool_use_id,
                    expected_is_error=is_error,
                    host_result=material["host_result"],
                )
        except ToolResultError:
            raise NativeCaptureError("mcp_delivery_mismatch") from None
        if request.tool_name == "run_visible_checks":
            execution_error = _check_execution_error(normalized.result)
            _require(not execution_error or is_error, "check_error_identity")
            if normalized.result.get("status") in {"passed", "failed"} and not execution_error:
                _require(not is_error, "check_error_identity")
        _require(event != "PostToolUseFailure" or is_error, "hook_error_mismatch")
        digest = normalized.result_sha256
    elif is_error:
        _require(event == "PostToolUseFailure", "hook_error_mismatch")
        error = post.get("error")
        _require(type(error) is str and bool(error), "hook_error_shape")
        delivered = _text(native["tool_result"])
        _require(
            delivered == error and native["structured_result"] == "Error: " + error,
            "failure_delivery_mismatch",
        )
        _require(material.get("file_before") == material.get("file_after"), "failed_write_changed")
        digest = hashlib.sha256(_canonical(native).encode()).hexdigest()
    else:
        _require(event == "PostToolUse", "hook_error_mismatch")
        _same(post.get("tool_response"), native["structured_result"], "structured_result_mismatch")
        if request.tool_name == "Read":
            read_content_delivered = _verify_read(request, native, material)
        else:
            _verify_write(request, native, material)
        digest = hashlib.sha256(_canonical(native).encode()).hexdigest()
    return VerifiedResult(request.digest, digest, is_error, read_content_delivered)


def verify_denied(native: object, material: Mapping) -> None:
    """A trusted Pre rejection plus a native refusal closes without a Post hook."""
    pre = _bound(native, material)
    _require(
        material.get("request") is None
        and type(material.get("denied")) is str
        and bool(material["denied"]),
        "trusted_denial_missing",
    )
    # Claude 2.1.296 renders either permissionDecisionReason directly or a
    # blocking Hook error as JH(`PreToolUse:${tool.name}`, blockingError).
    # The stream sidecar is exactly "Error: " followed by that same refusal.
    # Neither path accepts a substring match against an unrelated failure.
    reason = material["denied"]
    delivered = native["tool_result"]
    _require(
        native["is_error"] is True
        and type(delivered) is str
        and delivered in (reason, f"PreToolUse:{material['full_name']} hook error: {reason}")
        and native["structured_result"] == "Error: " + delivered,
        "denial_result_mismatch",
    )
    _require(material.get("file_before") == material.get("file_after"), "denied_file_changed")
    post = material.get("post_hook")
    if post is not None:
        _hook(post, material, native["tool_use_id"], "PostToolUseFailure")
        _same(post["tool_input"], pre["tool_input"], "post_input_mismatch")
        _require(
            post.get("error") == delivered and "tool_response" not in post,
            "denial_result_mismatch",
        )


def _verify_thinking_tokens(row: dict) -> None:
    required = {
        "type",
        "subtype",
        "estimated_tokens",
        "estimated_tokens_delta",
        "session_id",
        "uuid",
    }
    _require(
        required <= set(row) <= required | {"user_message_uuid"},
        "native_thinking_shape",
    )
    value = row["uuid"]
    _require(type(value) is str, "native_thinking_shape")
    try:
        valid_uuid = str(UUID(value)) == value
    except ValueError:
        valid_uuid = False
    _require(valid_uuid, "native_thinking_shape")
    if "user_message_uuid" in row:
        value = row["user_message_uuid"]
        _require(type(value) is str and 0 < len(value) <= 300, "native_thinking_shape")
    for key in ("estimated_tokens", "estimated_tokens_delta"):
        value = row[key]
        _require(type(value) in (int, float), "native_thinking_shape")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        _require(finite and value >= 0, "native_thinking_shape")


class NativeStream:
    """Consume bounded full JSONL records from exactly one pinned invocation.

    Starts are retained until their result: stdout can precede the Pre Hook.
    ``complete`` is a trusted host callback, never an endpoint exposed to MCP.
    Any capture error permanently closes this consumer.
    """

    def __init__(
        self,
        *,
        identity: RunIdentity,
        expected_model: str,
        expected_tools: tuple[str, ...],
        material: Callable[[str], Mapping],
        complete: Callable[[str, dict], object],
    ):
        _require(isinstance(identity, RunIdentity), "identity_shape")
        _require(type(expected_model) is str and bool(expected_model), "model_shape")
        _require(
            bool(expected_tools)
            and len(set(expected_tools)) == len(expected_tools)
            and set(expected_tools) <= set(TOOL_NAMES),
            "tools_shape",
        )
        self.identity = identity
        self.expected_model = expected_model
        self.expected_tools = frozenset(expected_tools)
        self._material, self._complete = material, complete
        self._init = False
        self._result = None
        self._starts: dict[str, dict] = {}
        self._done: set[str] = set()
        self._issues: list[str] = []
        self._ended = False
        self._records = 0
        self._total_bytes = 0

    def feed(self, line: str) -> None:
        """Consume one newline-terminated record, rejecting duplicate JSON keys."""
        try:
            _require(not self._issues and not self._ended, "stream_closed")
            _require(type(line) is str and len(line) <= _MAX_BYTES, "native_payload_too_large")
            _require(line.endswith("\n"), "truncated_native_record")
            size = len(line.encode("utf-8"))
            _require(size <= _MAX_BYTES, "native_payload_too_large")
            self._total_bytes += size
            self._records += 1
            _require(
                self._records <= 10000 and self._total_bytes <= 33554432, "native_stream_too_large"
            )
            try:
                row = _parse_object(line)
            except ToolResultError:
                raise NativeCaptureError("native_json_shape") from None
            self._consume(row)
        except NativeCaptureError as exc:
            self._issues.append(exc.code)
            raise
        except Exception:
            self._issues.append("native_callback_failure")
            raise NativeCaptureError("native_callback_failure") from None

    def _consume(self, row: dict) -> None:
        _require(row.get("session_id") == self.identity.session_id, "native_session_mismatch")
        _require(row.get("parent_tool_use_id") is None, "nested_native_session")
        kind = row.get("type")
        if kind == "system" and row.get("subtype") in {"hook_started", "hook_response"}:
            _require(
                type(row.get("hook_id")) is str and type(row.get("hook_event")) is str,
                "native_hook_shape",
            )
            return
        _require(self._result is None, "native_after_result")
        if kind == "system" and row.get("subtype") == "init":
            _require(not self._init and not self._starts, "duplicate_native_init")
            _require(row.get("model") == self.expected_model, "native_model_mismatch")
            tools = row.get("tools")
            _require(
                type(tools) is list
                and all(type(item) is str for item in tools)
                and len(set(tools)) == len(tools)
                and set(tools) == self.expected_tools,
                "native_tools_mismatch",
            )
            _verify_probe_context(row)
            self._init = True
            return
        _require(self._init, "native_init_missing")
        if kind == "system" and row.get("subtype") == "thinking_tokens":
            _verify_thinking_tokens(row)
            return
        if kind == "result":
            _require(
                type(row.get("is_error")) is bool
                and row.get("subtype")
                in {
                    "success",
                    "error_during_execution",
                    "error_max_turns",
                    "error_max_budget_usd",
                    "error_max_structured_output_retries",
                }
                and row["is_error"] is (row["subtype"] != "success"),
                "native_final_shape",
            )
            _require(not (set(self._starts) - self._done), "native_pending_calls")
            usage = row.get("modelUsage")
            _require(
                type(usage) is dict and set(usage) == {self.expected_model}, "native_model_mismatch"
            )
            cost = row.get("total_cost_usd")
            _require(
                type(cost) in (int, float) and math.isfinite(cost) and cost >= 0,
                "native_cost_shape",
            )
            _require(
                type(row.get("num_turns")) is int and row["num_turns"] >= 0, "native_turns_shape"
            )
            _require(type(row.get("usage")) is dict, "native_usage_shape")
            self._result = {
                key: row[key] for key in ("is_error", "subtype", "num_turns", "total_cost_usd")
            }
            self._result["usage"] = {
                key: value
                for key, value in row["usage"].items()
                if key
                in {
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                }
                and type(value) is int
                and value >= 0
            }
            return
        _require(kind in {"assistant", "user"}, "unknown_native_event")
        message = row.get("message")
        _require(
            type(message) is dict and type(message.get("content")) is list, "native_message_shape"
        )
        if kind == "assistant":
            _require(message.get("model") == self.expected_model, "native_model_mismatch")
        results = [
            b for b in message["content"] if type(b) is dict and b.get("type") == "tool_result"
        ]
        _require(len(results) <= 1, "ambiguous_native_results")
        for block in message["content"]:
            _require(type(block) is dict, "native_block_shape")
            block_type = block.get("type")
            if block_type == "text":
                _require(type(block.get("text")) is str, "native_text_shape")
            elif block_type in {"thinking", "redacted_thinking"}:
                _require(kind == "assistant", "native_role_mismatch")
            elif block_type == "tool_use":
                _require(kind == "assistant", "native_role_mismatch")
                tool_id = block.get("id")
                _require(type(tool_id) is str and 0 < len(tool_id) <= 300, "native_tool_id_shape")
                _require(tool_id not in self._starts, "duplicate_native_start")
                _require(
                    block.get("name") in self.expected_tools and type(block.get("input")) is dict,
                    "native_tool_shape",
                )
                self._starts[tool_id] = {"name": block["name"], "input": block["input"]}
            elif block_type == "tool_result":
                _require(kind == "user", "native_role_mismatch")
                tool_id = block.get("tool_use_id")
                _require(type(tool_id) is str and tool_id in self._starts, "unknown_native_tool")
                _require(tool_id not in self._done, "duplicate_native_result")
                material = self._material(tool_id)
                pre = material.get("pre_hook")
                _require(type(pre) is dict, "missing_hook")
                start = self._starts[tool_id]
                _same(start["input"], pre.get("tool_input"), "native_start_input_mismatch")
                _require(
                    start["name"] == material.get("full_name") == pre.get("tool_name"),
                    "native_start_name_mismatch",
                )
                native = {
                    "session_id": row["session_id"],
                    "tool_use_id": tool_id,
                    "is_error": block.get("is_error", False),
                    "tool_result": block.get("content"),
                    "structured_result": row.get("tool_use_result"),
                }
                self._complete(tool_id, native)
                self._done.add(tool_id)
            else:
                raise NativeCaptureError("unknown_native_block")

    def summary(self) -> dict[str, Any]:
        """Return counters and safe reasons, without raw text, paths or secrets."""
        return {
            "complete": self._ended and not self._issues,
            "issues": list(self._issues),
            "initialized": self._init,
            "model": self.expected_model if self._init else None,
            "started_tools": len(self._starts),
            "completed_tools": len(self._done),
            "result": dict(self._result) if self._result else None,
            "total_cost_usd": self._result["total_cost_usd"] if self._result else None,
        }

    def finish(self) -> dict[str, Any]:
        """Close on EOF; missing initialization, calls or final result fail closed."""
        if not self._ended:
            self._ended = True
            if not self._init:
                self._issues.append("native_init_missing")
            if set(self._starts) - self._done:
                self._issues.append("native_pending_calls")
            if self._result is None:
                self._issues.append("native_result_missing")
        if self._issues:
            raise NativeCaptureError(self._issues[0])
        return self.summary()
