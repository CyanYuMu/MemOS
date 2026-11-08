"""Host-started acceptance with bounded output and an OS-confined candidate.

The trusted oracle and candidate still share a Python interpreter. This protects
host files and other runs, not oracle internals from malicious Python reflection.
The collector and its artifact directory must remain trusted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time

from pathlib import Path

from memos.skill_adapter import events
from memos.skill_adapter.isolation import (
    BACKEND,
    IsolationSpec,
    runtime_paths,
    sandbox_test_command,
)


OUTPUT_LIMIT = 128 * 1024
REPORT_LIMIT = 1024 * 1024


def implementation_digest() -> str:
    """Bind the executor and policy source, beyond a human-readable backend name."""
    directory = Path(__file__).parent
    sources = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("verification.py", "isolation.py", "events.py")
    }
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()


def execute_guarded(
    oracle: Path,
    candidate: Path,
    report: Path,
    job: Path,
    timeout: float,
    *,
    secret_values: tuple[str, ...] = (),
) -> dict:
    """Execute the current oracle with no network/fork or host artifact access.

    This is an optional ``verify_candidate(executor=...)`` host-side callback.
    Unsupported machines raise before any candidate is executed; there is no
    fallback to the ordinary verifier process.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    # Validate redaction configuration before candidate execution. Raw output is
    # never written to durable artifacts, including when the subprocess fails.
    events.sanitize_public({}, secret_values=secret_values)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="memos-acceptance-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        control, config, scratch = (root / name for name in ("control", "config", "scratch"))
        for directory in (control, config, scratch):
            directory.mkdir()
        isolated_oracle = control / "oracle.py"
        shutil.copyfile(oracle, isolated_oracle)
        isolated_report = scratch / "report.json"
        spec = IsolationSpec(
            workspace=Path(candidate).resolve(),
            control_root=control,
            config_root=config,
            runtime_read_roots=runtime_paths(Path(sys.executable)),
            forbidden_roots=(Path(__file__).resolve().parents[3], job.resolve().parents[2]),
            writable_scratch=(scratch,),
        )
        # A hard file-size cap also bounds result files produced by candidate code.
        bootstrap = (
            "import resource, runpy, sys; "
            f"resource.setrlimit(resource.RLIMIT_FSIZE, ({REPORT_LIMIT}, {REPORT_LIMIT})); "
            "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        command = sandbox_test_command(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                bootstrap,
                str(isolated_oracle),
                str(candidate),
                str(isolated_report),
            ],
            spec,
        )
        profile = command[2].encode()
        (job / "isolation.sb").write_bytes(profile)
        isolation = {
            "backend": BACKEND,
            "profile_sha256": hashlib.sha256(profile).hexdigest(),
            "implementation_digest": implementation_digest(),
            "output_limit_bytes": OUTPUT_LIMIT,
            "report_limit_bytes": REPORT_LIMIT,
        }
        stdout_path, stderr_path = job / "stdout.txt", job / "stderr.txt"
        timed_out = output_limited = False
        try:
            process = subprocess.Popen(
                command,
                cwd=candidate,
                env={"PATH": os.defpath, "TMPDIR": str(scratch)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            stdout_path.write_bytes(b"")
            stderr_path.write_bytes(b"")
            return {
                "returncode": None,
                "timed_out": False,
                "background_processes": False,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "start_error": type(error).__name__,
                "isolation": isolation,
            }
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        counts = {"stdout": 0, "stderr": 0}
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    timed_out = True
                    process.kill()
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
                        output_limited = True
                        process.kill()
                        break
                if output_limited:
                    break
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            selector.close()
            process.stdout.close()
            process.stderr.close()
        for name, destination in (("stdout", stdout_path), ("stderr", stderr_path)):
            # Omit an overflowing stream instead of persisting a cut credential.
            text = (
                "[OUTPUT LIMIT: CONTENT OMITTED]"
                if counts[name] > OUTPUT_LIMIT
                else captured[name].decode("utf-8", errors="replace")
            )
            public = events.sanitize_public(text, secret_values=secret_values)
            if not isinstance(public, str):
                raise TypeError("Public log sanitizer must return text")
            bounded = public.encode("utf-8")[:OUTPUT_LIMIT].decode("utf-8", errors="ignore")
            destination.write_text(bounded, encoding="utf-8")
        if isolated_report.is_symlink():
            output_limited = True
        elif isolated_report.is_file():
            if isolated_report.stat().st_size > REPORT_LIMIT:
                output_limited = True
            else:
                try:
                    raw_report = json.loads(isolated_report.read_text(encoding="utf-8"))
                    public_report = events.sanitize_public(raw_report, secret_values=secret_values)
                    serialized = json.dumps(public_report, ensure_ascii=False).encode("utf-8")
                except (UnicodeError, ValueError, RecursionError):
                    # Preserve failure without persisting an unstructured body.
                    serialized = b'{"invalid_report": true}'
                if len(serialized) > REPORT_LIMIT:
                    output_limited = True
                else:
                    report.write_bytes(serialized)
        return {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "background_processes": False,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "output_limited": output_limited,
            "isolation": isolation,
        }
