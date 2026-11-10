"""Recover completed host feedback without rewriting a terminal unknown job."""

from pathlib import Path

from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import digest, now, require, text, validate_report


def read_feedback(artifacts_root, job, candidate):
    # These imports are lazy: the frozen collector also uses service projections.
    from memos.skill_governance.collector import _inside, _read, _sha
    from memos.skill_management import p5_runtime
    from memos.skill_p5.collector import _read_run
    from memos.skill_p5.service_runtime import dev_case
    from memos.skill_p5.tickets import artifact_hashes

    require(job["status"] == "unknown", "feedback_not_unknown", 409)
    digest(job["jobId"])
    require(job["caseId"].startswith("p5-"), "feedback_protocol_unsupported", 422)
    case = dev_case(job["caseId"])
    root = _inside(Path(artifacts_root) / "runs" / job["jobId"], Path(artifacts_root).resolve())
    manifest_path, result_path = root / "manifest.json", root / "managed-result.json"
    for path in (manifest_path, result_path):
        _inside(path, root)
    manifest, result = _read(manifest_path), _read(result_path)
    tracked = {manifest_path: _sha(manifest_path), result_path: _sha(result_path)}
    require(
        manifest.get("experimentId")
        == hash_json({k: v for k, v in manifest.items() if k != "experimentId"})
        and manifest.get("protocol") == "p5-normal-2"
        and manifest.get("runtimeProducer") == _sha(Path(p5_runtime.__file__))
        and manifest.get("executionMode") == job["mode"]
        and manifest.get("candidateRevision") == job["revision"] == candidate["revision"]
        and manifest.get("contentHash") == job["contentHash"] == candidate["content_hash"]
        and manifest.get("exportHash") == job["exportHash"]
        and manifest.get("maxAgentRuns") == 1
        and manifest.get("independentCoverage") is False
        and len(manifest.get("units", [])) == 1,
        "feedback_manifest_mismatch",
        409,
    )
    unit = manifest["units"][0]
    require(
        unit.get("id") == case.case_id.removeprefix("p5-") + "-skill"
        and unit.get("split") == "dev"
        and unit.get("group") == "skill",
        "feedback_unit_mismatch",
        409,
    )
    run = _inside(root / "runs" / unit["id"], root)
    trial = _read_run(candidate, job["exportHash"], run, root, manifest, root, ())
    report, context = trial["report"], trial["context"]
    completion = _read(run / "execution-ticket.json")
    require(report.get("execution_ticket") is not None, "feedback_ticket_missing", 409)
    validation = result.get("validation")
    require(isinstance(validation, dict), "feedback_incomplete", 422)
    validate_report(candidate, validation)
    # The original end-admission claim can only be retained conservatively.
    # A successful claim additionally needs a completed, graph-verified ticket.
    current = validation["skillUsage"]["endedAdmission"] == "allowed"
    require(not current or completion["status"] == "completed", "feedback_admission_mismatch", 409)
    status = (
        "unknown"
        if not current
        or not trial["loaded"]
        or not trial["capture"]
        or trial["unitFacts"]["status"] == "incomplete"
        else "passed"
        if trial["accepted"]
        else "failed"
    )
    expected = {
        "kind": "isolated_dev",
        "purpose": "actual",
        "status": status,
        "contentHash": candidate["content_hash"],
        "exportHash": job["exportHash"],
        "caseId": case.case_id,
        "caseRevision": case.revision,
        "lineageId": case.lineage_id,
        "split": "dev",
        "runId": context["run_id"],
        "attemptId": context["attempt_id"],
        "taskResult": {
            "status": trial["final_receipt"]["status"],
            "accepted": trial["accepted"],
            "receiptMatches": True,
        },
        "skillUsage": {
            "loading": "verified" if trial["loaded"] else "unknown",
            "capture": "complete" if trial["capture"] else "partial",
            "usageReview": "unreviewed",
            "declaredRuleIds": sorted(
                {
                    e["payload"]["rule_id"]
                    for e in trial["events"]
                    if e["kind"] == "rule_declaration"
                }
            ),
            "endedAdmission": "allowed" if current else "needs_review",
        },
        "receiptVerified": True,
        "loadVerified": trial["loaded"],
        "receipt": {"uri": Path(report["receipt"]).as_uri(), "sha256": report["receipt_sha256"]},
        "branchCoverage": [],
        "boundaryCoverage": [],
        "scope": {"caseIds": [case.case_id], "agent": "claude_code", "mode": "controlled_task"},
    }
    require(
        validation == expected and result.get("status") == status, "feedback_result_mismatch", 409
    )
    tracked.update(trial["tracked"])
    require(all(_sha(p) == sha for p, sha in tracked.items()), "feedback_evidence_changed", 409)
    return {
        "validation": expected,
        "ticket": report["execution_ticket"],
        "completion": completion,
        "artifacts": artifact_hashes(report, _sha(run / "report.json")),
        "evidenceDigest": hash_json({str(p.relative_to(root)): sha for p, sha in tracked.items()}),
    }


