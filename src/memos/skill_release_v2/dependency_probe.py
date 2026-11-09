"""Qualify a copied, complete Feedback import without exposing the repository.

This is a host-only feasibility probe, not a task generator or a Skill validator.
The Python audit log supplies diagnostics; the reused OS profile enforces access.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import sys
import sysconfig
import time

from pathlib import Path, PurePosixPath

from memos.exceptions import MemOSError
from memos.skill_adapter import events, isolation
from memos.skill_verification.executor import _capture


PROTOCOL = "feedback-dependency-probe-1"
TARGET = "memos.mem_feedback.feedback"
MAX_FILES = 1000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ACCESS_RECORDS = 10000
MAX_REPORT_BYTES = 8 * 1024 * 1024
MODULE_PATTERN = re.compile(r"memos(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


class DependencyProbeError(MemOSError, ValueError):
    """A dependency bundle cannot be safely constructed or qualified."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: dict) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _module_name(name: str) -> str:
    if not isinstance(name, str) or not MODULE_PATTERN.fullmatch(name):
        raise DependencyProbeError("invalid_module")
    if any(part.lower().startswith("skill_") for part in name.split(".")):
        raise DependencyProbeError("forbidden_module")
    return name


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise DependencyProbeError("invalid_source_path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "memos"
        or any(ord(char) < 32 for char in value)
        or any(part.lower().startswith((".env", "skill_")) for part in relative.parts)
    ):
        raise DependencyProbeError("forbidden_source_path")
    return relative


def _source_file(root: Path, relative: str) -> Path:
    path = root / _relative(relative)
    if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root)):
        raise DependencyProbeError("source_symlink")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise DependencyProbeError("source_file_missing")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DependencyProbeError("source_file_too_large")
    return path


def _resolve(root: Path, name: str) -> Path | None:
    _module_name(name)
    relative = PurePosixPath(*name.split("."))
    for path in (relative.with_suffix(".py"), relative / "__init__.py"):
        target = root / path
        if target.is_file() or target.is_symlink():
            return _source_file(root, path.as_posix())
    return None


def _imports(tree):
    # Include conditional module-level imports conservatively. Imports only
    # executed inside functions must be explicitly added after a failed probe.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node
        else:
            yield from _imports(node)


def build_manifest(
    source_root: Path,
    *,
    target: str = TARGET,
    extra_modules: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
) -> dict:
    """Return an explicit, hashed whitelist; never copy a directory wholesale."""
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir() or type(extra_modules) is not tuple or type(resources) is not tuple:
        raise DependencyProbeError("invalid_manifest_input")
    pending = [_module_name(target), *map(_module_name, extra_modules)]
    modules: dict[str, str] = {}
    external = set()
    while pending:
        name = pending.pop()
        if name in modules:
            continue
        path = _resolve(root, name)
        if path is None:
            if name == target or name in extra_modules:
                raise DependencyProbeError("required_module_missing")
            continue
        modules[name] = path.relative_to(root).as_posix()
        if len(modules) > MAX_FILES:
            raise DependencyProbeError("module_limit_exceeded")
        parts = name.split(".")
        pending.extend(".".join(parts[:index]) for index in range(1, len(parts)))
        for node in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                base = node.module or ""
                if node.level:
                    package = parts if path.name == "__init__.py" else parts[:-1]
                    base = ".".join(
                        package[: len(package) - node.level + 1] + ([base] if base else [])
                    )
                targets = [base, *(base + "." + a.name for a in node.names if a.name != "*")]
            for imported in targets:
                if imported == "memos" or imported.startswith("memos."):
                    _module_name(imported)
                    if imported not in modules and _resolve(root, imported) is not None:
                        pending.append(imported)
                elif imported:
                    external.add(imported.split(".")[0])
    names = sorted(set(modules.values()) | {str(_relative(name)) for name in resources})
    if len(names) > MAX_FILES:
        raise DependencyProbeError("file_limit_exceeded")
    files = []
    for name in names:
        raw = _source_file(root, name).read_bytes()
        files.append({"path": name, "sha256": _sha(raw), "size": len(raw)})
    manifest = {
        "protocol": PROTOCOL,
        "target": target,
        "modules": dict(sorted(modules.items())),
        "extraModules": list(extra_modules),
        "resources": sorted(resources),
        "externalImports": sorted(external),
        "files": files,
    }
    return {**manifest, "manifestSha256": _digest(manifest)}


