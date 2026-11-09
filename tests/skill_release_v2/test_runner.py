"""Protocol probes are explicitly separate from project repair qualification."""

import hashlib

from types import SimpleNamespace

import pytest

from memos.skill_release_v2.runner import BASELINE, TARGET, ProbeChecks, probe_task


def test_probe_definitions_do_not_claim_skill_qualification():
    for name in ("legal", "denied", "persistent_error", "unreachable"):
        task = probe_task(name)
        assert task["purpose"] == "protocol_probe"
        assert task["skill_qualification"] is False
        assert "TASK.md" in task["files"] and task["files"]["value.py"] == BASELINE
    with pytest.raises(ValueError, match="unknown_protocol_probe"):
        probe_task("latest")


def test_public_probe_observes_exact_bytes_and_never_executes_candidate(tmp_path):
    (tmp_path / "value.py").write_text(BASELINE)
    checks = ProbeChecks("legal", tmp_path, tmp_path / "checks")
    request = SimpleNamespace(digest="a" * 64, snapshot="b" * 64)
    first, error = checks(request)
    assert not error and first["status"] == "failed"
    marker = tmp_path / "not-executed"
    (tmp_path / "value.py").write_text(f"open({str(marker)!r},'w').write('unsafe')\n")
    second, error = checks(request)
    assert not error and second["status"] == "failed" and not marker.exists()
    (tmp_path / "value.py").write_text(TARGET)
    third, error = checks(request)
    assert not error and third["status"] == "passed"
    assert len(checks.records) == 3


def test_fault_stays_unavailable_for_second_and_third_check(tmp_path):
    (tmp_path / "value.py").write_text(BASELINE)
    checks = ProbeChecks("persistent_error", tmp_path, tmp_path / "checks")
    request = SimpleNamespace(digest="a" * 64, snapshot="b" * 64)
    assert checks(request)[1] is False
    for _ in range(2):
        body, error = checks(request)
        assert error is True and body["status"] == "error" and body["checks"] == []
        assert body["execution_error"] == "protocol_probe_persistent_unavailable"


def test_check_records_are_immutable(tmp_path):
    (tmp_path / "value.py").write_text(BASELINE)
    folder = tmp_path / "checks"
    checks = ProbeChecks("legal", tmp_path, folder)
    request = SimpleNamespace(digest="a" * 64, snapshot="b" * 64)
    checks(request)
    before = (folder / "check-1.json").read_bytes()
    with pytest.raises(FileExistsError):
        ProbeChecks("legal", tmp_path, folder)
    assert (folder / "check-1.json").read_bytes() == before


def test_unjoined_consumer_never_reenters_its_locked_state(tmp_path, monkeypatch):
    from memos.skill_adapter.runner import RunConfig
    from memos.skill_release_v2 import runner

    tools = tmp_path / "tools"
    tools.mkdir()
    cli = tools / "probe-cli"
    cli.write_text("#!/bin/sh\nprintf '2.1.296 (fake probe)\\n'\n")
    cli.chmod(0o700)
    monkeypatch.setattr(runner, "CLI_SHA256", hashlib.sha256(cli.read_bytes()).hexdigest())
    monkeypatch.setattr(runner, "_managed_configuration_present", lambda: False)
    monkeypatch.setattr(runner.isolation, "probe_isolation", lambda spec: {"passed": True})

    def forbidden_state():
        raise AssertionError("an unjoined worker's lock must not be entered")

    def blocked_capture(*args, **kwargs):
        gate = kwargs["invalidate"].__self__
        gate.state = forbidden_state
        gate.audit = forbidden_state
        kwargs["consumer"].summary = forbidden_state
        return {
            "issues": ["consumer_unresponsive"],
            "consumer_joined": False,
            "invalidation_joined": False,
            "ownership_verified": True,
            "remaining_owned_processes": 0,
        }

    monkeypatch.setattr(runner, "capture_native_process", blocked_capture)
    config = RunConfig(
        cli_path=cli, model="claude-opus-5-5", timeout_seconds=180, max_turns=16, max_budget_usd=2
    )
    report = runner.run_protocol_probe(
        "legal",
        tmp_path / "run",
        config,
        expected_producer=runner.producer_identity()["digest"],
        provider_environment={},
    )
    assert report["status"] == "incomplete"
    assert report["gate_state_available"] is False and report["gate"] is None
    assert report["gate_audit"] == []
    assert (tmp_path / "run" / "report.json").is_file()
