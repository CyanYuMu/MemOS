"""Expected stops retain raw results and require current schema 6 adjudication."""

import importlib.util
import sys

from pathlib import Path

from memos.skill_drafting.domain import hash_json
from memos.skill_drafting.graph import edge_key
from memos.skill_management.domain import readiness


_FIXTURE_ROOT = Path(__file__).parents[1] / "skill_release"
sys.path.insert(0, str(_FIXTURE_ROOT))
_spec = importlib.util.spec_from_file_location(
    "lifecycle_release_collector_tests", _FIXTURE_ROOT / "test_collector.py"
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def _candidate(repo):
    value = {
        "skill_id": "synthetic-release-repair",
        "revision": "a" * 64,
        "project_id": repo.project_id,
        "title": "Synthetic six-outcome management integration",
        "purpose": "repair",
        "tags": ["management_test"],
        "requires": [],
        "status": "candidate",
        "source_refs": [
            {"source_id": "d" * 64, "case_id": "synthetic-source", "lineage_id": "synthetic-source"}
        ],
        "rules": [
            {
                "rule_id": name,
                "branches": [
                    {"when": "Synthetic " + target, "target": target} for target in targets
                ],
            }
            for name, targets in {
                "check_update_contract": [
                    "exit:not_applicable",
                    "exit:needs_evidence",
                    "repair_and_verify_update",
                ],
                "repair_and_verify_update": [
                    "exit:success",
                    "exit:needs_evidence",
                    "exit:unresolved",
                ],
            }.items()
        ],
    }
    value["content_hash"] = hash_json(value)
    manifest = {
        "schema_version": 1,
        "skill_id": value["skill_id"],
        "revision": value["revision"],
        "project_id": repo.project_id,
        "candidate_content_hash": value["content_hash"],
        "manifest_id": "c" * 64,
    }
    skill = {"kind": "Skill", "key": value["skill_id"], "payload": {"skill_id": value["skill_id"]}}
    revision = {
        "kind": "SkillRevision",
        "key": value["skill_id"] + "@" + value["revision"],
        "payload": {
            "skill_id": value["skill_id"],
            "revision": value["revision"],
            "status": "candidate",
            "requires": [],
            "candidate": value,
        },
    }
    left, right = ({k: item[k] for k in ("kind", "key")} for item in (skill, revision))
    repo.store_bundle(
        [skill, revision],
        [
            {
                "kind": "HAS_REVISION",
                "key": edge_key("HAS_REVISION", left, right, {}),
                "source": left,
                "target": right,
                "payload": {},
            }
        ],
    )
    repo.register_candidate(value, manifest, "/synthetic/six-outcome")
    return value


def test_six_collector_outcomes_adjudicate_and_publish_without_rewriting_raw_status(
    repo, tmp_path, monkeypatch
):
    from release_run_fixture import make_pair

    from memos.skill_release import collector
    from memos.skill_release.tasks import SUITES

    candidate = _candidate(repo)
    original = {}
    for suite in SUITES:
        root = tmp_path / suite
        root.mkdir()
        fixture = make_pair(
            root, monkeypatch, trial_options={"suite_id": suite}, candidate_override=candidate
        )
        monkeypatch.setattr(
            collector,
            "require_qualification",
            lambda *a, proofs=fixture["proofs"], **k: proofs[k["suite_id"]],
        )
        collected = _fixture.collect(fixture)
        validation = repo.record_validation(
            candidate["skill_id"],
            candidate["revision"],
            report=collected["validation"],
            actor="synthetic-native-collector",
            operation_id="validation-" + suite,
        )
        assessment = repo.record_assessment(
            candidate["skill_id"],
            candidate["revision"],
            assessment=collected["assessment"],
            actor="synthetic-native-collector",
            operation_id="assessment-" + suite,
        )
        original[validation["validationId"]] = collected["validation"]["status"]
        package = repo.prepare_adjudication(
            candidate["skill_id"],
            candidate["revision"],
            assessment_id=assessment["assessmentId"],
            source_watermark=["d" * 64],
            actor="synthetic-reviewer",
        )
        decision = _fixture.decision_for(fixture, collected["assessment"])
        result = repo.adjudicate(
            candidate["skill_id"],
            candidate["revision"],
            package_id=package["packageId"],
            package_hash=package["packageHash"],
            expected_sequence=package["sequence"],
            operation_id="adjudicate-" + suite,
            actor="synthetic-reviewer",
            reason="Synthetic protocol review; no real Agent efficacy claimed",
            source_watermark=["d" * 64],
            decision=decision,
        )
        assert result["contribution"]["resolvedHistory"] is False
    detail = repo.get_revision(candidate["skill_id"], candidate["revision"])
    assert {v["validationId"]: v["status"] for v in detail["validations"]} == original
    assert any(status != "passed" for status in original.values())
    qualification = detail["governance"]["qualification"]
    assert len(qualification["verifiedExpectedStopValidationIds"]) == 4
    assert qualification["resolvedValidationIds"] == []
    package = repo.prepare_review(
        candidate["skill_id"],
        candidate["revision"],
        source_watermark=["d" * 64],
        actor="synthetic-reviewer",
    )
    assert package["readiness"] == {"status": "PASS", "reasons": []}
    release = repo.publish(
        candidate["skill_id"],
        candidate["revision"],
        package_id=package["packageId"],
        package_hash=package["packageHash"],
        expected_sequence=package["sequence"],
        operation_id="publish-six-outcomes",
        actor="synthetic-reviewer",
        reason="Synthetic transaction fixture only; not real model publication",
        source_watermark=["d" * 64],
    )
    assert release["state"] == "published"
    assert {
        v["validationId"]: v["status"]
        for v in repo.get_revision(candidate["skill_id"], candidate["revision"])["validations"]
    } == original


def test_only_current_verified_expected_stops_bypass_raw_failure_gate():
    from test_repository import _fixtures

    candidate = _fixtures.candidate()
    report = _fixtures.report(candidate, status="failed")
    report["validationId"] = "a" * 64
    state = _fixtures.state(candidate)
    state["qualification"] = {
        "contributions": {
            report["validationId"]: {
                "branchCoverage": report["branchCoverage"],
                "boundaryCoverage": report["boundaryCoverage"],
            }
        },
        "verifiedExpectedStopValidationIds": [report["validationId"]],
    }
    assert readiness(state, [report], [])["status"] == "PASS"
    state["qualification"]["verifiedExpectedStopValidationIds"] = []
    assert "validation_failure_unresolved" in readiness(state, [report], [])["reasons"]
