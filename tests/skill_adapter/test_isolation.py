"""Exercise the operating-system boundary, not a prompt-level permission convention."""

import json
import shutil
import socket
import subprocess
import sys
import tempfile

from dataclasses import replace
from pathlib import Path

import pytest

from memos.skill_adapter.isolation import (
    IsolationSpec,
    IsolationUnavailableError,
    build_profile,
    build_test_profile,
    probe_isolation,
    runtime_paths,
    sandbox_command,
    sandbox_test_command,
)


def _spec(tmp_path):
    paths = {name: tmp_path / name for name in ("work", "control", "config", "hidden", "other")}
    for path in paths.values():
        path.mkdir()
    return IsolationSpec(
        workspace=paths["work"],
        control_root=paths["control"],
        config_root=paths["config"],
        runtime_read_roots=(),
        forbidden_roots=(paths["hidden"], paths["other"]),
    )


@pytest.mark.parametrize("field", ["workspace", "control_root", "config_root"])
def test_root_alias_or_ancestor_overlap_is_rejected(tmp_path, field):
    spec = _spec(tmp_path)
    with pytest.raises(ValueError, match="disjoint|overlap"):
        build_profile(replace(spec, **{field: tmp_path}))


@pytest.mark.parametrize("root", [Path("/"), Path.home(), Path("/private/tmp")])
def test_broad_runtime_read_grants_are_rejected(tmp_path, root):
    with pytest.raises(ValueError, match="broad"):
        build_profile(replace(_spec(tmp_path), runtime_read_roots=(root,)))


def test_runtime_cannot_expose_hidden_material_or_widen_to_repository(tmp_path):
    spec = _spec(tmp_path)
    repository = spec.forbidden_roots[0]
    private = repository / "oracle"
    private.mkdir()
    for root in (repository, private, tmp_path):
        with pytest.raises(ValueError, match="forbidden|broad"):
            build_profile(replace(spec, runtime_read_roots=(root,)))


def test_exact_virtualenv_is_an_explicit_runtime_exception(tmp_path):
    spec = _spec(tmp_path)
    venv = spec.forbidden_roots[0] / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    profile = build_profile(replace(spec, runtime_read_roots=(venv,)))
    assert json.dumps(str(venv.resolve())) in profile


@pytest.mark.parametrize("root", [Path("/usr"), Path("/Library"), Path("/private/var")])
def test_system_directories_cannot_become_writable_workspaces(tmp_path, root):
    with pytest.raises(ValueError, match="broad|disjoint"):
        build_profile(replace(_spec(tmp_path), workspace=root))


