"""Admission is separate from model completion and task acceptance."""

import copy
import hashlib
import json

from types import SimpleNamespace

import pytest

from memos.skill_management import runtime


@pytest.fixture
def candidate():
    return {
        "skill_id": "contract-repair",
        "revision": "a" * 64,
        "content_hash": "b" * 64,
        "title": "Repair contracts",
        "goal": "Preserve correct rejection",
        "applicability": ["Explicit independent contract"],
        "exclusions": ["Unknown contract"],
        "prerequisites": ["Visible checks"],
        "constraints": ["Never weaken rejection"],
        "acceptance": ["All required checks"],
        "limitations": ["Not a benefit claim"],
        "entry_rule": "R1",
        "requires": [],
        "rules": [
            {
                "rule_id": "R1",
                "basis": "normative",
                "condition": "Contract available",
                "observation": "Caller mismatch",
                "action": "Repair caller",
                "expected_result": "Valid write",
                "branches": [{"when": "Complete", "target": "exit:success"}],
                "evidence_links": [
                    {"uri": "private-evidence-file", "value": "oracle-private-data"}
                ],
            }
        ],
        "exits": {
            "success": "Checks pass",
            "unresolved": "Failure remains",
            "needs_evidence": "No proof",
            "not_applicable": "Outside scope",
        },
    }


def test_export_preserves_procedure_and_excludes_review_evidence(candidate):
    spec = runtime.native_skill(candidate)
    assert spec.revision == candidate["revision"] and spec.rule_ids == ("R1",)
    for required in (
        "Preserve correct rejection",
        "Explicit independent contract",
        "Repair caller",
        "exit:success",
        "All required checks",
        "Not a benefit claim",
    ):
        assert required in spec.body
    assert "oracle-private-data" not in spec.body and "private-evidence-file" not in spec.body
    altered = copy.deepcopy(candidate)
    altered["rules"][0]["action"] = "Different action"
    assert (
        runtime.export_identity(altered)["exportHash"]
        != runtime.export_identity(candidate)["exportHash"]
    )


def test_adapter_limits_are_not_silently_truncated(candidate):
    candidate["rules"][0]["rule_id"] = "x" * 65
    with pytest.raises(ValueError):
        runtime.native_skill(candidate)


@pytest.mark.parametrize("case_id", ["source-dream-update", "holdout-caller-only", "../../../file"])
def test_only_registered_dev_tasks_can_be_launched(candidate, case_id):
    with pytest.raises(ValueError):
        runtime.dev_case(case_id)


def test_normal_denial_precedes_any_agent_process(tmp_path, candidate, monkeypatch):
    monkeypatch.setattr(runtime, "load_candidate", lambda _: {"candidate": candidate})
    monkeypatch.setattr(runtime, "run_case", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="managed_admission_denied"):
        runtime.execute_task(
            tmp_path / "candidate",
            "dev-strict-signature",
            tmp_path / "job",
            None,
            {},
            mode="normal",
            authorize=lambda: {"allowed": False},
        )
    assert not (tmp_path / "job").exists()


def test_projection_explains_the_recorder_reference_contract(candidate):
    spec = runtime.native_skill(candidate)
    assert "mcp__memos_run__record_rule" in spec.body
    assert "evidence_refs=[]" in spec.body
    assert "check_index" in spec.body
    assert "not verified rule use" in spec.body
    assert runtime.PROJECTION_VERSION == "management-native-2"


@pytest.fixture
def run_fixture(tmp_path, candidate, monkeypatch):
    monkeypatch.setattr(runtime, "load_candidate", lambda _: {"candidate": candidate})
    monkeypatch.setattr(
        runtime,
        "check_receipt",
        lambda *a: SimpleNamespace(matches=True, verification_status="passed"),
    )
    case = runtime.dev_case("dev-strict-signature")

    def fake_run(case_id, output, config, **kwargs):
        output.mkdir(parents=True)
        (output / "candidate").mkdir()
        (output / "validation").mkdir()
        receipt = output / "validation/receipt.json"
        receipt.write_text(
            json.dumps({"run_id": "run", "attempt_id": "attempt", "case": {"case_id": case_id}})
        )
        (output / "events.jsonl").write_text("")
        report = {
            "status": "passed",
            "run_context": {
                "case_id": case_id,
                "case_revision": case.revision,
                "split": "dev",
                "lineage_id": case.lineage_id,
                "run_id": "run",
                "attempt_id": "attempt",
                "skill_id": candidate["skill_id"],
                "skill_revision": candidate["revision"],
            },
            "case_asset_digest": runtime.case_digest(case),
            "events_sha256": hashlib.sha256(b"").hexdigest(),
            "task_status": "passed",
            "task_accepted": True,
            "receipt_matches": True,
            "receipt": str(receipt),
            "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "skill_export": runtime.export_identity(candidate)["manifest"],
            "loading": {"status": "verified"},
            "capture": {"status": "complete"},
        }
        path = output / "report.json"
        path.write_text(json.dumps(report))
        return path

    monkeypatch.setattr(runtime, "run_case", fake_run)
    return tmp_path / "source", tmp_path / "job"


def grant(candidate):
    return {
        "allowed": True,
        "sequence": 3,
        "releaseId": None,
        "contentHash": candidate["content_hash"],
        "exportHash": runtime.export_identity(candidate)["exportHash"],
    }


@pytest.mark.parametrize("mode", ["normal", "candidate_evaluation"])
def test_incomplete_grant_cannot_launch(run_fixture, monkeypatch, mode):
    source, output = run_fixture
    monkeypatch.setattr(runtime, "run_case", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="managed_admission_mismatch"):
        runtime.execute_task(
            source,
            "dev-strict-signature",
            output,
            None,
            {},
            mode=mode,
            authorize=lambda: {"allowed": True},
        )


def test_dev_success_is_not_independent_branch_validation(run_fixture, candidate):
    source, output = run_fixture
    result = runtime.execute_task(
        source,
        "dev-strict-signature",
        output,
        None,
        {},
        mode="candidate_evaluation",
        authorize=lambda: grant(candidate),
    )
    assert result["status"] == "passed"
    assert result["validation"]["kind"] == "isolated_dev"
    assert result["validation"]["branchCoverage"] == result["validation"]["boundaryCoverage"] == []
    assert result["validation"]["skillUsage"]["usageReview"] == "unreviewed"


def test_changed_permission_during_run_invalidates_result_admission(run_fixture, candidate):
    source, output = run_fixture
    observations = iter([grant(candidate), {"allowed": False, "sequence": 4}])
    result = runtime.execute_task(
        source,
        "dev-strict-signature",
        output,
        None,
        {},
        mode="candidate_evaluation",
        authorize=lambda: next(observations),
    )
    assert result["status"] == "unknown" and result["errorCode"] == "admission_changed"
    assert result["validation"]["taskResult"]["accepted"] is True
    assert result["validation"]["skillUsage"]["endedAdmission"] == "needs_review"


def test_receipt_mismatch_never_becomes_success(run_fixture, monkeypatch, candidate):
    monkeypatch.setattr(
        runtime,
        "check_receipt",
        lambda *a: SimpleNamespace(matches=False, verification_status="passed"),
    )
    source, output = run_fixture
    result = runtime.execute_task(
        source,
        "dev-strict-signature",
        output,
        None,
        {},
        mode="candidate_evaluation",
        authorize=lambda: grant(candidate),
    )
    assert result["status"] == "unknown"
    assert result["validation"]["receiptVerified"] is False
