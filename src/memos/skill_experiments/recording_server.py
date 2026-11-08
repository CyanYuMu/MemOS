"""Standalone stdio MCP claim receipt endpoint; it never writes trusted evidence.

This file is copied into the read-only control directory and deliberately uses
only Python's standard library. The host must validate native call/result pairs
and evidence references before adding a rule declaration to its event record.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import selectors
import socket
import subprocess
import sys
import time
import uuid

from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import TextIO


PROTOCOL_VERSION = "2025-06-18"
_PHASES = ("observe", "select", "act", "check", "exit")
_FIELDS = {
    "rule_id",
    "phase",
    "branch",
    "observation",
    "decision",
    "evidence_refs",
    "related_tool_events",
    "local_result",
    "change_ref",
}
OUTPUT_LIMIT = 128 * 1024
BROKER_RESPONSE_LIMIT = 512 * 1024


def _valid_test_config(context: dict) -> bool:
    command = context.get("test_command")
    timeout = context.get("test_timeout_seconds")
    return (
        isinstance(command, list)
        and 0 < len(command) <= 128
        and all(
            isinstance(arg, str) and arg and "\x00" not in arg and len(arg) <= 8192
            for arg in command
        )
        and Path(command[0]).is_absolute()
        and isinstance(context.get("test_cwd"), str)
        and Path(context["test_cwd"]).is_absolute()
        and type(timeout) in (int, float)
        and math.isfinite(timeout)
        and 0 < timeout <= 60
    )


def _valid_broker_config(context: dict) -> bool:
    path = context.get("test_broker_socket")
    timeout = context.get("test_broker_timeout_seconds", 15)
    return (
        isinstance(path, str)
        and bool(path)
        and "\x00" not in path
        and Path(path).is_absolute()
        and type(timeout) in (int, float)
        and math.isfinite(timeout)
        and 0 < timeout <= 65
    )


def _valid_context(context: object) -> bool:
    return (
        isinstance(context, dict)
        and all(
            isinstance(context.get(key), str) and context[key] for key in ("run_id", "attempt_id")
        )
        and (context.get("skill_revision") is None or isinstance(context["skill_revision"], str))
        and isinstance(context.get("rule_ids"), list)
        and all(isinstance(rule, str) and rule for rule in context["rule_ids"])
        and len(context["rule_ids"]) == len(set(context["rule_ids"]))
        and ("test_command" not in context or _valid_test_config(context))
        and ("test_broker_socket" not in context or _valid_broker_config(context))
    )


def _valid_declaration(value: object, context: dict) -> bool:
    if not isinstance(value, dict) or set(value) - _FIELDS:
        return False
    if not isinstance(value.get("rule_id"), str) or value["rule_id"] not in context["rule_ids"]:
        return False
    if not isinstance(value.get("phase"), str) or value["phase"] not in _PHASES:
        return False
    if any(
        not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 4000
        for key in ("observation", "decision")
    ):
        return False
    for key in ("evidence_refs", "related_tool_events"):
        references = value.get(key, [])
        if (
            not isinstance(references, list)
            or len(references) > 100
            or any(not isinstance(ref, str) or not ref or len(ref) > 256 for ref in references)
        ):
            return False
        if len(references) != len(set(references)):
            return False
    return not any(
        key in value and (not isinstance(value[key], str) or len(value[key]) > 4000)
        for key in ("branch", "local_result", "change_ref")
    )


def _tool() -> dict:
    return {
        "name": "record_rule",
        "description": (
            "Submit a short public observation and decision for a rule in this fixed run. "
            "Use observable action/result references only; do not include private reasoning. "
            "The reply acknowledges a declaration; it does not prove execution or correctness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "phase": {"type": "string", "enum": list(_PHASES)},
                "observation": {"type": "string", "minLength": 1, "maxLength": 4000},
                "decision": {"type": "string", "minLength": 1, "maxLength": 4000},
                **{
                    key: {"type": "string", "maxLength": 4000}
                    for key in ("branch", "local_result", "change_ref")
                },
                **{
                    key: {
                        "type": "array",
                        "maxItems": 100,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    }
                    for key in ("evidence_refs", "related_tool_events")
                },
            },
            "required": ["rule_id", "phase", "observation", "decision"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    }


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tools(context: dict) -> list[dict]:
    # P4 exposes identical capabilities; empty rule IDs still reject every declaration.
    tools = [_tool()]
    if _valid_broker_config(context):
        tools.append(
            {
                "name": "run_visible_checks",
                "description": (
                    "Run the fixed visible task checks in the configured isolated process. "
                    "No command, working-directory, timeout or test-selection overrides are accepted. "
                    "An exit code of zero is a local test result, not external task acceptance."
                ),
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            }
        )
    return tools


def run_fixed_check(context: dict) -> dict:
    """Execute only the host's fixed sandbox command; the child may not fork.

    Process and filesystem restrictions are enforced by the immutable sandbox
    profile in test_command, not by this transport. No new process session is
    created, and the child receives no model authentication environment.
    """
    if not isinstance(context, dict) or not _valid_test_config(context):
        raise ValueError("invalid_fixed_test_configuration")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    outcome = {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": None,
    }
    try:
        process = subprocess.Popen(
            context["test_command"],
            cwd=context["test_cwd"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        outcome["execution_error"] = "test_process_start_failed"
        return outcome
    deadline = time.monotonic() + context["test_timeout_seconds"]
    remaining = OUTPUT_LIMIT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    outcome["timed_out"] = True
                    break
                for selected, _ in selector.select(min(left, 0.1)):
                    data = os.read(selected.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(selected.fileobj)
                        continue
                    kept = data[:remaining]
                    buffers[selected.data].extend(kept)
                    remaining -= len(kept)
                    if len(data) != len(kept):
                        outcome["truncated"] = True
                        break
                if outcome["truncated"]:
                    break
        if (outcome["timed_out"] or outcome["truncated"]) and process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            outcome["timed_out"] = True
            process.kill()
            process.wait(timeout=1)
        outcome["exit_code"] = process.returncode
    except (OSError, subprocess.TimeoutExpired):
        outcome["execution_error"] = "test_process_control_failed"
    finally:
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                outcome["execution_error"] = "test_process_cleanup_failed"
        process.stdout.close()
        process.stderr.close()
    text_budget = OUTPUT_LIMIT
    for stream, data in buffers.items():
        encoded = data.decode("utf-8", errors="replace").encode("utf-8")
        if len(encoded) > text_budget:
            outcome["truncated"] = True
        outcome[stream] = encoded[:text_budget].decode("utf-8", errors="ignore")
        text_budget -= len(outcome[stream].encode("utf-8"))
    return outcome


def _request_visible_checks(context: dict) -> dict:
    """Ask the host broker to run its fixed command; never execute candidate code here."""
    failure = {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": "test_broker_failed",
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            timeout = context.get("test_broker_timeout_seconds", 15)
            deadline = time.monotonic() + timeout
            connection.settimeout(timeout)
            connection.connect(context["test_broker_socket"])
            connection.sendall(b'{"action":"run_visible_checks"}\n')
            received = bytearray()
            while b"\n" not in received:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("broker response deadline exceeded")
                connection.settimeout(remaining)
                data = connection.recv(min(8192, BROKER_RESPONSE_LIMIT + 1 - len(received)))
                if not data:
                    raise ValueError("missing response terminator")
                received.extend(data)
                if len(received) > BROKER_RESPONSE_LIMIT:
                    raise ValueError("response too large")
            line, remainder = received.split(b"\n", 1)
            if remainder.strip():
                raise ValueError("multiple broker responses")
            outcome = json.loads(line)
        required = {"exit_code", "stdout", "stderr", "truncated", "timed_out", "execution_error"}
        if (
            not isinstance(outcome, dict)
            or not required <= set(outcome)
            or set(outcome) - required - {"evidence_ref"}
        ):
            raise ValueError("invalid broker result")
        if (
            (outcome["exit_code"] is not None and type(outcome["exit_code"]) is not int)
            or not isinstance(outcome["stdout"], str)
            or not isinstance(outcome["stderr"], str)
            or type(outcome["truncated"]) is not bool
            or type(outcome["timed_out"]) is not bool
            or (
                outcome["execution_error"] is not None
                and not isinstance(outcome["execution_error"], str)
            )
        ):
            raise ValueError("invalid broker fields")
        if "evidence_ref" in outcome:
            reference = outcome["evidence_ref"]
            if (
                not isinstance(reference, dict)
                or set(reference) != {"check_index", "candidate_snapshot"}
                or type(reference["check_index"]) is not int
                or not isinstance(reference["candidate_snapshot"], str)
            ):
                raise ValueError("invalid broker evidence reference")
        return outcome
    except (OSError, ValueError):
        return failure


def serve(context: dict, input_stream: TextIO, output_stream: TextIO) -> None:
    """Serve declarations and the host's fixed, isolated visible-check command."""
    initialized = False
    for line in input_stream:
        try:
            request = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "Invalid JSON")
        else:
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
            ):
                response = _error(None, -32600, "Invalid request")
            elif "id" not in request:
                continue
            else:
                request_id = request["id"]
                method, params = request["method"], request.get("params", {})
                if type(request_id) not in (str, int) or not isinstance(params, dict):
                    response = _error(None, -32600, "Invalid request")
                elif method == "initialize":
                    if initialized:
                        response = _error(request_id, -32600, "Already initialized")
                    else:
                        initialized = True
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "protocolVersion": PROTOCOL_VERSION,
                                "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": "memos-run-recorder", "version": "p4.1"},
                            },
                        }
                elif method == "ping":
                    response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
                elif not initialized:
                    response = _error(request_id, -32002, "Server is not initialized")
                elif method == "tools/list":
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"tools": _tools(context)},
                    }
                elif method == "tools/call":
                    arguments = params.get("arguments")
                    valid = params.get("name") == "record_rule" and _valid_declaration(
                        arguments, context
                    )
                    content = (
                        {
                            "receipt_id": uuid.uuid4().hex,
                            "status": "declaration_received",
                            "execution_verified": False,
                            "evidence_validation": "pending_host_validation",
                            "run_id": context["run_id"],
                            "attempt_id": context["attempt_id"],
                            "skill_revision": context.get("skill_revision"),
                            "rule_id": arguments["rule_id"],
                            "phase": arguments["phase"],
                        }
                        if valid
                        else {"error": "invalid_rule_declaration"}
                    )
                    test_call = (
                        params.get("name") == "run_visible_checks"
                        and arguments == {}
                        and _valid_broker_config(context)
                    )
                    if test_call:
                        content = _request_visible_checks(context)
                    is_error = (
                        bool(
                            content["timed_out"]
                            or content["truncated"]
                            or content["execution_error"]
                        )
                        if test_call
                        else not valid
                    )
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(content, ensure_ascii=False)}
                            ],
                            "isError": is_error,
                        },
                    }
                else:
                    response = _error(request_id, -32601, "Method not found")
        output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        output_stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Receive public fixed-run rule declarations")
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args()
    try:
        context = json.loads(args.context.read_text(encoding="utf-8"))
        if not _valid_context(context):
            raise ValueError("invalid context")
    except (OSError, ValueError, TypeError):
        print("invalid_recording_context", file=sys.stderr)
        return 2
    serve(context, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