def test_existing_symlink_root_and_nul_path_cannot_change_boundary(tmp_path):
    spec = _spec(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(spec.forbidden_roots[0], target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_profile(replace(spec, workspace=alias))
    with pytest.raises(ValueError, match="path"):
        build_profile(replace(spec, workspace=Path("bad\x00path")))


def test_missing_platform_or_backend_fails_closed(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr("memos.skill_adapter.isolation.sys.platform", "linux")
    with pytest.raises(IsolationUnavailableError, match="macOS"):
        sandbox_command(["/bin/echo", "probe"], spec)


def test_unknown_or_empty_command_is_not_executed(tmp_path):
    spec = _spec(tmp_path)
    with pytest.raises(ValueError, match="command"):
        sandbox_command([], spec)
    with pytest.raises(ValueError, match="command"):
        sandbox_command(["echo", "probe"], spec)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Seatbelt validation")
def test_real_os_boundary_blocks_hidden_reads_writes_and_descendants(tmp_path):
    report = probe_isolation(_spec(tmp_path))
    assert report["passed"] is True, report
    assert report["backend"] == "macos-sandbox-exec"
    assert report["profile_sha256"]
    names = {row["name"] for row in report["checks"]}
    assert {
        "workspace_write",
        "control_read",
        "control_write",
        "config_unknown_write",
        "config_control_write",
        "hidden_read_0",
        "hidden_list_0",
        "hidden_write_0",
        "hidden_read_1",
        "symlink_read",
        "child_read",
        "hardlink_read",
    } <= names
    assert all(row["passed"] for row in report["checks"])


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Seatbelt validation")
def test_config_allows_known_state_but_refuses_new_configuration(tmp_path):
    spec = _spec(tmp_path)
    runtime = spec.config_root / "projects"
    runtime.mkdir()
    state = runtime / "run-state.json"
    proc = subprocess.run(
        sandbox_command(["/bin/sh", "-c", 'printf state > "$1"', "sh", str(state)], spec),
        cwd=spec.workspace,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert state.read_text() == "state"
    for name in ("settings.json", "settings.local.json", "arbitrary-new-file", ".claude.json"):
        target = spec.config_root / name
        attempt = subprocess.run(
            sandbox_command(["/bin/sh", "-c", 'printf changed > "$1"', "sh", str(target)], spec),
            cwd=spec.workspace,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert attempt.returncode != 0, name
        assert not target.exists(), name


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Seatbelt validation")
def test_native_claude_version_runs_without_exposing_home(tmp_path):
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("Claude Code is not installed")
    binary = Path(executable).resolve()
    spec = replace(_spec(tmp_path), runtime_read_roots=(binary.parent,))
    result = subprocess.run(
        sandbox_command([str(binary), "--version"], spec),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(spec.config_root)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Claude Code" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Seatbelt validation")
def test_project_virtualenv_starts_without_granting_repository_listing(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    executable = repository / ".venv" / "bin" / "python"
    if not executable.exists():
        pytest.skip("Project virtual environment is absent")
    spec = replace(
        _spec(tmp_path),
        forbidden_roots=(repository,),
        runtime_read_roots=runtime_paths(executable),
    )
    result = subprocess.run(
        sandbox_command(
            [str(executable), "-I", "-B", "-c", "import json, hashlib, ssl; print('runtime-ok')"],
            spec,
        ),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runtime-ok"
    for command in (["/bin/ls", str(repository)], ["/bin/cat", str(repository / "README.md")]):
        denied = subprocess.run(
            sandbox_command(command, spec),
            cwd=spec.workspace,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert denied.returncode != 0 and not denied.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS DNS capability validation")
def test_network_enabled_supports_dns_without_weakening_file_boundary(tmp_path):
    try:
        socket.getaddrinfo("api.anthropic.com", 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        pytest.skip("Host DNS is unavailable; cannot compare sandbox DNS")
    spec = replace(
        _spec(tmp_path), runtime_read_roots=runtime_paths(Path(sys.executable)), network=True
    )
    result = subprocess.run(
        sandbox_command(
            [
                str(Path(sys.executable)),
                "-I",
                "-B",
                "-c",
                "import socket; print(len(socket.getaddrinfo('api.anthropic.com', 443, type=socket.SOCK_STREAM)))",
            ],
            spec,
        ),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0
    assert probe_isolation(spec)["passed"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Unix IPC validation")
def test_network_enabled_does_not_permit_unrelated_unix_socket():
    # macOS Unix sockets have a short pathname limit; pytest's directory is too long.
    with (
        tempfile.TemporaryDirectory(prefix="memos-ipc-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX) as listener,
    ):
        spec = replace(
            _spec(Path(directory).resolve()),
            runtime_read_roots=runtime_paths(Path(sys.executable)),
            network=True,
        )
        target = spec.workspace / "unrelated.sock"
        listener.bind(str(target))
        listener.listen(1)
        result = subprocess.run(
            sandbox_command(
                [
                    str(Path(sys.executable)),
                    "-I",
                    "-B",
                    "-c",
                    "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])",
                    str(target),
                ],
                spec,
            ),
            cwd=spec.workspace,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS shadow configuration validation")
@pytest.mark.parametrize(
    "relative",
    [
        ".claude/settings.json",
        ".claude/skills/extra/SKILL.md",
        ".mcp.json",
        "CLAUDE.md",
        "CLAUDE.local.md",
        "AGENTS.md",
        "nested/.claude/commands/extra.md",
        "nested/CLAUDE.md",
        ".CLAUDE/settings.json",
        "nested/agents.MD",
    ],
)
def test_workspace_cannot_create_shadow_control_files(tmp_path, relative):
    spec = _spec(tmp_path)
    target = spec.workspace / relative
    result = subprocess.run(
        sandbox_command(
            [
                "/bin/sh",
                "-c",
                '/bin/mkdir -p "$(/usr/bin/dirname "$1")" && printf shadow > "$1"',
                "sh",
                str(target),
            ],
            spec,
        ),
        cwd=spec.workspace,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0, relative
    assert "Operation not permitted" in result.stderr
    assert not target.exists(), relative


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS shadow configuration validation")
def test_existing_workspace_controls_cannot_be_changed_or_removed(tmp_path):
    spec = _spec(tmp_path)
    target = spec.workspace / "CLAUDE.md"
    target.write_text("fixed visible instructions")
    for command in (
        ["/bin/sh", "-c", 'printf changed > "$1"', "sh", str(target)],
        ["/bin/rm", str(target)],
        ["/bin/mv", str(target), str(spec.workspace / "moved.md")],
    ):
        result = subprocess.run(
            sandbox_command(command, spec),
            cwd=spec.workspace,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr
        assert target.read_text() == "fixed visible instructions"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS candidate execution boundary")
def test_candidate_test_profile_disallows_fork_and_subprocess(tmp_path):
    spec = replace(
        _spec(tmp_path), runtime_read_roots=runtime_paths(Path(sys.executable)), network=True
    )
    code = """
import os, subprocess
try:
    pid = os.fork()
except PermissionError:
    print('fork-denied')
else:
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    print('fork-allowed')
try:
    subprocess.run(['/bin/echo', 'child'], check=True, capture_output=True)
except PermissionError:
    print('spawn-denied')
else:
    print('spawn-allowed')
"""
    result = subprocess.run(
        sandbox_test_command([sys.executable, "-I", "-B", "-c", code], spec),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["fork-denied", "spawn-denied"]


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS candidate execution boundary")
def test_candidate_test_profile_keeps_workspace_readonly_and_scratch_writable(tmp_path):
    spec = _spec(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    spec = replace(
        spec,
        runtime_read_roots=runtime_paths(Path(sys.executable)),
        writable_scratch=(scratch,),
        network=True,
    )
    code = """
from pathlib import Path
import sys
Path(sys.argv[2]).write_text('scratch-ok')
try:
    Path(sys.argv[1]).write_text('must not write')
except PermissionError:
    print('workspace-denied')
else:
    print('workspace-allowed')
"""
    result = subprocess.run(
        sandbox_test_command(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(spec.workspace / "source.py"),
                str(scratch / "output.txt"),
            ],
            spec,
        ),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "workspace-denied"
    assert not (spec.workspace / "source.py").exists()
    assert (scratch / "output.txt").read_text() == "scratch-ok"
    assert build_test_profile(spec) != build_profile(spec)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS candidate execution boundary")
def test_candidate_test_profile_runs_visible_dev_checks(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    spec = _spec(tmp_path)
    spec.workspace.rmdir()
    materialize_case("dev-strict-signature", spec.workspace, "reference")
    spec = replace(spec, runtime_read_roots=runtime_paths(Path(sys.executable)))
    result = subprocess.run(
        sandbox_test_command(
            [sys.executable, "-I", "-B", str(spec.workspace / "visible_checks.py")], spec
        ),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS candidate execution boundary")
def test_candidate_test_profile_denies_network_even_when_agent_network_is_enabled(tmp_path):
    spec = replace(
        _spec(tmp_path), runtime_read_roots=runtime_paths(Path(sys.executable)), network=True
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        code = """
import socket, sys
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=1)
except PermissionError:
    print('network-denied')
else:
    print('network-allowed')
"""
        result = subprocess.run(
            sandbox_test_command([sys.executable, "-I", "-B", "-c", code, str(port)], spec),
            cwd=spec.workspace,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "network-denied"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS candidate execution boundary")
def test_candidate_test_profile_cannot_rewrite_claude_runtime_state(tmp_path):
    spec = replace(_spec(tmp_path), runtime_read_roots=runtime_paths(Path(sys.executable)))
    state = spec.config_root / "projects"
    state.mkdir()
    target = state / "run.json"
    target.write_text("original")
    result = subprocess.run(
        sandbox_test_command(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('changed')",
                str(target),
            ],
            spec,
        ),
        cwd=spec.workspace,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0 and "Operation not permitted" in result.stderr
    assert target.read_text() == "original"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS broker boundary")
def test_main_profile_connects_only_to_declared_broker_socket():
    with (
        tempfile.TemporaryDirectory(prefix="memos-broker-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX) as broker,
        socket.socket(socket.AF_UNIX) as unrelated,
    ):
        spec = _spec(Path(directory).resolve())
        broker_root = Path(directory).resolve() / "broker-only"
        broker_root.mkdir()
        broker_path = broker_root / "broker.sock"
        unrelated_path = broker_root / "unrelated.sock"
        broker.bind(str(broker_path))
        broker.listen(1)
        unrelated.bind(str(unrelated_path))
        unrelated.listen(1)
        spec = replace(
            spec,
            runtime_read_roots=runtime_paths(Path(sys.executable)),
            allowed_unix_sockets=(broker_path,),
        )
        code = """
import socket, sys
for path in sys.argv[1:]:
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
    except PermissionError:
        print('denied')
    else:
        print('connected')
"""
        result = subprocess.run(
            sandbox_command(
                [sys.executable, "-I", "-B", "-c", code, str(broker_path), str(unrelated_path)],
                spec,
            ),
            cwd=spec.workspace,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        strict = subprocess.run(
            sandbox_test_command([sys.executable, "-I", "-B", "-c", code, str(broker_path)], spec),
            cwd=spec.workspace,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["connected", "denied"]
    assert strict.returncode == 0, strict.stderr
    assert strict.stdout.strip() == "denied"


def test_broker_endpoint_must_be_existing_socket(tmp_path):
    spec = _spec(tmp_path)
    ordinary_file = spec.control_root / "regular-file"
    ordinary_file.write_text("not a socket")
    with pytest.raises(ValueError, match="socket"):
        build_profile(replace(spec, allowed_unix_sockets=(ordinary_file,)))
    with pytest.raises(ValueError, match="socket"):
        build_profile(replace(spec, allowed_unix_sockets=(spec.control_root,)))


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS broker boundary")
def test_broker_endpoint_cannot_be_replaced_from_workspace():
    with (
        tempfile.TemporaryDirectory(prefix="memos-broker-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX) as broker,
    ):
        spec = _spec(Path(directory).resolve())
        broker_path = spec.workspace / "broker.sock"
        broker.bind(str(broker_path))
        with pytest.raises(ValueError, match="socket.*writable"):
            build_profile(replace(spec, allowed_unix_sockets=(broker_path,)))


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS process information boundary")
@pytest.mark.parametrize("prepare", [sandbox_command, sandbox_test_command])
@pytest.mark.parametrize("relation", ["sibling", "ancestor"])
def test_process_environment_of_another_run_is_not_readable(tmp_path, prepare, relation):
    spec = replace(_spec(tmp_path), runtime_read_roots=runtime_paths(Path(sys.executable)))
    # Query only a fresh controlled process, never an existing user's environment.
    probe = """
import ctypes, json, sys
libc = ctypes.CDLL(None, use_errno=True)
query = (ctypes.c_int * 3)(1, 49, int(sys.argv[1]))  # CTL_KERN, KERN_PROCARGS2, PID
buffer = ctypes.create_string_buffer(65536)
length = ctypes.c_size_t(len(buffer))
result = libc.sysctl(query, 3, buffer, ctypes.byref(length), None, 0)
print(json.dumps({'result': result, 'exposed': b'memos-controlled-process-canary' in buffer.raw}))
"""
    sleeper = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import time; print('ready', flush=True); time.sleep(10)",
        ],
        env={"PATH": "/usr/bin:/bin", "MEMOS_CANARY": "memos-controlled-process-canary"},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert sleeper.stdout.readline().strip() == "ready"
        command = [sys.executable, "-I", "-B", "-c", probe, str(sleeper.pid)]
        host = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
        assert json.loads(host.stdout)["exposed"] is True
        isolated_command = prepare(command, spec)
        environment = {"PATH": "/usr/bin:/bin"}
        if relation == "ancestor":
            # A separate controlled host broker launches the sandbox, while the
            # sandbox child receives no canary in its own clean environment.
            launcher = """
import json, os, subprocess, sys
command = json.loads(sys.argv[1])
command[-1] = str(os.getpid())
result = subprocess.run(command, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
"""
            isolated_command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                launcher,
                json.dumps(isolated_command),
            ]
            environment["MEMOS_CANARY"] = "memos-controlled-process-canary"
        isolated = subprocess.run(
            isolated_command,
            cwd=spec.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert isolated.returncode == 0, isolated.stderr
        assert json.loads(isolated.stdout) == {"result": -1, "exposed": False}
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
        sleeper.stdout.close()
