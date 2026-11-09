"""Copyable stdlib-only Claude Hook client; run with python -I -S -B."""

import argparse
import json
import os
import secrets
import socket
import stat
import sys


MAX_BYTES = 1048576


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
    if config["role"] != "hook" or len(config["token"]) != 64:
        raise ValueError("config_role")
    return config


def request(config, payload):
    if (
        payload.get("session_id") != config["session_id"]
        or payload.get("cwd") != config["workspace"]
    ):
        raise ValueError("hook_identity_mismatch")
    if payload.get("hook_event_name") not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
        raise ValueError("hook_event_shape")
    envelope = {
        **{key: config[key] for key in ("role", "token", "run_id", "attempt_id", "session_id")},
        "nonce": secrets.token_hex(16),
        "operation": "hook",
        "payload": payload,
    }
    raw = json.dumps(envelope, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("message_too_large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(4)
        client.connect(config["socket_path"])
        client.sendall(raw)
        with client.makefile("rb") as stream:
            raw = stream.readline(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid_response")
    response = parse(raw)
    if (
        response.keys() != {"ok", "result"}
        or response["ok"] is not True
        or type(response["result"]) is not dict
    ):
        raise ValueError("host_rejected")
    result = response["result"]
    if payload["hook_event_name"] == "PreToolUse":
        specific = result.get("hookSpecificOutput", {})
        if specific.get("hookEventName") != "PreToolUse" or specific.get(
            "permissionDecision"
        ) not in {"allow", "deny"}:
            raise ValueError("invalid_hook_decision")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("hook_too_large")
        response = request(config, parse(raw))
        print(json.dumps(response, allow_nan=False, separators=(",", ":")), flush=True)
        return 0
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        # Exit 2 is a blocking Hook failure. Never echo payloads or private config.
        print("memos_v2_hook_failed", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
