"""Protocol-shaped transport fixtures; no private batch artifacts are committed."""

import hashlib
import json

import pytest

from memos.exceptions import MemOSError
from memos.skill_release_v2.capture import (
    ToolResultError,
    normalize_tool_result,
    verify_tool_delivery,
)


TOOL_ID = "toolu_protocol_capture"
FAULT = {
    "exit_code": None,
    "stdout": "",
    "stderr": "",
    "timed_out": False,
    "execution_error": "registered_check_unavailable",
    "evidence_ref": {"check_index": 2, "candidate_snapshot": "a" * 64},
}


def payload(result=None, *, error=False, string=False, text=None):
    if text is None:
        text = json.dumps({"status": "passed"} if result is None else result)
    return {
        "tool_use_id": TOOL_ID,
        "is_error": error,
        "tool_result": text if string else [{"type": "text", "text": text}],
    }


def normalize(value, **kwargs):
    return normalize_tool_result(value, expected_tool_use_id=TOOL_ID, **kwargs)


@pytest.mark.parametrize("error,string", [(False, False), (True, False), (True, True)])
def test_success_and_error_wrappers_retain_error_and_original_representation(error, string):
    original = payload(FAULT if error else {"status": "passed"}, error=error, string=string)
    normalized = normalize(original)
    assert normalized.tool_use_id == TOOL_ID
    assert normalized.is_error is error
    assert normalized.representation == ("error_json_string" if string else "text_blocks")
    assert json.loads(normalized.raw_envelope_json) == original
    assert (
        normalized.raw_envelope_sha256
        == hashlib.sha256(normalized.raw_envelope_json.encode("utf-8")).hexdigest()
    )
    assert normalized.text_sha256 == hashlib.sha256(normalized.text.encode("utf-8")).hexdigest()
    assert normalized.result_sha256 == hashlib.sha256(normalized.result_json.encode()).hexdigest()
    assert normalized.result == (FAULT if error else {"status": "passed"})


def test_original_json_text_is_retained_while_parsed_values_are_canonicalized():
    first = normalize(payload(text=' { "b": 2, "a": 1 }\n'))
    second = normalize(payload(text='{"a":1,"b":2}'))
    assert first.result_json == second.result_json
    assert first.result_sha256 == second.result_sha256
    assert first.text != second.text
    assert first.text_sha256 != second.text_sha256
    assert first.raw_envelope_sha256 != second.raw_envelope_sha256


def test_caller_mutation_does_not_change_retained_result_or_hashes():
    original = payload({"nested": {"values": [1]}})
    normalized = normalize(original)
    expected = normalized.raw_envelope_json, normalized.result_json, normalized.result_sha256
    original["tool_result"][0]["text"] = "forged"
    normalized.result["nested"]["values"].append(2)
    assert normalized.result == {"nested": {"values": [1]}}
    assert (
        normalized.raw_envelope_json,
        normalized.result_json,
        normalized.result_sha256,
    ) == expected


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(actor="forged"),
        lambda p: p.pop("is_error"),
        lambda p: p.update(is_error=1),
        lambda p: p.update(is_error=None),
        lambda p: p.update(tool_use_id=None),
        lambda p: p.update(tool_use_id=""),
        lambda p: p.update(tool_use_id="contains newline\n"),
    ],
)
def test_envelope_requires_exact_fields_and_types(change):
    original = payload()
    change(original)
    with pytest.raises(ToolResultError, match="invalid_tool_result_envelope"):
        normalize(original)


@pytest.mark.parametrize("value", [None, [], "raw", True])
def test_non_mapping_envelope_is_rejected(value):
    with pytest.raises(ToolResultError, match="invalid_tool_result_envelope"):
        normalize(value)


@pytest.mark.parametrize("tool_id", [None, 1, False, "", " ", "x" * 301])
def test_expected_tool_identity_must_be_valid(tool_id):
    with pytest.raises(ToolResultError, match="invalid_expected_tool_use_id"):
        normalize_tool_result(payload(), expected_tool_use_id=tool_id)


def test_mismatched_native_tool_call_cannot_be_bound_to_another_call():
    with pytest.raises(ToolResultError, match="tool_use_id_mismatch"):
        normalize_tool_result(payload(), expected_tool_use_id="toolu_other")


@pytest.mark.parametrize(
    "representation",
    [
        "{}",  # Success-string is not one of the observed supported envelopes.
        [],
        [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}],
        [{"type": "text", "text": "{}"}, {"type": "image", "data": "x"}],
        [{"type": "text", "text": "{}", "unregistered": True}],
        [{"type": "text", "text": {}}],
        [{"type": "image", "text": "{}"}],
        {"type": "text", "text": "{}"},
        None,
        True,
    ],
)
def test_ambiguous_mixed_or_unknown_wrapper_is_rejected(representation):
    original = payload()
    original["tool_result"] = representation
    with pytest.raises(ToolResultError, match="invalid_tool_result_representation"):
        normalize(original)


