"""P4 adds a separate controlled entry without weakening P1 source evidence."""

import io
import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.runner import RunConfig
from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_experiments import recording_server
from memos.skill_experiments.runner import (
    adapter_digest,
    context_matches,
    raw_read_observed,
    run_experiment_case,
)


def config():
    return RunConfig(cli_path=Path(sys.executable), model="claude-pinned-test")


@pytest.mark.parametrize(
    "case_id,group,raw",
    [
        ("source-scoped-update", "none", None),
        ("holdout-record", "unknown", None),
        ("holdout-record", "none", "unexpected"),
        ("holdout-record", "raw_memory", None),
        ("holdout-record", "raw_memory", "x" * 32769),
        ("holdout-record", "skill", None),
    ],
)
def test_invalid_group_or_source_rejected_before_output(tmp_path, case_id, group, raw):
    output = tmp_path / "run"
    with pytest.raises(ValueError):
        run_experiment_case(
            case_id,
            output,
            config(),
            group=group,
            experiment_id="a" * 64,
            expected_case_digest=case_digest(get_case(case_id)),
            raw_memory=raw,
        )
    assert not output.exists()


def test_task_drift_rejected_before_cli_probe(tmp_path):
    with pytest.raises(ValueError, match="task_digest"):
        run_experiment_case(
            "holdout-record",
            tmp_path / "run",
            config(),
            group="none",
            experiment_id="a" * 64,
            expected_case_digest="b" * 64,
        )
    assert not (tmp_path / "run").exists()


def test_empty_rule_group_has_same_tools_but_cannot_declare():
    context = {
        "run_id": "run",
        "attempt_id": "attempt",
        "rule_ids": [],
        "test_broker_socket": "/tmp/host-broker.sock",
    }
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "record_rule",
                "arguments": {
                    "rule_id": "invented",
                    "phase": "observe",
                    "observation": "observable",
                    "decision": "unsupported",
                },
            },
        },
    ]
    stream = io.StringIO()
    recording_server.serve(context, io.StringIO("\n".join(map(json.dumps, messages))), stream)
    results = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert {t["name"] for t in results[1]["result"]["tools"]} == {
        "record_rule",
        "run_visible_checks",
    }
    assert results[2]["result"]["isError"] is True


def test_raw_read_requires_matching_successful_tool_result():
    path = Path("/tmp/control/raw-memory.txt")
    use = {
        "kind": "tool_started",
        "native_tool_call_id": "t",
        "payload": {"tool_name": "Read", "tool_input": {"file_path": str(path)}},
    }
    result = {"kind": "tool_completed", "native_tool_call_id": "t", "payload": {"is_error": False}}
    assert not raw_read_observed([use], path)
    assert raw_read_observed([use, result], path)
    assert not raw_read_observed([use, {**result, "payload": {"is_error": True}}], path)
    assert not raw_read_observed([use, result], Path("/tmp/another"))


def test_all_groups_require_identical_tools():
    cfg = config()
    init = {
        "kind": "session_init",
        "payload": {
            "model": cfg.model,
            "claude_code_version": cfg.expected_version,
            "permissionMode": "dontAsk",
            "tools": [
                *cfg.tools,
                "mcp__memos_run__run_visible_checks",
                "mcp__memos_run__record_rule",
            ],
            "skills": ["doctor", "plugin-authoring"],
            "plugins": [],
            "mcp_servers": [{"name": "memos_run", "status": "connected"}],
        },
    }
    assert context_matches([init], cfg, None)
    init["payload"]["tools"].remove("mcp__memos_run__record_rule")
    assert not context_matches([init], cfg, None)


def test_adapter_digest_binds_new_and_old_execution_code():
    assert len(adapter_digest()) == 64


