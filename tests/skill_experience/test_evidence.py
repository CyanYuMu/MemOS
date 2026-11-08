"""Real guarded receipts plus synthetic native events exercise the sealing boundary.

These fixtures are deliberately not real Claude runs. ``trusted_collection=True``
simulates the host-owned entry point; no fixture is written into a source Memory.
"""

import hashlib
import json
import sys

from datetime import datetime, timezone
from pathlib import Path

import pytest

from memos.skill_adapter.events import EventRecorder
from memos.skill_adapter.runner import _adapter_digest
from memos.skill_adapter.verification import execute_guarded
from memos.skill_evaluation.snapshots import SnapshotStore
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import verify_candidate
from memos.skill_experience.evidence import (
    load_source_summary,
    seal_evidence,
    verify_evidence,
)


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real guarded acceptance")


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_run(root, *, case_id="source-dream-update", final_variant="reference"):
    """Build actual P0 receipts, not mocked pass flags, for a synthetic Agent trace."""
    case = get_case(case_id)
    preflight = root / "preflight"
    preflight.mkdir(parents=True)
    variants = {}
    for variant in ("baseline", "initial"):
        candidate = materialize_case(case_id, preflight / f"{variant}-candidate", variant)
        artifacts = preflight / f"{variant}-validation"
        checked = verify_candidate(case_id, candidate, artifacts, executor=execute_guarded)
        variants[variant] = {
            "candidate": str(candidate),
            "artifact_root": str(artifacts),
            "receipt": str(checked.receipt_path),
        }
    write_json(
        preflight / "preflight.json",
        {
            "schema_version": 1,
            "case_id": case_id,
            "case_revision": case.revision,
            "asset_digest": case_digest(case),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "variants": variants,
        },
    )
    run = root / "run"
    store = SnapshotStore(run / "snapshots")
    initial = store.capture(Path(variants["initial"]["candidate"]))
    context = {
        "run_id": "synthetic-test-run",
        "attempt_id": "synthetic-test-attempt",
        "case_id": case_id,
        "case_revision": case.revision,
        "split": case.split,
        "lineage_id": case.lineage_id,
        "initial_snapshot": initial.snapshot_id,
    }
    candidate = materialize_case(case_id, run / "candidate", final_variant)
    final = store.capture(candidate)
    control = root / "control"
    control.mkdir()
    write_json(control / "context.json", context)
    control_snapshot = store.capture(control)
    journal = EventRecorder(run / "events.jsonl", context, expected_session_id="native-test")
    journal.ingest_hook({"hook_event_name": "SessionStart", "session_id": "native-test"})
    journal.ingest_stream(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "native-test",
            "model": "claude-test-pinned-1",
            "claude_code_version": "2.1.296",
            "permissionMode": "dontAsk",
            "tools": ["Read", "Edit", "Write", "mcp__memos_run__run_visible_checks"],
            "skills": ["doctor", "plugin-authoring"],
            "plugins": [],
            "mcp_servers": [{"name": "memos_run", "status": "connected"}],
        }
    )
    journal.ingest_hook({"hook_event_name": "UserPromptSubmit", "session_id": "native-test"})
    journal.ingest_stream(
        {
            "type": "assistant",
            "session_id": "native-test",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "edit-1",
                        "name": "Edit",
                        "input": {"file_path": "fake_graph.py"},
                    }
                ]
            },
        }
    )
    journal.ingest_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "native-test",
            "tool_use_id": "edit-1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "fake_graph.py"},
        }
    )
    journal.ingest_hook(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "native-test",
            "tool_use_id": "edit-1",
            "tool_name": "Edit",
            "tool_response": "edited",
        }
    )
    journal.ingest_stream(
        {
            "type": "user",
            "session_id": "native-test",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "edit-1", "content": "edited"}]
            },
        }
    )
    journal.ingest_hook({"hook_event_name": "Stop", "session_id": "native-test"})
    journal.ingest_stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "native-test",
            "result": "Synthetic test observation",
            "modelUsage": {"claude-test-pinned-1": {}},
        }
    )
    receipt = verify_candidate(
        case_id,
        candidate,
        run / "validation",
        executor=execute_guarded,
        run_id=context["run_id"],
        attempt_id=context["attempt_id"],
    )
    write_json(run / "isolation.json", {"passed": True})
    report = {
        "schema_version": 1,
        "adapter_version": "0.1",
        "purpose": "adapter_validation",
        "run_context": context,
        "session_id": "native-test",
        "status": "passed" if receipt.status == "passed" else "incomplete",
        "cli_version": "2.1.296",
        "cli_sha256": sha(Path(sys.executable)),
        "adapter_sha256": _adapter_digest(),
        "requested_model": "claude-test-pinned-1",
        "max_turns": 8,
        "max_budget_usd": 1,
        "effort": "low",
        "timeout_seconds": 120,
        "tools": ["Read", "Edit", "Write"],
        "case_asset_digest": case_digest(case),
        "initial_snapshot": initial.snapshot_id,
        "candidate_snapshot": final.snapshot_id,
        "control_snapshot": control_snapshot.snapshot_id,
        "profile_sha256": "a" * 64,
        "task_accepted": receipt.status == "passed",
        "task_status": receipt.status,
        "qualified_agent_experience": False,
        "effectiveness_evaluation": False,
        "skill_export": None,
        "receipt": str(receipt.receipt_path),
        "receipt_matches": True,
        "capture": journal.finish(),
        "events_sha256": sha(run / "events.jsonl"),
        "receipt_sha256": sha(receipt.receipt_path),
        "reported_models": ["claude-test-pinned-1"],
        "context_matches": True,
        "visible_checks": [],
        "execution": {"returncode": 0, "timed_out": False, "elapsed_seconds": 1},
        "loading": {
            "status": "not_requested",
            "discovered": False,
            "native_expanded": False,
            "canary_observed": None,
        },
    }
    write_json(run / "report.json", report)
    return run, preflight, report


