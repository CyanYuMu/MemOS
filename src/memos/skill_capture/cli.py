"""Local capture installation, status and long-running delivery worker."""

import argparse
import fcntl
import json
import os
import shlex
import sys
import threading
import time

from pathlib import Path

from .core import CaptureStore, load_config, receive
from .install import ignore_private_settings, install, uninstall


def hook_command(config_path, entrypoint=None):
    return shlex.join(
        [
            sys.executable,
            "-I",
            str(entrypoint or Path(__file__).with_name("entrypoint.py")),
            "hook",
            "--config",
            str(Path(config_path).resolve()),
        ]
    )


def serve(config, config_path=None):
    from .dashboard import server_for
    from .worker import tick

    # The standalone entrypoint adds src/memos. This client is stdlib-only;
    # importing the full memos package in every tool hook would be costly.
    try:
        from skill_experience.client import ProductMemoryClient
    except ModuleNotFoundError:
        from memos.skill_experience.client import ProductMemoryClient
    store = CaptureStore(config)
    with (store.root / "worker.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        memory = config["memory"]
        client = ProductMemoryClient(
            memory["url"],
            timeout=10,
            headers={"Authorization": "Bearer " + memory["token"]} if memory.get("token") else None,
        )
        server = server_for(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"[capture] 后台已启动；状态页 http://127.0.0.1:{config['port']}", flush=True)
        try:
            while True:
                try:
                    if config_path:
                        config["enabled"] = load_config(config_path)["enabled"]
                    tick(store, client)
                except Exception:
                    print("[capture] worker_iteration_failed；本地事件保留", flush=True)
                time.sleep(3)
        finally:
            server.shutdown()
            server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("hook", "install", "uninstall", "status", "serve", "service-install", "reconcile"),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--experience", help="只读核对指定已尝试提交的经验，不重新 POST")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.command == "hook":
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("hook_input_too_large")
            receive(config, json.loads(raw))
        elif args.command == "serve":
            serve(config, args.config)
        elif args.command == "status":
            from .dashboard import snapshot

            print(json.dumps(snapshot(CaptureStore(config)), ensure_ascii=False, indent=2))
        elif args.command == "service-install":
            from .deployment import service_install

            service_install(args.config.resolve(), config)
        elif args.command == "reconcile":
            CaptureStore(config).reconcile(args.experience)
            print('{"status":"queued_for_readback","automaticPost":false}')
        else:
            from .deployment import deploy

            root = CaptureStore(config).root
            manifest = root / "installation.json"
            previous = json.loads(manifest.read_text()) if manifest.exists() else {}
            command = (
                hook_command(args.config, deploy(root))
                if args.command == "install"
                else previous.get("command")
            )
            if not command:
                raise ValueError("installation_missing")
            for project in config["projects"]:
                ignore_private_settings(Path(project["path"]))
                settings = Path(project["path"]) / ".claude/settings.local.json"
                result = (
                    install(settings, command, previous.get("command"))
                    if args.command == "install"
                    else uninstall(settings, command)
                )
                print(
                    json.dumps({"project": project["name"], "status": result}, ensure_ascii=False)
                )
            manifest.write_text(
                json.dumps({"command": command, "installed": args.command == "install"})
            )
            config["enabled"] = args.command == "install"
            args.config.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        if args.command == "hook":
            # Observation failure must not alter the user's coding task or permissions.
            # Only a safe error marker is emitted; no input/config/exception text.
            print("[memos-capture] capture_failed；本次观察未确认", file=sys.stderr)
            return 0
        print("[capture] operation_failed；请检查私有配置和后台状态", file=sys.stderr)
        return 1
