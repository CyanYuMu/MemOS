"""Project native public observations before persistence; never store raw reasoning."""

from __future__ import annotations

import json
import math
import re

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


_PRIVATE_KEYS = {"thinking", "redacted_thinking", "signature", "reasoning", "reasoning_content"}
_SECRET_KEY = re.compile(
    r"(?:authorization|api[-_]?key|(?:^|[_-])token$|password|passwd|secret|credential|cookie)",
    re.I,
)
_BEARER = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[^\s\"'<>]+")
_AUTH_HEADER = re.compile(r"(?im)(\b(?:proxy-)?authorization\s*:\s*)[^\r\n]+")
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:[a-z][a-z0-9_-]*[_-])?"
    r"(?:api[-_]?key|access[-_]?token|auth[-_]?token|password|passwd|secret)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\"']+)"
)
_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_PHASES = {"observe", "select", "act", "check", "exit"}
_CONTEXT_KEYS = {
    "run_id",
    "attempt_id",
    "case_id",
    "case_revision",
    "split",
    "lineage_id",
    "initial_snapshot",
    "skill_id",
    "skill_revision",
}


def _normalized_secrets(secret_values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(secret_values, tuple | list) or any(
        not isinstance(secret, str) or not secret for secret in secret_values
    ):
        raise ValueError("capture_incomplete: secrets must be nonempty strings")
    return tuple(sorted(secret_values, key=len, reverse=True))


def _redact_public_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    value = _AUTH_HEADER.sub(r"\1[REDACTED]", value)
    value = _BEARER.sub(r"\1[REDACTED]", value)
    value = _ASSIGNMENT.sub(r"\1[REDACTED]", value)
    return _TOKEN.sub("[REDACTED]", value)


def _public_projection(value: object, secrets: tuple[str, ...], issues: list[str], depth=0):
    if depth > 64:
        issues.append("unsupported_payload_value")
        return "[UNSUPPORTED]"
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in {
            "thinking",
            "redacted_thinking",
        }:
            return {"type": "omitted_private_content"}
        result = {}
        omitted = 0
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _PRIVATE_KEYS:
                continue
            if _redact_public_text(key, secrets) != key:
                omitted += 1
                continue
            result[key] = (
                "[REDACTED]"
                if _SECRET_KEY.search(key)
                else _public_projection(item, secrets, issues, depth + 1)
            )
        if omitted:
            marker = "_omitted_sensitive_keys"
            suffix = 1
            while marker in result:
                suffix += 1
                marker = f"_omitted_sensitive_keys_{suffix}"
            if _redact_public_text(marker, secrets) == marker:
                result[marker] = omitted
        return result
    if isinstance(value, list | tuple):
        return [_public_projection(item, secrets, issues, depth + 1) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except RecursionError:
                issues.append("unsupported_payload_value")
                return "[UNSUPPORTED]"
            except ValueError:
                pass
            else:
                return json.dumps(
                    _public_projection(structured, secrets, issues, depth + 1),
                    ensure_ascii=False,
                    allow_nan=False,
                )
        return _redact_public_text(value, secrets)
    if type(value) is float and not math.isfinite(value):
        issues.append("invalid_numeric_payload")
        return "[INVALID_NUMBER]"
    if value is None or type(value) in (bool, int, float):
        return value
    issues.append("unsupported_payload_value")
    return "[UNSUPPORTED]"


def sanitize_public(value: object, secret_values: tuple[str, ...] = ()) -> object:
    """Return a public JSON-safe projection without modifying the caller's value.

    Ordinary keys retain their identity; keys containing credentials and private
    reasoning fields are omitted. Credential values are redacted. Omitted-key
    counts use a safe unused marker key. Structured JSON carried in a string
    remains a string. Unsupported or excessively nested input receives a placeholder.
    """
    return _public_projection(value, _normalized_secrets(secret_values), [])


class EventRecorder:
    """A single-writer collector with authoritative context and fail-closed completeness.

    The runner owns serialization of hook and stream deliveries. This class does
    not authenticate the caller or turn Agent declarations into execution proof.
    """

    def __init__(
        self,
        path: Path,
        context: dict,
        *,
        rule_ids: tuple[str, ...] = (),
        secret_values: tuple[str, ...] = (),
        expected_session_id: str | None = None,
    ):
        if not isinstance(context, dict) or any(
            not isinstance(context.get(key), str) or not context[key]
            for key in ("run_id", "attempt_id")
        ):
            raise ValueError("capture_incomplete: run and attempt identities are required")
        if set(context) - _CONTEXT_KEYS:
            raise ValueError("capture_incomplete: unsupported RunContext fields")
        if any(not isinstance(rule, str) or not rule for rule in rule_ids) or len(
            set(rule_ids)
        ) != len(rule_ids):
            raise ValueError("capture_incomplete: invalid rule identities")
        secrets = _normalized_secrets(secret_values)
        if expected_session_id is not None and (
            not isinstance(expected_session_id, str) or not expected_session_id
        ):
            raise ValueError("capture_incomplete: invalid expected native session")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8"):
                pass
        except FileExistsError as error:
            raise ValueError("capture_incomplete: event store already exists") from error
        self._context = deepcopy(context)
        self._rule_ids = set(rule_ids)
        self._secrets = secrets
        self._events: list[dict] = []
        self._seen: set[tuple] = set()
        self._observations: dict[tuple, set[str]] = {}
        self._issues: list[str] = []
        self._result: dict | None = None
        self._finished: dict | None = None
        self._bound_session_id = expected_session_id

    @property
    def events(self) -> list[dict]:
        return deepcopy(self._events)

    def _open(self) -> None:
        if self._finished is not None:
            raise ValueError("capture_incomplete: collector is finished")

    def _sanitize(self, value: object) -> object:
        issues: list[str] = []
        result = _public_projection(value, self._secrets, issues)
        for reason in issues:
            self._issue(reason)
        return result

    def _issue(self, reason: str) -> None:
        if reason not in self._issues:
            self._issues.append(reason)

    def _bind_session(self, session_id: object) -> None:
        if isinstance(session_id, str) and session_id:
            if self._bound_session_id is None:
                self._bound_session_id = session_id
            elif session_id != self._bound_session_id:
                self._issue("native_session_mismatch")

    def mark_incomplete(self, reason: str) -> dict:
        """Record a runner-observed parse, timeout or capture failure without raw input."""
        self._open()
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_:-]{1,120}", reason):
            raise ValueError("capture_incomplete: invalid capture reason")
        self._issue(reason)
        return self._append("collector", "capture_issue", {"reason": reason})

    def _append(
        self,
        producer: str,
        kind: str,
        payload: dict,
        *,
        session_id: object | None = None,
        message_id: object | None = None,
        tool_id: object | None = None,
    ) -> dict:
        self._open()
        if producer in {"native_stream", "native_hook"}:
            self._bind_session(session_id)
        sequence = len(self._events) + 1
        event = {
            "schema_version": 1,
            "event_id": f"{self._context['run_id']}:{self._context['attempt_id']}:{sequence:06d}",
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": self._sanitize(self._context),
            "producer": producer,
            "kind": kind,
            "native_session_id": session_id if isinstance(session_id, str) else None,
            "native_message_id": message_id if isinstance(message_id, str) else None,
            "native_tool_call_id": tool_id if isinstance(tool_id, str) else None,
            "payload": self._sanitize(payload),
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
            )
        self._events.append(event)
        return deepcopy(event)

    def _observe_tool(self, channel: str, session: object, tool_id: object, phase: str) -> None:
        if not isinstance(tool_id, str) or not tool_id:
            self._issue("missing_tool_identity")
            return
        key = (channel, session if isinstance(session, str) else None, tool_id)
        phases = self._observations.setdefault(key, set())
        if phase in phases:
            self._issue("duplicate_tool_phase")
        phases.add(phase)

    def ingest_stream(self, message: dict) -> list[dict]:
        """Project the non-partial stream-json messages into public observations."""
        self._open()
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return [self.mark_incomplete("invalid_stream_message")]
        self._bind_session(message.get("session_id"))
        native_uuid = message.get("uuid")
        if isinstance(native_uuid, str):
            identity = ("stream", native_uuid)
            if identity in self._seen:
                return []
            self._seen.add(identity)
        kind, subtype = message["type"], message.get("subtype")
        if kind == "system" and subtype == "thinking_tokens":
            # Native private-thinking progress carries no public execution evidence.
            return []
        session = message.get("session_id")
        common = {"session_id": session, "message_id": native_uuid}
        if kind == "system" and subtype == "init":
            payload = {
                key: message[key]
                for key in (
                    "model",
                    "tools",
                    "slash_commands",
                    "skills",
                    "plugins",
                    "mcp_servers",
                    "permissionMode",
                    "claude_code_version",
                )
                if key in message
            }
            return [self._append("native_stream", "session_init", payload, **common)]
        if kind == "system" and subtype == "informational":
            payload = {
                key: message[key]
                for key in ("content", "level", "prevent_continuation")
                if key in message
            }
            return [self._append("native_stream", "informational", payload, **common)]
        if kind == "system" and isinstance(subtype, str) and subtype.startswith("hook_"):
            payload = {
                key: message[key]
                for key in ("hook_id", "hook_name", "hook_event", "exit_code", "outcome")
                if key in message
            }
            if message.get("exit_code") not in (None, 0) or message.get("outcome") in (
                "error",
                "failed",
                "cancelled",
            ):
                self._issue("hook_execution_failed")
            return [self._append("native_stream", subtype, payload, **common)]
        if kind == "result":
            if self._result is not None:
                self._issue("multiple_native_results")
            payload = {
                key: message[key]
                for key in (
                    "subtype",
                    "is_error",
                    "result",
                    "errors",
                    "num_turns",
                    "duration_ms",
                    "duration_api_ms",
                    "total_cost_usd",
                    "usage",
                    "modelUsage",
                    "model",
                )
                if key in message
            }
            if type(message.get("is_error")) is not bool or not isinstance(subtype, str):
                self._issue("invalid_native_result")
            self._result = self._sanitize(payload)
            return [self._append("native_stream", "native_result", payload, **common)]
        if kind in {"assistant", "user"}:
            content = (
                message.get("message", {}).get("content")
                if isinstance(message.get("message"), dict)
                else None
            )
            if not isinstance(content, list):
                return [self.mark_incomplete("invalid_message_content")]
            emitted = []
            for block in content:
                if not isinstance(block, dict):
                    emitted.append(self.mark_incomplete("invalid_content_block"))
                    continue
                block_type = block.get("type")
                if block_type in {"thinking", "redacted_thinking"}:
                    continue
                if block_type == "text":
                    if not isinstance(block.get("text"), str):
                        emitted.append(self.mark_incomplete("invalid_text_block"))
                    else:
                        emitted.append(
                            self._append(
                                "native_stream", f"{kind}_text", {"text": block["text"]}, **common
                            )
                        )
                elif block_type == "tool_use":
                    tool_id = block.get("id")
                    if not isinstance(block.get("name"), str) or not isinstance(
                        block.get("input"), dict
                    ):
                        self._issue("invalid_tool_use")
                    self._observe_tool("stream", session, tool_id, "pre")
                    emitted.append(
                        self._append(
                            "native_stream",
                            "tool_started",
                            {"tool_name": block.get("name"), "tool_input": block.get("input")},
                            tool_id=tool_id,
                            **common,
                        )
                    )
                elif block_type == "tool_result":
                    tool_id = block.get("tool_use_id")
                    self._observe_tool("stream", session, tool_id, "post")
                    emitted.append(
                        self._append(
                            "native_stream",
                            "tool_completed",
                            {
                                "tool_result": block.get("content"),
                                "is_error": block.get("is_error", False),
                            },
                            tool_id=tool_id,
                            **common,
                        )
                    )
                else:
                    emitted.append(self.mark_incomplete("unsupported_content_block"))
            return emitted
        # Parent parses command-hook JSON; unrecognised protocol extensions cannot
        # silently count as complete collection in this fixed-version adapter.
        self._issue("unsupported_stream_message")
        diagnostic = {"reason": "unsupported_stream_message"}
        for name, value in (("stream_type", kind), ("stream_subtype", subtype)):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
                diagnostic[name] = value
        return [self._append("collector", "capture_issue", diagnostic)]

    def ingest_hook(self, payload: dict) -> dict:
        """Accept one runner-parsed native hook payload, not arbitrary Agent claims."""
        self._open()
        if not isinstance(payload, dict) or not isinstance(payload.get("hook_event_name"), str):
            return self.mark_incomplete("invalid_hook_payload")
        hook = payload["hook_event_name"]
        session, tool_id = payload.get("session_id"), payload.get("tool_use_id")
        identity = ("hook", hook, session, tool_id)
        if hook in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} and isinstance(tool_id, str):
            if identity in self._seen:
                return deepcopy(
                    next(
                        event
                        for event in self._events
                        if event["producer"] == "native_hook"
                        and event["kind"] == hook
                        and event["native_tool_call_id"] == tool_id
                        and event["native_session_id"] == session
                    )
                )
            self._seen.add(identity)
            self._observe_tool("hook", session, tool_id, "pre" if hook == "PreToolUse" else "post")
        elif hook in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            self._issue("missing_tool_identity")
        allowed = (
            "hook_event_name",
            "tool_name",
            "tool_input",
            "tool_response",
            "error",
            "is_interrupt",
            "stop_hook_active",
            "command_name",
            "command_source",
            "command_args",
            "arguments",
            "source",
            "expansion_type",
            "gate",
            "reason_code",
            "prompt",
        )
        projected = {key: payload[key] for key in allowed if key in payload}
        return self._append(
            "native_hook",
            hook,
            projected,
            session_id=session,
            message_id=payload.get("prompt_id"),
            tool_id=tool_id,
        )

    def record_rule(self, declaration: dict) -> dict:
        """Validate an Agent's public claim; references do not prove rule correctness."""
        self._open()
        allowed = {
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
        if not isinstance(declaration, dict) or set(declaration) - allowed:
            raise ValueError("invalid_rule_declaration: unsupported or authoritative fields")
        if (
            not isinstance(declaration.get("rule_id"), str)
            or declaration["rule_id"] not in self._rule_ids
            or not isinstance(declaration.get("phase"), str)
            or declaration["phase"] not in _PHASES
        ):
            raise ValueError("invalid_rule_declaration: unknown rule or phase")
        if any(
            not isinstance(declaration.get(key), str)
            or not declaration[key].strip()
            or len(declaration[key]) > 4000
            for key in ("observation", "decision")
        ):
            raise ValueError(
                "invalid_rule_declaration: concise public observation and decision required"
            )
        evidence = declaration.get("evidence_refs", [])
        related = declaration.get("related_tool_events", [])
        for values in (evidence, related):
            if (
                not isinstance(values, list)
                or len(values) > 100
                or any(not isinstance(ref, str) or not ref or len(ref) > 256 for ref in values)
                or len(values) != len(set(values))
            ):
                raise ValueError("invalid_rule_declaration: invalid evidence references")
        if any(
            key in declaration
            and (not isinstance(declaration[key], str) or len(declaration[key]) > 4000)
            for key in ("branch", "local_result", "change_ref")
        ):
            raise ValueError("invalid_rule_declaration: optional claims must be short text")
        references = list(dict.fromkeys([*evidence, *related]))
        known = {event["event_id"]: event for event in self._events}
        if any(
            ref not in known or known[ref]["producer"] not in {"native_stream", "native_hook"}
            for ref in references
        ) or any(not known[ref]["native_tool_call_id"] for ref in related):
            raise ValueError(
                "invalid_rule_declaration: evidence is outside this run or is only a claim"
            )
        if any(
            known[ref]["native_session_id"] not in (None, self._bound_session_id)
            for ref in references
        ):
            raise ValueError(
                "invalid_rule_declaration: evidence belongs to a foreign native session"
            )
        projected = deepcopy(declaration)
        projected["evidence_refs"] = references
        projected["related_tool_events"] = [
            ref for ref in references if known[ref]["native_tool_call_id"]
        ]
        projected["verification"] = "unreviewed" if references else "declared_only"
        return self._append("agent_declaration", "rule_declaration", projected)

    def finish(self) -> dict:
        """Seal the record; complete collection says nothing about task acceptance."""
        if self._finished is not None:
            return deepcopy(self._finished)
        if self._result is None:
            self._issue("missing_native_result")
        if any(phases != {"pre", "post"} for phases in self._observations.values()):
            self._issue("unpaired_tool_observation")
        stream_actions = {
            (session, tool) for channel, session, tool in self._observations if channel == "stream"
        }
        hook_actions = {
            (session, tool) for channel, session, tool in self._observations if channel == "hook"
        }
        if stream_actions - hook_actions:
            self._issue("missing_tool_hook")
        if hook_actions - stream_actions:
            self._issue("missing_tool_stream")
        actions = {(session, tool) for _, session, tool in self._observations}
        self._finished = {
            "status": "partial" if self._issues else "complete",
            "error_code": "capture_incomplete" if self._issues else None,
            "reasons": list(self._issues),
            "event_count": len(self._events),
            "tool_action_count": len(actions),
            "tool_observation_count": sum(len(phases) for phases in self._observations.values()),
            "native_result": deepcopy(self._result),
            "native_outcome": "unknown"
            if self._result is None
            else "reported_error"
            if self._result.get("is_error")
            else "reported_success",
            "discovered": [
                deepcopy(event) for event in self._events if event["kind"] == "session_init"
            ],
            "native_expansions": [
                deepcopy(event) for event in self._events if event["kind"] == "UserPromptExpansion"
            ],
            "rule_declarations": [
                deepcopy(event) for event in self._events if event["kind"] == "rule_declaration"
            ],
        }
        return deepcopy(self._finished)