def seal(root, **kwargs):
    run, preflight, _ = make_run(root, **kwargs)
    path = seal_evidence(run, preflight, root / "evidence", trusted_collection=True)
    return path, run, preflight


def test_guarded_source_success_is_bound_and_summarized(tmp_path):
    path, run, _ = seal(tmp_path)
    evidence = verify_evidence(path)
    assert evidence["qualification"] == "qualified_success"
    assert evidence["unknowns"]["root_cause"] == "unknown"
    assert evidence["changes"]["files"]
    summary = load_source_summary(path)
    assert summary["evidence_revision"] == evidence["evidence_revision"]
    assert summary["split"] == "source"
    assert 0 < len(summary["content"]) <= 16000
    assert all(key.startswith("skill_") for key in summary["info"])
    # P1b makes a new judgment; it never rewrites the historical P1a qualification.
    assert json.loads((run / "report.json").read_text())["qualified_agent_experience"] is False


def test_failed_repair_is_failure_fact_not_unreviewed_root_cause(tmp_path):
    path, _, _ = seal(tmp_path, final_variant="initial")
    result = verify_evidence(path)
    assert result["qualification"] == "qualified_failure"
    assert result["unknowns"]["failure_attribution"] == "unknown"


def test_default_does_not_authenticate_imported_json(tmp_path):
    run, preflight, _ = make_run(tmp_path)
    result = verify_evidence(seal_evidence(run, preflight, tmp_path / "evidence"))
    assert result["qualification"] == "excluded"
    with pytest.raises(ValueError, match="source_ineligible"):
        load_source_summary(tmp_path / "evidence" / "evidence.json")


def test_missing_initial_failure_requires_more_evidence(tmp_path):
    run, preflight, _ = make_run(tmp_path)
    data = json.loads((preflight / "preflight.json").read_text())
    del data["variants"]["initial"]
    write_json(preflight / "preflight.json", data)
    path = seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)
    assert verify_evidence(path)["qualification"] == "needs_evidence"


@pytest.mark.parametrize("case_id", ["dev-strict-signature", "holdout-strict-signature"])
def test_non_source_tasks_cannot_be_relabelled(tmp_path, case_id):
    if case_id.startswith("holdout"):
        from memos.skill_evaluation.holdout_tasks import HOLDOUT_CASE_IDS

        case_id = HOLDOUT_CASE_IDS[0]
    path, _, _ = seal(tmp_path, case_id=case_id)
    assert verify_evidence(path)["qualification"] == "excluded"


@pytest.mark.parametrize("change", ["events", "candidate", "receipt", "snapshot", "evidence"])
def test_artifact_tampering_is_detected_on_every_read(tmp_path, change):
    path, run, _ = seal(tmp_path)
    report = json.loads((run / "report.json").read_text())
    target = {
        "events": run / "events.jsonl",
        "candidate": run / "candidate" / "fake_graph.py",
        "receipt": Path(report["receipt"]),
        "snapshot": run / "snapshots" / report["initial_snapshot"] / "tree" / "fake_graph.py",
        "evidence": path,
    }[change]
    target.write_text(target.read_text() + "\n ")
    with pytest.raises(ValueError, match="evidence_"):
        verify_evidence(path)