def copy_manifest(source_root: Path, destination: Path, manifest: dict) -> None:
    """Copy verified complete files, exclusively, with no editable reference twin."""
    root, destination = Path(source_root).resolve(strict=True), Path(destination)
    if os.path.lexists(destination):
        raise DependencyProbeError("destination_exists")
    body = {key: value for key, value in manifest.items() if key != "manifestSha256"}
    if manifest.get("manifestSha256") != _digest(body):
        raise DependencyProbeError("manifest_digest_mismatch")
    verified = []
    for entry in manifest["files"]:
        raw = _source_file(root, entry["path"]).read_bytes()
        if _sha(raw) != entry["sha256"] or len(raw) != entry["size"]:
            raise DependencyProbeError("source_changed")
        verified.append((entry["path"], raw))
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, raw in verified:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o444)


_CHILD = r"""
import errno
import importlib
import json
import os
import pathlib
import resource
import runpy
import socket
import subprocess
import sys
import traceback

control, bundle, scratch, site = map(pathlib.Path, sys.argv[1:5])
config = json.loads((control / "input.json").read_text())
resource.setrlimit(resource.RLIMIT_FSIZE, (config["reportLimit"], config["reportLimit"]))
resource.setrlimit(resource.RLIMIT_CPU, (config["cpuSeconds"], config["cpuSeconds"]))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
sys.path = [str(bundle), str(site)] + [p for p in sys.path if p and "site-packages" not in p]
os.environ.update({
    "MEMOS_BASE_PATH": str(scratch),
    "MEMOS_LEGACY_TIME_OFFSET": "+08:00",
    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
})
access = []
access_count = 0
def location(value):
    if not isinstance(value, (str, bytes)): return {"kind": "descriptor"}
    path = pathlib.Path(os.fsdecode(value))
    for label, root in (("bundle",bundle),("dependency",site),("scratch",scratch),("control",control)):
        if path.is_relative_to(root): return {"kind": label, "path": path.relative_to(root).as_posix()}
    return {"kind": "outside", "name": path.name}
def audit(event, args):
    global access_count
    if event == "open":
        access_count += 1
        if len(access) < config["accessLimit"]:
            access.append({"event":event, **location(args[0]), "mode":str(args[1])})
sys.addaudithook(audit)
def denied(operation):
    try: operation()
    except OSError as exc: return exc.errno in (errno.EPERM, errno.EACCES)
    return False
def read(path):
    with open(path,"rb") as stream: stream.read(1)
def network():
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as stream:
        stream.settimeout(1); stream.connect(("127.0.0.1",9))
checks={name:denied(lambda p=path:read(p)) for name,path in config["forbiddenReads"].items()}
checks["networkDenied"]=denied(network)
checks["processDenied"]=denied(lambda:subprocess.run(["/usr/bin/true"],check=True))
checks["bundleWriteDenied"]=denied(lambda:(bundle/"write-canary.txt").write_text("write"))
report={"imported":False,"health":None,"isolationChecks":checks,"environmentKeys":sorted(os.environ),
        "pythonFlags":{"isolated":sys.flags.isolated,"noSite":sys.flags.no_site,
                       "dontWriteBytecode":int(sys.dont_write_bytecode)},"failure":None}
try:
    importlib.import_module(config["target"])
    report["imported"]=True
    if config["health"]:
        report["health"]=runpy.run_path(str(control/"health.py"))["run_health_checks"]()
except Exception as exc:
    report["failure"]={"type":type(exc).__name__,"module":getattr(exc,"name",None),
        "detail":str(exc)[:2000],"frames":[{"line":f.lineno,"function":f.name,
          **location(f.filename)} for f in traceback.extract_tb(exc.__traceback__)[-12:]]}
report["modules"]={}
report["unexpectedModules"]=[]
for name,module in sorted(sys.modules.items()):
    if name!="memos" and not name.startswith("memos."):continue
    filename=getattr(module,"__file__",None)
    if filename is None:continue
    path=pathlib.Path(filename)
    if path.is_relative_to(bundle): report["modules"][name]=path.relative_to(bundle).as_posix()
    else: report["unexpectedModules"].append(name)
report["access"]=access
report["accessEventCount"]=access_count
report["accessComplete"]=access_count <= config["accessLimit"]
(scratch/"report.json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True))
"""


def _json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    path.chmod(0o600)


