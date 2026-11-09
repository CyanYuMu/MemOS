"""Synthetic native events plus real isolated C2 checks, never model evidence."""

import hashlib
import json

from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias
from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_adapter.runner import RunConfig
from memos.skill_branch_c2 import batch, qualification, runner
from memos.skill_branch_c2.tasks import get_suite, suite_digest
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION
from memos.skill_drafting.domain import hash_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def make_run(
    tmp_path,
    monkeypatch,
    *,
    group="skill",
    dream_regression=False,
    delivery_mode=None,
    after_final=False,
    over_budget=False,
    cancelled=False,
    initial_only=False,
    skip_checks=False,
    public_exit_code=None,
):
    suite = get_suite("scoped-healthy-dream-c2-v1")
    rule_targets = {
        "confirm_contract": [
            "exit:not_applicable",
            "exit:needs_evidence",
            "exit:success",
            "repair_scoped_reads",
            "repair_dream_update",
        ],
        "repair_scoped_reads": ["verify_scoped_reads"],
        "verify_scoped_reads": ["repair_dream_update", "verify_dream_update", "exit:success"],
        "repair_dream_update": ["verify_dream_update"],
        "verify_dream_update": ["exit:success"],
    }
    candidate = {
        "skill_id": "synthetic-c2-repair",
        "revision": CANDIDATE_REVISION,
        "tags": ["management_test"],
        "source_refs": [
            {"source_id": "d" * 64, "case_id": "synthetic-source", "lineage_id": "synthetic-source"}
        ],
        "rules": [
            {
                "rule_id": name,
                "branches": [
                    {"when": "Synthetic " + target, "target": target} for target in targets
                ],
            }
            for name, targets in rule_targets.items()
        ],
    }
    candidate["content_hash"] = hash_json(candidate)
    skill = SkillRevision(
        candidate["skill_id"],
        CANDIDATE_REVISION,
        "Synthetic management integration fixture, not source evidence.",
        tuple(rule_targets),
    )
    export_hash = export_skill(skill, tmp_path / "fixed-export")["export_hash"]
    alias = runtime_alias(skill)
    cli = tmp_path / "bin/claude"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    config = RunConfig(cli_path=cli, model="synthetic-host-fixture")
    proof = {
        "qualificationId": "b" * 64,
        "binding": qualification._binding("scoped-healthy-dream-c2-v1"),
    }
    monkeypatch.setattr(runner, "require_qualification", lambda _, **kw: proof)
    identity = {
        "protocol": "c2-batch-1",
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
                "suiteId": suite_id,
                "taskId": get_suite(suite_id).task_id,
                "group": arm,
                "taskDigest": suite_digest(get_suite(suite_id)),
            }
            for name, suite_id, arm in batch.UNITS
        ],
        "maxAgentRuns": 2,
        "newIndependentSamples": 0,
        "branchCoverage": [],
        "effectivenessEvaluation": False,
    }
    experiment_id = hash_json(identity)
    root = tmp_path / "batch"
    root.mkdir()
    write_json(root / "manifest.json", {**identity, "experimentId": experiment_id})

    def simulate(command, cwd, env, journal, timeout, **kwargs):
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
        calls = 0
        checks = 0

        def tool(name, arguments, operation):
            nonlocal calls
            calls += 1
            tool_id = "tool-" + str(calls)
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
            if name.endswith("run_visible_checks") and checks == 2 and delivery_mode == "missing":
                return result
            delivered = json.loads(json.dumps(result))
            if name.endswith("run_visible_checks") and checks == 2 and delivery_mode == "wrong":
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

        def check():
            nonlocal checks
            checks += 1
            return tool(
                "mcp__memos_run__run_visible_checks", {}, lambda: _request_visible_checks(context)
            )

        def declare(rule, phase="act"):
            if group != "skill":
                return
            declaration = {
                "rule_id": rule,
                "phase": phase,
                "observation": "Synthetic complete grouped check observed before action",
                "decision": "Synthetic fixture follows the registered contract",
            }
            tool("mcp__memos_run__record_rule", declaration, lambda: {"recorded": True})
            journal.record_rule(declaration)

        def write_variant(variant):
            for name in suite.editable_paths:
                content = suite.variants[variant][name]
                if (
                    public_exit_code is not None
                    and variant == "reference"
                    and name == "fake_graph.py"
                ):
                    content += f"\nraise SystemExit({public_exit_code})\n"

                def write(path=cwd / name, value=content):
                    path.write_text(value)
                    return {"written": True}

                tool("Write", {"file_path": str(cwd / name), "content": content}, write)

        if not skip_checks:
            assert check()["exit_code"] == 1
        declare("confirm_contract", "select")
        if not initial_only and not skip_checks:
            declare("repair_scoped_reads")
            if over_budget:
                for _ in range(2):
                    write_variant("initial")
                    assert check()["exit_code"] == 1
            if dream_regression:
                write_variant("unsafe_update_arguments")
                assert check()["exit_code"] == 1
            write_variant("reference")
            assert check()["exit_code"] == (public_exit_code or 0)
            declare("verify_scoped_reads", "select")
            declare("verify_dream_update", "exit")
            if after_final:
                write_variant("reference")
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
        return {
            "returncode": 0,
            "timed_out": False,
            "cancelled": cancelled,
            "elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(runner, "capture_process", simulate)
    unit = "c2-skill" if group == "skill" else "c2-none"
    report_path = runner.run_extension_task(
        suite.suite_id,
        root / "runs" / unit,
        config,
        qualification_directory=tmp_path / "qualification",
        group=group,
        experiment_id=experiment_id,
        expected_suite_digest=suite_digest(suite),
        skill=skill if group == "skill" else None,
        expected_export_hash=export_hash if group == "skill" else None,
    )
    report = json.loads(report_path.read_text())
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