@pytest.mark.parametrize(
    "field,value", [("split", "dev"), ("run_id", "foreign-run"), ("lineage_id", "invented")]
)
def test_report_context_cannot_relabel_registered_source_or_events(tmp_path, field, value):
    run, preflight, report = make_run(tmp_path)
    report["run_context"][field] = value
    write_json(run / "report.json", report)
    with pytest.raises(ValueError, match="evidence_"):
        seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)


@pytest.mark.parametrize(
    "operation",
    ["foreign_session", "missing_hook", "result_mismatch", "reordered", "changed_adapter"],
)
def test_pass_flag_and_resigned_events_do_not_override_native_evidence(tmp_path, operation):
    run, preflight, report = make_run(tmp_path)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    if operation == "foreign_session":
        events[3]["native_session_id"] = "foreign-session"
    elif operation == "missing_hook":
        events = [event for event in events if event["kind"] != "PostToolUse"]
        for index, event in enumerate(events, 1):
            event["sequence"] = index
            event["event_id"] = f"synthetic-test-run:synthetic-test-attempt:{index:06d}"
        report["capture"]["event_count"] = len(events)
    elif operation == "result_mismatch":
        events[-1]["payload"]["is_error"] = True
    elif operation == "reordered":
        events[3], events[4] = events[4], events[3]
    else:
        report["adapter_sha256"] = "0" * 64
    (run / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n")
    report["events_sha256"] = sha(run / "events.jsonl")
    write_json(run / "report.json", report)
    try:
        path = seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)
    except ValueError as error:
        assert str(error).startswith("evidence_")
    else:
        assert verify_evidence(path)["qualification"] == "needs_evidence"


def test_initial_snapshot_must_match_the_actual_failed_candidate(tmp_path):
    run, preflight, report = make_run(tmp_path)
    report["initial_snapshot"] = report["candidate_snapshot"]
    report["run_context"]["initial_snapshot"] = report["candidate_snapshot"]
    write_json(run / "report.json", report)
    with pytest.raises(ValueError, match="evidence_"):
        seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)


def test_existing_destination_is_never_overwritten(tmp_path):
    path, run, preflight = seal(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="evidence_destination_exists"):
        seal_evidence(run, preflight, path.parent, trusted_collection=True)
    assert path.read_bytes() == before


def test_actual_unapproved_tool_use_cannot_hide_behind_valid_init(tmp_path):
    run, preflight, report = make_run(tmp_path)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    for event in events:
        if "tool_name" in event["payload"]:
            event["payload"]["tool_name"] = "Bash"
    (run / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n")
    report["events_sha256"] = sha(run / "events.jsonl")
    write_json(run / "report.json", report)
    path = seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)
    assert verify_evidence(path)["qualification"] == "needs_evidence"


def test_actual_hook_projection_does_not_invent_tool_input(tmp_path):
    run, preflight, report = make_run(tmp_path)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    for event in events:
        if event["producer"] == "native_hook":
            event["payload"].pop("tool_input", None)
    (run / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n")
    report["events_sha256"] = sha(run / "events.jsonl")
    write_json(run / "report.json", report)
    path = seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)
    assert verify_evidence(path)["qualification"] == "qualified_success"


def test_self_consistent_foreign_control_snapshot_is_rejected(tmp_path):
    run, preflight, report = make_run(tmp_path)
    control = tmp_path / "other-control"
    control.mkdir()
    write_json(control / "context.json", {**report["run_context"], "run_id": "another-run"})
    report["control_snapshot"] = SnapshotStore(run / "snapshots").capture(control).snapshot_id
    write_json(run / "report.json", report)
    with pytest.raises(ValueError, match="evidence_control_context_mismatch"):
        seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)


@pytest.mark.parametrize("field", ["profile_sha256", "cli_sha256"])
def test_missing_runtime_identity_prevents_qualification(tmp_path, field):
    run, preflight, report = make_run(tmp_path)
    del report[field]
    write_json(run / "report.json", report)
    path = seal_evidence(run, preflight, tmp_path / "evidence", trusted_collection=True)
    assert verify_evidence(path)["qualification"] == "needs_evidence"
