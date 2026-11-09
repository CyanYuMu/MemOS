"""Transport evidence must distinguish a failed test from a failed tool."""

import json

from copy import deepcopy

import pytest

from memos.skill_p5.delivery import _visible_deliveries


def sample(*, error=False, string=False):
    result = {
        "exit_code": None if error else 1,
        "execution_error": "registered_check_unavailable" if error else None,
        "evidence_ref": {"check_index": 1, "candidate_snapshot": "a" * 64},
    }
    common = {
        "producer": "native_stream",
        "native_session_id": "session",
        "native_tool_call_id": "call",
    }
    content = json.dumps(result)
    events = [
        {
            **common,
            "event_id": "start",
            "kind": "tool_started",
            "payload": {"tool_name": "mcp__memos_run__run_visible_checks", "tool_input": {}},
        },
        {
            **common,
            "event_id": "end",
            "kind": "tool_completed",
            "payload": {
                "is_error": error,
                "tool_result": content if string else [{"type": "text", "text": content}],
            },
        },
    ]
    return [{"check_index": 1, "result": result}], events


@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("string", [False, True])
def test_actual_native_shapes_bind_the_same_retained_result(error, string):
    records, events = sample(error=error, string=string)
    assert _visible_deliveries(records, events, ()) == [
        {
            "check_index": 1,
            "native_tool_call_id": "call",
            "started_event": "start",
            "completed_event": "end",
        }
    ]


@pytest.mark.parametrize("error", [False, True])
def test_error_bit_cannot_be_changed_or_coerced(error):
    records, events = sample(error=error, string=True)
    for bit in [not error, int(error), None]:
        events[-1]["payload"]["is_error"] = bit
        assert _visible_deliveries(records, events, ()) is None


@pytest.mark.parametrize("change", ["duplicate", "missing", "wrong_call", "replayed", "record"])
def test_missing_replayed_or_forged_delivery_does_not_prove_execution(change):
    records, events = sample(error=True, string=True)
    if change == "duplicate":
        events[-1]["payload"]["tool_result"] = '{"evidence_ref":{},"evidence_ref":{}}'
    elif change == "missing":
        events.pop()
    elif change == "wrong_call":
        events[-1]["native_tool_call_id"] = "other"
    elif change == "replayed":
        events.append(deepcopy(events[-1]))
    else:
        records[0]["result"]["execution_error"] = "another_error"
    assert _visible_deliveries(records, events, ()) is None
