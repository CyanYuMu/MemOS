"""Recovery rechecks native bytes; synthetic events are not Claude evidence."""

import json

import pytest

from p5_run_fixture import make_run
from test_execution_tickets import Guard

from memos.skill_management import p5_runtime as runtime


@pytest.fixture
def retained(tmp_path, monkeypatch):
    f = make_run(tmp_path / "fixture", monkeypatch)
    monkeypatch.setattr(runtime, "load_candidate", lambda _: {"candidate": f["candidate"]})
    projection = {"contentHash": f["candidate"]["content_hash"], "exportHash": f["export_hash"]}
    monkeypatch.setattr(runtime, "export_identity", lambda _: projection)
    monkeypatch.setattr(runtime, "native_skill", lambda _: f["skill"])
    monkeypatch.setattr(runtime, "audit_protocol", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    job = {
        "jobId": "e" * 64,
        "skillId": f["candidate"]["skill_id"],
        "revision": f["candidate"]["revision"],
        "caseId": "p5-update-repair-r1",
        "mode": "normal",
        "status": "unknown",
        **projection,
    }
    root = tmp_path / "artifacts" / "runs" / job["jobId"]
    result = runtime.execute_task(
        tmp_path,
        job["caseId"],
        root,
        f["config"],
        {},
        mode="normal",
        authorize=lambda: {
            "allowed": True,
            "sequence": 1,
            "releaseId": "approved-release",
            **projection,
        },
        execution_guard=Guard(),
    )
    assert result["status"] == "passed"
    return tmp_path / "artifacts", f["candidate"], job, result, root


def test_completed_unknown_can_recover_without_calling_model(retained, monkeypatch):
    from memos.skill_management.feedback import read_feedback

    artifacts, candidate, job, result, _ = retained
    monkeypatch.setattr(runtime, "run_extension_task", lambda *a, **k: pytest.fail("model rerun"))
    recovered = read_feedback(artifacts, job, candidate)
    assert recovered["validation"] == result["validation"]
    assert recovered["validation"]["branchCoverage"] == []
    assert recovered["ticket"]["mode"] == "normal"
    assert job["status"] == "unknown"


@pytest.mark.parametrize(
    "fault", ["missing", "manifest", "snapshot", "validation", "wrong_job", "not_unknown"]
)
def test_unbound_or_tampered_evidence_is_rejected(retained, fault):
    from memos.skill_management.feedback import read_feedback

    artifacts, candidate, job, _, root = retained
    if fault == "missing":
        (root / "managed-result.json").unlink()
    elif fault in {"manifest", "validation"}:
        path = root / ("manifest.json" if fault == "manifest" else "managed-result.json")
        value = json.loads(path.read_text())
        if fault == "manifest":
            value["executionMode"] = "candidate_evaluation"
        else:
            value["validation"]["taskResult"]["accepted"] = False
        path.write_text(json.dumps(value))
    elif fault == "snapshot":
        (root / "runs/update-repair-r1-skill/candidate/unexpected.py").write_text("pass\n")
    elif fault == "wrong_job":
        job["exportHash"] = "0" * 64
    else:
        job["status"] = "passed"
    with pytest.raises((ValueError, OSError, KeyError)):
        read_feedback(artifacts, job, candidate)
