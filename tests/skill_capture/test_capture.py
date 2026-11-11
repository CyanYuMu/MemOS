"""Daily capture preserves observations without granting repair qualification."""

import json
import sqlite3
import subprocess
import sys
import threading

from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from memos.skill_capture import entrypoint
from memos.skill_capture.core import CaptureStore, clean, load_config, receive
from memos.skill_capture.dashboard import render, server_for, snapshot
from memos.skill_capture.deployment import deploy, launch_spec
from memos.skill_capture.install import install, uninstall
from memos.skill_capture.worker import archive, sync_one


@pytest.fixture
def config(tmp_path):
    project = tmp_path / "MemOS"
    project.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return {
        "version": 1,
        "enabled": True,
        "stateDir": str(state),
        "projects": [{"path": str(project), "name": "MemOS"}],
        "memory": {"url": "http://127.0.0.1:64068", "userId": "daily", "cubeId": "daily"},
        "redactValues": ["test-secret-known-12345"],
        "port": 5282,
        "settleSeconds": 0,
    }


def event(config, kind, **extra):
    return {
        "session_id": "session-one",
        "cwd": config["projects"][0]["path"],
        "hook_event_name": kind,
        **extra,
    }


def complete(config):
    receive(config, event(config, "SessionStart", source="startup"))
    receive(config, event(config, "UserPromptSubmit", prompt="Repair the update call"))
    receive(
        config,
        event(
            config,
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="t1",
            tool_input={"command": "pytest -q"},
        ),
    )
    receive(
        config,
        event(
            config,
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="t1",
            tool_response={"stdout": "2 passed", "exitCode": 0},
        ),
    )
    receive(config, event(config, "Stop", last_assistant_message="Checks passed"))


def test_new_turn_is_observation_not_success(config):
    complete(config)
    store = CaptureStore(config)
    assert archive(store, idle_seconds=0) == 1
    item = store.experiences()[0]
    assert item["qualification"] == "observed_unverified"
    assert item["status"] == "pending"
    assert item["evidence"]["closed_by"] == "Stop"
    assert not item["evidence"]["issues"]
    assert "2 passed" in json.dumps(item)
    assert archive(store, idle_seconds=0) == 0


def test_scope_disabled_and_symlink_escape(config, tmp_path):
    outside = tmp_path / "MemOS-other"
    outside.mkdir()
    link = tmp_path / "MemOS" / "outside"
    link.symlink_to(outside, target_is_directory=True)
    for path in (outside, link):
        assert (
            receive(config, event(config, "UserPromptSubmit", cwd=str(path), prompt="no")) is None
        )
    config["enabled"] = False
    assert receive(config, event(config, "Stop", last_assistant_message="no")) is None


def test_credentials_thinking_sensitive_file_and_size_are_not_stored(config):
    receive(
        config,
        event(
            config, "UserPromptSubmit", prompt="API_KEY=abcd-secret-value test-secret-known-12345"
        ),
    )
    receive(
        config,
        event(
            config,
            "PostToolUse",
            tool_name="Read",
            tool_use_id="secret",
            tool_input={"file_path": config["projects"][0]["path"] + "/.env"},
            tool_response="private-body",
            thinking="private-reasoning",
        ),
    )
    receive(config, event(config, "Stop", last_assistant_message="x" * 40000))
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    raw = json.dumps(store.experiences())
    for secret in (
        "abcd-secret-value",
        "test-secret-known-12345",
        "private-body",
        "private-reasoning",
    ):
        assert secret not in raw
    assert "truncated" in raw


def test_tool_retry_dedup_and_identical_user_turns(config):
    receive(config, event(config, "UserPromptSubmit", prompt="again"))
    tool = event(config, "PostToolUse", tool_name="Read", tool_use_id="one", tool_response="text")
    receive(config, tool)
    receive(config, tool)
    receive(config, event(config, "Stop", last_assistant_message="done"))
    receive(config, event(config, "UserPromptSubmit", prompt="again"))
    receive(config, event(config, "Stop", last_assistant_message="done"))
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    rows = store.experiences()
    assert len(rows) == 2
    assert sum(len(x["evidence"]["events"]) for x in rows) == 5


def test_parallel_sessions_do_not_merge(config):
    def collect(i):
        receive(config, event(config, "UserPromptSubmit", session_id=f"s-{i}", prompt=f"task-{i}"))
        receive(
            config, event(config, "Stop", session_id=f"s-{i}", last_assistant_message=f"done-{i}")
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(collect, range(8)))
    store = CaptureStore(config)
    assert archive(store, idle_seconds=0) == 8
    assert len({x["session_id"] for x in store.experiences()}) == 8


