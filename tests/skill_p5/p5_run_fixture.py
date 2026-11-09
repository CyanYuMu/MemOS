"""Synthetic native events plus real isolated C2 checks, never model evidence."""

import hashlib
import json

from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias
from memos.skill_adapter.recording_server import _request_visible_checks
from memos.skill_adapter.runner import RunConfig
from memos.skill_drafting.domain import hash_json
from memos.skill_p5 import batch, qualification, runner
from memos.skill_p5.execution_policy import PROMPT, policy_identity
from memos.skill_p5.tasks import get_suite, suite_digest


CANDIDATE_REVISION = "a" * 64


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def make_run(
    tmp_path,
    monkeypatch,
    *,
    group="skill",
    suite_id="update-repair-r1",
    dream_regression=False,
    delivery_mode=None,
    after_final=False,
    over_budget=False,
    cancelled=False,
    initial_only=False,
    skip_checks=False,
    public_exit_code=None,
    native_returncode=0,
    native_timed_out=False,
    native_error=False,
    execution_guard=None,
    final_caller=None,
    candidate_override=None,
):
    suite = get_suite(suite_id)
    rule_targets = {
        "check_update_contract": [
            "exit:not_applicable",
            "exit:needs_evidence",
            "repair_and_verify_update",
        ],
        "repair_and_verify_update": ["exit:success", "exit:needs_evidence", "exit:unresolved"],
    }
    intents = {
        name: (
            ["check_update_contract#0"]
            if name == "scope-outside-r1"
            else ["check_update_contract#1"]
            if name == "contract-missing-r1"
            else [
                "check_update_contract#2",
                "repair_and_verify_update#"
                + (
                    "2"
                    if name == "update-unresolved-r1"
                    else "1"
                    if name == "check-unavailable-r1"
                    else "0"
                ),
            ]
        )
        for name in batch.SUITES
    }
    candidate = {
        "skill_id": "synthetic-p5-repair",
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
    if candidate_override is not None:
        candidate = candidate_override
    skill = SkillRevision(
        candidate["skill_id"],
        candidate["revision"],
        "Synthetic management integration fixture, not source evidence.",
        tuple(rule_targets),
    )
    export_hash = export_skill(skill, tmp_path / ("fixed-export-" + group))["export_hash"]
    alias = runtime_alias(skill)
    cli = tmp_path / "bin/claude"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    config = RunConfig(cli_path=cli, model="synthetic-host-fixture")
    proofs = {
        name: {"qualificationId": "b" * 64, "binding": qualification._binding(name)}
        for name in batch.SUITES
    }
    monkeypatch.setattr(runner, "require_qualification", lambda _, **kw: proofs[kw["suite_id"]])
    identity = {
        "protocol": "p5-batch-1",
        "planPurpose": "release",
        "batchNonce": hash_json(str(tmp_path))[:32],
        "batchDigest": sha(batch.__file__),
        "executionPolicy": policy_identity(),
        "adapterDigest": runner.adapter_digest(),
        "configurationDigest": "c" * 64,
        "qualification": proofs,
        "branchIntents": intents,
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
        "maxAgentRuns": 7,
        "newIndependentSamples": 0,
        "branchCoverage": [],
        "effectivenessEvaluation": False,
    }
    experiment_id = hash_json(identity)
    root = tmp_path / "batch"
    root.mkdir(exist_ok=True)
    write_json(root / "manifest.json", {**identity, "experimentId": experiment_id})

    def simulate(command, cwd, env, journal, timeout, **kwargs):
        if execution_guard is not None:
            assert execution_guard.ticket is not None
        assert any(PROMPT in argument for argument in command)
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
                                "is_error": bool(result.get("execution_error"))
                                if isinstance(result, dict)
                                else False,
                                "content": json.dumps(delivered)
                                if isinstance(result, dict) and result.get("execution_error")
                                else [{"type": "text", "text": json.dumps(delivered)}],
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
                if variant == "reference" and name == "caller.py" and final_caller is not None:
                    content = final_caller
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
            check()
        declare("check_update_contract", "select")
        if suite.outcome not in {"out_of_scope", "missing_evidence"}:
            declare("repair_and_verify_update", "select")
        if (
            not initial_only
            and not skip_checks
            and suite.outcome in {"repair", "unresolved", "tool_failure"}
        ):
            if over_budget:
                for _ in range(2):
                    write_variant("initial")
                    check()
            write_variant("reference")
            check()
            declare("repair_and_verify_update", "exit")
            if after_final:
                write_variant("reference")
        tool("Read", {"file_path": str(cwd / "TASK.md")}, lambda: "Synthetic task inspected")
        journal.ingest_stream(
            {
                "type": "assistant",
                "session_id": session,
                "message": {
                    "content": [{"type": "text", "text": "Synthetic observed outcome reported."}]
                },
            }
        )
        journal.ingest_hook({"hook_event_name": "Stop", "session_id": session, "gate": "allowed"})
        journal.ingest_stream(
            {
                "type": "result",
                "subtype": "error_during_execution" if native_error else "success",
                "session_id": session,
                "is_error": native_error,
                "result": "Synthetic host fixture",
                "total_cost_usd": 0,
            }
        )
        return {
            "returncode": native_returncode,
            "timed_out": native_timed_out,
            "cancelled": cancelled,
            "elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(runner, "capture_process", simulate)
    unit = suite.suite_id + "-skill" if group == "skill" else "p5-none"
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
        execution_guard=execution_guard,
        execution_metadata={
            "batchId": experiment_id,
            "unitId": unit,
            "caseId": suite.task_id,
            "group": group,
            "mode": "independent_validation",
            "contentHash": candidate["content_hash"],
            "exportHash": export_hash,
        }
        if execution_guard
        else None,
    )
    report = json.loads(report_path.read_text())
    return {
        "candidate": candidate,
        "config": config,
        "skill": skill,
        "export_hash": export_hash,
        "run": report_path.parent,
        "batch": root,
        "qualification": tmp_path / "qualification",
        "proofs": proofs,
        "allowed": tmp_path,
        "report": report,
    }


def make_pair(
    tmp_path, monkeypatch, *, calibration_options=None, trial_options=None, candidate_override=None
):
    calibration = make_run(
        tmp_path,
        monkeypatch,
        group="none",
        candidate_override=candidate_override,
        **(calibration_options or {}),
    )
    trial = make_run(
        tmp_path, monkeypatch, candidate_override=candidate_override, **(trial_options or {})
    )
    trial["calibration"] = calibration
    return trial
