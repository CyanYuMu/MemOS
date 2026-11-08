"""Explicit host fixture with real isolated execution, never model evidence."""

import hashlib
import json

from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias
from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_adapter.runner import RunConfig
from memos.skill_branch_validation import batch, qualification, runner
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION, get_task, task_digest
from memos.skill_drafting.domain import hash_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def make_run(tmp_path, monkeypatch, *, group="skill", public_regression=False, delivery_mode=None):
    task = get_task("branch-scoped-table")
    candidate = {
        "skill_id": "synthetic-branch-repair",
        "revision": CANDIDATE_REVISION,
        "tags": ["management_test"],
        "source_refs": [
            {"source_id": "d" * 64, "case_id": "synthetic-source", "lineage_id": "synthetic-source"}
        ],
        "rules": [
            {
                "rule_id": "confirm_contract",
                "branches": [
                    {"when": "Synthetic", "target": "exit:not_applicable"},
                    {"when": "Synthetic", "target": "exit:needs_evidence"},
                    {"when": "Synthetic", "target": "exit:success"},
                    {"when": "Synthetic scope failure", "target": "repair_scoped_reads"},
                ],
            },
            {
                "rule_id": "repair_scoped_reads",
                "branches": [
                    {"when": "Synthetic repaired", "target": "verify_scoped_reads"},
                ],
            },
            {
                "rule_id": "verify_scoped_reads",
                "branches": [
                    {"when": "Synthetic", "target": "exit:unresolved"},
                    {"when": "Synthetic", "target": "exit:needs_evidence"},
                    {"when": "Synthetic scoped only", "target": "exit:success"},
                ],
            },
        ],
    }
    candidate["content_hash"] = hash_json(candidate)
    skill = SkillRevision(
        candidate["skill_id"],
        CANDIDATE_REVISION,
        "Synthetic management integration fixture, not source evidence.",
        tuple(rule["rule_id"] for rule in candidate["rules"]),
    )
    export_hash = export_skill(skill, tmp_path / "fixed-export")["export_hash"]
    alias = runtime_alias(skill)
    cli = tmp_path / "bin/claude"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    config = RunConfig(cli_path=cli, model="synthetic-host-fixture")
    proof = {"qualificationId": "b" * 64, "binding": qualification._binding()}
    monkeypatch.setattr(runner, "require_qualification", lambda _: proof)
    identity = {
        "protocol": "branch-core-batch-1",
        "batchDigest": sha(batch.__file__),
        "adapterDigest": runner.adapter_digest(),
        "configurationDigest": "c" * 64,
        "qualification": proof,
        "candidateRevision": candidate["revision"],
        "contentHash": candidate["content_hash"],
        "exportHash": export_hash,
        "runtime": {
            "cliHash": sha(cli),
            "cliVersion": config.expected_version,
            "model": config.model,
            "effort": config.effort,
            "tools": list(config.tools),
            "timeoutSeconds": config.timeout_seconds,
            "maxTurns": config.max_turns,
            "maxBudgetUsd": config.max_budget_usd,
        },
        "units": [
            {
                "id": name,
                "taskId": task_id,
                "group": arm,
                "taskDigest": task_digest(get_task(task_id)),
            }
            for name, task_id, arm in batch.UNITS
        ],
        "maxAgentRuns": 3,
        "branchCoverage": [],
        "effectivenessEvaluation": False,
    }
    experiment_id = hash_json(identity)
    root = tmp_path / "batch"
    root.mkdir()
    write_json(root / "manifest.json", {**identity, "experimentId": experiment_id})

    def simulate(command, cwd, env, journal, timeout):
        session = command[command.index("--session-id") + 1]
        tools = command[command.index("--allowedTools") + 1].split(",")
        journal.ingest_stream(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "model": config.model,
                "claude_code_version": config.expected_version,
                "permissionMode": "dontAsk",
                "tools": tools,
                "skills": ["doctor", "plugin-authoring"] + ([alias] if group == "skill" else []),
                "slash_commands": [alias] if group == "skill" else [],
                "plugins": [],
                "mcp_servers": [{"name": "memos_run", "status": "connected"}],
            }
        )
        for name in ("SessionStart", "UserPromptSubmit"):
            journal.ingest_hook({"hook_event_name": name, "session_id": session, "gate": "allowed"})
        if group == "skill":
            journal.ingest_hook(
                {
                    "hook_event_name": "UserPromptExpansion",
                    "session_id": session,
                    "command_name": alias,
                    "gate": "allowed",
                }
            )
        context = json.loads((cwd.parent / ".claude/context.json").read_text())

        def tool(tool_id, name, arguments, operation):
            journal.ingest_stream(
                {
                    "type": "assistant",
                    "session_id": session,
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}
                        ]
                    },
                }
            )
            journal.ingest_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": session,
                    "tool_use_id": tool_id,
                    "tool_name": name,
                    "gate": "allowed",
                }
            )
            result = operation()
            journal.ingest_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session,
                    "tool_use_id": tool_id,
                    "tool_name": name,
                    "gate": "allowed",
                }
            )
            if tool_id == "check-2" and delivery_mode == "missing":
                return result
            delivered = json.loads(json.dumps(result))
            if tool_id == "check-2" and delivery_mode == "wrong":
                delivered["evidence_ref"]["candidate_snapshot"] = "e" * 64
            journal.ingest_stream(
                {
                    "type": "user",
                    "session_id": session,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "is_error": False,
                                "content": [{"type": "text", "text": json.dumps(delivered)}],
                            }
                        ]
                    },
                }
            )
            return result

        assert (
            tool(
                "check-1",
                "mcp__memos_run__run_visible_checks",
                {},
                lambda: _request_visible_checks(context),
            )["exit_code"]
            == 1
        )

        repaired = task.reference_files["fake_graph.py"]
        if public_regression:
            repaired = repaired.replace(
                "return deepcopy(pair[1])",
                "return None if memory_id == 'one' else deepcopy(pair[1])",
            )

        def repair():
            (cwd / "fake_graph.py").write_text(repaired)
            return {"written": True}

        tool(
            "repair",
            "Write",
            {
                "file_path": str(cwd / "fake_graph.py"),
                "content": repaired,
            },
            repair,
        )
        assert tool(
            "check-2",
            "mcp__memos_run__run_visible_checks",
            {},
            lambda: _request_visible_checks(context),
        )["exit_code"] == (1 if public_regression else 0)
        if group == "skill":
            for index, rule in enumerate(skill.rule_ids):
                declaration = {
                    "rule_id": rule,
                    "phase": "check",
                    "observation": "Synthetic observed scoped checks before and after repair",
                    "decision": "Synthetic fixture confirms the registered rule path",
                }
                tool(
                    "rule-" + str(index),
                    "mcp__memos_run__record_rule",
                    declaration,
                    lambda: {"recorded": True},
                )
                journal.record_rule(declaration)
        journal.ingest_hook({"hook_event_name": "Stop", "session_id": session, "gate": "allowed"})
        journal.ingest_stream(
            {
                "type": "result",
                "subtype": "success",
                "session_id": session,
                "is_error": False,
                "result": "Synthetic host fixture",
                "total_cost_usd": 0,
            }
        )
        return {"returncode": 0, "timed_out": False, "cancelled": False, "elapsed_seconds": 0.01}

    monkeypatch.setattr(runner, "capture_process", simulate)
    unit = "scoped-skill" if group == "skill" else "scoped-none"
    report_path = runner.run_branch_task(
        task.task_id,
        root / "runs" / unit,
        config,
        qualification_directory=tmp_path / "qualification",
        group=group,
        experiment_id=experiment_id,
        expected_task_digest=task_digest(task),
        skill=skill if group == "skill" else None,
        expected_export_hash=export_hash if group == "skill" else None,
    )
    report = json.loads(report_path.read_text())
    assert report["status"] == (
        "incomplete" if public_regression or delivery_mode is not None else "passed"
    ), report
    return {
        "candidate": candidate,
        "export_hash": export_hash,
        "run": report_path.parent,
        "batch": root,
        "qualification": tmp_path / "qualification",
        "proof": proof,
        "allowed": tmp_path,
        "report": report,
    }
