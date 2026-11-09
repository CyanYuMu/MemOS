"""Host-owned orchestration; HTTP cannot submit validations or artifact paths."""

from __future__ import annotations

import logging
import re
import threading
import uuid

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


def evidence_projection(fact):
    """Keep the original hash while explicitly marking a redacted display copy."""

    def redact(value):
        if isinstance(value, str):
            return re.sub(
                r"file://[^\s<>\"']+|/(?:Users|home|private|tmp|var/folders)/[^\s<>\"']+",
                "<host-artifact-redacted>",
                value,
            )
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value

    display = redact(fact["value"])
    return {**public(fact), "value": display, "display_redacted": display != fact["value"]}


@dataclass
class Project:
    repository: object
    check_sources: Callable[[dict], dict]
    observe: Callable[[list], list] | None = None


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
        self.experiments = None

    def configure_experiments(self, registrations):
        from memos.skill_experiments.controller import ExperimentController

        self.experiments = ExperimentController(
            registrations,
            self.runtime_config,
            self.provider,
            executor=self.executor,
            authorize=self._experiment_authorize,
            on_validation=self._experiment_validation,
        )

    def _experiment_authorize(self, project_id, manifest):
        from memos.skill_drafting.domain import hash_json

        project = self._project(project_id)
        detail = project.repository.get_revision(manifest["skillId"], manifest["revision"])
        packet = self._sources(project, detail)
        require(
            sorted(packet["watermark"]) == manifest["sourceWatermark"],
            "source_watermark_changed",
            409,
        )
        require(
            detail["candidate"]["content_hash"] == manifest["contentHash"]
            and export_identity(detail["candidate"]) == manifest["projection"],
            "artifact_mismatch",
            409,
        )
        require(
            detail["management"]["state"] not in {"blocked", "paused", "withdrawn"},
            "not_ready",
            422,
        )
        return hash_json(
            {
                k: detail["management"].get(k)
                for k in ("state", "sequence", "exportHash", "challenges")
            }
        )

    def _experiment_validation(self, project_id, manifest, row, output):
        from memos.skill_experiments.manifest import read_json, sha

        report = read_json(output / "report.json")
        context = report["run_context"]
        status = row["status"] if row["status"] in {"passed", "failed"} else "unknown"
        accepted = row["taskAccepted"] is True and row["receiptVerified"] is True
        validation = {
            "kind": "independent_task",
            "status": status,
            "contentHash": manifest["contentHash"],
            "exportHash": manifest["projection"]["exportHash"],
            "caseId": row["caseId"],
            "caseRevision": context["case_revision"],
            "lineageId": context["lineage_id"],
            "split": "holdout",
            "runId": context["run_id"],
            "attemptId": context["attempt_id"],
            "taskResult": {
                "status": "passed"
                if accepted
                else "failed"
                if report.get("task_status") == "failed"
                else "unknown",
                "accepted": accepted,
                "receiptMatches": row["receiptVerified"],
            },
            "skillUsage": {
                "loading": "verified" if row["loading"] == "verified" else "unknown",
                "capture": row["capture"]
                if row["capture"] in {"complete", "partial"}
                else "unknown",
                "usageReview": "unreviewed",
                "declaredRuleIds": row.get("declaredRuleIds", []),
                "endedAdmission": "allowed",
            },
            "receiptVerified": row["receiptVerified"],
            "loadVerified": row["loading"] == "verified",
            "branchCoverage": [],
            "boundaryCoverage": [],
            "purpose": "actual",
            "scope": {
                "caseIds": [c["caseId"] for c in manifest["cases"]],
                "agent": "claude_code",
                "mode": "controlled_task",
            },
        }
        if row["receiptVerified"]:
            receipt = Path(report["receipt"])
            validation["receipt"] = {"uri": receipt.as_uri(), "sha256": sha(receipt)}
        self._project(project_id).repository.record_validation(
            manifest["skillId"],
            manifest["revision"],
            report=validation,
            actor="host_independent_experiment",
            operation_id="experiment-" + manifest["experimentId"][:32] + "-" + row["caseId"],
        )

    def close(self):
        if self.experiments is not None:
            self.experiments.close()
        self.executor.shutdown(wait=True)
        for project in self.projects.values():
            project.repository.close()

    def _project(self, project_id):
        require(project_id in self.projects, "project_not_found", 404)
        return self.projects[project_id]

    def _scan(
        self,
        project,
        skill_id,
        revision,
        *,
        actor="host_lifecycle_scan",
        reason="Fresh source and tool observations before admission",
        operation_id=None,
        expected_sequence=None,
    ):
        if project.observe is None:
            return None
        return project.repository.scan_lifecycle(
            skill_id,
            revision,
            observer=project.observe,
            actor=actor,
            reason=reason,
            operation_id=operation_id or "host-scan-" + uuid.uuid4().hex,
            expected_sequence=expected_sequence,
        )

    @staticmethod
    def _case(case_id):
        if case_id.startswith("p5-"):
            from memos.skill_p5.service_runtime import dev_case as p5_case

            return p5_case(case_id)
        if case_id.startswith("release-"):
            from memos.skill_release.service_runtime import dev_case as release_case

            return release_case(case_id)
        return dev_case(case_id)

    @staticmethod
    def _case_options():
        from memos.skill_p5.service_runtime import case_options as p5_options
        from memos.skill_release.service_runtime import case_options as release_options

        return case_options() + release_options() + p5_options()

    def _sources(self, project, detail):
        if any(
            subject["kind"] == "memory" and subject["status"] not in {"healthy", "restored"}
            for subject in detail.get("lifecycle", {}).get("subjects", [])
        ):
            raise ManagementError("source_unavailable", 503)
        try:
            packet = project.check_sources(detail)
            expected = sorted(source["source_id"] for source in detail["candidate"]["source_refs"])
            require(sorted(packet["watermark"]) == expected, "source_watermark_changed", 409)
            return packet
        except Exception:
            raise ManagementError("source_unavailable", 503) from None

    def _grant(self, project, skill_id, revision, case_id, mode):
        self._scan(project, skill_id, revision)
        detail = project.repository.get_revision(skill_id, revision)
        if mode == "normal":
            grant = project.repository.admission(skill_id, revision, case_id)
            if not grant.get("allowed"):
                return grant
            self._sources(project, detail)
            identity = export_identity(detail["candidate"])
            if grant.get("allowed") and any(
                grant.get(k) != identity[k] for k in ("contentHash", "exportHash")
            ):
                return {"allowed": False, "reason": "artifact_mismatch"}
            return grant
        self._sources(project, detail)
        identity = export_identity(detail["candidate"])
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
        if action in {"experiments_list", "experiment_detail", "experiment_start"}:
            if self.experiments is None and action == "experiments_list":
                return {"items": [], "total": 0}
            require(self.experiments is not None, "experiment_not_found", 404)
            if action == "experiments_list":
                return self.experiments.list(body["projectId"])
            if action == "experiment_detail":
                return self.experiments.detail(body["projectId"], body["experimentId"])
            with self._lock:
                require(not self._active, "busy", 409)
                return self.experiments.start(
                    body["projectId"], body["experimentId"], body["operationId"], actor
                )
        if action == "list":
            return public(repo.list_candidates(body.get("query", "")))
        if action == "job":
            return public(repo.get_job(body["jobId"]))
        skill_id, revision = body["skillId"], body["revision"]
        detail = repo.get_revision(skill_id, revision)
        if action == "detail":
            return {
                **public(detail),
                "projectId": body["projectId"],
                "caseOptions": self._case_options(),
            }
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
                    "facts": [
                        evidence_projection(f)
                        for f in loaded["packet"]["facts"]
                        if f["fact_id"] in ids
                    ],
                }
            )
        if action in {"load_preview", "evaluate"}:
            try:
                self._case(body["caseId"])
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
        if action == "impact_scan":
            require(project.observe is not None, "lifecycle_not_enabled", 409)
            return public(
                self._scan(
                    project,
                    skill_id,
                    revision,
                    actor=actor,
                    reason=body["reason"],
                    operation_id=body["operationId"],
                    expected_sequence=body["expectedSequence"],
                )
            )
        if action in {
            "impact_prepare",
            "impact_review",
            "challenge_resolve",
            "review_prepare",
            "adjudication_prepare",
            "publish",
            "adjudicate",
        }:
            self._scan(project, skill_id, revision)
            detail = repo.get_revision(skill_id, revision)
        if action == "impact_prepare":
            return {
                **public(repo.prepare_impact(skill_id, revision, actor=actor)),
                "projectId": body["projectId"],
            }
        if action == "review_prepare":
            source = self._sources(project, detail)
            return public(
                repo.prepare_review(
                    skill_id, revision, source_watermark=source["watermark"], actor=actor
                )
            )
        if action == "adjudication_prepare":
            source = self._sources(project, detail)
            package = public(
                repo.prepare_adjudication(
                    skill_id,
                    revision,
                    assessment_id=body["assessmentId"],
                    source_watermark=source["watermark"],
                    actor=actor,
                )
            )
            return {**package, "projectId": body["projectId"]}
        kwargs = {
            "expected_sequence": body["expectedSequence"],
            "operation_id": body["operationId"],
            "actor": actor,
            "reason": body["reason"],
        }
        if action == "impact_review":
            return public(
                repo.review_impact(
                    skill_id,
                    revision,
                    package_id=body["packageId"],
                    package_hash=body["packageHash"],
                    decision=body["decision"],
                    root_ids=body["rootIds"],
                    validation_ids=body["validationIds"],
                    replacement_revision=body["replacementRevision"],
                    **kwargs,
                )
            )
        if action == "challenge_resolve":
            return public(
                repo.resolve_challenge(
                    skill_id,
                    revision,
                    package_id=body["packageId"],
                    package_hash=body["packageHash"],
                    challenge_id=body["challengeId"],
                    decision=body["decision"],
                    evidence_ids=body["evidenceIds"],
                    replacement_revision=body["replacementRevision"],
                    **kwargs,
                )
            )
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
        if action == "adjudicate":
            source = self._sources(project, detail)
            return public(
                repo.adjudicate(
                    skill_id,
                    revision,
                    package_id=body["packageId"],
                    package_hash=body["packageHash"],
                    source_watermark=source["watermark"],
                    decision=body["judgment"],
                    supersedes=body.get("supersedes"),
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
            self._case(case_id)
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
            require(self.experiments is None or not self.experiments.busy, "busy", 409)
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
            execute = execute_task
            execution_options = {}
            if case_id.startswith("p5-"):
                from memos.skill_p5.service_runtime import execute_task as execute_p5

                execute = execute_p5
            elif case_id.startswith("release-"):
                from memos.skill_lifecycle.execution import make_execution_guard
                from memos.skill_release.service_runtime import execute_task as execute_release

                execute = execute_release
                execution_options["execution_guard"] = make_execution_guard(
                    repo, project.observe, detail["candidate"], actor="host_normal_execution"
                )
            result = execute(
                Path(detail["artifactDirectory"]),
                case_id,
                self.artifacts_root / "runs" / job_id,
                self.runtime_config,
                self.provider,
                mode=mode,
                authorize=lambda: self._grant(project, skill_id, revision, case_id, mode),
                **execution_options,
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
