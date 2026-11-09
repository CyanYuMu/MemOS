"""Normal use checks admission before a model call and after retained evidence."""

from types import SimpleNamespace

import pytest

from memos.skill_management import p5_runtime as runtime
from memos.skill_p5.service_runtime import case_options


def test_real_cli_size_is_not_subject_to_json_evidence_limit(tmp_path):
    import hashlib

    binary = tmp_path / "cli"
    content = b"x" * 8_000_001
    binary.write_bytes(content)
    assert runtime._binary_sha(binary) == hashlib.sha256(content).hexdigest()
    with pytest.raises(ValueError, match="evidence_size_invalid"):
        runtime._sha(binary)


def test_service_lists_only_repair_and_health_and_denies_before_execution(tmp_path, monkeypatch):
    assert len(case_options()) == 2
    with pytest.raises(ValueError):
        runtime.dev_case("p5-check-unavailable-r1")
    monkeypatch.setattr(
        runtime, "load_candidate", lambda _: {"candidate": {"content_hash": "a" * 64}}
    )
    monkeypatch.setattr(
        runtime, "export_identity", lambda _: {"contentHash": "a" * 64, "exportHash": "b" * 64}
    )
    with pytest.raises(ValueError, match="admission_denied"):
        runtime.execute_task(
            tmp_path,
            "p5-update-repair-r1",
            tmp_path / "run",
            SimpleNamespace(),
            {},
            mode="normal",
            authorize=lambda: {"allowed": False},
        )
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize(
    "native", [{}, {"native_returncode": 1}, {"native_timed_out": True}, {"native_error": True}]
)
def test_actual_native_and_os_checks_require_current_end_admission(
    tmp_path, monkeypatch, changed, native
):
    from p5_run_fixture import make_run

    from memos.skill_management.domain import validate_report

    f = make_run(tmp_path / "fixture", monkeypatch, **native)
    monkeypatch.setattr(runtime, "load_candidate", lambda _: {"candidate": f["candidate"]})
    identity = {"contentHash": f["candidate"]["content_hash"], "exportHash": f["export_hash"]}
    monkeypatch.setattr(runtime, "export_identity", lambda _: identity)
    monkeypatch.setattr(runtime, "native_skill", lambda _: f["skill"])
    # Qualification has its own real matrix tests; retain this synthetic runner's
    # explicitly fake qualification identity while exercising real check receipts.
    monkeypatch.setattr(runtime, "audit_protocol", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    calls = []

    def authorize():
        calls.append(True)
        return {
            "allowed": True,
            "releaseId": "approved-release",
            "sequence": 1 + int(changed and len(calls) > 1),
            **identity,
        }

    result = runtime.execute_task(
        tmp_path,
        "p5-update-repair-r1",
        tmp_path / "normal",
        f["config"],
        {},
        mode="normal",
        authorize=authorize,
        execution_guard=__import__("test_execution_tickets").Guard(),
    )
    assert len(calls) == 2, result
    assert result["status"] == ("unknown" if changed or native else "passed"), result
    assert result["errorCode"] == (
        "admission_changed" if changed else "execution_unverified" if native else None
    )
    assert result["validation"]["taskResult"]["status"] == "passed"
    v = validate_report(f["candidate"], result["validation"])
    assert v["kind"] == "isolated_dev" and v["split"] == "dev"
    assert v["branchCoverage"] == v["boundaryCoverage"] == []
    assert v["skillUsage"]["endedAdmission"] == ("needs_review" if changed else "allowed")


def test_unpublished_candidate_keeps_candidate_evaluation_ticket_mode(tmp_path, monkeypatch):
    from p5_run_fixture import make_run
    from test_execution_tickets import Guard

    f = make_run(tmp_path / "fixture", monkeypatch)
    monkeypatch.setattr(runtime, "load_candidate", lambda _: {"candidate": f["candidate"]})
    identity = {"contentHash": f["candidate"]["content_hash"], "exportHash": f["export_hash"]}
    monkeypatch.setattr(runtime, "export_identity", lambda _: identity)
    monkeypatch.setattr(runtime, "native_skill", lambda _: f["skill"])
    monkeypatch.setattr(runtime, "audit_protocol", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    guard = Guard()
    result = runtime.execute_task(
        tmp_path,
        "p5-update-repair-r1",
        tmp_path / "candidate-eval",
        f["config"],
        {},
        mode="candidate_evaluation",
        execution_guard=guard,
        authorize=lambda: {"allowed": True, "sequence": 1, "releaseId": None, **identity},
    )
    assert guard.ticket["mode"] == "candidate_evaluation"
    assert result["status"] == "passed", result
    assert result["validation"]["kind"] == "isolated_dev"
    assert result["validation"]["branchCoverage"] == []
