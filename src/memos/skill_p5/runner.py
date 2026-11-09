"""Fixed Dream task execution with isolated checks and host-issued identities.

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
from memos.skill_branch_c2.runner import _declarations_complete
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_experiments import recording_server as fixed_recorder
from memos.skill_experiments.process import capture_process
from memos.skill_p5.broker import VisibleCheckBroker, check_visible_record
from memos.skill_p5.delivery import _visible_deliveries
from memos.skill_p5.execution_policy import (
    PROMPT,
    outcome_facts,
    policy_identity,
    task_outcome,
)
from memos.skill_p5.process import (
    build_process_evidence,
)
from memos.skill_p5.qualification import require_qualification
from memos.skill_p5.tasks import get_suite, materialize, suite_digest
from memos.skill_p5.verifier import _no_symlinks, check_receipt, verify_suite
from memos.skill_verification import runner as qualified_runner
from memos.skill_verification.runner import context_matches
from memos.skill_verification.verifier import _read


if TYPE_CHECKING:
    from threading import Event


ADAPTER_VERSION = "p5-claude-2"


def adapter_digest(suite_id: str = "update-repair-r1") -> str:
    """Bind this protocol, its current qualification and the unchanged native capture."""
    from memos.skill_p5 import delivery as delivery_helper
    from memos.skill_p5 import process, qualification

    files = {
        "legacy/" + p.name: _file_digest(p)
        for p in sorted(Path(legacy_runner.__file__).parent.glob("*.py"))
    }
    for name in ("recording_server.py", "process.py"):
        files["experiment/" + name] = _file_digest(Path(fixed_recorder.__file__).with_name(name))
    files["p5-v2/runner"] = _file_digest(Path(__file__))
    files["p5-v1/helpers"] = _file_digest(Path(frozen_runner.__file__))
    files["executionPolicy"] = policy_identity()
    files["extension/process"] = _file_digest(Path(process.__file__))
    files["executionTicketContract"] = _file_digest(Path(__file__).with_name("tickets.py"))
    files["delivery_helper"] = _file_digest(Path(delivery_helper.__file__))
    files["context_helper"] = _file_digest(Path(qualified_runner.__file__))
    from memos.skill_p5.tasks import SUITES

    files["qualification"] = {name: qualification._binding(name) for name in SUITES}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _validate_inputs(task, group, experiment_id, expected_digest, skill, expected_export_hash):
    if not isinstance(experiment_id, str) or not re.fullmatch(r"[a-f0-9]{64}", experiment_id):
        raise ValueError("extension_invalid_identity")
    if suite_digest(task) != expected_digest:
        raise ValueError("extension_task_digest_mismatch")
    if group not in ("none", "skill") or (group == "skill") != (skill is not None):
        raise ValueError("extension_skill_group_mismatch")
    if skill is None:
        if expected_export_hash is not None:
            raise ValueError("extension_unexpected_export_hash")
        return None
    if not re.fullmatch(r"[a-f0-9]{64}", skill.revision) or len(skill.body.encode()) > 32768:
        raise ValueError("extension_skill_revision_mismatch")
    # Before probing the CLI, render and check the complete fixed export bytes.
    with tempfile.TemporaryDirectory(prefix="memos-branch-export-") as directory:
        manifest = export_skill(skill, Path(directory).resolve() / "skill")
    if manifest["export_hash"] != expected_export_hash:
        raise ValueError("extension_export_hash_mismatch")
    return manifest


def _visible_complete(records, evidence, initial_snapshot, final_snapshot):
    if (
        len(records) < 1
        or records[0]["candidate_snapshot"] != initial_snapshot
        or records[-1]["candidate_snapshot"] != final_snapshot
        or records[0]["result"]["exit_code"] not in (None, 0, 1)
        or records[-1]["result"]["exit_code"] not in (None, 0, 1)
    ):
        return False
    store = SnapshotStore(evidence / "snapshots")
    for record in records:
        with tempfile.TemporaryDirectory(prefix="memos-branch-visible-binding-") as directory:
            candidate = store.restore(record["candidate_snapshot"], Path(directory) / "candidate")
            path = evidence / f"check-{record['check_index']}.json"
            if not check_visible_record(path, candidate):
                return False
    return True


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
    task_split: str = "holdout",
    execution_guard=None,
    execution_metadata: dict | None = None,
    execution_mode: str = "independent_validation",
) -> Path:
    """Require fresh qualification and bind both task states after the Agent exits."""
    qualification = require_qualification(Path(qualification_directory), suite_id=suite_id)
    validate_config(config)
    task = get_suite(suite_id)
    if task_split not in {"holdout", "dev"} or (task_split == "dev" and group != "skill"):
        raise ValueError("release_invalid_split")
    if execution_mode not in {"independent_validation", "normal", "candidate_evaluation"} or (
        task_split == "holdout"
    ) != (execution_mode == "independent_validation"):
        raise ValueError("release_invalid_execution_mode")
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
    ticket = None
    if execution_guard is not None:
        from memos.skill_p5.tickets import validate_start

        if (
            not isinstance(execution_metadata, dict)
            or execution_metadata.get("batchId") != experiment_id
            or execution_metadata.get("caseId") != task.task_id
            or execution_metadata.get("group") != group
            or execution_metadata.get("mode") != execution_mode
            or (skill and execution_metadata.get("exportHash") != expected_export_hash)
        ):
            raise ValueError("execution_metadata_mismatch")
        ticket = validate_start(execution_guard.begin(execution_metadata), execution_metadata)
        run_id, attempt_id = ticket["runId"], ticket["attemptId"]
    elif execution_metadata is not None:
        raise ValueError("execution_guard_missing")
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
            "split": task_split,
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
            "purpose": "normal_use"
            if task_split == "dev"
            else "release_validation"
            if skill
            else "protocol_calibration",
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
        if ticket is not None:
            report["execution_ticket"] = ticket
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
            report["visible_checks_complete"] = deliveries is not None and public_accepted
            observed = outcome_facts(
                task,
                _read(initial_verification.receipt_path),
                _read(verification.receipt_path),
                initial.snapshot_id,
                final.snapshot_id,
                changed,
                report["visible_checks"],
                report["process_evidence"],
                report["visible_checks_complete"],
                initial_binding.matches and binding.matches,
            )
            report["task_outcome"] = task_outcome(suite_id, observed)
            report["task_accepted"] = report["task_outcome"]["eligible"]
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
            if (
                successful_run(capture, report["execution"], loaded, True, matching_context)
                and report["execution"].get("timed_out") is False
                and report["execution"].get("cancelled") is False
            ):
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
    if ticket is not None and all(
        key in report for key in ("initial_receipt_sha256", "receipt_sha256")
    ):
        from memos.skill_p5.tickets import artifact_hashes, validate_finish

        artifacts = artifact_hashes(report, _file_digest(output / "report.json"))
        finished = execution_guard.finish(ticket, artifacts)
        validate_finish(finished, ticket, artifacts)
        _write(output / "execution-ticket.json", finished)
    return output / "report.json"
