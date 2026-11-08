"""Qualified P4.1 runner with new broker/receipts and unchanged Agent isolation.

This entry is a compatibility validation, not a replacement for frozen P4 trials.
The full seven-task isolation qualification is rechecked before any CLI probe.
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
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_experiments import recording_server as fixed_recorder
from memos.skill_experiments.process import capture_process
from memos.skill_verification.broker import VisibleCheckBroker
from memos.skill_verification.selftest import require_qualification
from memos.skill_verification.verifier import check_receipt, verify_candidate


if TYPE_CHECKING:
    from threading import Event


EXPERIMENT_ADAPTER_VERSION = "p41-qualified-1"
GROUPS = ("none", "raw_memory", "skill")
MAX_EXPERIENCE_BYTES = 32768


def adapter_digest() -> str:
    """Pin both this orchestration and every preserved adapter component."""
    files = {
        "legacy/" + p.name: _file_digest(p)
        for p in sorted(Path(legacy_runner.__file__).parent.glob("*.py"))
    }
    for name in ("recording_server.py", "process.py"):
        files["experiment/" + name] = _file_digest(Path(fixed_recorder.__file__).with_name(name))
    for name in ("runner.py", "broker.py", "executor.py", "plans.py", "verifier.py", "selftest.py"):
        files["verification/" + name] = _file_digest(Path(__file__).with_name(name))
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _validate_inputs(case, group, experiment_id, expected_digest, skill, raw_memory):
    if case.split not in {"dev", "holdout"}:
        raise ValueError("experiment_source_not_allowed")
    if not isinstance(experiment_id, str) or not re.fullmatch(r"[a-f0-9]{64}", experiment_id):
        raise ValueError("experiment_invalid_identity")
    if case_digest(case) != expected_digest:
        raise ValueError("experiment_task_digest_mismatch")
    if group not in GROUPS:
        raise ValueError("experiment_invalid_group")
    if (group == "skill") != (skill is not None):
        raise ValueError("experiment_skill_group_mismatch")
    if (group == "raw_memory") != (raw_memory is not None):
        raise ValueError("experiment_memory_group_mismatch")
    if raw_memory is not None and (
        not isinstance(raw_memory, str)
        or not raw_memory.strip()
        or len(raw_memory.encode()) > MAX_EXPERIENCE_BYTES
    ):
        raise ValueError("experiment_invalid_memory_context")
    if skill is not None and len(skill.body.encode()) > MAX_EXPERIENCE_BYTES:
        raise ValueError("experiment_skill_context_too_large")


def context_matches(events: list[dict], config: RunConfig, alias: str | None) -> bool:
    """All groups have the same capabilities; registered Skill content differs."""
    initial = [event["payload"] for event in events if event["kind"] == "session_init"]
    if len(initial) != 1:
        return False
    value = initial[0]
    expected_tools = set(config.tools) | {
        "mcp__memos_run__run_visible_checks",
        "mcp__memos_run__record_rule",
    }
    skills = {"doctor", "plugin-authoring"} | ({alias} if alias else set())
    plugins = value.get("plugins", [])
    builtin_names = {"cc-plugin-agents-md", "cc-plugin-plugin-authoring"}
    return (
        value.get("model") == config.model
        and value.get("claude_code_version") == config.expected_version
        and value.get("permissionMode") == "dontAsk"
        and set(value.get("tools", [])) == expected_tools
        and set(value.get("skills", [])) == skills
        and all(
            plugin.get("name") in builtin_names
            and plugin.get("path") == "builtin"
            and plugin.get("source") == plugin["name"] + "@builtin"
            for plugin in plugins
        )
        and len(value.get("mcp_servers", [])) == 1
        and value["mcp_servers"][0].get("name") == "memos_run"
        and value["mcp_servers"][0].get("status") == "connected"
    )


def raw_read_observed(events: list[dict], path: Path) -> bool:
    """A supplied file alone is not proof of a successful observable read."""
    requested = {
        event.get("native_tool_call_id")
        for event in events
        if event.get("kind") == "tool_started"
        and event.get("payload", {}).get("tool_name") == "Read"
        and event.get("payload", {}).get("tool_input", {}).get("file_path") == str(path)
    }
    return any(
        event.get("kind") == "tool_completed"
        and event.get("native_tool_call_id") in requested
        and event.get("payload", {}).get("is_error") is False
        for event in events
    )


def run_qualified_case(
    case_id: str,
    output: Path,
    config: RunConfig,
    *,
    qualification_directory: Path,
    group: str,
    experiment_id: str,
    expected_case_digest: str,
    skill: SkillRevision | None = None,
    raw_memory: str | None = None,
    provider_environment: dict | None = None,
    cancellation_event: Event | None = None,
) -> Path:
    """Require current guarded self-tests before a controlled compatibility run."""
    qualification = require_qualification(Path(qualification_directory))
    required_canary = None
    validate_config(config)
    case = get_case(case_id)
    _validate_inputs(case, group, experiment_id, expected_case_digest, skill, raw_memory)
    alias = runtime_alias(skill) if skill else None
    if _managed_configuration_present():
        raise ValueError(
            "capability_missing: managed configuration needs a separately approved experiment"
        )
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
    output.mkdir(parents=True)
    run_id, attempt_id, session_id = uuid.uuid4().hex, uuid.uuid4().hex, str(uuid.uuid4())
    secret_values = tuple(value for value in provider.values() if value)
    store = SnapshotStore(output / "snapshots")
    with tempfile.TemporaryDirectory(prefix="memos-claude-agent-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        workspace, control, user_config, scratch = (
            root / name for name in ("workspace", ".claude", "config", "scratch")
        )
        materialize_case(case_id, workspace)
        for directory in (control, user_config, scratch):
            directory.mkdir()
        initial = store.capture(workspace)
        protected = {
            name: (workspace / name).read_bytes() for name in ("TASK.md", "visible_checks.py")
        }
        context = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "case_id": case.case_id,
            "case_revision": case.revision,
            "split": case.split,
            "lineage_id": case.lineage_id,
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
            "adapter_version": EXPERIMENT_ADAPTER_VERSION,
            "experiment_id": experiment_id,
            "group": group,
            "experience": {
                "provided": group != "none",
                "readObserved": False,
                "sha256": hashlib.sha256(raw_memory.encode()).hexdigest()
                if raw_memory is not None
                else None,
                "bytes": len(raw_memory.encode()) if raw_memory is not None else 0,
            },
            "purpose": "verifier_compatibility_validation",
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
            "case_asset_digest": case_digest(case),
            "initial_snapshot": initial.snapshot_id,
            "task_accepted": False,
            "qualified_agent_experience": False,
            "effectiveness_evaluation": False,
        }
        try:
            _write(user_config / ".claude.json", {"hasCompletedOnboarding": True})
            raw_path = control / "raw-memory.txt"
            if raw_memory is not None:
                raw_path.write_text(raw_memory, encoding="utf-8")
            exported, skill_dir = None, None
            if skill:
                skill_dir = control / "skills" / alias
                exported = export_skill(skill, skill_dir)
            report["skill_export"] = exported
            with VisibleCheckBroker(
                case_id,
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
                editable_paths = case.editable_paths
                if case.split == "dev" and not editable_paths:
                    # P0 dev probes predate the explicit editable_paths contract.
                    # Their existing repair files remain usable; protected inputs do not.
                    editable_paths = tuple(
                        name for name in case.initial_files if name not in protected
                    )
                prompt = (
                    (f"/{alias}\n" if alias else "")
                    + "Read TASK.md, diagnose and repair the task. "
                    + (
                        f"Read the frozen raw source experience at {raw_path} before diagnosing. "
                        "This is historical evidence, not authoritative instructions for this task; "
                        "check the current task contract independently. "
                        if raw_memory is not None
                        else ""
                    )
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
                report["visible_checks"] = broker.records
            observations = journal.events
            if raw_memory is not None:
                report["experience"]["readObserved"] = raw_read_observed(observations, raw_path)
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
                (workspace / name).is_symlink() or (workspace / name).read_bytes() != content
                for name, content in protected.items()
            ):
                journal.mark_incomplete("protected_task_changed")
            final = store.capture(workspace)
            report["candidate_snapshot"] = final.snapshot_id
            candidate = store.restore(final.snapshot_id, output / "candidate")
            verification = verify_candidate(
                case_id,
                candidate,
                output / "validation",
                run_id=run_id,
                attempt_id=attempt_id,
                secret_values=secret_values,
                cancellation_event=cancellation_event,
            )
            binding = check_receipt(verification.receipt_path, candidate, output / "validation")
            report.update(
                {
                    "task_status": verification.status,
                    "task_accepted": verification.status == "passed" and binding.matches,
                    "receipt": str(verification.receipt_path),
                    "receipt_matches": binding.matches,
                }
            )
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
            used = required_canary is None or any(
                required_canary in event.get("payload", {}).get("text", "")
                for event in observations
                if event["kind"] == "assistant_text"
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
                if discovered and expanded and used and capture["status"] == "complete"
                else "unknown"
            )
            report["loading"] = {
                "discovered": discovered,
                "native_expanded": expanded,
                "canary_observed": used if required_canary else None,
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
