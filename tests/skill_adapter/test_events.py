"""Only observable public evidence may be persisted or promoted into a claim."""

import json

import pytest

from memos.skill_adapter.events import EventRecorder, sanitize_public


def recorder(tmp_path, **kwargs):
    return EventRecorder(
        tmp_path / "events.jsonl",
        {"run_id": "run-1", "attempt_id": "attempt-1", "split": "dev", "skill_revision": "1"},
        rule_ids=("R1", "R2"),
        **kwargs,
    )


def start(tool_id="tool-1"):
    return {
        "type": "assistant",
        "uuid": "msg-1",
        "session_id": "native-session",
        "message": {
            "id": "native-message",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": "python -B test.py"},
                },
            ],
        },
    }


def complete(tool_id="tool-1", *, error=False):
    return {
        "type": "user",
        "uuid": "msg-2",
        "session_id": "native-session",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": "passed",
                    "is_error": error,
                }
            ]
        },
    }


def result(error=False):
    return {
        "type": "result",
        "session_id": "native-session",
        "subtype": "success" if not error else "error_max_turns",
        "is_error": error,
        "result": "Task complete",
        "total_cost_usd": 0.02,
    }


def paired_hooks(collector, tool_id="tool-1", session="native-session", error=False):
    for event in ("PreToolUse", "PostToolUseFailure" if error else "PostToolUse"):
        collector.ingest_hook(
            {
                "hook_event_name": event,
                "session_id": session,
                "tool_use_id": tool_id,
                "tool_name": "Bash",
            }
        )


def test_native_action_and_hooks_are_observations_of_one_action(tmp_path):
    collector = recorder(tmp_path)
    collector.ingest_stream(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "native-session",
            "slash_commands": ["fixed-r1"],
            "tools": ["Bash"],
            "model": "test-model",
        }
    )
    collector.ingest_stream(start())
    for event in ("PreToolUse", "PostToolUse"):
        collector.ingest_hook(
            {
                "hook_event_name": event,
                "session_id": "native-session",
                "tool_use_id": "tool-1",
                "tool_name": "Bash",
            }
        )
    collector.ingest_stream(complete())
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "complete"
    assert report["tool_action_count"] == 1
    assert report["tool_observation_count"] == 4
    assert report["native_result"]["total_cost_usd"] == 0.02
    assert all(event["context"]["run_id"] == "run-1" for event in collector.events)
    assert [event["sequence"] for event in collector.events] == list(
        range(1, len(collector.events) + 1)
    )


def test_private_thinking_and_secrets_never_reach_disk(tmp_path):
    collector = recorder(tmp_path, secret_values=("live-secret-value",))
    collector.ingest_stream(
        {
            "type": "assistant",
            "session_id": "session",
            "message": {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "PRIVATE-THOUGHT",
                        "signature": "PRIVATE-SIGNATURE",
                    },
                    {"type": "redacted_thinking", "data": "PRIVATE-REDACTED"},
                    {
                        "type": "text",
                        "text": "Token live-secret-value and Authorization: Bearer abc123",
                    },
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "Bash",
                        "input": {
                            "api_key": "SECRET-KEY",
                            "nested": {"authorization": "SECRET-HEADER"},
                            "command": "echo live-secret-value",
                        },
                    },
                ]
            },
        }
    )
    collector.ingest_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_use_id": "t",
            "tool_response": {"password": "SECRET-PASSWORD", "stdout": "live-secret-value"},
            "thinking": "PRIVATE-HOOK",
        }
    )
    content = (tmp_path / "events.jsonl").read_text()
    for value in (
        "PRIVATE-THOUGHT",
        "PRIVATE-SIGNATURE",
        "PRIVATE-REDACTED",
        "PRIVATE-HOOK",
        "live-secret-value",
        "abc123",
        "SECRET-KEY",
        "SECRET-HEADER",
        "SECRET-PASSWORD",
    ):
        assert value not in content
    assert "[REDACTED]" in content