def test_late_event_creates_new_revision_and_unfinished_marks_gap(config):
    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    first = store.experiences()[0]["id"]
    receive(
        config,
        event(config, "PostToolUseFailure", tool_name="Bash", tool_use_id="late", error="failed"),
    )
    assert archive(store, idle_seconds=0) == 1
    rows = store.experiences()
    assert len(rows) == 2
    assert rows[0]["evidence"]["supersedes"] == first
    receive(config, event(config, "UserPromptSubmit", session_id="unfinished", prompt="work"))
    assert archive(store, idle_seconds=0) == 1
    assert "turn_not_closed" in store.experiences()[0]["evidence"]["issues"]


class Memory:
    def __init__(self):
        self.records = {}
        self.posts = 0
        self.lose_response = False
        self.wrong_owner = False

    def add(self, payload):
        self.posts += 1
        content = payload["messages"][0]["content"]
        self.records["memory-1"] = {
            "id": "memory-1",
            "memory": content,
            "metadata": {
                **payload["info"],
                "user_id": payload["user_id"],
                "sources": [{"content": content}],
            },
        }
        if self.lose_response:
            raise OSError("sensitive endpoint and credential must not be logged")
        return {"data": [{"memory_id": "memory-1", "cube_id": "daily"}]}

    def get(self, memory_id, cube):
        value = json.loads(json.dumps(self.records[memory_id]))
        if self.wrong_owner:
            value["metadata"]["user_id"] = "other"
        return {"data": value}

    def find(self, cube, user, filters):
        assert filters["user_id"] == user
        self.filters = filters
        values = list(self.records.values())
        return {
            "data": {
                "text_mem": [{"cube_id": cube, "total_nodes": len(values), "memories": values}]
            }
        }


def test_memory_readback_and_restart_no_duplicate_post(config):
    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Memory()
    row = store.experiences()[0]
    sync_one(store, row["id"], memory)
    sync_one(CaptureStore(config), row["id"], memory)
    saved = store.experiences()[0]
    assert saved["status"] == "synced"
    assert saved["memory_ids"] == ["memory-1"]
    assert saved["readback"][0]["id"] == "memory-1"
    assert memory.posts == 1


def test_uncertain_post_is_reconciled_never_replayed(config):
    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Memory()
    memory.lose_response = True
    identifier = store.experiences()[0]["id"]
    sync_one(store, identifier, memory)
    assert store.experiences()[0]["status"] == "pending_reconcile"
    sync_one(CaptureStore(config), identifier, memory)
    assert store.experiences()[0]["status"] == "synced"
    assert memory.posts == 1


def test_empty_recovery_and_invalid_owner_stay_unverified(config):
    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Memory()
    memory.wrong_owner = True
    identifier = store.experiences()[0]["id"]
    sync_one(store, identifier, memory)
    assert store.experiences()[0]["status"] == "invalid"
    assert memory.posts == 1


def test_install_is_additive_idempotent_and_uninstall_preserves_user_edits(config, tmp_path):
    settings = tmp_path / "settings.local.json"
    original = {
        "permissions": {"allow": ["Read"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "original"}]}]},
    }
    settings.write_text(json.dumps(original))
    command = "python /trusted/entrypoint.py hook --config /private/config.json"
    install(settings, command)
    install(settings, command)
    value = json.loads(settings.read_text())
    assert len(value["hooks"]["Stop"]) == 2
    assert value["permissions"] == original["permissions"]
    value["model"] = "user-new-model"
    settings.write_text(json.dumps(value))
    uninstall(settings, command)
    value = json.loads(settings.read_text())
    assert value == {**original, "model": "user-new-model"}


