"""Copyable stdlib-only MCP stdio client for the fixed memos_v2 host protocol."""

import argparse
import json
import math
import os
import secrets
import socket
import stat
import sys


MAX_BYTES = 1048576
DECLARATION_FIELDS = {
    "rule_id",
    "phase",
    "branch_index",
    "observation",
    "decision",
    "evidence_refs",
}
TOOLS = [
    {
        "name": "record_rule",
        "description": "Declare the next rule step using current delivered evidence. Receipt remains pending until native delivery is verified.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(DECLARATION_FIELDS),
            "properties": {
                "rule_id": {"type": "string", "minLength": 1},
                "phase": {"type": "string", "enum": ["select", "act", "check", "exit"]},
                "branch_index": {"type": ["integer", "null"], "minimum": 0},
                "observation": {"type": "string", "minLength": 1},
                "decision": {"type": "string", "minLength": 1},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
            },
        },
    },
    {
        "name": "run_visible_checks",
        "description": "Run the fixed public checks for the host-selected workspace and return a delivered evidence reference.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError("non_finite_json")


def parse(raw):
    value = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    json.dumps(value, allow_nan=False).encode("utf-8")
    if type(value) is not dict:
        raise ValueError("object_required")
    return value


def load_config(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_config")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("config_too_large")
    config = parse(raw)
    if set(config) != {
        "socket_path",
        "workspace",
        "role",
        "token",
        "run_id",
        "attempt_id",
        "session_id",
    }:
        raise ValueError("config_shape")
    if any(type(value) is not str or not value for value in config.values()):
        raise ValueError("config_shape")
    if config["role"] != "mcp" or len(config["token"]) != 64:
        raise ValueError("config_role")
    return config


def validate_call(params):
    if (
        type(params) is not dict
        or not {"name", "arguments"} <= set(params)
        or set(params) - {"name", "arguments", "_meta"}
    ):
        raise ValueError("call_shape")
    if "_meta" in params:
        metadata = params["_meta"]
        if type(metadata) is not dict or set(metadata) - {"progressToken", "claudecode/toolUseId"}:
            raise ValueError("call_metadata_shape")
        if "progressToken" in metadata:
            token = metadata["progressToken"]
            if not (
                (type(token) is str and len(token) <= 300)
                or type(token) is int
                or (type(token) is float and math.isfinite(token))
            ):
                raise ValueError("call_metadata_shape")
        if "claudecode/toolUseId" in metadata:
            hint = metadata["claudecode/toolUseId"]
            if type(hint) is not str or not 0 < len(hint) <= 300:
                raise ValueError("call_metadata_shape")
    name, arguments = params["name"], params["arguments"]
    if (
        type(name) is not str
        or name not in {"record_rule", "run_visible_checks"}
        or type(arguments) is not dict
    ):
        raise ValueError("call_shape")
    if name == "run_visible_checks":
        if arguments:
            raise ValueError("check_input_shape")
        return
    if set(arguments) != DECLARATION_FIELDS:
        raise ValueError("declaration_shape")
    if any(
        type(arguments[key]) is not str or not arguments[key].strip() or len(arguments[key]) > 16384
        for key in ("rule_id", "phase", "observation", "decision")
    ):
        raise ValueError("declaration_shape")
    if arguments["phase"] not in {"select", "act", "check", "exit"}:
        raise ValueError("declaration_shape")
    branch = arguments["branch_index"]
    if branch is not None and (type(branch) is not int or branch < 0):
        raise ValueError("declaration_shape")
    refs = arguments["evidence_refs"]
    if (
        type(refs) is not list
        or any(type(ref) is not str or not ref for ref in refs)
        or len(refs) != len(set(refs))
    ):
        raise ValueError("declaration_shape")


def request(config, params):
    validate_call(params)
    envelope = {
        **{key: config[key] for key in ("role", "token", "run_id", "attempt_id", "session_id")},
        "nonce": secrets.token_hex(16),
        "operation": "mcp",
        # Runtime metadata is not business input or a source of native identity.
        "payload": {"name": params["name"], "arguments": params["arguments"]},
    }
    raw = json.dumps(envelope, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("message_too_large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(20 if params["name"] == "run_visible_checks" else 4)
        client.connect(config["socket_path"])
        client.sendall(raw)
        with client.makefile("rb") as stream:
            raw = stream.readline(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid_response")
    response = parse(raw)
    if response.keys() != {"ok", "result"} or response["ok"] is not True:
        raise ValueError("host_rejected")
    result = response["result"]
    if (
        type(result) is not dict
        or set(result) != {"result", "is_error"}
        or type(result["result"]) is not dict
        or type(result["is_error"]) is not bool
    ):
        raise ValueError("invalid_response")
    return tool_result(result["result"], result["is_error"])


def tool_result(body, is_error):
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, allow_nan=False, sort_keys=True, separators=(",", ":")),
            }
        ],
        "isError": is_error,
    }


def dispatch(message, config):
    request_id = message.get("id")
    if (
        message.get("jsonrpc") != "2.0"
        or type(message.get("method")) is not str
        or set(message) - {"jsonrpc", "id", "method", "params"}
    ):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    if "id" not in message:
        return None
    if type(request_id) not in (str, int) and request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    method = message["method"]
    params = message.get("params", {})
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "memos_v2", "version": "2"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = request(config, params)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            result = tool_result({"status": "error", "error": "memos_v2_call_failed"}, True)
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError, RecursionError):
        print("memos_v2_config_failed", file=sys.stderr, flush=True)
        return 2
    while True:
        raw = sys.stdin.buffer.readline(MAX_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
            return 2
        try:
            response = dispatch(parse(raw), config)
        except (ValueError, TypeError, RecursionError):
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        if response is not None:
            print(json.dumps(response, allow_nan=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
