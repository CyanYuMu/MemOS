"""Bind native deliveries to retained checks, including genuine MCP errors."""

import json

from memos.skill_adapter.events import sanitize_public


def _visible_deliveries(records, events, secret_values):
    """Bind each retained check to the result delivered by the native tool call.

    A host record alone proves execution, not that the Agent received it. The
    pinned MCP transport returns one JSON text block; prose claims do not count.
    """
    pending, completed, deliveries = {}, set(), []

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_delivery_field")
            result[key] = value
        return result

    try:
        for event in events:
            if event["producer"] != "native_stream":
                continue
            payload = event["payload"]
            identity = (event["native_session_id"], event["native_tool_call_id"])
            if (
                event["kind"] == "tool_started"
                and payload.get("tool_name") == "mcp__memos_run__run_visible_checks"
            ):
                if (
                    not all(isinstance(part, str) and part for part in identity)
                    or identity in pending
                    or identity in completed
                    or payload.get("tool_input") != {}
                ):
                    return None
                pending[identity] = event["event_id"]
            elif event["kind"] == "tool_completed" and identity in pending:
                content = payload.get("tool_result")
                if isinstance(content, str):
                    delivered = json.loads(content, object_pairs_hook=unique)
                elif (
                    isinstance(content, list)
                    and len(content) == 1
                    and isinstance(content[0], dict)
                    and set(content[0]) == {"type", "text"}
                    and content[0]["type"] == "text"
                ):
                    delivered = json.loads(content[0]["text"], object_pairs_hook=unique)
                else:
                    return None
                if not isinstance(delivered, dict):
                    return None
                # An actual transport error must match a retained execution error.
                # Business test failures have exit_code=1 and is_error=False.
                if payload.get("is_error") is not bool(delivered.get("execution_error")):
                    return None
                index = delivered["evidence_ref"]["check_index"]
                if type(index) is not int or not 1 <= index <= len(records):
                    return None
                record = records[index - 1]
                if (
                    record["check_index"] != index
                    or any(d["check_index"] == index for d in deliveries)
                    or delivered != sanitize_public(record["result"], secret_values)
                ):
                    return None
                deliveries.append(
                    {
                        "check_index": index,
                        "native_tool_call_id": identity[1],
                        "started_event": pending.pop(identity),
                        "completed_event": event["event_id"],
                    }
                )
                completed.add(identity)
            elif event["kind"] == "tool_completed" and identity in completed:
                return None
        if pending or len(deliveries) != len(records):
            return None
        return sorted(deliveries, key=lambda row: row["check_index"])
    except (KeyError, ValueError, TypeError, RecursionError):
        return None
