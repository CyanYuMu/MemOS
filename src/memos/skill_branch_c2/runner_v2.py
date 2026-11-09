"""Versioned scoped repair orchestration preserving initially healthy Dream.

This protocol collects reviewable evidence; it never grants branch coverage or
publishes a Skill. Preserved adapters only provide generic native capture.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from memos.skill_adapter import runner as legacy_runner
from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias, verify_export
from memos.skill_adapter.events import EventRecorder
from memos.skill_adapter.isolation import (
    IsolationSpec,
    build_profile,
    probe_isolation,
    runtime_paths,
    sandbox_command,
)
from memos.skill_adapter.runner import (
    _HOOKS,
    RunConfig,
    _environment,
    _file_digest,
    _managed_configuration_present,
    _write,
    command_for,
    successful_run,
    validate_config,
)
from memos.skill_branch_c2 import runner as frozen_runner
from memos.skill_branch_c2.broker import VisibleCheckBroker
from memos.skill_branch_c2.execution_policy import PROMPT, policy_identity
from memos.skill_branch_c2.process import (
    build_process_evidence,
    initial_checks_match,
)
from memos.skill_branch_c2.qualification import require_qualification
from memos.skill_branch_c2.runner import (
    _declarations_complete,
    _validate_inputs,
    _visible_complete,
    _visible_deliveries,
)
from memos.skill_branch_c2.tasks import get_suite, materialize, suite_digest
from memos.skill_branch_c2.verifier import _no_symlinks, check_receipt, verify_suite
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_experiments import recording_server as fixed_recorder
from memos.skill_experiments.process import capture_process
from memos.skill_verification import runner as qualified_runner
from memos.skill_verification.runner import context_matches
from memos.skill_verification.verifier import _read


if TYPE_CHECKING:
    from threading import Event


ADAPTER_VERSION = "c2-claude-2"


def adapter_digest(suite_id: str = "scoped-healthy-dream-c2-v1") -> str:
    """Bind this protocol, its current qualification and the unchanged native capture."""
    from memos.skill_branch_c2 import process, qualification
    from memos.skill_branch_validation import runner as delivery_helper

    files = {
        "legacy/" + p.name: _file_digest(p)
        for p in sorted(Path(legacy_runner.__file__).parent.glob("*.py"))
    }
    for name in ("recording_server.py", "process.py"):
        files["experiment/" + name] = _file_digest(Path(fixed_recorder.__file__).with_name(name))
    files["c2-v2/runner"] = _file_digest(Path(__file__))
    files["c2-v1/helpers"] = _file_digest(Path(frozen_runner.__file__))
    files["executionPolicy"] = policy_identity()
    files["extension/process"] = _file_digest(Path(process.__file__))
    files["delivery_helper"] = _file_digest(Path(delivery_helper.__file__))
    files["context_helper"] = _file_digest(Path(qualified_runner.__file__))
    files["qualification"] = qualification._binding(suite_id)
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def run_extension_task(
    suite_id: str,
    output: Path,
    config: RunConfig,
    *,
    qualification_directory: Path,
    group: str,
    experiment_id: str,
    expected_suite_digest: str,
    skill: SkillRevision | None = None,
    expected_export_hash: str | None = None,
    provider_environment: dict | None = None,
    cancellation_event: Event | None = None,
) -> Path:
    """Require fresh qualification and bind both task states after the Agent exits."""
    qualification = require_qualification(Path(qualification_directory), suite_id=suite_id)
    validate_config(config)
    task = get_suite(suite_id)
    expected_export = _validate_inputs(
        task, group, experiment_id, expected_suite_digest, skill, expected_export_hash
    )
    alias = runtime_alias(skill) if skill else None
    if _managed_configuration_present():
        raise ValueError(
            "capability_missing: managed configuration needs a separately approved experiment"
        )
    _no_symlinks(Path(output))
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Run output already exists")
    provider = provider_environment or {}
    # Validate before creating artifacts or invoking the provider.
    _environment(provider, Path("/unused"), Path("/unused"))
    try:
        version = subprocess.run(
            [str(config.cli_path.resolve()), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("capability_missing: CLI version probe failed") from error
    if version.returncode or version.stdout.strip() != f"{config.expected_version} (Claude Code)":
        raise ValueError("capability_missing: CLI version mismatch")
    executable_hash = _file_digest(config.cli_path.resolve())
    adapter_hash = adapter_digest(suite_id)
    output.mkdir(parents=True, mode=0o700)
    # Bind native events and receipts to this batch without changing the sealed
    # recorder context schema (the verifier permits identifiers up to 80 chars).
    run_id = experiment_id + "-" + group + "-" + uuid.uuid4().hex[:8]
    attempt_id, session_id = uuid.uuid4().hex, str(uuid.uuid4())
    secret_values = tuple(value for value in provider.values() if value)
    store = SnapshotStore(output / "snapshots")
    with tempfile.TemporaryDirectory(prefix="memos-claude-agent-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        workspace, control, user_config, scratch = (
            root / name for name in ("workspace", ".claude", "config", "scratch")
        )
        materialize(suite_id, workspace)
        for directory in (control, user_config, scratch):
            directory.mkdir()
        initial = store.capture(workspace)
        protected = {
            name: (workspace / name).read_bytes() for name in ("TASK.md", "visible_checks.py")
        }
        context = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "case_id": task.task_id,
            "case_revision": task.revision,
            "split": "holdout",
            "lineage_id": task.lineage_id,
            "initial_snapshot": initial.snapshot_id,
        }
        if skill:
            context.update({"skill_id": skill.skill_id, "skill_revision": skill.revision})
        journal = EventRecorder(
            output / "events.jsonl",
            context,
            rule_ids=skill.rule_ids if skill else (),
            secret_values=secret_values,
            expected_session_id=session_id,
        )
        report = {
            "schema_version": 1,
            "adapter_version": ADAPTER_VERSION,
            "execution_policy": policy_identity(),
            "experiment_id": experiment_id,
            "group": group,
            "experience": {
                "provided": group != "none",
                "readObserved": False,
                "sha256": expected_export_hash,
                "bytes": len(skill.body.encode()) if skill else 0,
            },
            "purpose": "branch_extension_validation" if skill else "protocol_calibration",
            "suite_id": suite_id,
            "check_mapping": list(task.checks),
            "task_purpose": task.purpose,
            "branch_coverage": [],
            "process_evidence": None,
            "intermediate_verifications": [],
            "verification_qualification": qualification,
            "run_context": context,
            "session_id": session_id,
            "status": "incomplete",
            "cli_version": config.expected_version,
            "cli_sha256": executable_hash,
            "adapter_sha256": adapter_hash,
            "requested_model": config.model,
            "max_turns": config.max_turns,
            "max_budget_usd": config.max_budget_usd,
            "effort": config.effort,
            "timeout_seconds": config.timeout_seconds,
            "tools": list(config.tools),
            "case_asset_digest": suite_digest(task),
            "initial_snapshot": initial.snapshot_id,
            "task_accepted": False,
            "visible_checks": [],
            "visible_checks_complete": False,
            "visible_delivery_refs": [],
            "declarations_complete": skill is None,
            "qualified_agent_experience": False,
            "effectiveness_evaluation": False,
        }
        try:
            _write(user_config / ".claude.json", {"hasCompletedOnboarding": True})
            exported, skill_dir = None, None
            if skill:
                skill_dir = control / "skills" / alias
                exported = export_skill(skill, skill_dir)
                if exported != expected_export:
                    raise ValueError("extension_export_changed")
            report["skill_export"] = exported
            with VisibleCheckBroker(
                suite_id,
                workspace,
                output / "visible-checks",
                secret_values=secret_values,
                cancellation_event=cancellation_event,
            ) as broker:
                hook_context = {
                    **context,
                    "runtime_alias": alias,
                    "workspace": str(workspace),
                    "rule_ids": list(skill.rule_ids) if skill else [],
                    "test_broker_socket": str(broker.socket_path),
                    "test_broker_timeout_seconds": 20,
                }
                if skill:
                    hook_context.update(
                        {
                            "skill_path": str(skill_dir / "SKILL.md"),
                            "skill_sha256": exported["files"]["SKILL.md"],
                        }
                    )
                _write(control / "context.json", hook_context)
                shutil.copyfile(
                    Path(legacy_runner.__file__).with_name("hooks.py"), control / "hook.py"
                )
                shutil.copyfile(Path(fixed_recorder.__file__), control / "recording_server.py")
                python = str(Path(sys.executable).resolve())
                hook_command = shlex.join(
                    [python, "-I", "-B", str(control / "hook.py"), str(control / "context.json")]
                )
                settings = {
                    "disableBundledSkills": True,
                    "autoMemoryEnabled": False,
                    "syncClaudeAiSkills": False,
                    "syncClaudeAiPlugins": False,
                    "hooks": {
                        name: [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": hook_command,
                                        "timeout": 5,
                                        "onFailure": "block",
                                    }
                                ]
                            }
                        ]
                        for name in _HOOKS
                    },
                }
                _write(control / "settings.json", settings)
                _write(
                    control / "mcp.json",
                    {
                        "mcpServers": {
                            "memos_run": {
                                "command": python,
                                "args": [
                                    "-I",
                                    "-B",
                                    str(control / "recording_server.py"),
                                    "--context",
                                    str(control / "context.json"),
                                ],
                            }
                        }
                    },
                )
                repository = Path(__file__).resolve().parents[3]
                runtimes = (config.cli_path.resolve().parent, *runtime_paths(Path(sys.executable)))
                isolation = IsolationSpec(
                    workspace=workspace,
                    control_root=control,
                    config_root=user_config,
                    runtime_read_roots=runtimes,
                    forbidden_roots=(repository, output),
                    writable_scratch=(scratch,),
                    network=True,
                    allowed_unix_sockets=(broker.socket_path,),
                )
                probe = probe_isolation(isolation)
                _write(output / "isolation.json", probe)
                if not probe["passed"]:
                    raise ValueError("OS isolation probe failed")
                report["profile_sha256"] = hashlib.sha256(
                    build_profile(isolation).encode()
                ).hexdigest()
                control_digest = store.capture(control).snapshot_id
                report["control_snapshot"] = control_digest
                available_tools = [*config.tools, "mcp__memos_run__run_visible_checks"]
                available_tools.append("mcp__memos_run__record_rule")
                editable_paths = task.editable_paths
                prompt = (
                    (f"/{alias}\n" if alias else "")
                    + "Read TASK.md, diagnose and repair the task. "
                    + f"Available tools: {', '.join(available_tools)}. Use only these tools. "
                    + "Do not request Bash, shell commands, or arbitrary program execution. "
                    + f"Editable task paths: {', '.join(editable_paths) or '(none)'}. "
                    + "Only modify these paths relative to the task workspace. "
                    + "Do not create scratch files, temporary tests, or extra files; "
                    + "do not write outside the task workspace. "
                    + "Keep TASK.md and visible_checks.py unchanged. "
                    + "Run checks only with mcp__memos_run__run_visible_checks. "
                    + "Obtain an initial check before editing and recheck after edits. "
                    + PROMPT
                    + "Each check returns all applicable groups as structured JSON in stdout. "
                    + "The host performs additional independent acceptance after you finish; "
                    + "do not recreate or execute extra tests yourself. "
                    + (
                        ""
                        if skill
                        else "This task starts without a Skill; do not load a Skill or invoke slash commands. "
                    )
                    + (
                        "The record_rule tool is available uniformly, but this group has no registered "
                        "Skill rules; do not invent or submit rule IDs. "
                        if skill is None
                        else ""
                    )
                    + "Report the observed results and any remaining limitations."
                )
                command = command_for(
                    config,
                    control,
                    prompt,
                    session_id,
                    runtime_alias=alias,
                    recorder=True,
                )
                report["execution"] = capture_process(
                    sandbox_command(command, isolation),
                    workspace,
                    _environment(provider, user_config, scratch),
                    journal,
                    config.timeout_seconds,
                    **(
                        {"cancellation_event": cancellation_event}
                        if cancellation_event is not None
                        else {}
                    ),
                )
                report["visible_checks"] = list(broker.records)
                if report["execution"].get("cancelled"):
                    # Preserve final snapshot and partial evidence promptly. Do not
                    # start another verifier process while the service is stopping.
                    raise ValueError("experiment_cancelled")
                if (
                    store.capture(control).snapshot_id != control_digest
                    or _file_digest(config.cli_path.resolve()) != executable_hash
                    or adapter_digest(suite_id) != adapter_hash
                ):
                    journal.mark_incomplete("artifact_mismatch")
                if skill:
                    verify_export(skill, skill_dir)
            observations = journal.events
            matching_context = context_matches(observations, config, alias)
            reported_models = {
                name
                for event in observations
                if event["kind"] == "native_result"
                for name in event["payload"].get("modelUsage", {})
            }
            report["reported_models"] = sorted(reported_models)
            if reported_models and reported_models != {config.model}:
                matching_context = False
            report["context_matches"] = matching_context
            if not matching_context:
                journal.mark_incomplete("runtime_context_mismatch")
            if not {"SessionStart", "UserPromptSubmit", "Stop"} <= {
                event["kind"] for event in observations
            }:
                journal.mark_incomplete("required_hook_missing")
            if any(
                not (workspace / name).is_file()
                or (workspace / name).is_symlink()
                or (workspace / name).read_bytes() != content
                for name, content in protected.items()
            ):
                journal.mark_incomplete("protected_task_changed")
            final = store.capture(workspace)
            report["candidate_snapshot"] = final.snapshot_id
            candidate = store.restore(final.snapshot_id, output / "candidate")
            initial_candidate = store.restore(initial.snapshot_id, output / "initial-candidate")
            initial_verification = verify_suite(
                suite_id,
                initial_candidate,
                output / "validation",
                run_id=run_id,
                attempt_id=attempt_id + "-initial",
                secret_values=secret_values,
                cancellation_event=cancellation_event,
            )
            initial_binding = check_receipt(
                initial_verification.receipt_path, initial_candidate, output / "validation"
            )
            verification = verify_suite(
                suite_id,
                candidate,
                output / "validation",
                run_id=run_id,
                attempt_id=attempt_id,
                secret_values=secret_values,
                cancellation_event=cancellation_event,
            )
            binding = check_receipt(verification.receipt_path, candidate, output / "validation")
            changed = any(
                not (candidate / name).is_file()
                or (candidate / name).is_symlink()
                or (candidate / name).read_bytes() != task.initial_files[name].encode()
                for name in task.editable_paths
            )
            report.update(
                {
                    "initial_status": initial_verification.status,
                    "initial_receipt": str(initial_verification.receipt_path),
                    "initial_receipt_matches": initial_binding.matches,
                    "initial_receipt_sha256": _file_digest(initial_verification.receipt_path),
                    "task_status": verification.status,
                    "behavior_changed": changed,
                    "task_accepted": initial_verification.status == "failed"
                    and initial_binding.matches
                    and initial_checks_match(
                        suite_id, _read(initial_verification.receipt_path)["checks"]
                    )
                    and verification.status == "passed"
                    and binding.matches
                    and changed,
                    "receipt": str(verification.receipt_path),
                    "receipt_matches": binding.matches,
                }
            )
            deliveries = _visible_deliveries(report["visible_checks"], observations, secret_values)
            report["visible_delivery_refs"] = deliveries or []
            public_accepted = _visible_complete(
                report["visible_checks"],
                output / "visible-checks",
                initial.snapshot_id,
                final.snapshot_id,
            )
            report["process_evidence"] = build_process_evidence(
                suite_id, report["visible_checks"], observations, deliveries
            )
            if not report["process_evidence"]["complete"]:
                journal.mark_incomplete("process_evidence_incomplete")
            # C2 requires no manufactured intermediate state. Every public
            # checkpoint is retained, and only initial/final hidden checks run.
            report["intermediate_verifications"] = []
            # Full capture and final behavior are separate from budget compliance.
            # An over-budget repair can pass behavior, but cannot gain correct use.
            report["task_accepted"] = (
                report["task_accepted"]
                and public_accepted
                and report["visible_checks"][-1]["result"]["exit_code"] == 0
                and report["process_evidence"]["complete"]
                and initial_checks_match(
                    suite_id, report["process_evidence"]["checkpoints"][0]["checks"]
                )
            )
            report["visible_checks_complete"] = deliveries is not None and public_accepted
            if not report["visible_checks_complete"]:
                journal.mark_incomplete("visible_checks_incomplete")
            report["declarations_complete"] = not skill or _declarations_complete(observations)
            if not report["declarations_complete"]:
                journal.mark_incomplete("rule_declarations_incomplete")
            discovered = skill is not None and any(
                alias in event.get("payload", {}).get("slash_commands", [])
                for event in observations
                if event["kind"] == "session_init"
            )
            expanded = skill is not None and any(
                event["kind"] == "UserPromptExpansion"
                and event.get("payload", {}).get("command_name") == alias
                and event.get("payload", {}).get("gate") == "allowed"
                for event in observations
            )
            # The provider may take minutes; qualification must still describe
            # the executor that produced this final receipt when the run ends.
            if (
                adapter_digest(suite_id) != adapter_hash
                or require_qualification(Path(qualification_directory), suite_id=suite_id)
                != qualification
            ):
                journal.mark_incomplete("verification_qualification_changed")
            capture = journal.finish()
            loaded = (
                "not_requested"
                if not skill
                else "verified"
                if discovered and expanded and capture["status"] == "complete"
                else "unknown"
            )
            report["loading"] = {
                "discovered": discovered,
                "native_expanded": expanded,
                "canary_observed": None,
                "status": loaded,
            }
            if skill is not None:
                report["experience"].update(
                    sha256=exported["export_hash"],
                    bytes=len(skill.body.encode()),
                    readObserved=expanded,
                )
            report["capture"] = capture
            if successful_run(capture, report["execution"], loaded, True, matching_context):
                report["status"] = "passed" if report["task_accepted"] else "failed"
        except Exception as error:
            # Errors are categories only: exception messages may carry provider data.
            report["failure_type"] = type(error).__name__
            with suppress(ValueError):
                journal.mark_incomplete("runner_failed")
            report["capture"] = journal.finish()
        finally:
            if not (output / "candidate").exists():
                try:
                    final = store.capture(workspace)
                    report["candidate_snapshot"] = final.snapshot_id
                    store.restore(final.snapshot_id, output / "candidate")
                except (ValueError, OSError):
                    report["candidate_snapshot"] = None
                    # Preserve rejected inputs without following escaped symlinks.
                    with suppress(OSError):
                        workspace.rename(output / "quarantine")
                    report["candidate_quarantined"] = (output / "quarantine").exists()
            report["events_sha256"] = _file_digest(output / "events.jsonl")
            if "receipt" in report:
                report["receipt_sha256"] = _file_digest(Path(report["receipt"]))
            _write(output / "report.json", report)
    return output / "report.json"
