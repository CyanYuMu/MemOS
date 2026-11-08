"""A fail-closed, locally probed macOS boundary for the Claude CLI and descendants.

This backend deliberately supports the tested macOS environment only. Seatbelt's
``sandbox-exec`` interface is deprecated; other platforms need another backend,
not a silent unconfined fallback. Network access, when enabled, is not restricted
to a domain. The caller remains responsible for credentials and process cleanup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path


BACKEND = "macos-sandbox-exec"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_SYSTEM_READ_ROOTS = ("/System", "/usr", "/bin", "/sbin", "/Library", "/private/var/select")
_SYSTEM_READ_FILES = (
    "/",
    "/dev/null",
    "/dev/random",
    "/dev/urandom",
    "/private/etc/hosts",
    "/private/etc/resolv.conf",
    "/private/etc/localtime",
    "/private/etc/ssl/cert.pem",
)
_CONFIG_STATE_DIRECTORIES = (
    "projects",
    "debug",
    "todos",
    "tasks",
    "session-env",
    "cache",
    "telemetry",
    "statsig",
    "backups",
    "file-history",
    "shell-snapshots",
    "paste-cache",
    "plans",
)
_RUNTIME_SYSCTLS = (
    "hw.activecpu",
    "hw.cachelinesize",
    "hw.cpufamily",
    "hw.cpusubtype",
    "hw.cputype",
    "hw.logicalcpu",
    "hw.logicalcpu_max",
    "hw.machine",
    "hw.memsize",
    "hw.ncpu",
    "hw.nperflevels",
    "hw.pagesize",
    "hw.pagesize_compat",
    "hw.physicalcpu",
    "hw.physicalcpu_max",
    "kern.argmax",
    "kern.hostname",
    "kern.maxfiles",
    "kern.maxfilesperproc",
    "kern.osproductversion",
    "kern.osrelease",
    "kern.ostype",
    "kern.osvariant_status",
    "kern.osversion",
    "kern.version",
    "machdep.cpu.brand_string",
    "machdep.ptrauth_enabled",
)


class IsolationUnavailableError(RuntimeError):
    """No supported OS enforcement is available; the caller must not run the Agent."""

    code = "capability_missing"


@dataclass(frozen=True, kw_only=True)
class IsolationSpec:
    workspace: Path
    control_root: Path
    config_root: Path
    runtime_read_roots: tuple[Path, ...]
    forbidden_roots: tuple[Path, ...]
    writable_scratch: tuple[Path, ...] = ()
    network: bool = False
    allowed_unix_sockets: tuple[Path, ...] = ()


def _path(value: Path) -> Path:
    raw = str(value)
    if not raw or any(ord(character) < 32 for character in raw):
        raise ValueError("Invalid isolation path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Isolation path must be absolute")
    if path.is_symlink():
        raise ValueError("Isolation root must not be a symlink")
    return path.resolve(strict=True)


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _virtualenv_exception(runtime: Path, forbidden: Path) -> bool:
    return (
        runtime.parent == forbidden
        and runtime.name == ".venv"
        and (runtime / "pyvenv.cfg").is_file()
    )


def _validated(spec: IsolationSpec) -> IsolationSpec:
    if type(spec.network) is not bool:
        raise ValueError("network must be boolean")
    workspace, control, config = (
        _path(value) for value in (spec.workspace, spec.control_root, spec.config_root)
    )
    primary = (workspace, control, config)
    if not all(path.is_dir() for path in primary):
        raise ValueError("Workspace, control and config must be existing directories")
    if any(
        _overlap(first, second)
        for index, first in enumerate(primary)
        for second in primary[index + 1 :]
    ):
        raise ValueError("Workspace, control and config roots must be disjoint")
    forbidden = tuple(_path(path) for path in spec.forbidden_roots)
    runtime = tuple(_path(path) for path in spec.runtime_read_roots)
    scratch = tuple(_path(path) for path in spec.writable_scratch)
    sockets = tuple(_path(path) for path in spec.allowed_unix_sockets)
    broad = {
        Path("/"),
        Path.home().resolve(),
        Path("/Users"),
        Path("/private"),
        Path("/private/tmp"),
        Path("/private/var"),
        Path("/private/var/folders"),
        Path("/opt"),
        Path("/opt/homebrew"),
        Path("/Applications"),
        *(Path(path) for path in _SYSTEM_READ_ROOTS),
        Path(tempfile.gettempdir()).resolve(),
    }
    for path in runtime:
        if path in broad:
            raise ValueError("Runtime read root is too broad")
        if any(
            _overlap(path, hidden) and not _virtualenv_exception(path, hidden)
            for hidden in forbidden
        ):
            raise ValueError("Runtime read root exposes forbidden material or is too broad")
    for path in (*primary, *scratch):
        if path in broad or any(_overlap(path, hidden) for hidden in forbidden):
            raise ValueError("Run roots overlap forbidden material or are too broad")
    for path in scratch:
        if not path.is_dir() or any(_overlap(path, other) for other in (*primary, *runtime)):
            raise ValueError("Scratch roots must be disjoint existing directories")
    for path in runtime:
        if any(_overlap(path, writable) for writable in (workspace, config, *scratch)):
            raise ValueError("Runtime read roots must not overlap writable roots")
    for path in sockets:
        if not path.is_socket() or any(
            path.is_relative_to(writable) for writable in (workspace, config, *scratch)
        ):
            raise ValueError("Broker endpoint must be an existing socket outside writable roots")
    return IsolationSpec(
        workspace=workspace,
        control_root=control,
        config_root=config,
        runtime_read_roots=runtime,
        forbidden_roots=forbidden,
        writable_scratch=scratch,
        network=spec.network,
        allowed_unix_sockets=sockets,
    )


def _filter(path: Path | str, *, directory: bool = True) -> str:
    return f"({'subpath' if directory else 'literal'} {json.dumps(str(path), ensure_ascii=False)})"


def _control_pattern(root: Path) -> str:
    # The normal macOS filesystem is case-insensitive, so case variants cannot
    # become an alternate spelling of a protected Claude control path.
    names = (".claude", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json")
    alternatives = []
    for name in names:
        alternatives.append(
            "".join(
                f"[{character.lower()}{character.upper()}]"
                if character.isalpha()
                else re.escape(character)
                for character in name
            )
        )
    expression = "^" + re.escape(str(root)) + "/([^/]+/)*(" + "|".join(alternatives) + ")(/.*)?$"
    return "(regex " + json.dumps(expression) + ")"


def runtime_paths(python_executable: Path) -> tuple[Path, ...]:
    """Return narrow roots for the local Python install, virtualenv and Homebrew TLS.

    This recipe does not grant all of Homebrew. A different installation or an
    additional native dependency still needs an explicit root and a fresh probe.
    """
    executable = Path(python_executable)
    resolved = executable.resolve(strict=True)
    roots = []
    environment = executable.parent.parent
    if (environment / "pyvenv.cfg").is_file():
        roots.append(environment.resolve())
    cellar_install = next(
        (parent for parent in resolved.parents if parent.parent.parent.name == "Cellar"),
        None,
    )
    if cellar_install is not None:
        roots.append(cellar_install)
        openssl = cellar_install.parent.parent.parent / "opt" / "openssl@3"
        if openssl.exists():
            roots.append(openssl.resolve())
    elif not any(resolved.is_relative_to(root) for root in map(Path, _SYSTEM_READ_ROOTS)):
        roots.append(resolved.parent.parent)
    return tuple(dict.fromkeys(roots))


def build_profile(spec: IsolationSpec) -> str:
    """Construct a deterministic default-deny profile with no arbitrary config writes."""
    current = _validated(spec)
    reads = [_filter(path) for path in _SYSTEM_READ_ROOTS]
    reads.extend(_filter(path, directory=False) for path in _SYSTEM_READ_FILES)
    reads.extend(
        _filter(path)
        for path in (
            current.workspace,
            current.control_root,
            current.config_root,
            *current.writable_scratch,
        )
    )
    reads.extend(_filter(path, directory=path.is_dir()) for path in current.runtime_read_roots)
    writes = [_filter(current.workspace)]
    writes.extend(_filter(path) for path in current.writable_scratch)
    writes.extend(_filter(current.config_root / name) for name in _CONFIG_STATE_DIRECTORIES)
    writes.append(_filter("/dev/null", directory=False))
    ancestors = {
        parent
        for path in (
            current.workspace,
            current.control_root,
            current.config_root,
            *current.runtime_read_roots,
            *current.writable_scratch,
            *current.allowed_unix_sockets,
        )
        for parent in path.parents
    }
    # Homebrew opt aliases need metadata, not contents of the whole installation.
    cellar = Path("/opt/homebrew/Cellar")
    for path in current.runtime_read_roots:
        if path.is_relative_to(cellar) and len(path.relative_to(cellar).parts) >= 2:
            ancestors.add(Path("/opt/homebrew/opt") / path.relative_to(cellar).parts[0])
    clauses = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        # macOS KERN_PROCARGS2 needs an explicit process-info denial: default
        # deny alone still allowed another same-user process's environment.
        "(deny process-info*)",
        # The system DNS resolver queries its own process; do not grant inspection
        # of sibling CLI processes, the trusted broker, or any other sandbox.
        "(allow process-info* (target self))",
        # Runtime hardware/OS queries are allowed without a broad sysctl grant
        # or any kern.proc namespace; process-info denial above is also required.
        "(allow sysctl-read "
        + " ".join(f'(sysctl-name "{name}")' for name in _RUNTIME_SYSCTLS)
        + ' (sysctl-name-prefix "hw.optional.arm") (sysctl-name-prefix "hw.perflevel"))',
        "(allow file-read-metadata "
        + " ".join(_filter(path, directory=False) for path in sorted(ancestors))
        + ")",
        f"(allow file-read* {' '.join(reads)})",
        f"(allow file-write* {' '.join(writes)})",
        "(deny file-write* "
        + " ".join(
            _control_pattern(path) for path in (current.workspace, *current.writable_scratch)
        )
        + ")",
    ]
    if current.network or current.allowed_unix_sockets:
        clauses.append("(allow system-socket (socket-domain AF_UNIX))")
    if current.allowed_unix_sockets:
        clauses.append(
            "(allow network-outbound "
            + " ".join(
                f"(remote unix-socket {_filter(path, directory=False)})"
                for path in current.allowed_unix_sockets
            )
            + ")"
        )
        clauses.append(
            "(allow file-read-metadata "
            + " ".join(_filter(path, directory=False) for path in current.allowed_unix_sockets)
            + ")"
        )
    if current.network:
        clauses.extend(
            [
                '(allow network-outbound (remote ip "*:*"))',
                # getaddrinfo talks to the system DNS socket. Do not permit arbitrary
                # Unix IPC (Docker, launch agents, SSH agents) to restore DNS access.
                '(allow network-outbound (remote unix-socket (subpath "/private/var/run/mDNSResponder")))',
                '(allow file-read-metadata (literal "/var") (literal "/private/var/run") '
                '(literal "/private/var/run/mDNSResponder"))',
            ]
        )
    return "\n".join(clauses) + "\n"


def sandbox_command(command: list[str], spec: IsolationSpec) -> list[str]:
    """Return an enforced argv; unsupported machines never receive the plain command."""
    if sys.platform != "darwin":
        raise IsolationUnavailableError("The P1a isolation backend requires macOS")
    if not SANDBOX_EXEC.is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
        raise IsolationUnavailableError("macOS sandbox-exec is unavailable")
    if (
        not command
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        or not Path(command[0]).is_absolute()
    ):
        raise ValueError("Sandbox command must have an absolute executable and valid arguments")
    return [str(SANDBOX_EXEC), "-p", build_profile(spec), *command]


def build_test_profile(spec: IsolationSpec) -> str:
    """Constrain candidate Python to one process, no network, and read-only inputs."""
    current = _validated(spec)
    # A generic deny does not override Seatbelt's more specific IP/socket allow
    # filters. Remove every network grant before adding the explicit deny.
    return build_profile(replace(current, network=False, allowed_unix_sockets=())) + "\n".join(
        [
            "(deny process-fork)",
            "(deny network*)",
            "(deny file-write* "
            + " ".join(
                _filter(path)
                for path in (current.workspace, current.config_root, current.control_root)
            )
            + ")",
            "",
        ]
    )


def sandbox_test_command(command: list[str], spec: IsolationSpec) -> list[str]:
    """Prepare the trusted fixed test command with the stricter candidate boundary."""
    argv = sandbox_command(command, spec)
    argv[2] = build_test_profile(spec)
    return argv


def probe_isolation(spec: IsolationSpec) -> dict:
    """Exercise real OS reads/writes using only temporary, collector-created canaries.

    The caller supplies writable forbidden directories for probes. Temporary
    canaries are removed; existing private files are never read or modified.
    A false ``passed`` value means the Agent must not be launched.
    """
    current = _validated(spec)
    checks = []
    with ExitStack() as stack:

        def temporary(parent: Path) -> Path:
            return Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix=".memos-isolation-probe-", dir=parent)
                )
            )

        visible = temporary(current.workspace)
        controls = temporary(current.control_root)
        control_file = controls / "control.txt"
        control_file.write_text("fixed control sentinel", encoding="utf-8")

        def check(name: str, command: list[str], *, allowed: bool) -> None:
            process = subprocess.run(
                sandbox_command(command, current),
                cwd=current.workspace,
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            denied = process.returncode != 0 and any(
                message in process.stderr
                for message in ("Operation not permitted", "Permission denied")
            )
            checks.append(
                {
                    "name": name,
                    "expected": "allowed" if allowed else "denied",
                    "returncode": process.returncode,
                    "passed": process.returncode == 0 if allowed else denied,
                }
            )

        check(
            "workspace_write",
            ["/bin/sh", "-c", 'printf ok > "$1"', "sh", str(visible / "written")],
            allowed=True,
        )
        check("control_read", ["/bin/cat", str(control_file)], allowed=True)
        check(
            "control_write",
            ["/bin/sh", "-c", 'printf altered > "$1"', "sh", str(control_file)],
            allowed=False,
        )
        unknown = current.config_root / visible.name
        stack.callback(unknown.unlink, missing_ok=True)
        check(
            "config_unknown_write",
            ["/bin/sh", "-c", 'printf altered > "$1"', "sh", str(unknown)],
            allowed=False,
        )
        # Use a fresh protected subtree, never overwrite actual CLI configuration.
        protected_parent = current.config_root / "skills"
        created_parent = not protected_parent.exists()
        protected_parent.mkdir(exist_ok=True)
        if created_parent:
            stack.callback(protected_parent.rmdir)
        protected = temporary(protected_parent)
        protected_file = protected / "SKILL.md"
        protected_file.write_text("fixed configuration sentinel", encoding="utf-8")
        check(
            "config_control_write",
            ["/bin/sh", "-c", 'printf altered > "$1"', "sh", str(protected_file)],
            allowed=False,
        )
        for index, root in enumerate(current.forbidden_roots):
            if not root.is_dir():
                raise ValueError("Isolation probe requires forbidden directories")
            hidden = temporary(root)
            secret = hidden / "sentinel.txt"
            secret.write_text("hidden evaluation sentinel", encoding="utf-8")
            check(f"hidden_read_{index}", ["/bin/cat", str(secret)], allowed=False)
            check(f"hidden_list_{index}", ["/bin/ls", str(hidden)], allowed=False)
            check(
                f"hidden_write_{index}",
                ["/bin/sh", "-c", 'printf altered > "$1"', "sh", str(secret)],
                allowed=False,
            )
            if index == 0:
                escape = visible / "escape"
                escape.symlink_to(secret)
                check("symlink_read", ["/bin/cat", str(escape)], allowed=False)
                check(
                    "child_read",
                    ["/bin/sh", "-c", '/bin/cat "$1"', "sh", str(secret)],
                    allowed=False,
                )
                check(
                    "hardlink_read",
                    [
                        "/bin/sh",
                        "-c",
                        '/bin/ln "$1" "$2" && /bin/cat "$2"',
                        "sh",
                        str(secret),
                        str(visible / "hardlink"),
                    ],
                    allowed=False,
                )
        controls_unchanged = (
            control_file.read_text() == "fixed control sentinel"
            and protected_file.read_text() == "fixed configuration sentinel"
        )
    return {
        "backend": BACKEND,
        "profile_sha256": hashlib.sha256(build_profile(current).encode()).hexdigest(),
        "passed": bool(current.forbidden_roots)
        and controls_unchanged
        and all(row["passed"] for row in checks),
        "checks": checks,
        "network_domain_isolation": False,
    }
