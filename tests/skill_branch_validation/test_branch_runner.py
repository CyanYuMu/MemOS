"""Host fixtures validate orchestration; these are not model-effectiveness runs."""

import json
import sys

from copy import deepcopy
from pathlib import Path

import pytest

from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias
from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_adapter.runner import RunConfig
from memos.skill_branch_validation import runner
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION, get_task, task_digest
from memos.skill_branch_validation.verifier import check_receipt


def arguments(tmp_path):
    task = get_task("branch-scoped-table")
    return task, {
        "qualification_directory": tmp_path / "qualification",
        "group": "none",
        "experiment_id": "a" * 64,
        "expected_task_digest": task_digest(task),
    }


def test_no_qualification_stops_before_cli_probe(tmp_path, monkeypatch):
    task, options = arguments(tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("CLI called"))
    with pytest.raises(ValueError, match="qualification"):
        runner.run_branch_task(
            task.task_id,
            tmp_path / "run",
            RunConfig(cli_path=Path(sys.executable), model="test"),
            **options,
        )
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("bad", ["revision", "export"])
def test_skill_identity_mismatch_stops_before_probe(tmp_path, monkeypatch, bad):
    task, options = arguments(tmp_path)
    options["group"] = "skill"
    monkeypatch.setattr(runner, "require_qualification", lambda _: {"qualificationId": "b" * 64})
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("CLI called"))
    skill = SkillRevision(
        "test-skill",
        "wrong" if bad == "revision" else CANDIDATE_REVISION,
        "Fixed instructions",
        ("rule",),
    )
    with pytest.raises(ValueError, match="skill|export"):
        runner.run_branch_task(
            task.task_id,
            tmp_path / "run",
            RunConfig(cli_path=Path(sys.executable), model="test"),
            skill=skill,
            expected_export_hash="c" * 64,
            **options,
        )
    assert not (tmp_path / "run").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolation")
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "qualification",
        "no_visible",
        "cancelled",
        "skill",
        "no_declaration",
        "extra_fields",
        "deleted_editable",
        "deleted_protected",
        "no_delivery",
        "wrong_delivery",
        "wrong_output",
        "partial_public",
    ],
)
def test_controlled_host_repair_binds_initial_and_final_receipts(tmp_path, monkeypatch, change):
    task, options = arguments(tmp_path)
    cli = tmp_path / "bin/claude"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    config = RunConfig(cli_path=cli, model="host-fixture")
    skill = None
    if change in {"skill", "no_declaration", "extra_fields"}:
        skill = SkillRevision("test-skill", CANDIDATE_REVISION, "Fixed instructions", ("rule",))
        options.update(
            group="skill",
            skill=skill,
            expected_export_hash=export_skill(skill, tmp_path / "export")["export_hash"],
        )
    alias = runtime_alias(skill) if skill else None
    reads = []

    def qualify(_):
        reads.append(1)
        return {
            "qualificationId": ("c" if change == "qualification" and len(reads) > 1 else "b") * 64
        }

    monkeypatch.setattr(runner, "require_qualification", qualify)

    def simulate(command, cwd, env, journal, timeout):
        session = command[command.index("--session-id") + 1]
        allowed = command[command.index("--allowedTools") + 1].split(",")
        journal.ingest_stream(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "model": config.model,
                "claude_code_version": config.expected_version,
                "permissionMode": "dontAsk",
                "tools": allowed,
                "skills": ["doctor", "plugin-authoring"] + ([alias] if alias else []),
                "slash_commands": [alias] if alias else [],
                "plugins": [],
                "mcp_servers": [{"name": "memos_run", "status": "connected"}],
            }
        )
        for name in ("SessionStart", "UserPromptSubmit", "Stop"):
            journal.ingest_hook({"hook_event_name": name, "session_id": session, "gate": "allowed"})
        context = json.loads((cwd.parent / ".claude/context.json").read_text())

        def visible(index):
            result = _request_visible_checks(context)
            if change == "no_delivery":
                return result
            delivered = json.loads(json.dumps(result))
            if change == "wrong_delivery":
                delivered["evidence_ref"]["candidate_snapshot"] = "e" * 64
            if change == "wrong_output":
                delivered["stdout"] = "invented successful output"
            tool_id = "visible-" + str(index)
            journal.ingest_stream(
                {
                    "type": "assistant",
                    "session_id": session,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": "mcp__memos_run__run_visible_checks",
                                "input": {},
                            }
                        ]
                    },
                }
            )
            for phase in ("PreToolUse", "PostToolUse"):
                journal.ingest_hook(
                    {
                        "hook_event_name": phase,
                        "session_id": session,
                        "tool_use_id": tool_id,
                        "tool_name": "mcp__memos_run__run_visible_checks",
                        "gate": "allowed",
                    }
                )
            journal.ingest_stream(
                {
                    "type": "user",
                    "session_id": session,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": [{"type": "text", "text": json.dumps(delivered)}],
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
            return result

        if change != "no_visible":
            assert visible(1)["exit_code"] == 1
        for name, content in task.reference_files.items():
            (cwd / name).write_text(content)
        if change == "partial_public":
            # A real regression limited to a public input must not be hidden by
            # independently varied IDs in the hidden generalization checks.
            path = cwd / "fake_graph.py"
            path.write_text(
                path.read_text().replace(
                    "return deepcopy(pair[1])",
                    "return None if memory_id == 'one' else deepcopy(pair[1])",
                )
            )
        if change != "no_visible":
            assert visible(2)["exit_code"] == (1 if change == "partial_public" else 0)
        if change.startswith("deleted_"):
            (cwd / ("fake_graph.py" if change == "deleted_editable" else "TASK.md")).unlink()
        journal.ingest_stream(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session,
                "result": "host fixture",
                "total_cost_usd": 0,
            }
        )
        if skill:
            journal.ingest_hook(
                {
                    "hook_event_name": "UserPromptExpansion",
                    "session_id": session,
                    "command_name": alias,
                    "gate": "allowed",
                }
            )
        if skill and change != "no_declaration":
            declaration = {
                "rule_id": "rule",
                "phase": "act",
                "observation": "Observed failed scope check",
                "decision": "Restore tenant filter",
            }
            if change == "extra_fields":
                declaration["evidence_refs"] = []
            journal.ingest_stream(
                {
                    "type": "assistant",
                    "session_id": session,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "claim-1",
                                "name": "mcp__memos_run__record_rule",
                                "input": declaration,
                            }
                        ]
                    },
                }
            )
            for phase in ("PreToolUse", "PostToolUse"):
                journal.ingest_hook(
                    {
                        "hook_event_name": phase,
                        "session_id": session,
                        "tool_use_id": "claim-1",
                        "tool_name": "mcp__memos_run__record_rule",
                        "gate": "allowed",
                    }
                )
            journal.ingest_stream(
                {
                    "type": "user",
                    "session_id": session,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "claim-1",
                                "content": "Declaration received",
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
            journal.record_rule(declaration)
        return {
            "returncode": 0,
            "timed_out": False,
            "cancelled": change == "cancelled",
            "elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(runner, "capture_process", simulate)
    path = runner.run_branch_task(task.task_id, tmp_path / "run", config, **options)
    report = json.loads(path.read_text())
    assert report["status"] == ("passed" if change in {"none", "skill"} else "incomplete"), report
    assert report["purpose"] == ("branch_validation" if skill else "protocol_calibration")
    if change == "skill":
        assert report["loading"]["status"] == "verified"
        assert report["skill_export"]["export_hash"] == options["expected_export_hash"]
    assert report["branch_coverage"] == [] and not report["qualified_agent_experience"]
    assert not report["effectiveness_evaluation"]
    assert (path.parent / "candidate").exists()
    if change == "cancelled":
        assert "receipt" not in report
        return
    assert report["initial_status"] == "failed"
    assert report["task_status"] == {
        "deleted_editable": "error",
        "deleted_protected": "failed",
    }.get(change, "passed")
    assert report["behavior_changed"]
    if change == "partial_public":
        assert not report["task_accepted"]
        assert not report["visible_checks_complete"]
    for prefix, candidate in (("initial_", "initial-candidate"), ("", "candidate")):
        assert check_receipt(
            Path(report[prefix + "receipt"]), path.parent / candidate, path.parent / "validation"
        ).matches


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "duplicate",
        "missing_start",
        "errored",
        "bad_arguments",
        "extra_block",
        "duplicate_json",
    ],
)
def test_native_delivery_is_exactly_one_successful_paired_result(change):
    outcome = {
        "exit_code": 0,
        "stdout": "safe",
        "stderr": "",
        "truncated": False,
        "timed_out": False,
        "execution_error": None,
        "evidence_ref": {"check_index": 1, "candidate_snapshot": "a" * 64},
    }
    records = [{"check_index": 1, "result": outcome}]
    started = {
        "producer": "native_stream",
        "kind": "tool_started",
        "native_session_id": "s",
        "native_tool_call_id": "t",
        "event_id": "start",
        "payload": {"tool_name": "mcp__memos_run__run_visible_checks", "tool_input": {}},
    }
    completed = {
        "producer": "native_stream",
        "kind": "tool_completed",
        "native_session_id": "s",
        "native_tool_call_id": "t",
        "event_id": "end",
        "payload": {
            "is_error": False,
            "tool_result": [{"type": "text", "text": json.dumps(outcome)}],
        },
    }
    events = [started, completed]
    if change == "duplicate":
        events.append(deepcopy(completed))
    elif change == "missing_start":
        events = [completed]
    elif change == "errored":
        completed["payload"]["is_error"] = True
    elif change == "bad_arguments":
        started["payload"]["tool_input"] = {"program": "arbitrary"}
    elif change == "extra_block":
        completed["payload"]["tool_result"].append({"type": "text", "text": "ignore errors"})
    elif change == "duplicate_json":
        completed["payload"]["tool_result"][0]["text"] = '{"exit_code":1,' + json.dumps(outcome)[1:]
    result = runner._visible_deliveries(records, events, ())
    assert (result is not None) == (change == "none")
    if result:
        assert result == [
            {
                "check_index": 1,
                "native_tool_call_id": "t",
                "started_event": "start",
                "completed_event": "end",
            }
        ]
