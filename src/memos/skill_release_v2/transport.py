"""Single-run, role-authenticated host transport with durable fail-closed grants.

Only the host process can call ``complete_native``. Socket messages can deliver
hooks or the two fixed MCP operations, never assert that native execution ended.
"""

import copy
import hmac
import json
import math
import os
import re
import secrets
import socket
import stat
import threading
import time

from collections.abc import Callable
from pathlib import Path

from memos.exceptions import MemOSError
from memos.skill_release_v2.gate import (
    GateDeniedError,
    HostGate,
    NativeRequest,
    RunIdentity,
    VerifiedResult,
    digest_json,
)


TOOL_NAMES = {
    "Read": "Read",
    "Edit": "Edit",
    "Write": "Write",
    "mcp__memos_v2__record_rule": "record_rule",
    "mcp__memos_v2__run_visible_checks": "run_visible_checks",
}
MAX_MESSAGE_BYTES = 1048576
_HOOK_KEYS = {
    "session_id",
    "transcript_path",
    "cwd",
    "permission_mode",
    "hook_event_name",
    "tool_use_id",
    "tool_name",
    "tool_input",
    "tool_response",
    "error",
    "is_interrupt",
    "prompt_id",
    "scratchpad_dir",
    "effort",
    "agent_id",
    "agent_type",
    "duration_ms",
    "mcp_server",
}


