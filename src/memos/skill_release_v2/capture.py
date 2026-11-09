"""Strict normalization of host-extracted MCP check/declaration results.

This is not a parser for arbitrary native tools: a Read result, for example,
may legitimately contain free text. Nor does successful delivery imply that
the returned check, the task, or the Skill succeeded.
"""

import hashlib
import json
import math

from dataclasses import dataclass, field
from typing import Any, Literal

from memos.exceptions import MemOSError


_MAX_DEPTH = 64
_ENVELOPE_KEYS = {"tool_use_id", "is_error", "tool_result"}


class ToolResultError(MemOSError, ValueError):
    """A safe error code; result text and private host data are never echoed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NormalizedToolResult:
    """Retained representation and parsed JSON, without a business verdict.

    ``raw_envelope_json`` is the canonical serialization of the extracted
    envelope, including its unmodified contained text. Its hash is NOT a hash
    of original native stdin bytes. Immutable strings avoid later mutation of
    the caller's dictionaries invalidating the retained hashes.
    """

    tool_use_id: str
    is_error: bool
    representation: Literal["text_blocks", "error_json_string"]
    raw_envelope_json: str = field(repr=False)
    raw_envelope_sha256: str
    text: str = field(repr=False)
    text_sha256: str
    result_json: str = field(repr=False)
    result_sha256: str

    @property
    def result(self) -> dict[str, Any]:
        """Return a fresh value; mutations cannot change the retained result."""
        return json.loads(self.result_json)


def _tool_id_valid(value: object) -> bool:
    if type(value) is not str or not 0 < len(value) <= 300:
        return False
    return not any(
        char.isspace() or ord(char) < 33 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    )


def _validate_json(value: object, depth: int = 0) -> None:
    """Reject Python conveniences (tuples, integer keys, NaN) and deep cycles."""
    if depth > _MAX_DEPTH:
        raise ValueError("depth")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non_finite")
    elif type(value) is list:
        for item in value:
            _validate_json(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("key_type")
            key.encode("utf-8")
            _validate_json(item, depth + 1)
    else:
        raise ValueError("value_type")


def _canonical_json(value: object, *, code: str) -> str:
    try:
        _validate_json(value)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise ToolResultError(code) from None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non_finite")


def _parse_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
        if type(value) is not dict:
            raise ValueError("non_object")
        # This also catches finite JSON syntax (1e999) that decodes to infinity.
        _validate_json(value)
        return value
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise ToolResultError("invalid_tool_result_json") from None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_tool_result(
    payload: object, *, expected_tool_use_id: str, max_bytes: int = 262144
) -> NormalizedToolResult:
    """Validate the exact extracted envelope and retain native error identity.

    Accepted forms are a single JSON text block, or a JSON string when the
    native error flag is true. ``max_bytes`` bounds the UTF-8 serialization of
    the whole retained envelope, not merely the decoded result.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ToolResultError("invalid_tool_result_limit")
    if not _tool_id_valid(expected_tool_use_id):
        raise ToolResultError("invalid_expected_tool_use_id")
    if (
        type(payload) is not dict
        or payload.keys() != _ENVELOPE_KEYS
        or type(payload["is_error"]) is not bool
        or not _tool_id_valid(payload["tool_use_id"])
    ):
        raise ToolResultError("invalid_tool_result_envelope")
    if payload["tool_use_id"] != expected_tool_use_id:
        raise ToolResultError("tool_use_id_mismatch")

    raw = payload["tool_result"]
    if type(raw) is str and payload["is_error"]:
        text = raw
        representation = "error_json_string"
    elif (
        type(raw) is list
        and len(raw) == 1
        and type(raw[0]) is dict
        and raw[0].keys() == {"type", "text"}
        and raw[0]["type"] == "text"
        and type(raw[0]["text"]) is str
    ):
        text = raw[0]["text"]
        representation = "text_blocks"
    else:
        raise ToolResultError("invalid_tool_result_representation")
    if len(text) > max_bytes:
        raise ToolResultError("tool_result_too_large")
    envelope_json = _canonical_json(payload, code="invalid_tool_result_envelope")
    if len(envelope_json.encode("utf-8")) > max_bytes:
        raise ToolResultError("tool_result_too_large")
    result_json = _canonical_json(_parse_object(text), code="invalid_tool_result_json")
    return NormalizedToolResult(
        tool_use_id=payload["tool_use_id"],
        is_error=payload["is_error"],
        representation=representation,
        raw_envelope_json=envelope_json,
        raw_envelope_sha256=_digest(envelope_json),
        text=text,
        text_sha256=_digest(text),
        result_json=result_json,
        result_sha256=_digest(result_json),
    )


def verify_tool_delivery(
    payload: object,
    *,
    expected_tool_use_id: str,
    expected_is_error: bool,
    host_result: object,
    max_bytes: int = 262144,
) -> NormalizedToolResult:
    """Bind an observed return to host-retained data, not to a task verdict.

    The caller must bind this native call to the current run/session. This
    pure function checks call ID, error flag, and exact JSON values and types.
    """
    normalized = normalize_tool_result(
        payload, expected_tool_use_id=expected_tool_use_id, max_bytes=max_bytes
    )
    if type(expected_is_error) is not bool or normalized.is_error is not expected_is_error:
        raise ToolResultError("tool_result_error_mismatch")
    if type(host_result) is not dict:
        raise ToolResultError("invalid_host_result")
    host_json = _canonical_json(host_result, code="invalid_host_result")
    # Dict equality would silently identify false with 0, and 1 with 1.0.
    if normalized.result_json != host_json:
        raise ToolResultError("tool_result_delivery_mismatch")
    return normalized
