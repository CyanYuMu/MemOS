"""Host-owned orchestration; HTTP cannot submit validations or artifact paths."""

from __future__ import annotations

import logging
import threading

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_management.domain import ManagementError, require
from memos.skill_management.runtime import case_options, dev_case, execute_task, export_identity


if TYPE_CHECKING:
    from collections.abc import Callable


_LOG = logging.getLogger(__name__)
_PRIVATE = {"artifactDirectory", "uri", "artifact_directory", "receiptPath", "sourcePath"}


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if key not in _PRIVATE}
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


@dataclass
class Project:
    repository: object
    check_sources: Callable[[dict], dict]


class SkillService:
    def __init__(self, projects, artifacts_root, runtime_config, provider, *, executor=None):
        self.projects = projects
        self.artifacts_root = Path(artifacts_root).resolve()
        self.runtime_config, self.provider = runtime_config, dict(provider)
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="skill-eval"
        )
        self._lock = threading.RLock()
        self._active = {}

    def close(self):
        self.executor.shutdown(wait=True)
        for project in self.projects.values():
            project.repository.close()

    def _project(self, project_id):
        require(project_id in self.projects, "project_not_found", 404)
        return self.projects[project_id]

    def _sources(self, project, detail):
        try:
            packet = project.check_sources(detail)
            expected = sorted(source["source_id"] for source in detail["candidate"]["source_refs"])
            require(sorted(packet["watermark"]) == expected, "source_watermark_changed", 409)
            return packet
        except Exception:
            raise ManagementError("source_unavailable", 503) from None

    def _grant(self, project, skill_id, revision, case_id, mode):
        detail = project.repository.get_revision(skill_id, revision)
        self._sources(project, detail)
        identity = export_identity(detail["candidate"])
        if mode == "normal":
            grant = project.repository.admission(skill_id, revision, case_id)
            if grant.get("allowed") and any(
                grant.get(k) != identity[k] for k in ("contentHash", "exportHash")
            ):
                return {"allowed": False, "reason": "artifact_mismatch"}
            return grant
        return {
            "allowed": True,
            "sequence": detail["management"]["sequence"],
            "releaseId": None,
            "contentHash": identity["contentHash"],
            "exportHash": identity["exportHash"],
        }

    def call(self, action, body, actor):
        project = self._project(body["projectId"])
        repo = project.repository
        if action == "list":
            return public(repo.list_candidates(body.get("query", "")))
        if action == "job":
            return public(repo.get_job(body["jobId"]))
        skill_id, revision = body["skillId"], body["revision"]
        detail = repo.get_revision(skill_id, revision)
        if action == "detail":
            return {**public(detail), "projectId": body["projectId"], "caseOptions": case_options()}
        if action == "evidence":
            loaded = load_candidate(Path(detail["artifactDirectory"]))
            require(loaded["candidate"] == detail["candidate"], "artifact_mismatch", 409)
            rule = next(
                (r for r in loaded["candidate"]["rules"] if r["rule_id"] == body["ruleId"]), None
            )
            require(rule is not None, "not_found", 404)
            ids = {link["fact_id"] for link in rule["evidence_links"]}
            return public(
                {
                    "rule": rule,
                    "facts": [f for f in loaded["packet"]["facts"] if f["fact_id"] in ids],
                }
            )
        if action in {"load_preview", "evaluate"}:
            try:
                dev_case(body["caseId"])
            except ValueError:
                raise ManagementError("invalid_input") from None
        if action == "load_preview":
            grant = self._grant(project, skill_id, revision, body["caseId"], "normal")
            return public(
                {
                    **{k: v for k, v in grant.items() if k != "reason"},
                    "reasons": [] if grant.get("allowed") else [grant.get("reason", "not_ready")],
                }
            )
        if action == "evaluate":
            return self.start(
                body["projectId"], skill_id, revision, body["caseId"], body["operationId"], actor
            )
        if action == "review_prepare":
            source = self._sources(project, detail)
            return public(
                repo.prepare_review(
                    skill_id, revision, source_watermark=source["watermark"], actor=actor
                )
            )
        kwargs = {
            "expected_sequence": body["expectedSequence"],
            "operation_id": body["operationId"],
            "actor": actor,
            "reason": body["reason"],
        }
        if action == "publish":
            source = self._sources(project, detail)
            return public(
                repo.publish(
                    skill_id,
                    revision,
                    package_id=body["packageId"],
                    package_hash=body["packageHash"],
                    source_watermark=source["watermark"],
                    **kwargs,
                )
            )
        if action == "review_decide":
            return public(
                repo.decide_review(skill_id, revision, decision=body["decision"], **kwargs)
            )
        if action == "status":
            return public(repo.set_status(skill_id, revision, action=body["action"], **kwargs))
        if action == "challenge":
            return public(repo.challenge(skill_id, revision, **kwargs))
        raise ManagementError("not_found", 404)

    def start(
        self,
        project_id,
        skill_id,
        revision,
        case_id,
        operation_id,
        actor,
        *,
        mode="candidate_evaluation",
    ):
        require(mode in {"normal", "candidate_evaluation"})
        project = self._project(project_id)
        try:
            dev_case(case_id)
        except ValueError:
            raise ManagementError("invalid_input") from None
        grant = self._grant(project, skill_id, revision, case_id, mode)
        require(grant.get("allowed") is True, "not_ready", 422)
        # One bounded execution slot. The graph remains authoritative for durable
        # idempotency; this map only prevents unbounded local process queues.
        key = (project_id, operation_id)
        payload = (skill_id, revision, case_id, mode, actor)
        with self._lock:
            prior = project.repository.find_job(
                skill_id,
                revision,
                case_id=case_id,
                actor=actor,
                operation_id=operation_id,
                export_hash=grant["exportHash"],
                mode=mode,
            )
            if prior is not None:
                return public(prior)
            if key in self._active:
                previous, job = self._active[key]
                require(previous == payload, "operation_conflict", 409)
                return public(job)
            require(not self._active, "busy", 409)
            job = project.repository.create_job(
                skill_id,
                revision,
                case_id=case_id,
                actor=actor,
                operation_id=operation_id,
                export_hash=grant["exportHash"],
                mode=mode,
            )
            created = job.pop("created", False)
            if not created:
                return public(job)
            self._active[key] = (payload, job)
            try:
                self.executor.submit(
                    self._run, key, project, skill_id, revision, case_id, actor, mode, job["jobId"]
                )
            except Exception:
                self._active.pop(key, None)
                project.repository.claim_job(job["jobId"])
                project.repository.finish_job(
                    job["jobId"], status="unknown", error_code="execution_unverified"
                )
                raise ManagementError("service_unavailable", 503) from None
            return public(job)

    def _run(self, key, project, skill_id, revision, case_id, actor, mode, job_id):
        repo = project.repository
        try:
            if repo.claim_job(job_id) is None:
                return
            detail = repo.get_revision(skill_id, revision)
            _LOG.info("skill evaluation started job=%s", job_id)
            result = execute_task(
                Path(detail["artifactDirectory"]),
                case_id,
                self.artifacts_root / "runs" / job_id,
                self.runtime_config,
                self.provider,
                mode=mode,
                authorize=lambda: self._grant(project, skill_id, revision, case_id, mode),
            )
            validation = result.get("validation")
            validation_id = None
            if validation is not None:
                record = repo.record_validation(
                    skill_id,
                    revision,
                    report=validation,
                    actor=actor,
                    operation_id="validation-" + job_id,
                )
                validation_id = record["validationId"]
            repo.finish_job(
                job_id,
                status=result["status"],
                validation_id=validation_id,
                task_result=validation["taskResult"] if validation else None,
                skill_usage=validation["skillUsage"] if validation else None,
                error_code=result.get("errorCode"),
            )
            _LOG.info("skill evaluation finished job=%s status=%s", job_id, result["status"])
        except Exception:
            _LOG.warning("skill evaluation unverified job=%s", job_id)
            try:
                repo.finish_job(job_id, status="unknown", error_code="execution_unverified")
            except Exception:
                _LOG.error("skill job recovery required job=%s", job_id)
        finally:
            with self._lock:
                self._active.pop(key, None)