@pytest.mark.parametrize("group", ["none", "raw_memory", "skill"])
def test_full_host_orchestration_keeps_task_and_control_separate(tmp_path, monkeypatch, group):
    from memos.skill_adapter.artifacts import SkillRevision
    from memos.skill_experiments import runner
    from memos.skill_experiments.scoring import score_run

    cli = tmp_path / "cli-bin/claude"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    cfg = RunConfig(cli_path=cli, model="claude-pinned-test")
    case = get_case("holdout-record")
    skill = (
        SkillRevision(
            skill_id="test-contract",
            revision="a" * 64,
            body="Only use the current contract.",
            rule_ids=("R1",),
        )
        if group == "skill"
        else None
    )

    def simulated_native(command, cwd, env, journal, timeout):
        # This is a host fixture, not an Agent run or independent efficacy evidence.
        session = command[command.index("--session-id") + 1]
        control = Path(command[command.index("--settings") + 1]).parent
        assert not (cwd / "raw-memory.txt").exists()
        assert (control / "raw-memory.txt").exists() == (group == "raw_memory")
        tools = command[command.index("--allowedTools") + 1].split(",")
        assert set(tools) == {
            *cfg.tools,
            "mcp__memos_run__record_rule",
            "mcp__memos_run__run_visible_checks",
        }
        alias = runner.runtime_alias(skill) if skill else None
        journal.ingest_stream(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "model": cfg.model,
                "claude_code_version": cfg.expected_version,
                "permissionMode": "dontAsk",
                "tools": tools,
                "skills": ["doctor", "plugin-authoring"] + ([alias] if alias else []),
                "slash_commands": [alias] if alias else [],
                "plugins": [],
                "mcp_servers": [{"name": "memos_run", "status": "connected"}],
            }
        )
        for name in ("SessionStart", "UserPromptSubmit", "Stop"):
            journal.ingest_hook({"hook_event_name": name, "session_id": session, "gate": "allowed"})
        if alias:
            journal.ingest_hook(
                {
                    "hook_event_name": "UserPromptExpansion",
                    "session_id": session,
                    "command_name": alias,
                    "gate": "allowed",
                }
            )
        if group == "raw_memory":
            journal.ingest_stream(
                {
                    "type": "assistant",
                    "session_id": session,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "read-1",
                                "name": "Read",
                                "input": {"file_path": str(control / "raw-memory.txt")},
                            }
                        ]
                    },
                }
            )
            journal.ingest_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_use_id": "read-1",
                    "tool_name": "Read",
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
                                "tool_use_id": "read-1",
                                "content": "source facts",
                                "is_error": False,
                            }
                        ]
                    },
                }
            )
            journal.ingest_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session,
                    "tool_use_id": "read-1",
                    "tool_name": "Read",
                }
            )
        for name, content in case.reference_files.items():
            (cwd / name).write_text(content)
        journal.ingest_stream(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session,
                "result": "fixture complete",
                "total_cost_usd": 0,
            }
        )
        return {"returncode": 0, "timed_out": False, "elapsed_seconds": 0.01}

    monkeypatch.setattr(runner, "capture_process", simulated_native)
    p = run_experiment_case(
        case.case_id,
        tmp_path / "run",
        cfg,
        group=group,
        experiment_id="b" * 64,
        expected_case_digest=case_digest(case),
        skill=skill,
        raw_memory="source facts" if group == "raw_memory" else None,
    )
    report = json.loads(p.read_text())
    assert report["task_accepted"] is True, report
    assert report["capture"]["status"] == "complete"
    assert report["experience"]["readObserved"] == (group != "none")
    assert report["status"] == "passed"
    assert score_run(p, group=group, expected_case_digest=case_digest(case))["repairPass"] == 1


def test_cancellation_before_launch_does_not_start_native_process(tmp_path, monkeypatch):
    import threading

    from memos.skill_adapter.events import EventRecorder
    from memos.skill_experiments import process as module

    event = threading.Event()
    event.set()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    journal = EventRecorder(tmp_path / "events.jsonl", {"run_id": "r", "attempt_id": "a"})
    result = module.capture_process(["unused"], tmp_path, {}, journal, 60, cancellation_event=event)
    assert result["cancelled"] is True and result["returncode"] is None
    assert "cancelled" in journal.finish()["reasons"]


