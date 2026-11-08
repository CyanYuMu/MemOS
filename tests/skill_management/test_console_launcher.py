"""The visible console launcher must never probe through a proxy or bind a used socket."""

from __future__ import annotations

import errno
import importlib.util
import io
import json

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[2] / "scripts/dev/skill_p3_console.py"
    spec = importlib.util.spec_from_file_location("skill_console_launcher_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def console(tmp_path, monkeypatch, launcher):
    """Only private temporary files and mocks are used; no host service is contacted."""
    go = tmp_path / "go"
    web = tmp_path / "web"
    (go / "bin").mkdir(parents=True)
    (go / "bin/memosd").touch()
    (web / "node_modules").mkdir(parents=True)
    service = tmp_path / "service.json"
    service.write_text(
        json.dumps(
            {
                "listen": {"host": "127.0.0.1", "port": 5281},
                "graph": {"password": "test-graph-password"},
                "serviceToken": "test-service-token",
                "runtime": {"providerEnvironment": {"TEST_TOKEN": "test-provider-token"}},
            }
        )
    )
    service.chmod(0o600)
    config = {
        "serviceConfig": str(service),
        "logRoot": str(tmp_path / "logs"),
        "goWorktree": str(go),
        "webWorktree": str(web),
        "projectId": "skill-console-test",
        "goEnvironment": {
            "MEMOSD_DB_PATH": str(tmp_path / "db/console.sqlite"),
            "MEMOSD_ADMIN_TOKEN": "test-admin-token",
            "MEMOSD_ADMIN_USER": "test-admin",
        },
    }
    config_path = tmp_path / "console.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    monkeypatch.setattr(launcher.sys, "argv", ["skill_p3_console.py", "--config", str(config_path)])

    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    process.stdout = iter(())
    process.pid = 123456
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher.threading, "Thread", MagicMock())
    monkeypatch.setattr(launcher.signal, "signal", MagicMock())
    monkeypatch.setattr(launcher.os, "killpg", MagicMock())
    monkeypatch.setattr(
        launcher.shutil, "which", lambda executable: "/test/pnpm" if executable == "pnpm" else None
    )
    check = MagicMock()
    check.connect_ex.return_value = errno.ECONNREFUSED
    # A disconnected socket may still be in TIME_WAIT. Binding it is not a
    # meaningful availability check for the application's reusable listener.
    check.bind.side_effect = OSError(errno.EADDRINUSE, "TIME_WAIT")
    check.__enter__.return_value = check
    monkeypatch.setattr(launcher.socket, "socket", MagicMock(return_value=check))

    def interrupt_after_readiness(seconds):
        if seconds == 0.5:
            raise KeyboardInterrupt

    monkeypatch.setattr(launcher.time, "sleep", interrupt_after_readiness)
    opener = MagicMock()
    opener.open.side_effect = AssertionError("unexpected HTTP request")
    build_opener = MagicMock(return_value=opener)
    proxy_handler = MagicMock(return_value=object())
    monkeypatch.setattr(launcher.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(launcher.urllib.request, "ProxyHandler", proxy_handler)
    return {
        "config": config,
        "config_path": config_path,
        "popen": popen,
        "socket": check,
        "opener": opener,
        "build_opener": build_opener,
        "proxy_handler": proxy_handler,
    }


def serve_json(console, responses):
    """Each request receives a fresh readable response, like urllib's context manager."""
    console["opener"].open.side_effect = [io.StringIO(json.dumps(value)) for value in responses]


def test_check_never_starts_processes_or_bootstraps_project(console, launcher, monkeypatch, capsys):
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        ["skill_p3_console.py", "--config", str(console["config_path"]), "--check"],
    )
    launcher.main()
    console["popen"].assert_not_called()
    console["opener"].open.assert_not_called()
    assert capsys.readouterr().out


def test_time_wait_is_not_mistaken_for_a_listening_service(console, launcher):
    launcher.ensure_available(5280)
    console["socket"].connect_ex.assert_called_once_with(("127.0.0.1", 5280))
    console["socket"].bind.assert_not_called()


def test_existing_listener_is_rejected_before_starting_any_service(console, launcher):
    console["socket"].connect_ex.return_value = 0
    with pytest.raises(ValueError, match="required_port_in_use"):
        launcher.main()
    console["popen"].assert_not_called()
    console["opener"].open.assert_not_called()


def test_successful_project_list_skips_creation_and_disables_proxy(console, launcher):
    serve_json(console, [{"code": 0, "data": [{"id": console["config"]["projectId"]}]}])
    launcher.main()
    assert console["popen"].call_count == 3
    console["opener"].open.assert_called_once()
    request = console["opener"].open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:5280/api/dashboard/project/dict"
    assert request.get_header("Authorization") == "Bearer test-admin-token"
    console["proxy_handler"].assert_called_once_with({})
    console["build_opener"].assert_called_once_with(console["proxy_handler"].return_value)


def test_missing_project_is_created_only_after_successful_dictionary_response(console, launcher):
    serve_json(
        console, [{"code": 0, "data": []}, {"code": 0, "data": {"id": "skill-console-test"}}]
    )
    launcher.main()
    assert console["opener"].open.call_count == 2
    request = console["opener"].open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:5280/api/dashboard/project/create"
    assert json.loads(request.data) == {
        "name": "skill-console-test",
        "description": "P3 Skill 审阅验证",
        "ownerUsername": "test-admin",
    }
    assert all(call.args == ({},) for call in console["proxy_handler"].call_args_list)


@pytest.mark.parametrize("reply", [{"code": 40300, "message": "denied"}, {"data": []}])
def test_non_success_dictionary_cannot_be_treated_as_missing_project(console, launcher, reply):
    serve_json(console, [reply])
    with pytest.raises(ValueError):
        launcher.main()
    console["opener"].open.assert_called_once()


@pytest.mark.parametrize("reply", [{"code": 40300}, {"data": {"id": "skill-console-test"}}])
def test_project_creation_requires_explicit_success_code(console, launcher, reply):
    serve_json(console, [{"code": 0, "data": []}, reply])
    with pytest.raises(ValueError, match="project_bootstrap_failed"):
        launcher.main()


def test_project_id_in_error_message_does_not_count_as_an_existing_project(console, launcher):
    serve_json(
        console, [{"code": 0, "data": [], "message": "skill-console-test is absent"}, {"code": 0}]
    )
    launcher.main()
    assert console["opener"].open.call_count == 2


def test_private_config_permissions_are_checked_before_any_process(console, launcher):
    console["config_path"].chmod(0o644)
    with pytest.raises(ValueError, match="private_config_required"):
        launcher.main()
    console["popen"].assert_not_called()
