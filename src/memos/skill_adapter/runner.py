"""Host-owned Claude CLI execution, bounded capture, and independent task acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias, verify_export
from memos.skill_adapter.broker import VisibleCheckBroker
from memos.skill_adapter.events import EventRecorder
from memos.skill_adapter.hooks import HOOK_MARKER
from memos.skill_adapter.isolation import (
    IsolationSpec,
    build_profile,
    probe_isolation,
    runtime_paths,
    sandbox_command,
)
from memos.skill_adapter.verification import execute_guarded
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import check_receipt, verify_candidate


ADAPTER_VERSION = "0.1"
PINNED_CLI_VERSION = "2.1.296"
_TOOLS = {"Read", "Edit", "Write"}
_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)
_PROVIDER_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
}


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    cli_path: Path
    model: str
    expected_version: str = PINNED_CLI_VERSION
    timeout_seconds: float = 120
    max_turns: int = 8
    max_budget_usd: float = 1
    effort: str = "low"
    tools: tuple[str, ...] = ("Read", "Edit", "Write")


def validate_config(config: RunConfig) -> None:
    if not config.cli_path.is_file():
        raise ValueError("capability_missing: CLI executable not found")
    if (
        not config.model
        or config.model.split("[")[0]
        in {"opus", "sonnet", "haiku", "fable", "latest", "best", "default", "inherit", "opusplan"}
        or "latest" in config.model.lower()
    ):
        raise ValueError("capability_missing: an explicit fixed model is required")
    if config.expected_version != PINNED_CLI_VERSION:
        raise ValueError("capability_missing: this CLI version has not been validated")
    if config.effort not in {"low", "medium", "high"}:
        raise ValueError("Invalid effort level")
    if any(
        not math.isfinite(value) or value <= 0
        for value in (config.timeout_seconds, config.max_budget_usd)
    ):
        raise ValueError("Invalid timeout or budget")
    if type(config.max_turns) is not int or not 1 <= config.max_turns <= 50:
        raise ValueError("Invalid turn limit")
    if (
        not isinstance(config.tools, tuple)
        or len(set(config.tools)) != len(config.tools)
        or not set(config.tools) <= _TOOLS
    ):
        raise ValueError("capability_missing: unsupported tool set")


def command_for(
    config: RunConfig,
    control: Path,
    prompt: str,
    session_id: str,
    *,
    runtime_alias: str | None,
    recorder: bool = False,
) -> list[str]:
    validate_config(config)
    if prompt.startswith("/") and prompt.split()[0] != f"/{runtime_alias}":
        raise ValueError("unknown_command: prompt is not the locked runtime alias")
    if runtime_alias and not prompt.startswith(f"/{runtime_alias}"):
        raise ValueError("load_unverified: fixed Skill requires explicit native dispatch")
    allowed = list(config.tools)
    allowed.append("mcp__memos_run__run_visible_checks")
    if recorder:
        allowed.append("mcp__memos_run__record_rule")
    return [
        str(config.cli_path.resolve()),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--setting-sources",
        "project",
        "--settings",
        str(control / "settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(control / "mcp.json"),
        "--tools",
        ",".join(config.tools),
        "--allowedTools",
        ",".join(allowed),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--no-chrome",
        "--session-id",
        session_id,
        "--max-turns",
        str(config.max_turns),
        "--max-budget-usd",
        str(config.max_budget_usd),
        "--model",
        config.model,
        "--effort",
        config.effort,
    ]


def _processes() -> dict[int, tuple[int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,lstart="],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    pairs = []
    for line in result.stdout.splitlines():
        values = line.split(maxsplit=2)
        if len(values) == 3 and all(value.isdigit() for value in values[:2]):
            pairs.append((int(values[0]), (int(values[1]), values[2])))
    return dict(pairs)


def _children(parent: int) -> dict[int, str]:
    pairs = _processes()
    found = {parent}
    while True:
        more = {pid for pid, (ppid, _) in pairs.items() if ppid in found}
        if more <= found:
            return {pid: pairs[pid][1] for pid in found - {parent}}
        found |= more


def _terminate(process: subprocess.Popen, descendants: dict[int, str]) -> None:
    process.poll()  # Reap an exited leader before signaling its potentially empty group.
    current = _processes()
    for pid, birth in descendants.items():
        if current.get(pid, (None, None))[1] == birth:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.3)
    current = _processes()
    for pid, birth in descendants.items():
        if current.get(pid, (None, None))[1] == birth:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.kill()
    process.wait(timeout=3)


def _consume(
    message: dict, journal: EventRecorder, pending_hooks: set[str], declarations: dict
) -> None:
    journal.ingest_stream(message)
    if message.get("type") == "system" and message.get("subtype") == "hook_started":
        pending_hooks.add(message.get("hook_id"))
    if message.get("type") == "system" and message.get("subtype") == "hook_response":
        pending_hooks.discard(message.get("hook_id"))
        if message.get("exit_code") != 0:
            journal.mark_incomplete("hook_failed")
        try:
            output = json.loads(message.get("stdout", ""))
            marker = output["systemMessage"]
            if not marker.startswith(HOOK_MARKER):
                raise ValueError("wrong marker")
            payload = json.loads(marker[len(HOOK_MARKER) :])
            if payload.get("hook_event_name") != message.get("hook_event"):
                raise ValueError("wrong event")
            journal.ingest_hook(payload)
            if payload.get("gate") != "allowed":
                journal.mark_incomplete("hook_blocked")
        except (ValueError, KeyError, TypeError, AttributeError):
            journal.mark_incomplete("hook_payload_missing")
    content = (
        message.get("message", {}).get("content", [])
        if isinstance(message.get("message"), dict)
        else []
    )
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "mcp__memos_run__record_rule":
            declarations[block.get("id")] = block.get("input")
        if block.get("type") == "tool_result" and block.get("tool_use_id") in declarations:
            declaration = declarations.pop(block["tool_use_id"])
            if not block.get("is_error"):
                try:
                    journal.record_rule(declaration)
                except ValueError:
                    journal.mark_incomplete("invalid_rule_declaration")


def capture_process(
    command: list[str], cwd: Path, environment: dict, journal: EventRecorder, timeout: float
) -> dict:
    """Consume JSON in memory; never persist raw stdout, stderr or reasoning blocks."""
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    pending_hooks: set[str] = set()
    declarations: dict = {}
    buffer = b""
    total = stderr_bytes = 0
    timed_out = False
    tracked: dict[int, str] = {}
    limited = False
    next_scan = 0.0
    try:
        while selector.get_map() and not limited:
            now = time.monotonic()
            if now >= next_scan and process.poll() is None:
                tracked |= _children(process.pid)
                next_scan = now + 0.1
            if now - started > timeout:
                timed_out = True
                journal.mark_incomplete("timeout")
                _terminate(process, tracked)
                break
            for key, _ in selector.select(timeout=0.05):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_bytes += len(data)
                    continue
                total += len(data)
                buffer += data
                if total > 16 * 1024 * 1024 or len(buffer) > 2 * 1024 * 1024:
                    journal.mark_incomplete("output_limit")
                    limited = True
                    _terminate(process, tracked)
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError("invalid stream item")
                        _consume(message, journal, pending_hooks, declarations)
                    except (ValueError, TypeError):
                        journal.mark_incomplete("invalid_stream_json")
        if buffer.strip():
            journal.mark_incomplete("truncated_stream_line")
        if pending_hooks:
            journal.mark_incomplete("unpaired_hook")
    finally:
        _terminate(process, tracked)
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if process.returncode != 0:
        journal.mark_incomplete("native_process_failed")
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_bytes": total,
        "stderr_bytes": stderr_bytes,
        "tracked_descendants": len(tracked),
    }


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _adapter_digest() -> str:
    files = {path.name: _file_digest(path) for path in sorted(Path(__file__).parent.glob("*.py"))}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def _environment(provider: dict, config: Path, scratch: Path) -> dict:
    if set(provider) - _PROVIDER_KEYS or any(
        not isinstance(value, str) for value in provider.values()
    ):
        raise ValueError("Unsupported provider environment")
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    environment.update(provider)
    environment.update(
        {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "CLAUDE_CONFIG_DIR": str(config),
            "TMPDIR": str(scratch),
            "CLAUDE_CODE_TMPDIR": str(scratch),
            "SHELL": "/bin/bash",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "CLAUDE_CODE_AUTO_CONNECT_IDE": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def context_matches(events: list[dict], config: RunConfig, alias: str | None) -> bool:
    """Reject unexpected native context; pinned built-ins remain explicitly visible."""
    initial = [event["payload"] for event in events if event["kind"] == "session_init"]
    if len(initial) != 1:
        return False
    value = initial[0]
    expected_tools = set(config.tools) | {"mcp__memos_run__run_visible_checks"}
    if alias:
        expected_tools.add("mcp__memos_run__record_rule")
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


def successful_run(
    capture: dict, execution: dict, loaded: str, accepted: bool, matching_context: bool
) -> bool:
    return (
        capture["status"] == "complete"
        and capture.get("native_outcome") == "reported_success"
        and execution["returncode"] == 0
        and loaded in {"verified", "not_requested"}
        and accepted
        and matching_context
    )


def _managed_configuration_present() -> bool:
    """Do not bypass an organization's policy to obtain a clean experiment."""
    roots = (
        Path("/Library/Application Support/ClaudeCode"),
        Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"),
        Path.home() / "Library/Preferences/com.anthropic.claudecode.plist",
    )
    return any(path.exists() for path in roots)


