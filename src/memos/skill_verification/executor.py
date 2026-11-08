"""One host-owned verifier process with immutable inputs and no descendants.

The host launches fixed pytest phases sequentially. Every process that can load
candidate code remains unable to fork, write its inputs, access host artifacts,
or use the network. This is process/resource confinement, not an interpreter
boundary between a trusted harness and candidate Python reflection.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import string
import subprocess
import sys
import tempfile
import time

from pathlib import Path, PurePosixPath

from memos.skill_adapter import events, isolation


BACKEND = isolation.BACKEND
EXECUTOR_VERSION = "host-fixed-phase-1"
OUTPUT_LIMIT = 128 * 1024
REPORT_LIMIT = 1024 * 1024
FILE_DESCRIPTOR_LIMIT = 64
PROGRAM_LIMIT = 4 * 1024 * 1024
CONTROL_LIMIT = 8 * 1024 * 1024
_FIELDS = {"candidate", "report", "control", "scratch"}


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def implementation_digest() -> str:
    """Bind this implementation and the reused enforcement/redaction policy."""
    sources = {
        "executor": _sha(Path(__file__).read_bytes()),
        "isolation": _sha(Path(isolation.__file__).read_bytes()),
        "events": _sha(Path(events.__file__).read_bytes()),
    }
    return _sha(json.dumps(sources, sort_keys=True).encode())


def policy_limits(timeout: float) -> dict:
    """Expose enforced limits without claiming a platform-wide memory ceiling."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_timeout")
    return {
        "output_limit_bytes": OUTPUT_LIMIT,
        "report_limit_bytes": REPORT_LIMIT,
        "cpu_limit_seconds": max(1, math.ceil(timeout)),
        "file_descriptor_limit": FILE_DESCRIPTOR_LIMIT,
        "max_candidate_processes": 1,
        "fork_allowed": False,
        "network_allowed": False,
        "hard_memory_limit": None,
    }


