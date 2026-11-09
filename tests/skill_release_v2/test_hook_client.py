"""Copied Hook executable stays independent of MemOS and site-packages."""

import json
import shutil
import subprocess
import sys

from pathlib import Path

from test_transport import hook
from test_transport import host as host_fixture


host = host_fixture
SOURCE = Path(__file__).parents[2] / "src/memos/skill_release_v2/hook_client.py"


def invoke(tmp_path, config, payload):
    script = tmp_path / "hook.py"
    shutil.copyfile(SOURCE, script)
    config_path = tmp_path / "hook.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(script), "--config", str(config_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=6,
    )


def test_isolated_hook_uses_real_socket_and_no_import_path(tmp_path, host):
    host.start()
    result = invoke(tmp_path, host.credentials("hook"), hook(workspace=host.gate.workspace))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert host.delivery_material("read")["pre_hook"]["tool_use_id"] == "read"


def test_hook_socket_unavailable_is_blocking_exit_and_never_allow(tmp_path, host):
    result = invoke(tmp_path, host.credentials("hook"), hook(workspace=host.gate.workspace))
    assert result.returncode == 2
    assert "allow" not in result.stdout
    assert host.credentials("hook")["token"] not in result.stdout + result.stderr


def test_hook_client_rejects_wrong_session_before_contact(tmp_path, host):
    host.start()
    payload = hook(workspace=host.gate.workspace)
    payload["session_id"] = "forged"
    result = invoke(tmp_path, host.credentials("hook"), payload)
    assert result.returncode == 2
    assert not host.gate.audit()
