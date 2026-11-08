"""Private, host-only evidence registry; no task execution or human decisions."""

from __future__ import annotations

import json

from pathlib import Path

from memos.skill_drafting.domain import hash_json
from memos.skill_governance.collector import _sha, assess_run, collector_digest
from memos.skill_governance.domain import validate_assessment
from memos.skill_management.config import read_config
from memos.skill_management.domain import digest, identity, require, text
from memos.skill_management.repository import ManagementRepository


def read_registry(path: Path) -> dict:
    path = Path(path)
    require(
        not path.is_symlink() and path.is_file() and path.stat().st_mode & 0o077 == 0,
        "private_registry_required",
    )
    require(path.stat().st_size <= 200000)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    registry = json.loads(path.read_bytes(), object_pairs_hook=unique)
    require(
        isinstance(registry, dict)
        and set(registry)
        == {
            "schemaVersion",
            "serviceConfig",
            "allowedRoot",
            "outputDirectory",
            "entries",
        }
    )
    require(type(registry["schemaVersion"]) is int and registry["schemaVersion"] == 1)
    for key in ("serviceConfig", "allowedRoot", "outputDirectory"):
        require(isinstance(registry[key], str) and Path(registry[key]).is_absolute())
    allowed = Path(registry["allowedRoot"]).resolve()
    output = Path(registry["outputDirectory"]).resolve()
    require(output.is_relative_to(allowed) and output != allowed)
    entries = registry["entries"]
    require(isinstance(entries, list) and 0 < len(entries) <= 100)
    seen = set()
    for entry in entries:
        require(
            isinstance(entry, dict)
            and set(entry)
            == {
                "projectId",
                "skillId",
                "revision",
                "validationId",
                "runDirectory",
                "reportSha256",
                "reassessment",
            }
        )
        text(entry["projectId"], maximum=300)
        identity(entry["skillId"], entry["revision"])
        digest(entry["validationId"])
        digest(entry["reportSha256"])
        key = (entry["projectId"], entry["validationId"])
        require(key not in seen)
        seen.add(key)
        require(
            isinstance(entry["runDirectory"], str) and Path(entry["runDirectory"]).is_absolute()
        )
        run = Path(entry["runDirectory"]).resolve()
        require(
            run.is_relative_to(allowed)
            and not output.is_relative_to(run)
            and not run.is_relative_to(output)
        )
        recheck = entry["reassessment"]
        if recheck is not None:
            require(isinstance(recheck, dict) and set(recheck) == {"receipt", "artifacts"})
            for value in recheck.values():
                require(
                    isinstance(value, str)
                    and Path(value).is_absolute()
                    and Path(value).resolve().is_relative_to(allowed)
                )
    return registry


def import_evidence(registry_path: Path, *, progress=None) -> dict:
    """Read and verify all entries first, then idempotently append host observations."""
    from memos.skill_experiments.__main__ import runtime
    from memos.skill_experiments.manifest import load_experiment

    registry = read_registry(registry_path)
    config = read_config(Path(registry["serviceConfig"]))
    settings = {row["projectId"]: row for row in config["projects"]}
    repositories, prepared = {}, []
    producer = collector_digest()
    identity_hash = hash_json({"registry": registry, "collector": producer})
    output = Path(registry["outputDirectory"]) / identity_hash
    try:
        for project in {entry["projectId"] for entry in registry["entries"]}:
            require(project in settings, "project_not_found", 404)
            setting = settings[project]
            for directory in setting.get("experiments", []):
                load_experiment(Path(directory), runtime(config))
            repositories[project] = ManagementRepository(
                project_id=setting["graphProject"], **config["graph"]
            )
        for entry in registry["entries"]:
            repo = repositories[entry["projectId"]]
            detail = repo.get_revision(entry["skillId"], entry["revision"])
            validation = next(
                (
                    row
                    for row in detail["validations"]
                    if row["validationId"] == entry["validationId"]
                ),
                None,
            )
            require(validation is not None, "validation_missing", 404)
            run = Path(entry["runDirectory"])
            require(
                _sha(run / "report.json") == entry["reportSha256"], "original_report_changed", 409
            )
            recheck = entry["reassessment"]
            raw = assess_run(
                detail["candidate"],
                validation,
                run,
                allowed_root=Path(registry["allowedRoot"]),
                reassessment=(Path(recheck["receipt"]), Path(recheck["artifacts"]))
                if recheck
                else None,
            )
            value = validate_assessment(detail["candidate"], validation, raw)
            prepared.append((repo, entry, raw, value))
        require(producer == collector_digest(), "collector_changed", 409)
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        for repo, entry, raw, value in prepared:
            path = output / (value["assessmentId"] + ".json")
            if path.exists():
                require(json.loads(path.read_bytes()) == value, "immutable_conflict", 409)
            else:
                with path.open("x") as stream:
                    json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
                path.chmod(0o600)
            repo.record_assessment(
                entry["skillId"],
                entry["revision"],
                assessment=raw,
                actor="host_evidence_verifier",
                operation_id="assessment-" + value["assessmentId"],
            )
            if progress:
                progress(
                    {
                        "caseId": value["caseId"],
                        "assessmentId": value["assessmentId"],
                        "captureComplete": value["captureComplete"],
                        "loadVerified": value["loadVerified"],
                        "humanJudgment": "pending",
                    }
                )
        summary = {
            "registryId": identity_hash,
            "collectorDigest": producer,
            "assessmentIds": [value["assessmentId"] for _, _, _, value in prepared],
            "humanJudgmentsCreated": 0,
            "modelCalls": 0,
            "releasesCreated": 0,
        }
        path = output / "summary.json"
        if path.exists():
            require(json.loads(path.read_bytes()) == summary, "immutable_conflict", 409)
        else:
            with path.open("x") as stream:
                json.dump(summary, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
            path.chmod(0o600)
        return summary
    finally:
        for repo in repositories.values():
            repo.close()
