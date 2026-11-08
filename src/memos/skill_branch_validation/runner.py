"""Explicit branch-task orchestration with initial/final receipts and fixed Skill.

This protocol collects reviewable evidence; it never grants branch coverage or
publishes a Skill. Preserved adapters only provide generic native capture.
"""

from __future__ import annotations

import hashlib
import json
import re
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
from memos.skill_adapter.events import EventRecorder, sanitize_public
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
from memos.skill_branch_validation.broker import VisibleCheckBroker, check_visible_record
from memos.skill_branch_validation.qualification import require_qualification
from memos.skill_branch_validation.tasks import (
    CANDIDATE_REVISION,
    get_task,
    materialize,
    task_digest,
)
from memos.skill_branch_validation.verifier import _no_symlinks, check_receipt, verify_task
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_experiments import recording_server as fixed_recorder
from memos.skill_experiments.process import capture_process
from memos.skill_verification import runner as qualified_runner
from memos.skill_verification.runner import context_matches


if TYPE_CHECKING:
    from threading import Event


ADAPTER_VERSION = "branch-claude-1"


def adapter_digest() -> str:
    """Bind this protocol, its current qualification and the unchanged native capture."""
    from memos.skill_branch_validation import qualification

    files = {
        "legacy/" + p.name: _file_digest(p)
        for p in sorted(Path(legacy_runner.__file__).parent.glob("*.py"))
    }
    for name in ("recording_server.py", "process.py"):
        files["experiment/" + name] = _file_digest(Path(fixed_recorder.__file__).with_name(name))
    files["branch/runner"] = _file_digest(Path(__file__))
    files["context_helper"] = _file_digest(Path(qualified_runner.__file__))
    files["qualification"] = qualification._binding()
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _validate_inputs(task, group, experiment_id, expected_digest, skill, expected_export_hash):
    if not isinstance(experiment_id, str) or not re.fullmatch(r"[a-f0-9]{64}", experiment_id):
        raise ValueError("branch_invalid_identity")
    if task_digest(task) != expected_digest:
        raise ValueError("branch_task_digest_mismatch")
    if group not in ("none", "skill") or (group == "skill") != (skill is not None):
        raise ValueError("branch_skill_group_mismatch")
    if skill is None:
        if expected_export_hash is not None:
            raise ValueError("branch_unexpected_export_hash")
        return None
    if skill.revision != CANDIDATE_REVISION or len(skill.body.encode()) > 32768:
        raise ValueError("branch_skill_revision_mismatch")
    # Before probing the CLI, render and check the complete fixed export bytes.
    with tempfile.TemporaryDirectory(prefix="memos-branch-export-") as directory:
        manifest = export_skill(skill, Path(directory).resolve() / "skill")
    if manifest["export_hash"] != expected_export_hash:
        raise ValueError("branch_export_hash_mismatch")
    return manifest


def _visible_complete(records, evidence, initial_snapshot, final_snapshot):
    if (
        len(records) < 2
        or records[0]["candidate_snapshot"] != initial_snapshot
        or records[-1]["candidate_snapshot"] != final_snapshot
        or records[0]["result"]["exit_code"] != 1
        or records[-1]["result"]["exit_code"] != 0
    ):
        return False
    store = SnapshotStore(evidence / "snapshots")
    for record in records:
        with tempfile.TemporaryDirectory(prefix="memos-branch-visible-binding-") as directory:
            candidate = store.restore(record["candidate_snapshot"], Path(directory) / "candidate")
            path = evidence / f"check-{record['check_index']}.json"
            if not check_visible_record(path, candidate) or record["result"].get("execution_error"):
                return False
    return True


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
                if (
                    payload.get("is_error") is not False
                    or not isinstance(content, list)
                    or len(content) != 1
                    or not isinstance(content[0], dict)
                    or set(content[0]) != {"type", "text"}
                    or content[0]["type"] != "text"
                ):
                    return None
                delivered = json.loads(content[0]["text"], object_pairs_hook=unique)
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


def _declarations_complete(events):
    declarations = [event for event in events if event["kind"] == "rule_declaration"]
    calls = [
        event["payload"].get("tool_input")
        for event in events
        if event["kind"] == "tool_started"
        and event["payload"].get("tool_name") == "mcp__memos_run__record_rule"
    ]
    return (
        bool(declarations)
        and len(calls) == len(declarations)
        and all(
            isinstance(call, dict) and set(call) == {"rule_id", "phase", "observation", "decision"}
            for call in calls
        )
    )


def run_branch_task(
    task_id: str,
    output: Path,
    config: RunConfig,
    *,
    qualification_directory: Path,
    group: str,
    experiment_id: str,
    expected_task_digest: str,
    skill: SkillRevision | None = None,
    expected_export_hash: str | None = None,
    provider_environment: dict | None = None,
    cancellation_event: Event | None = None,
) -> Path:
    """Require fresh qualification and bind both task states after the Agent exits."""
    qualification = require_qualification(Path(qualification_directory))
    validate_config(config)
    task = get_task(task_id)
    expected_export = _validate_inputs(
        task, group, experiment_id, expected_task_digest, skill, expected_export_hash
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
    adapter_hash = adapter_digest()
    output.mkdir(parents=True, mode=0o700)
    run_id, attempt_id, session_id = uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4())
    secret_values = tuple(value for value in provider.values() if value)
    store = SnapshotStore(output / "snapshots")
    with tempfile.TemporaryDirectory(prefix="memos-claude-agent-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        workspace, control, user_config, scratch = (
            root / name for name in ("workspace", ".claude", "config", "scratch")
        )
        materialize(task_id, workspace)
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
            "experiment_id": experiment_id,
            "group": group,
            "experience": {
                "provided": group != "none",
                "readObserved": False,
                "sha256": expected_export_hash,
                "bytes": len(skill.body.encode()) if skill else 0,
            },
            "purpose": "branch_validation" if skill else "protocol_calibration",
            "task_purpose": task.purpose,
            "branch_coverage": [],
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
            "case_asset_digest": task_digest(task),
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
                    raise ValueError("branch_export_changed")
            report["skill_export"] = exported
            with VisibleCheckBroker(
                task_id,
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
                    or adapter_digest() != adapter_hash
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
            initial_verification = verify_task(
                task_id,
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
            verification = verify_task(
                task_id,
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
            # Independent hidden inputs supplement the public contract. Passing
            # them cannot excuse a remaining failure on the public input set.
            report["task_accepted"] = report["task_accepted"] and public_accepted
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
                adapter_digest() != adapter_hash
                or require_qualification(Path(qualification_directory)) != qualification
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
            if successful_run(
                capture, report["execution"], loaded, report["task_accepted"], matching_context
            ):
                report["status"] = "passed"
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