def run_probe(
    repository: Path,
    output: Path,
    *,
    timeout: float = 60,
    target: str = TARGET,
    extra_modules: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
    health: bool = False,
) -> dict:
    """Run a fresh complete import and OS canaries; failures stay in the report."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise DependencyProbeError("invalid_timeout")
    if type(health) is not bool or (health and (target != TARGET or timeout > 90)):
        raise DependencyProbeError("invalid_health")
    repository = Path(repository).resolve(strict=True)
    output = Path(output)
    if os.path.lexists(output):
        raise DependencyProbeError("output_exists")
    output = output.resolve()
    if output.is_relative_to(repository) or repository.is_relative_to(output):
        raise DependencyProbeError("probe_overlaps_repository")
    manifest = build_manifest(
        repository / "src", target=target, extra_modules=extra_modules, resources=resources
    )
    producer_paths = [Path(__file__)]
    if health:
        producer_paths.append(Path(__file__).with_name("dependency_health.py"))
    producer_bytes = {path.name: path.read_bytes() for path in producer_paths}
    producer_sources = {name: _sha(raw) for name, raw in producer_bytes.items()}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    bundle, control, config, scratch, hidden = (
        output / name for name in ("bundle", "control", "config", "scratch", "forbidden")
    )
    copy_manifest(repository / "src", bundle, manifest)
    for path in (control, config, scratch, hidden):
        path.mkdir(mode=0o700)
    host_canary = hidden / "host-config-canary.txt"
    host_canary.write_text("non-sensitive host configuration probe")
    dotenv_canary = config / ".env"
    dotenv_canary.write_text("NON_SENSITIVE_PROBE_CANARY=1\n")
    original = repository / "src" / manifest["modules"][target]
    skill_canary = hidden / "old-skill-canary.txt"
    skill_canary.write_text("non-sensitive old Skill probe")
    site = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    spec = isolation.IsolationSpec(
        workspace=bundle,
        control_root=control,
        config_root=config,
        runtime_read_roots=isolation.runtime_paths(Path(sys.executable)),
        forbidden_roots=tuple(
            dict.fromkeys((repository, Path(__file__).resolve().parents[3], hidden))
        ),
        writable_scratch=(scratch,),
    )
    _json(
        control / "input.json",
        {
            "target": target,
            "health": health,
            "cpuSeconds": math.ceil(timeout),
            "accessLimit": MAX_ACCESS_RECORDS,
            "reportLimit": MAX_REPORT_BYTES,
            "forbiddenReads": {
                "originalSourceDenied": str(original),
                "hostConfigDenied": str(host_canary),
                "oldSkillDenied": str(skill_canary),
                "dotenvDenied": str(dotenv_canary),
                "dotenvCaseDenied": str(config / ".ENV"),
            },
        },
    )
    (control / "probe.py").write_text(_CHILD)
    if health:
        (control / "health.py").write_bytes(producer_bytes["dependency_health.py"])
    command = isolation.sandbox_test_command(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(control / "probe.py"),
            str(control),
            str(bundle),
            str(scratch),
            str(site),
        ],
        spec,
    )
    # Deny dotenv even beneath allowed runtime/config roots. The bundle itself
    # never contains one. This is an OS rule, not an import-time Python patch.
    dotenv_pattern = json.dumps(r"(^|/)\.[eE][nN][vV](\.[^/]*)?$")
    command[2] += f"(deny file-read* (regex {dotenv_pattern}))\n"
    (output / "isolation.sb").write_text(command[2])
    _json(output / "manifest.json", manifest)
    outcome, captured, counts = _capture(
        command,
        control=control,
        scratch=scratch,
        timeout=timeout,
        started=time.monotonic(),
        cancellation_event=None,
    )
    for name in ("stdout", "stderr"):
        value = events.sanitize_public(captured[name].decode("utf-8", errors="replace"))
        (output / f"{name}.txt").write_text(value)
    child_path = scratch / "report.json"
    child = None
    if (
        child_path.is_file()
        and not child_path.is_symlink()
        and child_path.stat().st_size <= MAX_REPORT_BYTES
    ):
        child = json.loads(child_path.read_text())
    unchanged = all(
        (bundle / entry["path"]).is_file()
        and _sha((bundle / entry["path"]).read_bytes()) == entry["sha256"]
        for entry in manifest["files"]
    )
    producer_unchanged = all(
        path.read_bytes() == producer_bytes[path.name] for path in producer_paths
    )
    passed = bool(
        child
        and child["imported"]
        and child["failure"] is None
        and (not health or (child["health"] and child["health"]["passed"]))
        and all(child["isolationChecks"].values())
        and child["accessComplete"]
        and not child["unexpectedModules"]
        and all(manifest["modules"].get(name) == path for name, path in child["modules"].items())
        and unchanged
        and producer_unchanged
        and outcome["returncode"] == 0
        and not outcome["limit_reasons"]
    )
    result = {
        "protocol": PROTOCOL,
        "passed": passed,
        "manifestSha256": manifest["manifestSha256"],
        "moduleCount": len(manifest["modules"]),
        "bundleUnchanged": unchanged,
        "implementationSha256": producer_sources[Path(__file__).name],
        "producerSha256": _digest(producer_sources),
        "producerSources": producer_sources,
        "producerUnchanged": producer_unchanged,
        "profileSha256": _sha(command[2].encode()),
        "execution": outcome,
        "outputBytes": counts,
        "child": child,
    }
    _json(output / "access.json", child["access"] if child else [])
    _json(output / "report.json", result)
    return result
