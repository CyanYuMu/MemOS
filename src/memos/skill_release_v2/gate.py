"""Locked, fail-closed host gate for declarations and native file-tool requests.

This module never edits a file or interprets a rule's natural-language meaning.
G1 must call begin_tool and consume_edit in the same blocking PreToolUse hook,
and call complete_tool with an independently verified native return. The injected
verifier is a trusted host component, never an Agent-controlled MCP argument.
Actual CLI blocking and crash-safe journal persistence remain G1 responsibilities.
"""

import hashlib
import json
import os
import stat
import threading

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from memos.exceptions import MemOSError


class GateDeniedError(MemOSError, ValueError):
    """A safe reason code; callers must block the native operation on this error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest_json(value: object) -> str:
    """Digest a canonical JSON value, including the exact normalized tool input."""
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _hash(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 16384


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    attempt_id: str
    session_id: str


@dataclass(frozen=True)
class EditPermit:
    identity: RunIdentity
    tool_use_id: str
    tool_name: str
    input_sha256: str
    path: str
    snapshot: str
    declarations: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    permit_id: str


@dataclass(frozen=True)
class NativeRequest:
    identity: RunIdentity
    tool_use_id: str
    tool_name: str
    input_json: str
    input_sha256: str
    path: str | None
    snapshot: str
    digest: str
    permit: EditPermit | None = None

    @property
    def input(self) -> dict[str, Any]:
        """Return a copy; callers cannot mutate the stored native request."""
        return json.loads(self.input_json)


@dataclass(frozen=True)
class VerifiedResult:
    """Trusted verifier output, bound to the full NativeRequest.digest.

    result_sha256 is the digest of the canonical normalized tool result, after
    native/retained-result equality was checked. is_error preserves transport
    errors; a normal result reporting failing business checks is not an error.
    read_content_delivered is false for a verified unchanged-file marker,
    which must never create full TASK request evidence.
    """

    request_digest: str
    result_sha256: str
    is_error: bool
    read_content_delivered: bool = True


@dataclass(frozen=True)
class Observation:
    reference: str
    tool_use_id: str
    kind: str
    snapshot: str
    result_sha256: str
    is_error: bool
    document_sha256: str | None = None


@dataclass(frozen=True)
class DeclarationReceipt:
    declaration_id: str
    request_digest: str
    input_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "declaration_id": self.declaration_id,
            "request_digest": self.request_digest,
            "input_sha256": self.input_sha256,
            "status": "pending",
        }


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    event: str
    tool_use_id: str | None
    tool_name: str | None
    reason: str | None
    details_json: str
    previous_hash: str
    canonical_json: str
    digest: str


@dataclass(frozen=True)
class GateState:
    identity: RunIdentity
    snapshot: str
    exited: bool
    invalidated: bool
    busy_tool: str | None
    pending_declarations: tuple[str, ...]
    committed_declarations: tuple[str, ...]
    observations: tuple[Observation, ...]
    audit_sequence: int


@dataclass
class _Call:
    request: NativeRequest
    inventory: dict[str, tuple[str, int]]
    status: str = "pending"
    receipt: DeclarationReceipt | None = None
    expected_edit_sha256: str | None = None


class HostGate:
    """A single-run host state machine. All transitions use the same RLock."""

    def __init__(
        self,
        *,
        workspace: Path,
        identity: RunIdentity,
        rules: Mapping[str, tuple[str, ...]],
        entry_rule: str,
        allowed_paths: tuple[str, ...],
        request_path: str,
        verify_result: Callable[[NativeRequest, object], VerifiedResult],
    ):
        if not isinstance(identity, RunIdentity) or not all(
            _text(v) for v in (identity.run_id, identity.attempt_id, identity.session_id)
        ):
            raise GateDeniedError("identity_shape")
        if not callable(verify_result) or not rules or entry_rule not in rules:
            raise GateDeniedError("configuration_shape")
        self._rules = {key: tuple(value) for key, value in rules.items()}
        if any(
            not _text(key)
            or not targets
            or any(
                not isinstance(target, str)
                or (target not in self._rules and not target.startswith("exit:"))
                or target == "exit:"
                for target in targets
            )
            for key, targets in self._rules.items()
        ):
            raise GateDeniedError("rules_shape")
        workspace = Path(workspace)
        if workspace.is_symlink() or not workspace.is_dir():
            raise GateDeniedError("workspace_path")
        self.workspace = workspace.resolve(strict=True)
        self._identity = identity
        self._entry_rule = entry_rule
        self._verifier = verify_result
        self._lock = threading.RLock()
        self._calls: dict[str, _Call] = {}
        self._seen: set[str] = set()
        self._consumed: set[str] = set()
        self._audit: list[AuditEntry] = []
        self._observations: dict[str, Observation] = {}
        self._latest_check: str | None = None
        self._committed: dict[str, NativeRequest] = {}
        self._entry: str | None = None
        self._target: str | None = None
        self._action: str | None = None
        self._active_rule: str | None = None
        self._expected_target: str | None = None
        self._busy: str | None = None
        self._exited = False
        self._invalidated = False
        self._allowed = frozenset(self._path(path) for path in allowed_paths)
        self._request_path = self._path(request_path)
        if (
            self._request_path in self._allowed
            or not (self.workspace / self._request_path).is_file()
        ):
            raise GateDeniedError("request_path")
        self._fixed_inventory = {
            path: value for path, value in self._inventory().items() if path not in self._allowed
        }

    def _append(
        self, event, request=None, *, tool_use_id=None, tool_name=None, reason=None, **details
    ):
        previous = self._audit[-1].digest if self._audit else "0" * 64
        tool_use_id = tool_use_id if isinstance(tool_use_id, str) else None
        tool_name = tool_name if isinstance(tool_name, str) else None
        row = {
            "sequence": len(self._audit) + 1,
            "event": event,
            "tool_use_id": request.tool_use_id if request else tool_use_id,
            "tool_name": request.tool_name if request else tool_name,
            "reason": reason,
            "details_json": _json(details),
            "previous_hash": previous,
        }
        canonical = _json(row)
        self._audit.append(
            AuditEntry(
                **row,
                canonical_json=canonical,
                digest=hashlib.sha256(canonical.encode()).hexdigest(),
            )
        )

    def _deny(self, code):
        raise GateDeniedError(code)

    def _identity_matches(self, identity):
        if identity != self._identity:
            self._deny("identity_mismatch")

    def _live(self):
        if self._invalidated:
            self._deny("invalidated")
        if self._exited:
            self._deny("exited")

    def _path(self, value):
        if not _text(value) or "\x00" in value or ".." in Path(value).parts:
            self._deny("invalid_path")
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.workspace)
            except ValueError:
                self._deny("outside_path")
        if not path.parts or str(path) == ".":
            self._deny("invalid_path")
        current = self.workspace
        for part in path.parts:
            current /= part
            if current.is_symlink():
                self._deny("symlink_path")
        return path.as_posix()

    def _inventory(self):
        result = {}
        try:
            for path in sorted(self.workspace.rglob("*")):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    self._deny("symlink_path")
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    self._deny("unsafe_file_path")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    actual = os.fstat(stream.fileno())
                    if actual.st_ino != info.st_ino or actual.st_dev != info.st_dev:
                        self._deny("file_changed_during_read")
                    content = stream.read()
                result[path.relative_to(self.workspace).as_posix()] = (
                    hashlib.sha256(content).hexdigest(),
                    stat.S_IMODE(actual.st_mode),
                )
        except OSError as exc:
            raise GateDeniedError("workspace_unavailable") from exc
        return result

    def _snapshot(self):
        inventory = self._inventory()
        return digest_json(inventory), inventory

    def _input(self, tool, value):
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            self._deny("input_shape")
        try:
            canonical = _json(value)
        except (ValueError, TypeError, RecursionError) as exc:
            raise GateDeniedError("input_shape") from exc
        if len(canonical.encode()) > 262144:
            self._deny("input_too_large")
        if tool == "record_rule":
            self._declaration_shape(value)
        elif tool == "run_visible_checks":
            if value:
                self._deny("check_input_shape")
        elif tool == "Read":
            if "file_path" not in value or set(value) - {"file_path", "offset", "limit"}:
                self._deny("read_input_shape")
            if any(
                type(value[k]) is not int or value[k] < 1 for k in ("offset", "limit") if k in value
            ):
                self._deny("read_input_shape")
        elif tool == "Edit":
            if not {"file_path", "old_string", "new_string"} <= set(value) or set(value) - {
                "file_path",
                "old_string",
                "new_string",
                "replace_all",
            }:
                self._deny("edit_input_shape")
            if any(not isinstance(value[k], str) for k in ("old_string", "new_string")) or (
                "replace_all" in value and type(value["replace_all"]) is not bool
            ):
                self._deny("edit_input_shape")
        elif tool == "Write":
            if set(value) != {"file_path", "content"} or not isinstance(value["content"], str):
                self._deny("write_input_shape")
        else:
            self._deny("unsupported_tool")
        return canonical

    def begin_tool(
        self, identity: RunIdentity, tool_use_id: str, tool_name: str, tool_input: dict
    ) -> NativeRequest:
        """PreToolUse: register an immutable request; edits additionally need consumption."""
        with self._lock:
            self._append("attempted", tool_use_id=tool_use_id, tool_name=tool_name)
            try:
                self._identity_matches(identity)
                if not _text(tool_use_id):
                    self._deny("tool_use_id_shape")
                if tool_use_id in self._seen:
                    self._deny("duplicate_tool_use_id")
                self._seen.add(tool_use_id)
                canonical = self._input(tool_name, tool_input)
                path = self._path(tool_input["file_path"]) if "file_path" in tool_input else None
                if tool_name != "Read":
                    self._live()
                if tool_name in {"Edit", "Write", "run_visible_checks"} and self._busy:
                    self._deny("operation_in_progress")
                snapshot, inventory = self._snapshot()
                digest = digest_json(
                    {
                        "identity": identity.__dict__,
                        "tool_use_id": tool_use_id,
                        "tool_name": tool_name,
                        "input": canonical,
                        "snapshot": snapshot,
                    }
                )
                request = NativeRequest(
                    identity,
                    tool_use_id,
                    tool_name,
                    canonical,
                    digest_json(tool_input),
                    path,
                    snapshot,
                    digest,
                )
                if tool_name in {"Edit", "Write"}:
                    if path not in self._allowed:
                        self._deny("protected_path")
                    declarations, refs = self._edit_evidence(snapshot)
                    permit = EditPermit(
                        identity,
                        tool_use_id,
                        tool_name,
                        request.input_sha256,
                        path,
                        snapshot,
                        declarations,
                        refs,
                        digest_json(
                            {"request": digest, "declarations": declarations, "evidence": refs}
                        ),
                    )
                    request = replace(request, permit=permit)
                expected = self._expected_edit(request, inventory) if request.permit else None
                self._calls[tool_use_id] = _Call(request, inventory, expected_edit_sha256=expected)
                if tool_name == "run_visible_checks":
                    # A new check supersedes the old observation immediately. If
                    # its delivery is lost, an earlier success must never revive.
                    self._latest_check = None
                if tool_name in {"Edit", "Write", "run_visible_checks"}:
                    self._busy = tool_use_id
                self._append("permitted", request, request_digest=digest, snapshot=snapshot)
                return request
            except GateDeniedError as exc:
                self._append(
                    "denied", tool_use_id=tool_use_id, tool_name=tool_name, reason=exc.code
                )
                raise

    def _declaration_shape(self, value):
        if not isinstance(value, dict) or set(value) != {
            "rule_id",
            "phase",
            "branch_index",
            "observation",
            "decision",
            "evidence_refs",
        }:
            self._deny("declaration_shape")
        if (
            not isinstance(value["rule_id"], str)
            or not isinstance(value["phase"], str)
            or value["rule_id"] not in self._rules
            or value["phase"] not in {"select", "act", "check", "exit"}
        ):
            self._deny("declaration_shape")
        if not all(_text(value[k]) for k in ("observation", "decision")):
            self._deny("declaration_shape")
        branch = value["branch_index"]
        if branch is not None and (
            type(branch) is not int or not 0 <= branch < len(self._rules[value["rule_id"]])
        ):
            self._deny("branch_index")
        refs = value["evidence_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or len(refs) > 128
            or not all(_text(ref) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            self._deny("evidence_shape")

    def _expected_edit(self, request, inventory):
        value = request.input
        if request.tool_name == "Write":
            content = value["content"]
        else:
            if request.path not in inventory:
                return None
            raw = (self.workspace / request.path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != inventory[request.path][0]:
                self._deny("stale_snapshot")
            try:
                original = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
            old = value["old_string"]
            count = original.count(old) if old else 0
            if not count or (count != 1 and not value.get("replace_all", False)):
                return None
            content = original.replace(old, value["new_string"])
        return hashlib.sha256(content.encode()).hexdigest()

    def _evidence(self, refs, snapshot):
        if any(ref not in self._observations for ref in refs):
            self._deny("unknown_evidence")
        values = [self._observations[ref] for ref in refs]
        current_request = self._inventory().get(self._request_path)
        for item in values:
            if item.kind == "request":
                if not current_request or item.document_sha256 != current_request[0]:
                    self._deny("stale_evidence")
            elif item.snapshot != snapshot:
                self._deny("stale_evidence")
        return values

    def _validate_declaration(self, request):
        self._live()
        if self._busy:
            self._deny("operation_in_progress")
        value = request.input
        snapshot, _ = self._snapshot()
        if request.snapshot != snapshot:
            self._deny("stale_snapshot")
        observations = self._evidence(value["evidence_refs"], snapshot)
        checks = [o for o in observations if o.kind == "check" and o.snapshot == snapshot]
        if not any(o.reference == self._latest_check for o in checks):
            self._deny("current_check_required")
        phase, rule, branch = value["phase"], value["rule_id"], value["branch_index"]
        if phase == "select" and rule == self._entry_rule:
            if branch is None:
                self._deny("branch_index")
            if not any(o.kind == "request" and not o.is_error for o in observations):
                self._deny("request_observation_missing")
            if not checks:
                self._deny("current_check_required")
        elif phase == "select":
            if self._expected_target != rule or self._entry is None or branch is not None:
                self._deny("target_selection_required")
            if not checks:
                self._deny("current_check_required")
        else:
            if rule != self._active_rule or (rule != self._entry_rule and self._target is None):
                self._deny("target_selection_required")
            if not checks:
                self._deny("current_check_required")
            if phase == "act" and self._observations[self._latest_check].is_error:
                self._deny("successful_check_required")
            if phase == "exit" and (
                branch is None or not self._rules[rule][branch].startswith("exit:")
            ):
                self._deny("exit_branch_required")

    def receive_declaration(self, value: dict) -> DeclarationReceipt:
        """MCP receive: match only one native input; the returned receipt is pending."""
        with self._lock:
            try:
                self._declaration_shape(value)
                fingerprint = digest_json(value)
                matches = [
                    call
                    for call in self._calls.values()
                    if call.status == "pending"
                    and call.receipt is None
                    and call.request.tool_name == "record_rule"
                    and call.request.input_sha256 == fingerprint
                ]
                if len(matches) != 1:
                    self._deny("ambiguous_declaration" if matches else "native_request_missing")
                call = matches[0]
                self._validate_declaration(call.request)
                receipt = DeclarationReceipt(
                    "declaration:" + call.request.digest, call.request.digest, fingerprint
                )
                call.receipt = receipt
                self._append("pending", call.request, declaration_id=receipt.declaration_id)
                return receipt
            except GateDeniedError as exc:
                self._append("denied", tool_name="record_rule", reason=exc.code)
                raise

    def _edit_evidence(self, snapshot):
        protected = {
            path: value for path, value in self._inventory().items() if path not in self._allowed
        }
        if protected != self._fixed_inventory:
            self._invalidated = True
            self._deny("protected_workspace_changed")
        if not all((self._entry, self._target, self._action)):
            self._deny("declaration_required")
        action = self._committed[self._action]
        refs = tuple(action.input["evidence_refs"])
        checks = [
            self._observations[ref]
            for ref in refs
            if ref in self._observations
            and self._observations[ref].kind == "check"
            and self._observations[ref].snapshot == snapshot
        ]
        if not checks or self._latest_check not in refs:
            self._deny("current_check_required")
        self._evidence(refs, snapshot)
        if self._observations[self._latest_check].is_error:
            self._deny("successful_check_required")
        if action.snapshot != snapshot:
            self._deny("action_required")
        return (self._entry, self._target, self._action), refs

    def consume_edit(
        self, permit: EditPermit, identity: RunIdentity, tool_use_id: str, tool_input: dict
    ) -> None:
        """Consume once immediately before invoking the permitted native file tool."""
        with self._lock:
            request = self._calls.get(tool_use_id)
            native = request.request if request else None
            try:
                self._identity_matches(identity)
                self._live()
                if not isinstance(permit, EditPermit):
                    self._deny("permit_mismatch")
                if permit.permit_id in self._consumed:
                    self._deny("permit_consumed")
                if not request or request.status != "pending" or native.permit != permit:
                    self._deny("permit_mismatch")
                if digest_json(tool_input) != permit.input_sha256:
                    self._deny("input_mismatch")
                self._path(tool_input["file_path"])
                snapshot, _ = self._snapshot()
                if snapshot != permit.snapshot:
                    self._deny("stale_snapshot")
                if self._busy != tool_use_id or self._edit_evidence(snapshot) != (
                    permit.declarations,
                    permit.evidence_refs,
                ):
                    self._deny("permit_mismatch")
                self._consumed.add(permit.permit_id)
                self._append("consumed", native, permit_id=permit.permit_id)
            except GateDeniedError as exc:
                self._append("denied", native, tool_use_id=tool_use_id, reason=exc.code)
                raise

    def _commit(self, call):
        self._validate_declaration(call.request)
        receipt = call.receipt
        value = call.request.input
        identity = receipt.declaration_id
        self._committed[identity] = call.request
        if value["phase"] == "select" and value["rule_id"] == self._entry_rule:
            self._entry, self._target, self._action = identity, None, None
            self._active_rule = self._entry_rule
            self._expected_target = self._rules[self._entry_rule][value["branch_index"]]
        elif value["phase"] == "select":
            self._target, self._action = identity, None
            self._active_rule = value["rule_id"]
        elif value["phase"] == "act":
            self._action = identity
        elif value["phase"] == "exit":
            self._exited = True
        self._append("committed", call.request, declaration_id=identity)
        return receipt

    def complete_tool(
        self, identity: RunIdentity, tool_use_id: str, native_result: object
    ) -> Observation | DeclarationReceipt | None:
        """PostToolUse: verify native/host equality before recording any observation."""
        with self._lock:
            call = self._calls.get(tool_use_id)
            try:
                self._identity_matches(identity)
                if not call or call.status != "pending":
                    self._deny("request_not_pending")
                request = call.request
                snapshot, inventory = self._snapshot()
                changed = sorted(
                    k
                    for k in set(inventory) | set(call.inventory)
                    if inventory.get(k) != call.inventory.get(k)
                )
                if request.tool_name in {"Edit", "Write"}:
                    if request.permit.permit_id not in self._consumed:
                        self._deny("permit_not_consumed")
                    if set(changed) - {request.path}:
                        self._invalidated = True
                        self._deny("unexpected_file_change")
                elif snapshot != request.snapshot:
                    self._deny("stale_snapshot")
                try:
                    verified = self._verifier(request, native_result)
                except Exception as exc:
                    raise GateDeniedError("unverified_result") from exc
                if (
                    not isinstance(verified, VerifiedResult)
                    or verified.request_digest != request.digest
                    or not _hash(verified.result_sha256)
                    or type(verified.is_error) is not bool
                    or type(verified.read_content_delivered) is not bool
                ):
                    self._deny("receipt_mismatch")
                if request.permit and not verified.is_error:
                    current = inventory.get(request.path)
                    original = call.inventory.get(request.path)
                    if (
                        not call.expected_edit_sha256
                        or not current
                        or current[0] != call.expected_edit_sha256
                        or (original and original[1] != current[1])
                    ):
                        self._invalidated = True
                        self._deny("edit_result_mismatch")
                result = None
                if request.tool_name == "record_rule":
                    if (
                        call.receipt is None
                        or verified.is_error
                        or verified.result_sha256 != digest_json(call.receipt.as_dict())
                    ):
                        self._deny("receipt_mismatch")
                    result = self._commit(call)
                elif request.tool_name in {"Read", "run_visible_checks"}:
                    kind = "check" if request.tool_name == "run_visible_checks" else "read"
                    full_request = request.path == self._request_path and not (
                        set(request.input) & {"offset", "limit"}
                    )
                    if (
                        kind == "read"
                        and full_request
                        and not verified.is_error
                        and verified.read_content_delivered
                    ):
                        kind = "request"
                    result = Observation(
                        "observation:" + request.digest,
                        tool_use_id,
                        kind,
                        snapshot,
                        verified.result_sha256,
                        verified.is_error,
                        inventory[self._request_path][0] if kind == "request" else None,
                    )
                    self._observations[result.reference] = result
                    if kind == "check":
                        self._latest_check = result.reference
                call.status = "failed" if verified.is_error else "completed"
                if self._busy == tool_use_id:
                    self._busy = None
                self._append(
                    "failed" if verified.is_error else "executed",
                    request,
                    changed_paths=changed,
                    result_sha256=verified.result_sha256,
                    is_error=verified.is_error,
                    **(
                        {"read_content_delivered": verified.read_content_delivered}
                        if request.tool_name == "Read"
                        else {}
                    ),
                )
                return result
            except GateDeniedError as exc:
                if call and call.status == "pending":
                    if call.request.tool_name == "run_visible_checks" or (
                        call.request.permit and call.request.permit.permit_id in self._consumed
                    ):
                        self._invalidated = True
                    call.status = "failed"
                    if self._busy == tool_use_id:
                        self._busy = None
                self._append(
                    "failed",
                    call.request if call else None,
                    tool_use_id=tool_use_id,
                    reason=exc.code,
                )
                raise

    def abort_tool(self, identity: RunIdentity, tool_use_id: str, reason: str) -> None:
        """Record missing/failed hooks without inventing native execution or delivery."""
        with self._lock:
            self._identity_matches(identity)
            call = self._calls.get(tool_use_id)
            if not call or call.status != "pending":
                self._deny("request_not_pending")
            snapshot, inventory = self._snapshot()
            changed = sorted(
                k
                for k in set(inventory) | set(call.inventory)
                if inventory.get(k) != call.inventory.get(k)
            )
            call.status = "failed"
            if self._busy == tool_use_id:
                self._busy = None
            if (
                changed
                or call.request.tool_name == "run_visible_checks"
                or (call.request.permit and call.request.permit.permit_id in self._consumed)
            ):
                self._invalidated = True
            self._append(
                "failed", call.request, reason=reason, changed_paths=changed, snapshot=snapshot
            )

    def invalidate(self, reason: str) -> None:
        with self._lock:
            self._invalidated = True
            self._append("failed", reason=reason)

    def state(self) -> GateState:
        with self._lock:
            snapshot, _ = self._snapshot()
            pending = tuple(
                call.receipt.declaration_id
                for call in self._calls.values()
                if call.status == "pending" and call.receipt
            )
            return GateState(
                self._identity,
                snapshot,
                self._exited,
                self._invalidated,
                self._busy,
                pending,
                tuple(self._committed),
                tuple(self._observations.values()),
                len(self._audit),
            )

    def audit(self) -> tuple[AuditEntry, ...]:
        with self._lock:
            return tuple(self._audit)