class TransportError(MemOSError, ValueError):
    """Stable public error code, without payloads or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value: object, limit: int = 300) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and all(not char.isspace() and ord(char) >= 33 and ord(char) != 127 for char in value)
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportError("duplicate_json_key")
        result[key] = value
    return result


def _constant(value):
    raise TransportError("invalid_json")


def _json(value):
    try:
        result = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise TransportError("invalid_json") from None


def _clone(value):
    return json.loads(_json(value))


def _deny(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class HostTransport:
    """Own one private Unix socket, two role credentials and a durable journal."""

    def __init__(
        self,
        gate: HostGate,
        identity: RunIdentity,
        *,
        directory: Path,
        check_runner: Callable[[NativeRequest], tuple[dict, bool]],
        verify_denied: Callable[[object, dict], None] | None = None,
        verify_result: Callable[[NativeRequest, object, dict], VerifiedResult] | None = None,
        completion_wait: float = 2.0,
        check_timeout: float = 20.0,
    ):
        if gate.state().identity != identity or not callable(check_runner):
            raise TransportError("configuration_shape")
        if not 0 < completion_wait <= 2 or not 0 < check_timeout <= 20:
            raise TransportError("configuration_timeout")
        self.gate = gate
        self.identity = identity
        self.check_runner = check_runner
        self.verify_denied = verify_denied
        self.verify_result = verify_result
        self.completion_wait = completion_wait
        self.check_timeout = check_timeout
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise TransportError("unsafe_control_directory")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = self.directory.resolve(strict=True)
        if self.directory == gate.workspace or gate.workspace in self.directory.parents:
            raise TransportError("control_directory_in_workspace")
        os.chmod(self.directory, 0o700)
        self.socket_path = self.directory / "host.sock"
        self.journal_path = self.directory / "journal.jsonl"
        self._journal = os.open(
            self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self._sequence = 0
        self._previous = "0" * 64
        self._gate_sequence = 0
        self._condition = threading.Condition(threading.RLock())
        self._tokens = {role: secrets.token_hex(32) for role in ("hook", "mcp")}
        self._nonces: set[str] = set()
        self._calls: dict[str, dict] = {}
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._append("opened", identity=identity.__dict__)

    @property
    def closed(self):
        return self._closed

    def credentials(self, role: str) -> dict:
        """Return private client configuration; callers must store it with mode 0600."""
        if role not in self._tokens:
            raise TransportError("role_forbidden")
        return {
            "role": role,
            "token": self._tokens[role],
            **self.identity.__dict__,
            "socket_path": str(self.socket_path),
            "workspace": str(self.gate.workspace),
        }

    def _append(self, event, **details):
        row = {
            "sequence": self._sequence + 1,
            "previous_hash": self._previous,
            "event": event,
            **details,
        }
        row["digest"] = digest_json(row)
        try:
            content = (_json(row) + "\n").encode()
            offset = 0
            while offset < len(content):
                written = os.write(self._journal, content[offset:])
                if written <= 0:
                    raise OSError("short journal write")
                offset += written
            os.fsync(self._journal)
        except OSError as exc:
            self._closed = True
            self.gate.invalidate("journal_unavailable")
            if self._listener is not None:
                self._listener.close()
            raise TransportError("journal_unavailable") from exc
        self._sequence += 1
        self._previous = row["digest"]

    def _flush_gate(self):
        for entry in self.gate.audit()[self._gate_sequence :]:
            self._append("gate", entry=json.loads(entry.canonical_json), gate_digest=entry.digest)
            self._gate_sequence = entry.sequence

    def _live(self):
        if self._closed:
            raise TransportError("transport_closed")

    def _invalidate(self, reason):
        self._closed = True
        self.gate.invalidate(reason)
        self._condition.notify_all()
        self._flush_gate()

    def _validate_hook(self, payload):
        if type(payload) is not dict or set(payload) - _HOOK_KEYS:
            raise TransportError("hook_shape")
        if payload.get("session_id") != self.identity.session_id:
            raise TransportError("session_mismatch")
        if payload.get("cwd") != str(self.gate.workspace):
            raise TransportError("cwd_mismatch")
        if not _text(payload.get("tool_use_id")) or not _text(payload.get("tool_name")):
            raise TransportError("hook_identity_shape")
        if type(payload.get("tool_input")) is not dict:
            raise TransportError("hook_input_shape")
        if payload.get("hook_event_name") not in {
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
        }:
            raise TransportError("hook_event_shape")
        if len(_json(payload).encode()) > MAX_MESSAGE_BYTES:
            raise TransportError("message_too_large")
        self._validate_hook_metadata(payload)

    def _validate_hook_metadata(self, payload):
        # Fixed CLI 2.1.296: $d supplies common fields, Okn/P9r supply
        # duration_ms, and _X/Mnr supply MCP provenance. None are Agent args.
        if "agent_id" in payload or "agent_type" in payload:
            raise TransportError("subagent_unsupported")
        if "prompt_id" in payload and (
            type(payload["prompt_id"]) is not str
            or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                payload["prompt_id"],
            )
        ):
            raise TransportError("prompt_id_shape")
        for key in ("scratchpad_dir", "transcript_path"):
            if key not in payload:
                continue
            value = payload[key]
            if (
                type(value) is not str
                or not 0 < len(value) <= 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or "\\" in value
                or not Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                raise TransportError("hook_path_metadata_shape")
        if "permission_mode" in payload and (
            type(payload["permission_mode"]) is not str
            or payload["permission_mode"]
            not in {
                "default",
                "plan",
                "acceptEdits",
                "auto",
                "dontAsk",
                "bypassPermissions",
            }
        ):
            raise TransportError("permission_mode_shape")
        # The frozen profile uses low. Other documented effort levels need
        # their own explicit profile rather than silently changing this run.
        if "effort" in payload and (
            type(payload["effort"]) is not dict or payload["effort"] != {"level": "low"}
        ):
            raise TransportError("effort_mismatch")
        if "duration_ms" in payload:
            duration = payload["duration_ms"]
            if (
                payload["hook_event_name"] not in {"PostToolUse", "PostToolUseFailure"}
                or type(duration) not in (int, float)
                or duration < 0
                or (type(duration) is float and not math.isfinite(duration))
            ):
                raise TransportError("duration_shape")
        if payload["tool_name"] in {
            "mcp__memos_v2__record_rule",
            "mcp__memos_v2__run_visible_checks",
        }:
            # --mcp-config loads this stdio server with scope dynamic. SDK,
            # plugin and persisted configuration scopes are different origins.
            provenance = payload.get("mcp_server")
            if type(provenance) is not dict or provenance != {
                "name": "memos_v2",
                "source": "dynamic",
            }:
                raise TransportError("mcp_provenance_mismatch")
        elif "mcp_server" in payload:
            raise TransportError("mcp_provenance_mismatch")

    def _record_rejected_hook_shape(self, payload, reason):
        """Retain only bounded structural diagnostics after authentication."""
        fields = []
        truncated = False
        names = {
            dict: "object",
            list: "array",
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }

        def visit(value, prefix="", depth=0):
            nonlocal truncated
            if type(value) is not dict:
                return
            for key, item in value.items():
                if len(fields) >= 64:
                    truncated = True
                    return
                name = (
                    key
                    if type(key) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
                    else "<invalid-key>"
                )
                path = prefix + name
                fields.append({"name": path, "type": names.get(type(item), "invalid")})
                if type(item) is dict:
                    if depth < 2:
                        visit(item, path + ".", depth + 1)
                    elif item:
                        truncated = True

        visit(payload)
        try:
            fingerprint = digest_json(json.loads(_json(payload)))
        except TransportError:
            fingerprint = None
        self._append(
            "hook_rejected_shape",
            reason=reason,
            payload_type=names.get(type(payload), "invalid"),
            payload_sha256=fingerprint,
            field_count=len(payload) if type(payload) is dict else 0,
            fields=fields,
            fields_truncated=truncated,
        )

    def _file(self, request):
        if request is None or request.path is None:
            return None
        return self._read_path(request.path)

    def _input_path(self, payload):
        if payload["tool_name"] not in {"Read", "Edit", "Write"}:
            return None
        value = payload["tool_input"].get("file_path")
        if type(value) is not str or not value or "\x00" in value or "\\" in value:
            return None
        path = Path(value)
        if ".." in path.parts:
            return None
        if path.is_absolute():
            try:
                path = path.relative_to(self.gate.workspace)
            except ValueError:
                return None
        current = self.gate.workspace
        for part in path.parts:
            current /= part
            if current.is_symlink():
                return None
        return path.as_posix() if path.parts else None

    def _read_path(self, relative_path):
        if relative_path is None:
            return None
        path = self.gate.workspace / relative_path
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise TransportError("unsafe_file_path")
                return stream.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TransportError("file_unavailable") from exc

    def handle_hook(self, payload: object) -> dict:
        """Process authenticated Hook input; no Post event waits for native stdout."""
        with self._condition:
            is_pre = type(payload) is dict and payload.get("hook_event_name") == "PreToolUse"
            validated = False
            try:
                self._live()
                self._validate_hook(payload)
                validated = True
                payload = _clone(payload)
                if is_pre:
                    return self._pre(payload)
                return self._post(payload)
            except (TransportError, GateDeniedError) as exc:
                if not validated and not self._closed:
                    try:
                        self._record_rejected_hook_shape(payload, exc.code)
                    except TransportError:
                        self._closed = True
                if not is_pre:
                    try:
                        self._invalidate(exc.code)
                    except TransportError:
                        self._closed = True
                    return {"continue": False, "stopReason": exc.code}
                return _deny(exc.code)

    def _pre(self, payload):
        tool_id = payload["tool_use_id"]
        if tool_id in self._calls:
            raise TransportError("duplicate_tool_use_id")
        deadline = time.monotonic() + self.completion_wait
        while self._needs_completion(payload["tool_name"]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._retain_denial(payload, "previous_completion_timeout")
            self._condition.wait(remaining)
            self._live()
        self._calls[tool_id] = {
            "request": None,
            "full_name": payload["tool_name"],
            "pre_hook": payload,
            "post_hook": None,
            "host_result": None,
            "host_is_error": None,
            "file_before": None,
            "denied": None,
            "completed": False,
            "mcp_started": False,
            "evidence_path": self._input_path(payload),
        }
        try:
            if payload["tool_name"] not in TOOL_NAMES:
                raise TransportError("unsupported_tool")
            self._calls[tool_id]["file_before"] = self._read_path(
                self._calls[tool_id]["evidence_path"]
            )
            request = self.gate.begin_tool(
                self.identity, tool_id, TOOL_NAMES[payload["tool_name"]], payload["tool_input"]
            )
            self._calls[tool_id]["request"] = request
            self._calls[tool_id]["file_before"] = self._file(request)
            if request.permit is not None:
                self.gate.consume_edit(request.permit, self.identity, tool_id, request.input)
            self._flush_gate()
            self._append(
                "pre_allowed", tool_use_id=tool_id, payload=payload, request_digest=request.digest
            )
        except (TransportError, GateDeniedError) as exc:
            self._calls[tool_id]["denied"] = exc.code
            if not self._closed:
                self._flush_gate()
                self._append("pre_denied", tool_use_id=tool_id, payload=payload, reason=exc.code)
            return _deny(exc.code)
        return {
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
        }

    def _needs_completion(self, full_name):
        for call in self._calls.values():
            if call["completed"]:
                continue
            previous = call["request"].tool_name if call["request"] else None
            if full_name == "Read" and previous not in {"Edit", "Write"}:
                continue
            if full_name == "mcp__memos_v2__run_visible_checks" and previous not in {
                "Edit",
                "Write",
                "run_visible_checks",
            }:
                continue
            return True
        return False

    def _retain_denial(self, payload, reason):
        evidence_path = self._input_path(payload)
        self._calls[payload["tool_use_id"]] = {
            "request": None,
            "full_name": payload["tool_name"],
            "pre_hook": payload,
            "post_hook": None,
            "host_result": None,
            "host_is_error": None,
            "file_before": self._read_path(evidence_path),
            "denied": reason,
            "completed": False,
            "mcp_started": False,
            "evidence_path": evidence_path,
        }
        self._append(
            "pre_denied", tool_use_id=payload["tool_use_id"], payload=payload, reason=reason
        )
        return _deny(reason)

    def _post(self, payload):
        tool_id = payload["tool_use_id"]
        call = self._calls.get(tool_id)
        if call is None or call["denied"] or call["completed"] or call["post_hook"] is not None:
            raise TransportError("post_not_pending")
        if (
            payload["tool_name"] != call["full_name"]
            or digest_json(payload["tool_input"]) != call["request"].input_sha256
        ):
            raise TransportError("post_input_mismatch")
        call["post_hook"] = payload
        self._append("post_retained", tool_use_id=tool_id, payload=payload)
        if call["request"].tool_name == "Read" and payload["hook_event_name"] == "PostToolUse":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "observation:" + call["request"].digest,
                }
            }
        return {}

    def _run_check(self, request):
        event = threading.Event()
        outcome = []

        def run():
            try:
                outcome.append(self.check_runner(request))
            except Exception:
                outcome.append(
                    ({"status": "error", "execution_error": "check_execution_failed"}, True)
                )
            finally:
                event.set()

        threading.Thread(target=run, name="memos-v2-check", daemon=True).start()
        if not event.wait(self.check_timeout):
            return {"status": "error", "execution_error": "check_timeout"}, True
        value = outcome[0]
        if (
            not isinstance(value, tuple)
            or len(value) != 2
            or type(value[0]) is not dict
            or type(value[1]) is not bool
        ):
            return {"status": "error", "execution_error": "invalid_check_result"}, True
        body, is_error = value
        if not is_error and (body.get("status") == "error" or body.get("execution_error")):
            return {"status": "error", "execution_error": "invalid_check_error_flag"}, True
        return _clone(body), is_error

    def handle_mcp(self, name: str, arguments: object) -> tuple[dict, bool]:
        """Match exact input to a single pending native call; never accept IDs in args."""
        with self._condition:
            self._live()
            if name not in {"record_rule", "run_visible_checks"} or type(arguments) is not dict:
                raise TransportError("mcp_shape")
            if name == "run_visible_checks" and arguments:
                raise TransportError("check_input_shape")
            fingerprint = digest_json(arguments)
            matches = [
                call
                for call in self._calls.values()
                if call["request"] is not None
                and not call["completed"]
                and not call["denied"]
                and not call["mcp_started"]
                and call["request"].tool_name == name
                and call["request"].input_sha256 == fingerprint
            ]
            if len(matches) != 1:
                raise TransportError(
                    "ambiguous_native_request" if matches else "native_request_missing"
                )
            call = matches[0]
            request = call["request"]
            call["mcp_started"] = True
            self._append("mcp_started", tool_use_id=request.tool_use_id)
            if name == "record_rule":
                try:
                    result, is_error = self.gate.receive_declaration(arguments).as_dict(), False
                except GateDeniedError as exc:
                    result, is_error = {"status": "error", "error": exc.code}, True
                self._flush_gate()
            else:
                result = None
                is_error = False
        if name == "run_visible_checks":
            result, is_error = self._run_check(request)
            result["observation_ref"] = "observation:" + request.digest
        with self._condition:
            self._live()
            call["host_result"], call["host_is_error"] = _clone(result), is_error
            self._append(
                "mcp_result", tool_use_id=request.tool_use_id, result=result, is_error=is_error
            )
            return _clone(result), is_error

    def delivery_material(self, tool_use_id: str) -> dict:
        """Return trusted host evidence; returned mutable values are independent copies."""
        with self._condition:
            call = self._calls.get(tool_use_id)
            if call is None:
                raise TransportError("native_request_missing")
            return {
                **{
                    key: copy.deepcopy(call[key])
                    for key in (
                        "request",
                        "full_name",
                        "pre_hook",
                        "post_hook",
                        "host_result",
                        "host_is_error",
                        "file_before",
                        "denied",
                    )
                },
                "file_after": self._read_path(call["evidence_path"]),
                "workspace": self.gate.workspace,
                "identity": self.identity,
            }

    def complete_native(self, tool_use_id: str, envelope: object):
        """Host-only completion; the injected verifier must close all three sources."""
        with self._condition:
            self._live()
            call = self._calls.get(tool_use_id)
            if call is None or call["completed"]:
                raise TransportError("native_not_pending")
            try:
                if call["denied"]:
                    if self.verify_denied is None:
                        raise TransportError("denial_verifier_required")
                    self.verify_denied(envelope, self.delivery_material(tool_use_id))
                    result = None
                else:
                    if call["post_hook"] is None:
                        raise TransportError("post_missing")
                    request = call["request"]
                    if request.tool_name == "record_rule" and call["host_is_error"] is True:
                        if self.verify_result is None:
                            raise TransportError("error_verifier_required")
                        verified = self.verify_result(
                            request, envelope, self.delivery_material(tool_use_id)
                        )
                        if (
                            not isinstance(verified, VerifiedResult)
                            or verified.request_digest != request.digest
                            or verified.is_error is not True
                            or verified.result_sha256 != digest_json(call["host_result"])
                        ):
                            raise TransportError("error_verification_mismatch")
                        self.gate.abort_tool(self.identity, tool_use_id, "declaration_rejected")
                        result = None
                    else:
                        result = self.gate.complete_tool(self.identity, tool_use_id, envelope)
                self._flush_gate()
                self._append(
                    "native_closed",
                    tool_use_id=tool_use_id,
                    denied=call["denied"],
                    envelope=envelope,
                )
                call["completed"] = True
                self._condition.notify_all()
                return result
            except Exception:
                self._invalidate("native_verification_failed")
                raise

    def dispatch(self, message: object):
        """Authenticate one socket envelope and restrict its operation to the role."""
        keys = {
            "role",
            "token",
            "run_id",
            "attempt_id",
            "session_id",
            "nonce",
            "operation",
            "payload",
        }
        with self._condition:
            self._live()
            if type(message) is not dict or set(message) != keys:
                raise TransportError("message_shape")
            role = message["role"]
            if (
                type(role) is not str
                or role not in self._tokens
                or type(message["token"]) is not str
                or not hmac.compare_digest(message["token"], self._tokens[role])
            ):
                raise TransportError("authentication_failed")
            if any(message[key] != value for key, value in self.identity.__dict__.items()):
                raise TransportError("identity_mismatch")
            if not _text(message["nonce"], 128):
                raise TransportError("nonce_shape")
            if message["nonce"] in self._nonces:
                raise TransportError("nonce_replayed")
            if message["operation"] != role:
                raise TransportError("operation_forbidden")
            if len(self._nonces) >= 10000:
                raise TransportError("request_limit")
            self._nonces.add(message["nonce"])
        if role == "hook":
            return self.handle_hook(message["payload"])
        payload = message["payload"]
        if type(payload) is not dict or set(payload) != {"name", "arguments"}:
            raise TransportError("mcp_shape")
        result, is_error = self.handle_mcp(payload["name"], payload["arguments"])
        return {"result": result, "is_error": is_error}

    def _connection(self, connection):
        with connection:
            connection.settimeout(4)
            try:
                with connection.makefile("rb") as stream:
                    line = stream.readline(MAX_MESSAGE_BYTES + 1)
                if not line.endswith(b"\n") or len(line) > MAX_MESSAGE_BYTES:
                    raise TransportError("message_too_large")
                message = json.loads(line, object_pairs_hook=_pairs, parse_constant=_constant)
                _json(message)
                response = {"ok": True, "result": self.dispatch(message)}
            except (TransportError, GateDeniedError) as exc:
                response = {"ok": False, "error": exc.code}
            except (OSError, ValueError, RecursionError):
                response = {"ok": False, "error": "invalid_request"}
            try:
                connection.sendall((_json(response) + "\n").encode())
            except OSError:
                # The supervisor will reject missing native closure; no retry is safe.
                return

    def start(self):
        with self._condition:
            self._live()
            if self._listener is not None:
                raise TransportError("already_started")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self.socket_path))
                os.chmod(self.socket_path, 0o600)
                listener.listen(16)
                listener.settimeout(0.1)
            except OSError:
                listener.close()
                raise
            self._listener = listener

        def accept():
            while not self._closed:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                threading.Thread(target=self._connection, args=(connection,), daemon=True).start()

        self._thread = threading.Thread(target=accept, name="memos-v2-socket", daemon=True)
        self._thread.start()
        return self

    def close(self, timeout: float = 0.2) -> bool:
        """Revoke admission immediately and acquire state only within a fixed budget.

        A false result means a daemon worker still owns state/journal resources;
        callers must mark the run incomplete and avoid synchronous gate access.
        """
        if type(timeout) not in (int, float) or not 0 <= timeout <= 2:
            raise TransportError("close_timeout_shape")
        deadline = time.monotonic() + timeout
        self._closed = True
        if self._listener is not None:
            self._listener.close()
        if not self._condition.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            self._condition.notify_all()
            if self._journal >= 0:
                os.close(self._journal)
                self._journal = -1
        finally:
            self._condition.release()
        if self._thread is not None:
            self._thread.join(timeout=max(0, deadline - time.monotonic()))
        self.socket_path.unlink(missing_ok=True)
        return self._thread is None or not self._thread.is_alive()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