def test_private_config_required_and_database_permissions(config, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private_config"):
        load_config(path)
    path.chmod(0o600)
    assert load_config(path)["enabled"]
    complete(config)
    db = CaptureStore(config).path
    assert db.stat().st_mode & 0o077 == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from events").fetchone()[0] == 5


def test_late_tool_result_keeps_original_turn(config):
    receive(config, event(config, "UserPromptSubmit", prompt="first", prompt_id="first"))
    receive(config, event(config, "PreToolUse", tool_name="Read", tool_use_id="late"))
    receive(config, event(config, "UserPromptSubmit", prompt="second", prompt_id="second"))
    receive(
        config,
        event(config, "PostToolUse", tool_name="Read", tool_use_id="late", tool_response="ok"),
    )
    receive(config, event(config, "Stop", last_assistant_message="second done"))
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    turns = {x["turn"]: x["evidence"] for x in store.experiences()}
    assert "tool_result_missing" not in turns["first"]["issues"]
    assert turns["second"]["closed_by"] == "Stop"
    assert not any(e["payload"].get("tool_use_id") for e in turns["second"]["events"])


def test_offline_before_send_stays_pending(config):
    class Offline(Memory):
        def find(self, *args):
            raise OSError("offline")

    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Offline()
    sync_one(store, store.experiences()[0]["id"], memory)
    assert memory.posts == 0
    assert store.experiences()[0]["status"] == "pending"


def test_empty_recovery_never_reposts(config):
    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Memory()
    memory.lose_response = True
    identifier = store.experiences()[0]["id"]
    sync_one(store, identifier, memory)
    memory.records.clear()
    for _ in range(3):
        sync_one(store, identifier, memory)
    assert memory.posts == 1
    assert store.experiences()[0]["status"] == "pending_reconcile"


def test_snapshot_runtime_is_content_bound(config):
    first = deploy(config["stateDir"])
    assert deploy(config["stateDir"]) == first
    assert first.is_file()
    spec = launch_spec("/private/config.json", first, config["stateDir"])
    assert spec["KeepAlive"] is True
    assert spec["ProgramArguments"][-1] == "/private/config.json"
    first.write_text("tampered")
    with pytest.raises(ValueError, match="snapshot_conflict"):
        deploy(config["stateDir"])


@pytest.mark.parametrize(
    "payload", ["invalid-json", "x" * (2 * 1024 * 1024 + 1)], ids=["invalid-json", "oversized"]
)
def test_hook_never_blocks_task_on_invalid_input(config, tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-I", entrypoint.__file__, "hook", "--config", str(path)],
        input=payload,
        text=True,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert "capture_failed" in result.stderr
    assert payload not in result.stderr


def test_standalone_real_stdin_capture(config, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-I", entrypoint.__file__, "hook", "--config", str(path)],
        input=json.dumps(event(config, "UserPromptSubmit", prompt="hello")),
        text=True,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert snapshot(CaptureStore(config))["events"] == 1


def test_upgrade_replaces_only_previous_owned_command(config, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "original"}))
    install(path, "old")
    install(path, "new", "old")
    value = json.loads(path.read_text())
    assert all(len(v) == 1 for v in value["hooks"].values())
    assert value["model"] == "original"
    uninstall(path, "new")
    assert json.loads(path.read_text()) == {"model": "original"}


def test_disabled_existing_hooks_and_symlink_config_are_rejected(config, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"disableAllHooks":true}')
    with pytest.raises(ValueError, match="disabled"):
        install(settings, "capture")
    link = tmp_path / "link.json"
    link.symlink_to(settings)
    with pytest.raises(ValueError, match="symlink"):
        install(link, "capture")


def test_dashboard_escapes_observations(config):
    receive(config, event(config, "UserPromptSubmit", prompt='<script>alert("x")</script>'))
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    page = render(snapshot(store))
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "待核验经验" in page


def test_dashboard_is_readonly_and_rejects_rebinding(config):
    config["port"] = 0
    store = CaptureStore(config)
    server = server_for(store)
    # The handler consults the configured advertised port; use the allocated test port.
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    try:
        for headers, data, expected in [
            ({"Host": "evil.example"}, None, 403),
            ({"Host": "127.0.0.1:0", "Sec-Fetch-Site": "cross-site"}, None, 403),
            ({"Host": "127.0.0.1:0"}, b"{}", 501),
        ]:
            with pytest.raises(HTTPError) as error:
                opener.open(
                    Request(f"http://127.0.0.1:{port}/", headers=headers, data=data), timeout=2
                )
            assert error.value.code == expected
        with opener.open(
            Request(f"http://127.0.0.1:{port}/status", headers={"Host": "127.0.0.1:0"}), timeout=2
        ) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert json.load(response)["events"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_full_source_on_partial_memory_body_is_explicitly_source_only(config):
    class Chunked(Memory):
        def add(self, payload):
            response = super().add(payload)
            self.records["memory-1"]["memory"] = "Derived partial text"
            return response

    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Chunked()
    sync_one(store, store.experiences()[0]["id"], memory)
    row = store.experiences()[0]
    assert row["status"] == "synced"
    assert row["readback"][0]["verification"] == "source_identity_only"
    assert row["qualification"] == "observed_unverified"


def test_explicit_readback_keeps_invalid_history_and_cannot_repost(config):
    complete(config)
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    memory = Memory()
    memory.wrong_owner = True
    identifier = store.experiences()[0]["id"]
    sync_one(store, identifier, memory)
    store.reconcile(identifier)
    memory.wrong_owner = False
    sync_one(store, identifier, memory)
    assert memory.posts == 1
    assert store.experiences()[0]["status"] == "synced"
    with store.db() as db:
        states = [r[0] for r in db.execute("SELECT status FROM delivery_observations ORDER BY seq")]
        assert "invalid" in states and states[-1] == "synced"


def test_stop_failure_never_becomes_success(config):
    receive(config, event(config, "UserPromptSubmit", prompt="work"))
    receive(
        config,
        event(
            config,
            "StopFailure",
            error="authentication_error",
            last_assistant_message="Not logged in",
        ),
    )
    store = CaptureStore(config)
    archive(store, idle_seconds=0)
    row = store.experiences()[0]
    assert "agent_api_failure" in row["evidence"]["issues"]
    assert row["evidence"]["task_success"] is None


def test_typed_private_reasoning_blocks_are_removed():
    assert "hidden" not in json.dumps(clean({"content": [{"type": "thinking", "text": "hidden"}]}))
