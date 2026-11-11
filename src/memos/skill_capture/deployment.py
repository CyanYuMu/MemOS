"""Private runtime snapshots survive switching branches in the source checkout."""

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time

from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from .core import canonical


LABEL = "org.memos.claude-capture"


def deploy(root):
    package = Path(__file__).parent
    files = {"skill_capture/" + p.name: p.read_bytes() for p in package.glob("*.py")}
    files["skill_experience/__init__.py"] = b'"""Standalone Memory transport."""\n'
    files["skill_experience/client.py"] = (
        package.parent / "skill_experience/client.py"
    ).read_bytes()
    revision = hashlib.sha256(
        canonical({k: hashlib.sha256(v).hexdigest() for k, v in files.items()}).encode()
    ).hexdigest()
    runtime = Path(root) / "runtime" / revision
    for name, data in files.items():
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError("runtime_snapshot_conflict")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
    return runtime / "skill_capture/entrypoint.py"


def launch_spec(config_path, entrypoint, state_dir):
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            "-I",
            str(entrypoint),
            "serve",
            "--config",
            str(config_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(Path(state_dir) / "worker.log"),
        "StandardErrorPath": str(Path(state_dir) / "worker-error.log"),
    }


def service_install(config_path, config):
    if sys.platform != "darwin":
        raise ValueError("launchd_macos_only")
    root = Path(config["stateDir"])
    entry = deploy(root)
    spec = launch_spec(config_path, entry, root)
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = plistlib.loads(path.read_bytes())
        if previous.get("Label") != LABEL or previous.get("ProgramArguments", [])[-1:] != [
            str(config_path)
        ]:
            raise ValueError("unowned_launch_agent")
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, check=False
        )
    path.write_bytes(plistlib.dumps(spec))
    path.chmod(0o600)
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, check=True
    )
    opener = build_opener(ProxyHandler({}))
    for _ in range(50):
        try:
            with opener.open(f"http://127.0.0.1:{config['port']}/status", timeout=0.3) as response:
                if "experiences" in json.load(response):
                    break
        except Exception:
            time.sleep(0.1)
    else:
        raise ValueError("capture_service_not_ready")
    print(json.dumps({"status": "started", "label": LABEL}))
