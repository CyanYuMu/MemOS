"""Synthetic native events plus real receipts exercise the host ingestion boundary."""

import hashlib
import json

import pytest

from management_fixtures import candidate_for, report

from memos.skill_adapter.artifacts import SkillRevision, export_skill
from memos.skill_adapter.events import EventRecorder
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import verify_candidate
from memos.skill_governance.collector import assess_run
from memos.skill_management.domain import ManagementError, validate_report


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def retained_run(tmp_path, case_id="holdout-record"):
    c, _ = candidate_for("synthetic-collector")
    case = get_case(case_id)
    root = tmp_path / "run"
    root.mkdir()
    store = SnapshotStore(root / "snapshots")
    initial = materialize_case(case.case_id, tmp_path / "initial")
    initial_id = store.capture(initial).snapshot_id
    candidate = materialize_case(case.case_id, root / "candidate", "reference")
    final_id = store.capture(candidate).snapshot_id
    control = tmp_path / "control"
    spec = SkillRevision(c["skill_id"], c["revision"], "Synthetic test instruction", ("R1",))
    from memos.skill_adapter.artifacts import runtime_alias

    manifest = export_skill(spec, control / "skills" / runtime_alias(spec))
    control_id = store.capture(control).snapshot_id
    context = {
        "run_id": "synthetic-run",
        "attempt_id": "synthetic-attempt",
        "case_id": case.case_id,
        "case_revision": case.revision,
        "lineage_id": case.lineage_id,
        "split": case.split,
        "initial_snapshot": initial_id,
        "skill_id": c["skill_id"],
        "skill_revision": c["revision"],
    }
    journal = EventRecorder(root / "events.jsonl", context, expected_session_id="synthetic-native")
    journal.ingest_hook({"hook_event_name": "SessionStart", "session_id": "synthetic-native"})
    journal.ingest_stream(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "synthetic-native",
            "skills": [manifest["runtime_alias"]],
            "slash_commands": [manifest["runtime_alias"]],
        }
    )
    for hook in ("UserPromptSubmit", "UserPromptExpansion", "Stop"):
        journal.ingest_hook(
            {
                "hook_event_name": hook,
                "session_id": "synthetic-native",
                **(
                    {"command_name": manifest["runtime_alias"], "gate": "allowed"}
                    if hook == "UserPromptExpansion"
                    else {}
                ),
            }
        )
    journal.ingest_stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "synthetic-native",
            "result": "Synthetic only",
        }
    )
    result = verify_candidate(
        case.case_id,
        candidate,
        root / "validation",
        run_id=context["run_id"],
        attempt_id=context["attempt_id"],
    )
    receipt = json.loads(result.receipt_path.read_text())
    value = {
        "run_context": context,
        "session_id": "synthetic-native",
        "capture": journal.finish(),
        "events_sha256": sha(root / "events.jsonl"),
        "receipt": str(result.receipt_path),
        "receipt_sha256": sha(result.receipt_path),
        "initial_snapshot": initial_id,
        "candidate_snapshot": final_id,
        "control_snapshot": control_id,
        "skill_export": manifest,
        "loading": {"status": "verified"},
        "context_matches": True,
        "case_asset_digest": case_digest(case),
        "visible_checks": [],
    }
    (root / "report.json").write_text(json.dumps(value))
    v = report(c, status="passed" if result.status == "passed" else "unknown")
    v.update(
        caseId=case.case_id,
        caseRevision=case.revision,
        lineageId=case.lineage_id,
        runId=context["run_id"],
        attemptId=context["attempt_id"],
        exportHash=manifest["export_hash"],
        scope={**v["scope"], "caseIds": [case.case_id]},
        receipt={"uri": result.receipt_path.as_uri(), "sha256": sha(result.receipt_path)},
    )
    return c, validate_report(c, v), root, value, receipt


def test_actual_receipt_and_retained_export_are_rechecked(tmp_path):
    c, v, root, _, receipt = retained_run(tmp_path)
    value = assess_run(c, v, root, allowed_root=tmp_path)
    assert value["verification"]["matches"] and value["verification"]["status"] == "passed"
    assert value["candidateSnapshot"] == receipt["candidate_snapshot"]
    assert value["captureComplete"] and value["loadVerified"]
    assert value["behaviorChanged"]
    assert value["branchCandidates"] == []
    assert "assessmentId" not in value


@pytest.mark.parametrize("mutation", ["patch", "events", "attempt", "export", "outside_root"])
def test_host_ingestion_rejects_substituted_or_changed_evidence(tmp_path, mutation):
    c, v, root, value, _ = retained_run(tmp_path)
    allowed = tmp_path
    if mutation == "patch":
        (root / "candidate/caller.py").write_text("# substituted\n")
    elif mutation == "events":
        (root / "events.jsonl").write_text("")
    elif mutation == "attempt":
        value["run_context"]["attempt_id"] = "substituted"
    elif mutation == "export":
        value["skill_export"]["export_hash"] = "f" * 64
    else:
        allowed = tmp_path / "unrelated"
    (root / "report.json").write_text(json.dumps(value))
    with pytest.raises((ManagementError, ValueError)):
        assess_run(c, v, root, allowed_root=allowed)


def test_posthoc_must_match_same_final_candidate_and_task(tmp_path):
    c, v, root, _, _ = retained_run(tmp_path)
    other = materialize_case("holdout-binding", tmp_path / "other", "reference")
    recheck = verify_candidate("holdout-binding", other, tmp_path / "posthoc")
    with pytest.raises((ManagementError, ValueError)):
        assess_run(
            c,
            v,
            root,
            allowed_root=tmp_path,
            reassessment=(recheck.receipt_path, tmp_path / "posthoc"),
        )


def test_manual_diagnosis_is_separate_from_repaired_behavior(tmp_path):
    c, v, root, _, _ = retained_run(tmp_path, "holdout-ambiguous")
    assessment = assess_run(c, v, root, allowed_root=tmp_path)
    assert assessment["verification"]["status"] == "needs_review"
    assert assessment["behaviorChanged"] is False
