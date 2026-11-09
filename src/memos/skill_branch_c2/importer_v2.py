"""Append verified C2-trial facts; no model call, judgement or publication."""

from __future__ import annotations

from pathlib import Path

from memos.skill_branch_c2.collector_v2 import collect_trial, collector_digest
from memos.skill_branch_c2.governance_v2 import validate_assessment
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION
from memos.skill_drafting.domain import hash_json
from memos.skill_governance.collector import _inside, _read
from memos.skill_management.config import read_config
from memos.skill_management.domain import require, validate_report
from memos.skill_management.repository import ManagementRepository
from memos.skill_verification.verifier import _write


def validated_pair(candidate, pair):
    validation = validate_report(candidate, pair["validation"])
    assessment = validate_assessment(candidate, validation, pair["assessment"])
    return {"validationId": validation["validationId"], "assessmentId": assessment["assessmentId"]}


def import_batch(
    config_path: Path,
    project_id: str,
    skill_id: str,
    batch_directory: Path,
    qualification_directory: Path,
    allowed_root: Path,
    output_directory: Path,
    *,
    progress=None,
) -> dict:
    config = read_config(config_path)
    settings = next((p for p in config["projects"] if p["projectId"] == project_id), None)
    require(settings is not None, "project_not_found", 404)
    allowed = Path(allowed_root).resolve()
    output = _inside(output_directory, allowed)
    for input_path in (Path(batch_directory).resolve(), Path(qualification_directory).resolve()):
        require(
            not output.is_relative_to(input_path) and not input_path.is_relative_to(output),
            "extension_import_output_overlaps_input",
        )
    repo = ManagementRepository(project_id=settings["graphProject"], **config["graph"])
    producer = collector_digest()
    try:
        detail = repo.get_revision(skill_id, CANDIDATE_REVISION)
        candidate, export_hash = detail["candidate"], detail["management"]["exportHash"]
        prepared = []
        # Append only after the complete fixed Skill trial and its process are re-read.
        for unit in ("c2-skill",):
            pair = collect_trial(
                candidate,
                export_hash,
                Path(batch_directory) / "runs" / unit,
                batch_directory=batch_directory,
                qualification_directory=qualification_directory,
                allowed_root=allowed,
                secret_values=tuple(config["runtime"]["providerEnvironment"].values()),
            )
            identities = validated_pair(candidate, pair)
            prepared.append((unit, pair, identities))
        require(producer == collector_digest(), "collector_changed", 409)
        identity = hash_json(
            {
                "projectId": project_id,
                "skillId": skill_id,
                "collectorDigest": producer,
                "facts": [ids for _, _, ids in prepared],
            }
        )
        target = output / identity
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        results = []
        for unit, pair, identities in prepared:
            path = target / (identities["assessmentId"] + ".json")
            if path.exists():
                require(_read(path) == pair, "immutable_conflict", 409)
            else:
                _write(path, pair)
            repo.record_validation(
                skill_id,
                CANDIDATE_REVISION,
                report=pair["validation"],
                actor="host_c2_v2_evidence_verifier",
                operation_id="c2-v2-validation-" + identities["validationId"],
            )
            repo.record_assessment(
                skill_id,
                CANDIDATE_REVISION,
                assessment=pair["assessment"],
                actor="host_c2_v2_evidence_verifier",
                operation_id="c2-v2-assessment-" + identities["assessmentId"],
            )
            row = {
                "unit": unit,
                **identities,
                "outcome": pair["assessment"]["outcome"],
                "humanJudgment": "pending",
                "branchCoverage": [],
            }
            results.append(row)
            if progress:
                progress(row)
        return {"importId": identity, "results": results, "humanApprovals": 0, "published": False}
    finally:
        repo.close()