@pytest.mark.parametrize(
    "text",
    [
        '{"status":"passed","status":"failed"}',
        '{"nested":{"same":1,"same":2}}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e999}',
        '{"value":-1e999}',
        '{"status":"passed"} trailing',
        '"{}"',
        "[]",
        "null",
        "false",
        "",
    ],
)
def test_invalid_or_non_object_json_does_not_become_an_observation(text):
    with pytest.raises(ToolResultError):
        normalize(payload(error=True, string=True, text=text))


def test_excessive_depth_and_invalid_unicode_fail_with_safe_errors():
    nested = '{"x":' * 70 + "null" + "}" * 70
    for text in (nested, '{"x":"\ud800"}'):
        with pytest.raises(ToolResultError) as error:
            normalize(payload(text=text))
        assert str(error.value).startswith("invalid_tool_result_")
        assert text not in str(error.value)


@pytest.mark.parametrize("limit", [None, False, 0, -1, 1.5, "100"])
def test_invalid_size_limit_is_rejected(limit):
    with pytest.raises(ToolResultError, match="invalid_tool_result_limit"):
        normalize(payload(), max_bytes=limit)


def test_size_limit_counts_utf8_bytes_of_the_retained_whole_envelope():
    original = payload({"message": "验证"})
    normalized = normalize(original)
    size = len(normalized.raw_envelope_json.encode("utf-8"))
    assert normalize(original, max_bytes=size) == normalized
    with pytest.raises(ToolResultError, match="tool_result_too_large"):
        normalize(original, max_bytes=size - 1)


@pytest.mark.parametrize("error,string", [(False, False), (True, True), (True, False)])
def test_host_delivery_verification_is_separate_from_business_success(error, string):
    # A field named 'passed' does not turn a native tool error into success.
    result = {"status": "passed", "valid": True}
    verified = verify_tool_delivery(
        payload(result, error=error, string=string),
        expected_tool_use_id=TOOL_ID,
        expected_is_error=error,
        host_result=result,
    )
    assert verified.is_error is error
    assert verified.result == result
    assert not hasattr(verified, "task_success")


@pytest.mark.parametrize("host_result", [{"status": "failed"}, {"status": "passed", "extra": 1}])
def test_host_result_mismatch_is_not_delivery(host_result):
    with pytest.raises(ToolResultError, match="tool_result_delivery_mismatch"):
        verify_tool_delivery(
            payload(),
            expected_tool_use_id=TOOL_ID,
            expected_is_error=False,
            host_result=host_result,
        )


@pytest.mark.parametrize("expected", [True, 0, None])
def test_host_error_flag_must_match_exact_boolean(expected):
    with pytest.raises(ToolResultError):
        verify_tool_delivery(
            payload(),
            expected_tool_use_id=TOOL_ID,
            expected_is_error=expected,
            host_result={"status": "passed"},
        )


@pytest.mark.parametrize("received,expected", [(False, 0), (True, 1), (1, 1.0)])
def test_json_types_do_not_collapse_under_python_equality(received, expected):
    assert received == expected  # The hazard the canonical comparison must avoid.
    with pytest.raises(ToolResultError, match="tool_result_delivery_mismatch"):
        verify_tool_delivery(
            payload({"value": received}),
            expected_tool_use_id=TOOL_ID,
            expected_is_error=False,
            host_result={"value": expected},
        )


@pytest.mark.parametrize(
    "host_result", [None, [], {1: "integer-key"}, {"x": float("nan")}, {"x": (1, 2)}]
)
def test_host_result_must_itself_be_strict_json(host_result):
    with pytest.raises(ToolResultError, match="invalid_host_result"):
        verify_tool_delivery(
            payload(),
            expected_tool_use_id=TOOL_ID,
            expected_is_error=False,
            host_result=host_result,
        )


def test_cyclic_host_result_and_sensitive_json_errors_are_not_echoed():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ToolResultError, match="invalid_host_result"):
        verify_tool_delivery(
            payload(), expected_tool_use_id=TOOL_ID, expected_is_error=False, host_result=cyclic
        )
    with pytest.raises(ToolResultError) as error:
        normalize(payload(text='{"secret":"DO_NOT_ECHO",broken}'))
    assert isinstance(error.value, MemOSError)
    assert "DO_NOT_ECHO" not in str(error.value)
    assert error.value.code == str(error.value)