class FeedbackMixin:
    @staticmethod
    def _job_view(records, job):
        recovered = records.get(("feedback_recovery", job["jobId"]))
        return {**job, **({"feedbackRecovery": recovered} if recovered else {})}

    def verify_feedback_execution(self, job_id, proof):
        """Retained JSON must agree with the immutable graph-issued execution."""

        def read(records, nodes, edges):
            job = self._job(records, job_id)
            require(job["status"] == "unknown", "feedback_not_unknown", 409)
            ticket = records.get(("execution_ticket", proof["ticket"]["ticketId"]), {})
            completed = records.get(("execution_finish", proof["ticket"]["ticketId"]))
            require(
                all(ticket.get(k) == v for k, v in proof["ticket"].items())
                and completed == proof["completion"]
                and all(completed.get(k) == v for k, v in proof["artifacts"].items())
                and all(
                    ticket.get(k) == job[k]
                    for k in ("skillId", "revision", "mode", "contentHash", "exportHash")
                )
                and ticket.get("caseId") == job["caseId"]
                and ticket.get("group") == "skill",
                "feedback_execution_mismatch",
                409,
            )
            return True

        return self._run(read)[0]

    def record_feedback_recovery(
        self, job_id, *, validation_id, evidence_digest, actor, operation_id
    ):
        for value in (job_id, validation_id, evidence_digest):
            digest(value)
        text(actor, maximum=300)
        request = {
            "action": "feedback_recovery",
            "jobId": job_id,
            "validationId": validation_id,
            "evidenceDigest": evidence_digest,
            "actor": actor,
        }

        def perform(records, nodes, edges):
            job = self._job(records, job_id)
            require(job["status"] == "unknown", "feedback_not_unknown", 409)
            validation = records.get(("validation", validation_id), {})
            require(
                all(
                    validation.get(k) == job[k]
                    for k in ("skillId", "revision", "caseId", "contentHash", "exportHash")
                )
                and validation.get("kind") == "isolated_dev"
                and validation.get("split") == "dev"
                and validation.get("branchCoverage") == validation.get("boundaryCoverage") == [],
                "feedback_validation_mismatch",
                409,
            )
            previous = records.get(("feedback_recovery", job_id))
            if previous:
                require(
                    previous["validationId"] == validation_id
                    and previous["evidenceDigest"] == evidence_digest,
                    "feedback_recovery_conflict",
                    409,
                )
                return self._job_view(records, job)
            recovered = {
                **{k: v for k, v in request.items() if k != "action"},
                **{k: job[k] for k in ("skillId", "revision")},
                **{k: validation[k] for k in ("status", "taskResult", "skillUsage")},
                "recoveredAt": now(),
            }
            recovered["recoveryId"] = hash_json(recovered)
            self._put(records, "feedback_recovery", job_id, recovered)
            self._changed(
                records,
                self._state(records, job["skillId"], job["revision"]),
                "feedback_recovered",
                actor,
            )
            return self._job_view(records, job)

        return self._run(perform, mutation=True, op=(operation_id, request))[0]