def profile_from_record(profile_spec: dict, *, candidate_path: Path, job: Path) -> str:
    """Rebuild the fixed policy even after its private temporary roots are gone.

    This is a constrained renderer for the *existing* no-fork policy, not a
    caller-supplied Seatbelt recipe. At execution time its result must exactly
    equal the validated old policy. Changes to that policy fail closed until
    this versioned renderer is deliberately updated.
    """
    keys = {
        "workspace",
        "control_root",
        "config_root",
        "runtime_read_roots",
        "forbidden_roots",
        "writable_scratch",
    }
    if not isinstance(profile_spec, dict) or set(profile_spec) != keys:
        raise ValueError("invalid_profile_spec")

    def recorded_path(value):
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or Path(value).as_posix() != value
            or ".." in Path(value).parts
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("invalid_profile_path")
        return Path(value)

    workspace = recorded_path(profile_spec["workspace"])
    control = recorded_path(profile_spec["control_root"])
    config = recorded_path(profile_spec["config_root"])
    for key in ("runtime_read_roots", "forbidden_roots", "writable_scratch"):
        if not isinstance(profile_spec[key], list):
            raise ValueError("invalid_profile_paths")
    runtime = tuple(recorded_path(p) for p in profile_spec["runtime_read_roots"])
    forbidden = tuple(recorded_path(p) for p in profile_spec["forbidden_roots"])
    scratch = tuple(recorded_path(p) for p in profile_spec["writable_scratch"])
    repository = Path(__file__).resolve().parents[3]
    if (
        workspace != Path(candidate_path)
        or runtime != isolation.runtime_paths(Path(sys.executable))
        or forbidden != (repository, Path(job).resolve())
        or control.name != "control"
        or control.parent.parent != Path("/private/tmp")
        or not re.fullmatch(r"memos-verification-phase-[A-Za-z0-9_-]+", control.parent.name)
        or config != control.parent / "config"
        or scratch != (control.parent / "scratch",)
    ):
        raise ValueError("profile_binding_mismatch")
    primary = (workspace, control, config, *scratch)
    broad = {
        Path("/"),
        Path.home().resolve(),
        Path("/Users"),
        Path("/private"),
        Path("/private/tmp"),
        Path("/private/var"),
        Path("/opt"),
        Path("/opt/homebrew"),
    }
    for i, path in enumerate(primary):
        if path in broad or any(
            isolation._overlap(path, other) for other in (*primary[i + 1 :], *forbidden, *runtime)
        ):
            raise ValueError("profile_roots_overlap")
    # _filter and _control_pattern are byte-bound via implementation_digest.
    # Only paths validated above are interpolated; no recorded policy clauses
    # or network/socket capability settings are accepted.
    filter_path = isolation._filter
    reads = [filter_path(path) for path in isolation._SYSTEM_READ_ROOTS]
    reads.extend(filter_path(path, directory=False) for path in isolation._SYSTEM_READ_FILES)
    reads.extend(filter_path(path) for path in primary)
    reads.extend(filter_path(path, directory=path.is_dir()) for path in runtime)
    writes = [filter_path(workspace), *(filter_path(path) for path in scratch)]
    writes.extend(filter_path(config / name) for name in isolation._CONFIG_STATE_DIRECTORIES)
    writes.append(filter_path("/dev/null", directory=False))
    ancestors = {parent for path in (*primary, *runtime) for parent in path.parents}
    cellar = Path("/opt/homebrew/Cellar")
    for path in runtime:
        if path.is_relative_to(cellar) and len(path.relative_to(cellar).parts) >= 2:
            ancestors.add(Path("/opt/homebrew/opt") / path.relative_to(cellar).parts[0])
    clauses = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(deny process-info*)",
        "(allow process-info* (target self))",
        "(allow sysctl-read "
        + " ".join(f'(sysctl-name "{name}")' for name in isolation._RUNTIME_SYSCTLS)
        + ' (sysctl-name-prefix "hw.optional.arm") (sysctl-name-prefix "hw.perflevel"))',
        "(allow file-read-metadata "
        + " ".join(filter_path(path, directory=False) for path in sorted(ancestors))
        + ")",
        f"(allow file-read* {' '.join(reads)})",
        f"(allow file-write* {' '.join(writes)})",
        "(deny file-write* "
        + " ".join(isolation._control_pattern(path) for path in (workspace, *scratch))
        + ")",
        "(deny process-fork)",
        "(deny network*)",
        "(deny file-write* "
        + " ".join(filter_path(path) for path in (workspace, config, control))
        + ")",
    ]
    return "\n".join(clauses) + "\n"


def _json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)


def _validate(program, files, arguments, timeout):
    if not isinstance(program, str) or len(program.encode()) > PROGRAM_LIMIT:
        raise ValueError("invalid_fixed_program")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_timeout")
    if not isinstance(files, dict) or len(files) > 64:
        raise ValueError("invalid_control_files")
    total = len(program.encode())
    normalized = []
    for name, value in files.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("invalid_control_file")
        path = PurePosixPath(name)
        if (
            not name
            or not path.parts
            or path.is_absolute()
            or path.as_posix() != name
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0].casefold() == "main.py"
            or "\\" in name
            or any(ord(character) < 32 for character in name)
        ):
            raise ValueError("invalid_control_path")
        folded = PurePosixPath(name.casefold())
        if any(
            folded == other or folded.is_relative_to(other) or other.is_relative_to(folded)
            for other in normalized
        ):
            raise ValueError("overlapping_control_paths")
        normalized.append(folded)
        total += len(value.encode())
    if total > CONTROL_LIMIT:
        raise ValueError("control_files_too_large")
    if not isinstance(arguments, tuple) or len(arguments) > 64:
        raise ValueError("invalid_arguments")
    formatter = string.Formatter()
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or len(argument) > 8192
            or any(ord(character) < 32 for character in argument)
        ):
            raise ValueError("invalid_argument")
        for _text, field, specification, conversion in formatter.parse(argument):
            if field is not None and (
                field not in _FIELDS or specification or conversion is not None
            ):
                raise ValueError("invalid_argument_placeholder")


