"""Schema five separates host-plan qualification from approved single-run findings."""

import copy
import json
import os
import uuid

from pathlib import Path

import pytest

from c2_governance_v2_fixture import c2_v2_facts, refresh_plan

from memos.skill_drafting.domain import hash_json
from memos.skill_drafting.graph import edge_key, node_key
from memos.skill_management.domain import ManagementError
from memos.skill_management.repository import ManagementRepository


@pytest.mark.parametrize("plan_qualified", [True, False])
def test_c2_v2_graph_plan_gate_and_approved_single_run_findings(plan_qualified):
    path = os.environ.get("MEMOS_SKILL_GRAPH_TEST_CONFIG")
    if not path:
        pytest.skip("dedicated Skill Graph configuration was not supplied")
    config = json.loads(Path(path).read_text())
    repo = ManagementRepository(
        **{k: config[k] for k in ("uri", "user", "password", "database", "memory_database_id")},
        project_id="c2-test-" + uuid.uuid4().hex,
    )
    try:
        candidate, validation, assessment, decision = c2_v2_facts()
        if not plan_qualified:
            assessment["executionQualification"]["calibration"].update(
                cyclesUsed=3, budgetExceeded=True
            )
            refresh_plan(assessment)
        candidate["project_id"] = repo.project_id
        candidate["content_hash"] = hash_json(
            {k: v for k, v in candidate.items() if k != "content_hash"}
        )
        manifest = {
            "schema_version": 1,
            "skill_id": candidate["skill_id"],
            "revision": candidate["revision"],
            "project_id": repo.project_id,
            "candidate_content_hash": candidate["content_hash"],
            "manifest_id": "c" * 64,
        }
        skill = {
            "kind": "Skill",
            "key": candidate["skill_id"],
            "payload": {"skill_id": candidate["skill_id"]},
        }
        payload = {
            "skill_id": candidate["skill_id"],
            "revision": candidate["revision"],
            "status": "candidate",
            "requires": [],
            "candidate": candidate,
        }
        revision = {
            "kind": "SkillRevision",
            "key": node_key("SkillRevision", payload),
            "payload": payload,
        }
        left, right = ({k: item[k] for k in ("kind", "key")} for item in (skill, revision))
        edge = {
            "kind": "HAS_REVISION",
            "key": edge_key("HAS_REVISION", left, right, {}),
            "source": left,
            "target": right,
            "payload": {},
        }
        repo.store_bundle([skill, revision], [edge])
        repo.register_candidate(candidate, manifest, "/synthetic/c2", challenges=[])
        report = {
            k: v for k, v in validation.items() if k not in {"skillId", "revision", "validationId"}
        }
        report.update(contentHash=candidate["content_hash"], purpose="management_test")
        args = (candidate["skill_id"], candidate["revision"])
        validation = repo.record_validation(
            *args, report=report, actor="synthetic-c2", operation_id="c2-validate"
        )
        validation.pop("sequence")
        for key in (
            "validationId",
            "contentHash",
            "exportHash",
            "runId",
            "attemptId",
            "caseId",
            "caseRevision",
            "lineageId",
            "split",
            "purpose",
        ):
            assessment[key] = validation[key]
        registered = repo.record_assessment(
            *args, assessment=assessment, actor="synthetic-c2", operation_id="c2-assess"
        )
        assert registered == repo.record_assessment(
            *args, assessment=assessment, actor="synthetic-c2", operation_id="c2-assess"
        )
        before = copy.deepcopy(repo.get_revision(*args)["validations"])
        package = repo.prepare_adjudication(
            *args,
            assessment_id=registered["assessmentId"],
            source_watermark=["b" * 64],
            actor="synthetic-human",
        )
        kwargs = {
            "package_id": package["packageId"],
            "package_hash": package["packageHash"],
            "expected_sequence": package["sequence"],
            "operation_id": "c2-adjudicate",
            "actor": "synthetic-human",
            "reason": "Synthetic transaction only",
            "source_watermark": ["b" * 64],
            "decision": decision,
        }
        invalid = copy.deepcopy(kwargs)
        invalid["decision"]["branches"][2]["actionRefs"] = ["verify_scoped_reads_decl", "exit"]
        invalid["operation_id"] = "c2-invalid-transition"
        with pytest.raises(
            ManagementError,
            match="extension_branch_process_insufficient"
            if plan_qualified
            else "execution_plan_unqualified",
        ):
            repo.adjudicate(*args, **invalid)
        unchanged = repo.get_revision(*args)
        assert unchanged["governance"]["adjudications"] == []
        assert unchanged["management"]["sequence"] == package["sequence"]
        if not plan_qualified:
            kwargs["decision"]["branches"] = []
        result = repo.adjudicate(*args, **kwargs)
        assert result["contribution"]["repairConfirmed"]
        assert result["contribution"]["decision"]["usageDecision"] == "correct"
        assert result["contribution"]["qualifiedIndependent"] is plan_qualified
        assert result == repo.adjudicate(*args, **kwargs)
        detail = repo.get_revision(*args)
        assert detail["validations"] == before
        assert len(detail["governance"]["adjudications"]) == 1
        assert sum(row["status"] == "covered" for row in detail["governance"]["coverage"]) == (
            4 if plan_qualified else 0
        )
        assert (
            "branch_coverage_missing" if plan_qualified else "independent_validation_missing"
        ) in detail["management"]["readiness"]["reasons"]
        if not plan_qualified:
            assert detail["governance"]["qualification"]["contributions"] == {}
        release = repo.prepare_review(*args, source_watermark=["b" * 64], actor="synthetic-human")
        with pytest.raises(ManagementError, match="not_ready"):
            repo.publish(
                *args,
                package_id=release["packageId"],
                package_hash=release["packageHash"],
                expected_sequence=release["sequence"],
                operation_id="c2-publish-denied",
                actor="synthetic-human",
                reason="Synthetic test",
                source_watermark=["b" * 64],
            )
        assert repo.get_revision(*args)["releases"] == []
    finally:
        repo.close()