def test_cancellation_kills_only_run_and_its_descendants_and_closes_pipes(tmp_path, monkeypatch):
    import os
    import subprocess
    import threading
    import time

    from contextlib import suppress

    from memos.skill_adapter.events import EventRecorder
    from memos.skill_experiments import process as module

    real_popen = subprocess.Popen
    unrelated = real_popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
    processes = []
    event = threading.Event()
    child_pid = tmp_path / "child.pid"
    leader_pid = tmp_path / "leader.pid"

    def recording_popen(*args, **kwargs):
        created = real_popen(*args, **kwargs)
        processes.append(created)
        return created

    def cancel_after_child_started():
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        # Let the host collect the birth identity of a separately-sessioned child.
        time.sleep(0.25)
        event.set()

    script = (
        "import os,subprocess,sys,time\n"
        f"open({str(leader_pid)!r},'w').write(str(os.getpid()))\n"
        "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],start_new_session=True)\n"
        f"open({str(child_pid)!r},'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    journal = EventRecorder(tmp_path / "events.jsonl", {"run_id": "r", "attempt_id": "a"})
    monkeypatch.setattr(module.subprocess, "Popen", recording_popen)
    canceller = threading.Thread(target=cancel_after_child_started)
    canceller.start()
    try:
        result = module.capture_process(
            [sys.executable, "-I", "-c", script],
            tmp_path,
            {},
            journal,
            60,
            cancellation_event=event,
        )
        canceller.join(timeout=3)
        assert result["cancelled"] is True and result["timed_out"] is False
        assert result["elapsed_seconds"] < 10
        assert unrelated.poll() is None
        assert child_pid.exists() and leader_pid.exists()
        for path in (child_pid, leader_pid):
            pid = int(path.read_text())
            probe = subprocess.run(
                ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            )
            assert not probe.stdout.strip() or probe.stdout.strip().startswith("Z")
        native = next(p for p in processes if p.pid == int(leader_pid.read_text()))
        assert native.poll() is not None and native.stdout.closed and native.stderr.closed
        assert "cancelled" in journal.finish()["reasons"]
    finally:
        event.set()
        canceller.join(timeout=3)
        unrelated.terminate()
        unrelated.wait(timeout=3)
        for path in (child_pid, leader_pid):
            if path.exists():
                with suppress(ProcessLookupError):
                    os.kill(int(path.read_text()), 9)


def test_cancelled_native_preserves_partial_report_without_starting_verifier(tmp_path, monkeypatch):
    import threading

    from memos.skill_experiments import runner

    cli = tmp_path / "cli-bin/claude"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    cfg = RunConfig(cli_path=cli, model="claude-pinned-test")
    case = get_case("dev-strict-signature")
    event = threading.Event()
    observed = []

    def cancelled_capture(*args, cancellation_event):
        observed.append(cancellation_event)
        args[3].mark_incomplete("cancelled")
        return {"returncode": -15, "cancelled": True, "timed_out": False, "elapsed_seconds": 0.1}

    monkeypatch.setattr(runner, "capture_process", cancelled_capture)
    monkeypatch.setattr(runner, "probe_isolation", lambda _: {"passed": True})
    monkeypatch.setattr(
        runner,
        "verify_candidate",
        lambda *a, **k: pytest.fail("must not verify after cancellation"),
    )
    p = run_experiment_case(
        case.case_id,
        tmp_path / "run",
        cfg,
        group="none",
        experiment_id="b" * 64,
        expected_case_digest=case_digest(case),
        cancellation_event=event,
    )
    report = json.loads(p.read_text())
    assert observed == [event]
    assert report["execution"]["cancelled"] is True
    assert report["status"] == "incomplete" and report["task_accepted"] is False
    assert "cancelled" in report["capture"]["reasons"]
    assert report["candidate_snapshot"] and (p.parent / "candidate").is_dir()