def _stop(process: subprocess.Popen) -> None:
    # The OS denies fork in this process. Killing its owned Popen handle is
    # sufficient and does not risk signaling an unrelated process group.
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)


def _capture(command, *, control, scratch, timeout, started, cancellation_event):
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    counts = {"stdout": 0, "stderr": 0}
    outcome = {
        "returncode": None,
        "timed_out": False,
        "cancelled": False,
        "background_processes": False,
        "output_limited": False,
        "limit_reasons": [],
    }
    if cancellation_event is not None and cancellation_event.is_set():
        outcome["cancelled"] = True
        outcome["limit_reasons"].append("cancelled")
        return outcome, captured, counts
    try:
        process = subprocess.Popen(
            command,
            cwd=control,
            env={
                "PATH": os.defpath,
                "TMPDIR": str(scratch),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        outcome["start_error"] = type(error).__name__
        return outcome, captured, counts
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    try:
        # Keep polling the owned process even if it closes both output streams.
        # Otherwise an early close could skip cancellation and the wall deadline.
        while selector.get_map() or process.poll() is None:
            if cancellation_event is not None and cancellation_event.is_set():
                outcome["cancelled"] = True
                outcome["limit_reasons"].append("cancelled")
                break
            if time.monotonic() - started >= timeout:
                outcome["timed_out"] = True
                outcome["limit_reasons"].append("wall_time")
                break
            for key, _ in selector.select(timeout=0.02):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = max(0, OUTPUT_LIMIT - counts[key.data])
                captured[key.data].extend(chunk[:available])
                counts[key.data] += len(chunk)
                if counts[key.data] > OUTPUT_LIMIT:
                    outcome["output_limited"] = True
                    outcome["limit_reasons"].append("output_bytes")
                    break
            if outcome["output_limited"]:
                break
    finally:
        _stop(process)
        selector.close()
        process.stdout.close()
        process.stderr.close()
    outcome["returncode"] = process.returncode
    return outcome, captured, counts


def execute_program(
    program: str,
    candidate: Path,
    job: Path,
    timeout: float,
    *,
    files: dict[str, str] | None = None,
    arguments: tuple[str, ...] = (),
    secret_values: tuple[str, ...] = (),
    cancellation_event=None,
) -> dict:
    """Run one fixed phase; the caller supplies trusted code, never an HTTP body.

    ``job`` is created exclusively. Arguments default to candidate/report; explicit
    argv may interpolate only the four documented per-run roots. Every durable
    output is bounded and sanitized. Program/control source is trusted input and
    must not contain credentials. No fallback removes OS enforcement.
    """
    files = {} if files is None else files
    _validate(program, files, arguments, timeout)
    effective_arguments = arguments or ("{candidate}", "{report}")
    events.sanitize_public({}, secret_values=secret_values)
    candidate, job = Path(candidate), Path(job)
    if candidate.is_symlink() or not candidate.is_dir() or os.path.lexists(job):
        if os.path.lexists(job):
            raise FileExistsError("execution_job_exists")
        raise ValueError("invalid_candidate")
    candidate, job = candidate.resolve(), job.resolve()
    if job.is_relative_to(candidate) or candidate.is_relative_to(job):
        raise ValueError("execution_roots_overlap")
    job.mkdir(parents=True, exist_ok=False, mode=0o700)
    (job / "program.py").write_text(program)
    (job / "program.py").chmod(0o600)
    file_hashes = {name: _sha(value.encode()) for name, value in sorted(files.items())}
    for name, value in files.items():
        target = job / "control-files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
        target.chmod(0o600)
    limits = policy_limits(timeout)
    cpu_limit = limits["cpu_limit_seconds"]
    metadata = {
        "executor_version": EXECUTOR_VERSION,
        "program_sha256": _sha(program.encode()),
        "control_files_sha256": file_hashes,
        "arguments": list(arguments),
        "effective_arguments": list(effective_arguments),
        "timeout_seconds": float(timeout),
    }
    _json(job / "control-manifest.json", metadata)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="memos-verification-phase-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        control, config, scratch = (root / name for name in ("control", "config", "scratch"))
        for directory in (control, config, scratch):
            directory.mkdir(mode=0o700)
        (control / "main.py").write_text(program)
        for name, value in files.items():
            target = control / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        isolated_report = scratch / "report.json"
        replacements = {
            "candidate": str(candidate),
            "report": str(isolated_report),
            "control": str(control),
            "scratch": str(scratch),
        }
        argv = [argument.format_map(replacements) for argument in effective_arguments]
        spec = isolation.IsolationSpec(
            workspace=candidate,
            control_root=control,
            config_root=config,
            runtime_read_roots=isolation.runtime_paths(Path(sys.executable)),
            forbidden_roots=(Path(__file__).resolve().parents[3], job),
            writable_scratch=(scratch,),
        )
        bootstrap = (
            "import resource,runpy,sys; "
            f"resource.setrlimit(resource.RLIMIT_FSIZE, ({REPORT_LIMIT},{REPORT_LIMIT})); "
            f"resource.setrlimit(resource.RLIMIT_CPU, ({cpu_limit},{cpu_limit})); "
            f"resource.setrlimit(resource.RLIMIT_NOFILE, ({FILE_DESCRIPTOR_LIMIT},{FILE_DESCRIPTOR_LIMIT})); "
            "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        command = isolation.sandbox_test_command(
            [sys.executable, "-I", "-B", "-c", bootstrap, str(control / "main.py"), *argv], spec
        )
        profile = command[2].encode()
        profile_spec = {
            "workspace": str(candidate),
            "control_root": str(control),
            "config_root": str(config),
            "runtime_read_roots": [str(path) for path in spec.runtime_read_roots],
            "forbidden_roots": [str(path) for path in spec.forbidden_roots],
            "writable_scratch": [str(path) for path in spec.writable_scratch],
        }
        if profile_from_record(profile_spec, candidate_path=candidate, job=job).encode() != profile:
            raise ValueError("profile_renderer_mismatch")
        (job / "isolation.sb").write_bytes(profile)
        outcome, captured, counts = _capture(
            command,
            control=control,
            scratch=scratch,
            timeout=timeout,
            started=started,
            cancellation_event=cancellation_event,
        )
        for name in ("stdout", "stderr"):
            text = (
                "[OUTPUT LIMIT: CONTENT OMITTED]"
                if counts[name] > OUTPUT_LIMIT
                else captured[name].decode("utf-8", errors="replace")
            )
            public = events.sanitize_public(text, secret_values=secret_values)
            if not isinstance(public, str):
                raise TypeError("Public log sanitizer must return text")
            (job / f"{name}.txt").write_text(
                public.encode()[:OUTPUT_LIMIT].decode("utf-8", errors="ignore")
            )
        if isolated_report.is_symlink():
            outcome["output_limited"] = True
            outcome["limit_reasons"].append("report_symlink")
        elif isolated_report.is_file():
            if isolated_report.stat().st_size > REPORT_LIMIT:
                outcome["output_limited"] = True
                outcome["limit_reasons"].append("report_bytes")
            else:
                try:
                    report = json.loads(isolated_report.read_text())
                    public_report = events.sanitize_public(report, secret_values=secret_values)
                    serialized = json.dumps(public_report, ensure_ascii=False).encode()
                except (ValueError, UnicodeError, RecursionError):
                    serialized = b'{"invalid_report": true}'
                if len(serialized) > REPORT_LIMIT:
                    outcome["output_limited"] = True
                    outcome["limit_reasons"].append("report_bytes")
                else:
                    (job / "report.json").write_bytes(serialized)
        result = {
            **outcome,
            **metadata,
            "resolved_arguments": argv,
            "limits": limits,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "isolation": {
                "backend": BACKEND,
                "profile_sha256": _sha(profile),
                "implementation_digest": implementation_digest(),
                "profile_spec": profile_spec,
            },
        }
        for path in job.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        _json(job / "execution.json", result)
        return result
