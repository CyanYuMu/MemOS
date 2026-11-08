"""Run the P3 console visibly in the caller's terminal, with prefixed logs.

Usage: .venv/bin/python scripts/dev/skill_p3_console.py --config /private/console.json
The private configuration is documented in docs/skill-prototype-p3.md. This
launcher never installs dependencies and never stops pre-existing services.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve()
    if config_path.stat().st_mode & 0o077:
        raise ValueError("private_config_required")
    config = json.loads(config_path.read_text())
    service_path = Path(config["serviceConfig"])
    service = json.loads(service_path.read_text())
    repo = Path(__file__).resolve().parents[2]
    logs = Path(config["logRoot"])
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    go = Path(config["goWorktree"])
    web = Path(config["webWorktree"])
    binary = go / "bin/memosd"
    if not binary.is_file() or not (web / "node_modules").is_dir():
        raise ValueError("build_go_and_install_locked_frontend_first")
    for port in (5280, 5274, service["listen"]["port"]):
        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError("required_port_in_use") from None
    Path(config["goEnvironment"]["MEMOSD_DB_PATH"]).parent.mkdir(
        parents=True, exist_ok=True, mode=0o700
    )
    secrets = [
        v for k, v in config["goEnvironment"].items() if any(w in k for w in ("TOKEN", "PASSWORD"))
    ]
    secrets += [service["graph"]["password"], service["serviceToken"]]
    secrets += list(service["runtime"]["providerEnvironment"].values())
    print_lock = threading.Lock()
    children = []
    required = []

    def line(name, value):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<redacted>")
        with print_lock:
            print(f"[{name}] {value.rstrip()}", flush=True)
        with (logs / (name + ".log")).open("a") as file:
            file.write(value.rstrip() + "\n")

    def start(name, command, cwd, environment=None, *, essential=False):
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        children.append(child)
        if essential:
            required.append(child)

        def copy():
            for value in child.stdout:
                line(name, value)

        threading.Thread(target=copy, daemon=True).start()
        return child

    def request(url, payload=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        for item in config.get("followLogs", []):
            start(item["name"], ["tail", "-n", "0", "-F", item["path"]], repo)
        if shutil.which("docker"):
            for index, container in enumerate(config.get("dockerContainers", [])):
                start(
                    "storage-" + str(index + 1),
                    ["docker", "logs", "--tail", "3", "--follow", container],
                    repo,
                )
        start(
            "skill",
            [
                sys.executable,
                "-u",
                "-m",
                "memos.skill_management",
                "serve",
                "--config",
                str(service_path),
            ],
            repo,
            essential=True,
        )
        go_env = {**os.environ, **config["goEnvironment"]}
        start("go", [str(binary), "serve", "--role", "console"], go, go_env, essential=True)
        pnpm = shutil.which("pnpm")
        if not pnpm:
            raise ValueError("pnpm_missing")
        start(
            "web",
            [pnpm, "dev", "--host", "127.0.0.1", "--strictPort"],
            web,
            {**os.environ, "MOCK_ORIGIN": "http://127.0.0.1:5280"},
            essential=True,
        )
        token = config["goEnvironment"]["MEMOSD_ADMIN_TOKEN"]
        ready = False
        for _ in range(90):
            if any(child.poll() is not None for child in required):
                raise ValueError("required_service_stopped")
            try:
                projects = request("http://127.0.0.1:5280/api/dashboard/project/dict", token=token)
                if config["projectId"] not in json.dumps(projects):
                    created = request(
                        "http://127.0.0.1:5280/api/dashboard/project/create",
                        {
                            "name": config["projectId"],
                            "description": "P3 Skill 审阅验证",
                            "ownerUsername": config["goEnvironment"]["MEMOSD_ADMIN_USER"],
                        },
                        token,
                    )
                    if created.get("code") != 0:
                        raise ValueError("project_bootstrap_failed")
                ready = True
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        if not ready:
            raise ValueError("console_not_ready")
        line(
            "console",
            "前端 http://127.0.0.1:5274 — Skill 管理服务初始化就绪后可审阅；Ctrl+C 关闭本次新启动的服务。",
        )
        line("console", "登录凭据保存在私有 console.json 的 goEnvironment 中；日志不会打印密码。")
        while all(child.poll() is None for child in required):
            time.sleep(0.5)
        raise ValueError("required_service_stopped")
    except KeyboardInterrupt:
        line("console", "正在关闭本次服务；原 Memory、Neo4j、Qdrant 保持运行。")
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGINT)
        deadline = time.monotonic() + 15
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("[console] 启动未完成，请检查已显示的服务日志及私有配置。", flush=True)
        raise SystemExit(1) from None