def test_deduplicate_native_message_without_inventing_native_ids(tmp_path):
    collector = recorder(tmp_path)
    first = collector.ingest_stream(start())
    assert collector.ingest_stream(start()) == []
    assert len(collector.events) == len(first)
    public = collector.ingest_stream(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "observation"}]}}
    )[0]
    assert public["native_session_id"] is None
    assert public["native_tool_call_id"] is None


@pytest.mark.parametrize("missing", ["post", "pre", "result"])
def test_unpaired_native_tool_or_missing_result_is_partial(tmp_path, missing):
    collector = recorder(tmp_path)
    if missing != "pre":
        collector.ingest_stream(start())
    if missing != "post":
        collector.ingest_stream(complete())
    if missing != "result":
        collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "partial"
    assert report["error_code"] == "capture_incomplete"


def test_failed_tool_and_failed_agent_result_are_complete_evidence(tmp_path):
    collector = recorder(tmp_path)
    collector.ingest_stream(start())
    collector.ingest_stream(complete(error=True))
    paired_hooks(collector, error=True)
    collector.ingest_stream(result(error=True))
    report = collector.finish()
    assert report["status"] == "complete"
    assert report["native_outcome"] == "reported_error"


def test_rule_declaration_requires_current_rule_and_current_run_evidence(tmp_path):
    collector = recorder(tmp_path)
    evidence = collector.ingest_stream(start())[0]["event_id"]
    declaration = collector.record_rule(
        {
            "rule_id": "R1",
            "phase": "act",
            "observation": "Ran a public test",
            "decision": "Investigate the failure",
            "evidence_refs": [evidence],
        }
    )
    assert declaration["producer"] == "agent_declaration"
    assert declaration["payload"]["verification"] == "unreviewed"
    assert declaration["payload"]["related_tool_events"] == [evidence]
    for invalid in (
        {"rule_id": "R3", "phase": "act"},
        {"rule_id": "R1", "phase": "act", "evidence_refs": ["other-run:1"]},
        {"rule_id": "R1", "phase": "act", "run_id": "other"},
        {"rule_id": "R1", "phase": "reasoning", "thinking": "private"},
    ):
        with pytest.raises(ValueError):
            collector.record_rule(invalid)
    unlinked = collector.record_rule(
        {
            "rule_id": "R2",
            "phase": "observe",
            "observation": "Need more evidence",
            "decision": "unknown",
        }
    )
    assert unlinked["payload"]["verification"] == "declared_only"


def test_rule_claim_may_not_reference_another_claim_as_executed_action(tmp_path):
    collector = recorder(tmp_path)
    first = collector.record_rule(
        {"rule_id": "R1", "phase": "observe", "observation": "Incomplete", "decision": "unknown"}
    )
    with pytest.raises(ValueError):
        collector.record_rule(
            {
                "rule_id": "R2",
                "phase": "act",
                "observation": "An action",
                "decision": "apply",
                "evidence_refs": [first["event_id"]],
            }
        )


def test_malformed_stream_and_hook_failure_remain_partial(tmp_path):
    collector = recorder(tmp_path)
    collector.ingest_stream({"type": "assistant", "message": {"content": "malformed"}})
    collector.ingest_stream(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_id": "h",
            "exit_code": 2,
            "outcome": "error",
        }
    )
    collector.ingest_stream(result())
    assert collector.finish()["status"] == "partial"