def run_case(
    case_id: str,
    output: Path,
    config: RunConfig,
    *,
    skill: SkillRevision | None = None,
    provider_environment: dict | None = None,
    required_canary: str | None = None,
) -> Path:
    """Run one controlled dev/source case; preserve incomplete attempts explicitly."""
    validate_config(config)
    case = get_case(case_id)
    if case.split == "holdout" or (case.split == "source" and skill is not None):
        raise ValueError("capability_missing: P1a uses dev probes or unassisted source tasks")
    if required_canary and skill is None:
        raise ValueError("A loading canary requires a fixed Skill")
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
    adapter_hash = _adapter_digest()
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
            "adapter_version": ADAPTER_VERSION,
            "purpose": "adapter_validation",
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
            exported, skill_dir = None, None
            if skill:
                skill_dir = control / "skills" / alias
                exported = export_skill(skill, skill_dir)
            report["skill_export"] = exported
            with VisibleCheckBroker(
                case_id, workspace, output / "visible-checks", secret_values=secret_values
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
                shutil.copyfile(Path(__file__).with_name("hooks.py"), control / "hook.py")
                shutil.copyfile(
                    Path(__file__).with_name("recording_server.py"), control / "recording_server.py"
                )
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
                if skill:
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
                    + "Report the observed results and any remaining limitations."
                )
                command = command_for(
                    config,
                    control,
                    prompt,
                    session_id,
                    runtime_alias=alias,
                    recorder=skill is not None,
                )
                report["execution"] = capture_process(
                    sandbox_command(command, isolation),
                    workspace,
                    _environment(provider, user_config, scratch),
                    journal,
                    config.timeout_seconds,
                )
                if (
                    store.capture(control).snapshot_id != control_digest
                    or _file_digest(config.cli_path.resolve()) != executable_hash
                    or _adapter_digest() != adapter_hash
                ):
                    journal.mark_incomplete("artifact_mismatch")
                if skill:
                    verify_export(skill, skill_dir)
                report["visible_checks"] = broker.records
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
                executor=partial(execute_guarded, secret_values=secret_values),
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