def test_event_store_cannot_overwrite_and_finish_seals_the_collector(tmp_path):
    collector = recorder(tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        recorder(tmp_path)
    collector.ingest_stream(result())
    report = collector.finish()
    assert collector.finish() == report
    with pytest.raises(ValueError, match="finished"):
        collector.ingest_stream(result())
    copied = collector.events
    copied[0]["payload"]["result"] = "changed outside"
    assert collector.events[0]["payload"]["result"] == "Task complete"
    assert len(
        [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    ) == len(collector.events)


def test_usage_counters_remain_numbers_while_authentication_tokens_are_redacted(tmp_path):
    collector = recorder(tmp_path)
    message = result()
    message["usage"] = {
        "input_tokens": 15,
        "output_tokens": 4,
        "cache_read_input_tokens": 8,
        "access_token": "PRIVATE-AUTH",
    }
    collector.ingest_stream(message)
    usage = collector.finish()["native_result"]["usage"]
    assert usage["input_tokens"] == 15
    assert usage["output_tokens"] == 4
    assert usage["cache_read_input_tokens"] == 8
    assert usage["access_token"] == "[REDACTED]"


def test_malformed_tool_identity_does_not_become_complete_capture(tmp_path):
    collector = recorder(tmp_path)
    message = start()
    message["message"]["content"][0]["name"] = None
    message["message"]["content"][0]["input"] = "not an object"
    collector.ingest_stream(message)
    collector.ingest_stream(complete())
    collector.ingest_stream(result())
    assert collector.finish()["status"] == "partial"


def test_native_gate_observation_is_preserved_without_promoting_it_to_loaded(tmp_path):
    collector = recorder(tmp_path)
    event = collector.ingest_hook(
        {
            "hook_event_name": "UserPromptExpansion",
            "session_id": "session",
            "command_name": "fixed-r1",
            "source": "project",
            "expansion_type": "skill",
            "gate": "allowed",
            "reason_code": "fixed_artifact_verified",
        }
    )
    assert event["payload"]["gate"] == "allowed"
    assert event["payload"]["reason_code"] == "fixed_artifact_verified"
    assert "loaded" not in event["payload"]


def test_nested_private_blocks_and_basic_auth_headers_never_reach_disk(tmp_path):
    collector = recorder(tmp_path)
    message = complete()
    message["message"]["content"][0]["content"] = [
        {"type": "thinking", "text": "NESTED-PRIVATE"},
        {"type": "text", "text": "Authorization: Basic BASIC-CREDENTIAL\npublic result"},
    ]
    collector.ingest_stream(message)
    text = (tmp_path / "events.jsonl").read_text()
    assert "NESTED-PRIVATE" not in text
    assert "BASIC-CREDENTIAL" not in text
    assert "public result" in text


def test_informational_and_init_plugin_evidence_are_retained(tmp_path):
    collector = recorder(tmp_path)
    init = collector.ingest_stream(
        {"type": "system", "subtype": "init", "plugins": [], "skills": ["fixed-r1"]}
    )[0]
    assert init["payload"]["plugins"] == []
    info = collector.ingest_stream(
        {
            "type": "system",
            "subtype": "informational",
            "content": "A hook stopped continuation",
            "level": "warning",
            "prevent_continuation": True,
        }
    )[0]
    assert info["kind"] == "informational"
    assert info["payload"]["prevent_continuation"] is True
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "complete"
    assert report["native_outcome"] == "reported_success"
    assert "agent_outcome" not in report


def test_malformed_arbitrary_tool_payload_does_not_crash_or_write_nonfinite_values(tmp_path):
    collector = recorder(tmp_path)
    payload = start()
    payload["message"]["content"][0]["input"] = {"type": [], "amount": float("nan")}
    collector.ingest_stream(payload)
    collector.ingest_stream(complete())
    collector.ingest_stream(result())
    assert collector.finish()["status"] == "partial"
    assert "NaN" not in (tmp_path / "events.jsonl").read_text()


def test_explicit_related_tool_events_are_rechecked_on_the_host(tmp_path):
    collector = recorder(tmp_path)
    tool = collector.ingest_stream(start())[0]["event_id"]
    claim = collector.record_rule(
        {
            "rule_id": "R1",
            "phase": "act",
            "observation": "The tool was requested",
            "decision": "Inspect result",
            "related_tool_events": [tool],
        }
    )
    assert claim["payload"]["related_tool_events"] == [tool]
    assert claim["payload"]["evidence_refs"] == [tool]
    with pytest.raises(ValueError):
        collector.record_rule(
            {
                "rule_id": "R1",
                "phase": "act",
                "observation": "A claim",
                "decision": "Inspect result",
                "related_tool_events": ["foreign:tool"],
            }
        )


def test_unknown_stream_keeps_only_bounded_protocol_names_for_diagnostics(tmp_path):
    collector = recorder(tmp_path)
    event = collector.ingest_stream(
        {
            "type": "system",
            "subtype": "api_retry",
            "error": "PRIVATE-DIAGNOSTIC",
            "content": "PRIVATE-CONTENT",
        }
    )[0]
    assert event["payload"] == {
        "reason": "unsupported_stream_message",
        "stream_type": "system",
        "stream_subtype": "api_retry",
    }
    suspicious = collector.ingest_stream(
        {"type": "PRIVATE/URL", "subtype": "PRIVATE " * 50, "content": "PRIVATE-CONTENT"}
    )[0]
    assert suspicious["payload"] == {"reason": "unsupported_stream_message"}
    text = (tmp_path / "events.jsonl").read_text()
    assert "PRIVATE" not in text
    assert collector.finish()["status"] == "partial"


def test_stream_only_actions_are_partial_when_native_tool_hooks_are_missing(tmp_path):
    collector = recorder(tmp_path)
    collector.ingest_stream(start())
    collector.ingest_stream(complete())
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "partial"
    assert "missing_tool_hook" in report["reasons"]


def test_hook_only_actions_are_partial_when_native_stream_is_missing(tmp_path):
    collector = recorder(tmp_path)
    paired_hooks(collector)
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "partial"
    assert "missing_tool_stream" in report["reasons"]


def test_foreign_native_session_never_qualifies_as_complete_for_this_run(tmp_path):
    collector = recorder(tmp_path, expected_session_id="expected-native-session")
    collector.ingest_stream(start())
    collector.ingest_stream(complete())
    paired_hooks(collector)
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "partial"
    assert "native_session_mismatch" in report["reasons"]
    assert collector.events[0]["native_session_id"] == "native-session"


def test_session_is_bound_to_first_observation_when_not_supplied(tmp_path):
    collector = recorder(tmp_path)
    collector.ingest_stream({"type": "system", "subtype": "init", "session_id": "first"})
    collector.ingest_stream({**result(), "session_id": "second"})
    assert "native_session_mismatch" in collector.finish()["reasons"]


def test_private_thinking_progress_is_ignored_without_persistence_or_pairing(tmp_path):
    collector = recorder(tmp_path)
    collector.ingest_stream(start())
    paired_hooks(collector)
    collector.ingest_stream(complete())
    before = collector.events
    emitted = collector.ingest_stream(
        {
            "type": "system",
            "subtype": "thinking_tokens",
            "uuid": "progress-1",
            "session_id": "native-session",
            "content": "SYNTHETIC-PRIVATE-PROGRESS",
            "tool_use_id": "synthetic-not-a-tool",
        }
    )
    assert emitted == []
    assert collector.events == before
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "complete"
    assert report["tool_action_count"] == 1
    assert "SYNTHETIC-PRIVATE-PROGRESS" not in collector.path.read_text()


@pytest.mark.parametrize("kind, subtype", [("system", "api_retry"), ("future", "thinking_tokens")])
def test_ignoring_thinking_progress_does_not_accept_other_unknown_messages(tmp_path, kind, subtype):
    collector = recorder(tmp_path)
    collector.ingest_stream(
        {"type": kind, "subtype": subtype, "content": "SYNTHETIC-UNKNOWN-CONTENT"}
    )
    collector.ingest_stream(result())
    report = collector.finish()
    assert report["status"] == "partial"
    assert "unsupported_stream_message" in report["reasons"]
    assert "SYNTHETIC-UNKNOWN-CONTENT" not in collector.path.read_text()


def test_public_projection_and_event_recording_share_cross_line_secret_filter(tmp_path):
    value = {
        "stdout": 'public\nAuthorization:\n Bearer HEADER-SECRET\nBearer\nSTANDALONE-SECRET\nANTHROPIC_API_KEY=\'ASSIGNMENT-SECRET\'\n"api_key": "JSON-SECRET"\n',
        "thinking": "PRIVATE-REASONING",
        "nested": [{"type": "thinking", "text": "PRIVATE-BLOCK"}],
        "endpoint": "https://private.example/route",
        "usage": {"input_tokens": 5},
    }
    secrets = ("https://private.example/route",)
    clean = sanitize_public(value, secrets)
    collector = recorder(tmp_path, secret_values=secrets)
    recorded = collector.ingest_hook({"hook_event_name": "PostToolUse", "tool_response": value})
    assert recorded["payload"]["tool_response"] == clean
    rendered = json.dumps(clean)
    for private in (
        "HEADER-SECRET",
        "STANDALONE-SECRET",
        "ASSIGNMENT-SECRET",
        "JSON-SECRET",
        "PRIVATE-REASONING",
        "PRIVATE-BLOCK",
        "private.example",
    ):
        assert private not in rendered
    assert clean["usage"]["input_tokens"] == 5
    assert clean["stdout"].startswith("public\n")
    assert value["thinking"] == "PRIVATE-REASONING"


def test_public_projection_removes_serialized_private_thinking_blocks():
    source = json.dumps(
        {
            "content": [
                {"type": "thinking", "thinking": "PRIVATE-JSON"},
                {"type": "text", "text": "public answer"},
            ]
        }
    )
    sanitized = sanitize_public(source)
    assert "PRIVATE-JSON" not in sanitized
    assert "public answer" in sanitized


@pytest.mark.parametrize("secrets", [None, "secret", ("",), (None,)])
def test_public_projection_rejects_invalid_secret_configuration(secrets):
    with pytest.raises(ValueError, match="secrets must be nonempty strings"):
        sanitize_public("private value", secrets)


def test_public_projection_safely_projects_non_json_and_deep_values():
    value = {"path/to/module.py": object(), "duration": float("nan")}
    assert sanitize_public(value) == {
        "path/to/module.py": "[UNSUPPORTED]",
        "duration": "[INVALID_NUMBER]",
    }
    circular = []
    circular.append(circular)
    assert "[UNSUPPORTED]" in json.dumps(sanitize_public(circular))
    too_deep = "[" * 2000 + '"PRIVATE-DEEP"' + "]" * 2000
    sanitized = sanitize_public(too_deep)
    assert "[UNSUPPORTED]" in sanitized
    assert "PRIVATE-DEEP" not in sanitized


@pytest.mark.parametrize("serialized", [False, True])
def test_public_projection_omits_sensitive_keys_without_changing_schema_keys(serialized):
    secret = "synthetic-provider-secret-review"
    value = {
        secret: "observed",
        "Authorization: Bearer SYNTHETIC-HEADER": "observed",
        "ANTHROPIC_API_KEY='SYNTHETIC-ASSIGNMENT'": "observed",
        "nested": {"prefix-" + secret: "observed"},
        "run_id": "r1",
        "api_key": "SYNTHETIC-VALUE",
        "imported_files": {"src/module.py": "loaded"},
    }
    clean = sanitize_public(json.dumps(value) if serialized else value, (secret,))
    rendered = clean if serialized else json.dumps(clean)
    for private in (secret, "SYNTHETIC-HEADER", "SYNTHETIC-ASSIGNMENT", "SYNTHETIC-VALUE"):
        assert private not in rendered
    projected = json.loads(clean) if serialized else clean
    assert projected["run_id"] == "r1"
    assert projected["api_key"] == "[REDACTED]"
    assert projected["imported_files"] == {"src/module.py": "loaded"}
    assert projected["_omitted_sensitive_keys"] == 3
    assert projected["nested"] == {"_omitted_sensitive_keys": 1}


def test_public_projection_sensitive_key_omission_does_not_overwrite_existing_keys():
    value = {
        "SECRET-ONE": "first",
        "SECRET-TWO": "second",
        "[REDACTED]": "retained",
        "_omitted_sensitive_keys": "original",
        "_omitted_sensitive_keys_2": "also original",
    }
    clean = sanitize_public(value, ("SECRET-ONE", "SECRET-TWO"))
    assert clean == {
        "[REDACTED]": "retained",
        "_omitted_sensitive_keys": "original",
        "_omitted_sensitive_keys_2": "also original",
        "_omitted_sensitive_keys_3": 2,
    }
