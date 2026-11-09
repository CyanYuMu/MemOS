"""Re-read fixed release task evidence, native delivery and execution identities."""

from __future__ import annotations

import re
import tempfile

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from memos.skill_branch_c2 import collector as frozen_collector
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_experience import evidence
from memos.skill_experience.evidence import _events
from memos.skill_governance.collector import _inside, _loaded, _read, _sha
from memos.skill_management.domain import require, validate_report
from memos.skill_management.service import evidence_projection
from memos.skill_release import batch as batch
from memos.skill_release import contracts as frozen_governance
from memos.skill_release import governance as governance
from memos.skill_release import runner as runner
from memos.skill_release.broker import check_visible_record
from memos.skill_release.execution_policy import (
    PLAN_VERSION,
    outcome_facts,
    plan_issues,
    policy_identity,
    task_outcome,
    unit_issues,
)
from memos.skill_release.process import (
    build_process_evidence,
)
from memos.skill_release.qualification import require_qualification
from memos.skill_release.tasks import SUITES, get_suite, materialize, suite_digest
from memos.skill_release.verifier import PROTOCOL, check_receipt


def collector_digest() -> str:
    return hash_json(
        {
            "collector": _sha(Path(__file__)),
            "contract": _sha(Path(governance.__file__)),
            "frozenHelpers": _sha(Path(frozen_collector.__file__)),
            "policy": policy_identity(),
            "frozenGovernance": _sha(Path(frozen_governance.__file__)),
            "eventVerifier": _sha(Path(evidence.__file__)),
            "processVerifier": _sha(Path(build_process_evidence.__code__.co_filename)),
            "legacyLoadVerifier": _sha(Path(_loaded.__code__.co_filename)),
        }
    )


def _verify_receipt(path, candidate, artifacts, task, run_id, attempt):
    value = _read(path)
    binding = check_receipt(path, candidate, artifacts)
    require(binding.matches, "extension_receipt_unverified", 422)
    require(
        value["suite"]["taskId"] == task.task_id
        and value["suite"]["digest"] == suite_digest(task)
        and value["runId"] == run_id
        and value["attemptId"] == attempt,
        "extension_receipt_identity_mismatch",
        409,
    )
    return value


def _intermediate(task, report, root, artifacts, records, context, allowed):
    """C2 does not accept C1 midpoint receipts, even if their hashes are valid."""
    require(
        report.get("intermediate_verifications") == [],
        "release_intermediate_inventory_changed",
        409,
    )
    return []


def _manifest(candidate, export_hash, directory, qualification_directory):
    manifest = _read(directory / "manifest.json")
    identity = {k: v for k, v in manifest.items() if k != "experimentId"}
    require(manifest["experimentId"] == hash_json(identity), "extension_manifest_changed", 409)
    require(
        set(identity)
        == {
            "protocol",
            "planPurpose",
            "batchNonce",
            "batchDigest",
            "executionPolicy",
            "adapterDigest",
            "configurationDigest",
            "qualification",
            "branchIntents",
            "candidateRevision",
            "contentHash",
            "exportHash",
            "runtime",
            "units",
            "maxAgentRuns",
            "newIndependentSamples",
            "branchCoverage",
            "effectivenessEvaluation",
        },
        "extension_manifest_invalid",
    )
    require(
        identity["protocol"] == "release-batch-1"
        and isinstance(identity["batchNonce"], str)
        and re.fullmatch(r"[a-f0-9]{32}", identity["batchNonce"]) is not None
        and identity["executionPolicy"] == policy_identity()
        and identity["batchDigest"] == _sha(Path(batch.__file__))
        and identity["adapterDigest"] == runner.adapter_digest(),
        "extension_protocol_changed",
        409,
    )
    require(
        identity["candidateRevision"] == candidate["revision"]
        and identity["contentHash"] == candidate["content_hash"]
        and identity["exportHash"] == export_hash,
        "extension_candidate_changed",
        409,
    )
    require(
        identity["qualification"]
        == {
            name: require_qualification(qualification_directory / name, suite_id=name)
            for name in SUITES
        },
        "extension_qualification_changed",
        409,
    )
    require(
        identity["units"]
        == [
            {
                "id": name,
                "taskId": get_suite(suite_id).task_id,
                "suiteId": suite_id,
                "group": group,
                "taskDigest": suite_digest(get_suite(suite_id)),
            }
            for name, suite_id, group in batch.plan_units(identity["planPurpose"])
        ]
        and identity["maxAgentRuns"] == len(batch.plan_units(identity["planPurpose"]))
        and identity["newIndependentSamples"] == 0
        and identity["branchCoverage"] == []
        and identity["effectivenessEvaluation"] is False,
        "extension_plan_changed",
        409,
    )
    batch.validate_intents(candidate, identity["branchIntents"])
    return manifest


def _read_run(candidate, export_hash, root, batch_root, manifest, allowed, secret_values):
    unit = next((u for u in manifest["units"] if root == batch_root / "runs" / u["id"]), None)
    require(unit is not None, "release_unit_missing", 409)
    report_path = _inside(root / "report.json", allowed)
    report_hash = _sha(report_path)
    report = _read(report_path)
    tracked = {report_path: report_hash}
    group = unit["group"]
    task = get_suite(unit["suiteId"])
    context = report["run_context"]
    ticket = report.get("execution_ticket")
    if ticket is not None:
        from memos.skill_release.tickets import artifact_hashes, validate_finish, validate_start

        validate_start(
            ticket,
            {
                "batchId": manifest["experimentId"],
                "unitId": unit["id"],
                "caseId": task.task_id,
                "group": group,
                "mode": manifest.get("executionMode", "normal")
                if unit.get("split") == "dev"
                else "independent_validation",
                "contentHash": candidate["content_hash"],
                "exportHash": export_hash,
            },
        )
        require(
            context.get("run_id") == ticket["runId"]
            and context.get("attempt_id") == ticket["attemptId"],
            "execution_ticket_native_identity",
            409,
        )
        completion = _inside(root / "execution-ticket.json", allowed)
        validate_finish(_read(completion), ticket, artifact_hashes(report, report_hash))
        tracked[completion] = _sha(completion)
    require(
        isinstance(context.get("run_id"), str)
        and (
            ticket is not None
            or re.fullmatch(
                manifest["experimentId"] + "-" + group + r"-[a-f0-9]{8}", context["run_id"]
            )
            is not None
        ),
        "release_batch_run_identity_mismatch",
        409,
    )
    expected_context = {
        "case_id": task.task_id,
        "case_revision": task.revision,
        "lineage_id": task.lineage_id,
        "split": unit.get("split", "holdout"),
    }
    if group == "skill":
        expected_context.update(
            skill_id=candidate["skill_id"], skill_revision=candidate["revision"]
        )
    else:
        require(
            "skill_id" not in context and "skill_revision" not in context,
            "calibration_skill_context",
        )
    require(
        all(context.get(k) == v for k, v in expected_context.items()), "extension_task_changed", 409
    )
    require(
        report["adapter_version"] == runner.ADAPTER_VERSION
        and report["adapter_sha256"] == manifest["adapterDigest"]
        and report["experiment_id"] == manifest["experimentId"]
        and report["verification_qualification"] == manifest["qualification"][task.suite_id]
        and report["purpose"]
        == (
            "normal_use"
            if unit.get("split") == "dev"
            else "release_validation"
            if group == "skill"
            else "protocol_calibration"
        )
        and report["group"] == group
        and report.get("execution_policy") == manifest["executionPolicy"]
        and report["case_asset_digest"] == suite_digest(task)
        and report["task_purpose"] == task.purpose
        and report["suite_id"] == task.suite_id
        and report["check_mapping"] == list(task.checks),
        "extension_run_binding_changed",
        409,
    )
    for report_key, manifest_key in (
        ("cli_sha256", "cliHash"),
        ("cli_version", "cliVersion"),
        ("requested_model", "model"),
        ("effort", "effort"),
        ("tools", "tools"),
        ("timeout_seconds", "timeoutSeconds"),
        ("max_turns", "maxTurns"),
        ("max_budget_usd", "maxBudgetUsd"),
    ):
        require(
            report[report_key] == manifest["runtime"][manifest_key],
            "extension_runtime_changed",
            409,
        )
    artifacts = root / "validation"
    receipts = []
    for prefix, folder, attempt in (
        ("initial_", "initial-candidate", context["attempt_id"] + "-initial"),
        ("", "candidate", context["attempt_id"]),
    ):
        path = _inside(Path(report[prefix + "receipt"]), artifacts)
        require(_sha(path) == report[prefix + "receipt_sha256"], "extension_receipt_changed", 409)
        tracked[path] = report[prefix + "receipt_sha256"]
        receipts.append(
            _verify_receipt(
                path, _inside(root / folder, allowed), artifacts, task, context["run_id"], attempt
            )
        )
    initial_receipt, final_receipt = receipts
    initial_id, final_id = initial_receipt["candidateSnapshot"], final_receipt["candidateSnapshot"]
    require(
        initial_id == report["initial_snapshot"] == context["initial_snapshot"]
        and final_id == report["candidate_snapshot"],
        "extension_snapshot_changed",
        409,
    )
    with tempfile.TemporaryDirectory(prefix="memos-branch-initial-") as temporary:
        expected = materialize(task.suite_id, Path(temporary) / "initial")
        require(
            snapshot_fingerprint(expected) == initial_id, "extension_initial_variant_changed", 409
        )
    store = SnapshotStore(root / "snapshots")
    initial, final = store.verify(initial_id), store.verify(final_id)
    changed = initial.manifest["entries"] != final.manifest["entries"]
    gaps = []
    event_facts, events, first_native = _events(root, report, gaps)
    tracked[root / "events.jsonl"] = report["events_sha256"]
    runtime = manifest["runtime"]
    observed_context = runner.context_matches(
        events,
        SimpleNamespace(
            model=runtime["model"],
            expected_version=runtime["cliVersion"],
            tools=tuple(runtime["tools"]),
        ),
        report["skill_export"]["runtime_alias"] if group == "skill" else None,
    )
    observed_models = {
        name
        for event in events
        if event["kind"] == "native_result"
        for name in event["payload"].get("modelUsage", {})
    }
    if observed_models and observed_models != {runtime["model"]}:
        observed_context = False
    require(
        report["context_matches"] is observed_context
        and report["reported_models"] == sorted(observed_models),
        "extension_native_context_changed",
        409,
    )
    records = report["visible_checks"]
    require(isinstance(records, list), "extension_visible_invalid")
    visible_store = SnapshotStore(root / "visible-checks/snapshots")
    records_verified = bool(records)
    for index, record in enumerate(records, 1):
        path = _inside(root / "visible-checks" / f"check-{index}.json", allowed)
        require(
            record["check_index"] == index and _read(path) == record,
            "extension_visible_changed",
            409,
        )
        tracked[path] = _sha(path)
        with tempfile.TemporaryDirectory(prefix="memos-branch-visible-") as temporary:
            sealed = visible_store.restore(
                record["candidate_snapshot"], Path(temporary) / "candidate"
            )
            require(check_visible_record(path, sealed), "extension_visible_unverified", 422)
    if records:
        records_verified = (
            records_verified
            and records[0]["candidate_snapshot"] == initial_id
            and records[-1]["candidate_snapshot"] == final_id
        )
    deliveries = runner._visible_deliveries(records, events, secret_values)
    require(
        report.get("visible_delivery_refs") == (deliveries or []), "extension_delivery_changed", 409
    )
    process = build_process_evidence(task.suite_id, records, events, deliveries)
    require(report.get("process_evidence") == process, "extension_process_changed", 409)
    intermediate = _intermediate(task, report, root, artifacts, records, context, allowed)
    if group == "skill":
        loaded = _loaded(store, report, candidate, {"exportHash": export_hash}, events)
        loading = "verified" if loaded else "unknown"
    else:
        loaded = (
            report["skill_export"] is None
            and report["experience"]["provided"] is False
            and not any(
                event["kind"] == "UserPromptExpansion"
                or (
                    event["kind"] == "tool_started" and event["payload"].get("tool_name") == "Skill"
                )
                for event in events
            )
        )
        loading = "not_requested" if loaded else "unknown"
    require(report["loading"]["status"] == loading, "release_loading_mismatch", 409)
    capture = (
        not set(gaps) - {"source_rule_declaration"}
        and observed_context
        and (group == "none" or runner._declarations_complete(events))
    )
    public = {
        "initialExitCode": records[0]["result"]["exit_code"] if records else None,
        "finalExitCode": records[-1]["result"]["exit_code"] if records else None,
        "recordsVerified": bool(records_verified),
        "deliveriesVerified": bool(deliveries),
        "fault": None,
        "deliveryRefs": [
            {
                "checkIndex": d["check_index"],
                "startedEvent": d["started_event"],
                "completedEvent": d["completed_event"],
            }
            for d in deliveries or []
        ],
    }
    if task.fault and records and records[-1].get("fault") == task.fault and deliveries:
        record, delivery = records[-1], deliveries[-1]
        public["fault"] = {
            **task.fault,
            "recordSha256": hash_json(record),
            "startedEvent": delivery["started_event"],
            "completedEvent": delivery["completed_event"],
        }
    observed = outcome_facts(
        task,
        initial_receipt,
        final_receipt,
        initial_id,
        final_id,
        changed,
        records,
        process,
        bool(records_verified and deliveries),
        True,
    )
    outcome = task_outcome(task.suite_id, observed)
    accepted = outcome["eligible"]
    require(report.get("task_outcome") == outcome, "release_outcome_changed", 409)
    require(
        report["task_accepted"] is bool(accepted)
        and report["behavior_changed"] is changed
        and report["initial_status"] == initial_receipt["status"]
        and report["task_status"] == final_receipt["status"],
        "extension_result_changed",
        409,
    )
    execution = report["execution"]
    require(
        type(execution["returncode"]) is int or execution["returncode"] is None,
        "release_execution_invalid",
    )
    require(
        type(execution["timed_out"]) is bool and type(execution["cancelled"]) is bool,
        "release_execution_invalid",
    )
    successful = (
        capture
        and loaded
        and event_facts["native_outcome"] == "reported_success"
        and execution["returncode"] == 0
        and not execution["timed_out"]
        and not execution["cancelled"]
    )
    status = ("passed" if accepted else "failed") if successful else "incomplete"
    require(report["status"] == status, "release_status_mismatch", 409)
    facts = {
        "unit": unit["id"],
        "group": group,
        "runId": context["run_id"],
        "attemptId": context["attempt_id"],
        "reportSha256": report_hash,
        "eventsSha256": report["events_sha256"],
        "initialReceiptSha256": report["initial_receipt_sha256"],
        "receiptSha256": report["receipt_sha256"],
        "status": status,
        "taskAccepted": bool(accepted),
        "captureComplete": bool(capture),
        "loading": loading,
        "processComplete": process["complete"],
        "maxCycles": process["maxCycles"],
        "cyclesUsed": process["cyclesUsed"],
        "budgetExceeded": process["budgetExceeded"],
        "unverifiedEdits": process["unverifiedEdits"],
        "policyMatches": report["execution_policy"]
        == manifest["executionPolicy"]
        == policy_identity(),
    }
    issues = unit_issues(facts)
    facts.update(qualified=not issues, issues=issues)
    return {
        "unitFacts": facts,
        "tracked": tracked,
        "report": report,
        "report_hash": report_hash,
        "task": task,
        "context": context,
        "initial_receipt": initial_receipt,
        "final_receipt": final_receipt,
        "initial_id": initial_id,
        "final_id": final_id,
        "changed": changed,
        "capture": capture,
        "loaded": loaded,
        "public": public,
        "process": process,
        "intermediate": intermediate,
        "accepted": accepted,
        "task_outcome": outcome,
        "events": events,
        "first_native": first_native,
        "last_native": datetime.fromisoformat(event_facts["last_native_at"])
        if event_facts["last_native_at"]
        else None,
    }


def collect_trial(
    candidate: dict,
    export_hash: str,
    run_directory: Path,
    *,
    batch_directory: Path,
    qualification_directory: Path,
    allowed_root: Path,
    secret_values: tuple[str, ...] = (),
) -> dict:
    """Return unreviewed Validation and schema-6 facts after full artifact recheck."""
    allowed = Path(allowed_root).resolve()
    root, batch_root = _inside(run_directory, allowed), _inside(batch_directory, allowed)
    qualification_directory = _inside(qualification_directory, allowed)
    manifest_hash = _sha(batch_root / "manifest.json")
    manifest = _manifest(candidate, export_hash, batch_root, qualification_directory)
    unit = next((u for u in manifest["units"] if root == batch_root / "runs" / u["id"]), None)
    require(
        unit is not None and unit["group"] == "skill", "extension_calibration_not_independent", 422
    )
    calibration_root = _inside(batch_root / "runs/release-none", allowed)
    calibration = _read_run(
        candidate, export_hash, calibration_root, batch_root, manifest, allowed, secret_values
    )
    trial = _read_run(candidate, export_hash, root, batch_root, manifest, allowed, secret_values)
    require(
        calibration["last_native"] is not None
        and trial["first_native"] is not None
        and calibration["last_native"] <= trial["first_native"],
        "release_calibration_order_unverified",
        409,
    )
    require(
        calibration["unitFacts"]["runId"] != trial["unitFacts"]["runId"]
        and calibration["unitFacts"]["attemptId"] != trial["unitFacts"]["attemptId"],
        "release_cross_unit_identity",
        409,
    )
    issues = plan_issues(calibration["unitFacts"], trial["unitFacts"])
    execution_qualification = {
        "protocol": PLAN_VERSION,
        "policyDigest": policy_identity()["digest"],
        "batchProtocol": manifest["protocol"],
        "batchId": manifest["experimentId"],
        "manifestSha256": manifest_hash,
        "calibration": calibration["unitFacts"],
        "trial": trial["unitFacts"],
        "planQualified": not issues,
        "planIssues": issues,
    }
    report = trial["report"]
    report_hash = trial["report_hash"]
    task = trial["task"]
    context = trial["context"]
    initial_receipt = trial["initial_receipt"]
    final_receipt = trial["final_receipt"]
    initial_id = trial["initial_id"]
    final_id = trial["final_id"]
    changed = trial["changed"]
    capture = trial["capture"]
    loaded = trial["loaded"]
    public = trial["public"]
    process = trial["process"]
    intermediate = trial["intermediate"]
    accepted = trial["accepted"]
    behavior_accepted = accepted and task.outcome in {"repair", "healthy"}
    events = trial["events"]
    raw_validation = {
        "kind": "independent_task",
        "purpose": "actual",
        "status": "passed"
        if report["status"] == "passed" and behavior_accepted and capture and loaded
        else "failed"
        if final_receipt["status"] == "failed" and capture and loaded and process["complete"]
        else "unknown",
        "contentHash": candidate["content_hash"],
        "exportHash": export_hash,
        "caseId": task.task_id,
        "caseRevision": task.revision,
        "lineageId": task.lineage_id,
        "split": "holdout",
        "runId": context["run_id"],
        "attemptId": context["attempt_id"],
        "taskResult": {
            "status": final_receipt["status"]
            if final_receipt["status"] in {"passed", "failed"}
            else "unknown",
            "accepted": bool(behavior_accepted),
            "receiptMatches": True,
        },
        "skillUsage": {
            "loading": "verified" if loaded else "unknown",
            "capture": "complete" if capture else "partial",
            "usageReview": "unreviewed",
            "declaredRuleIds": sorted(
                {e["payload"]["rule_id"] for e in events if e["kind"] == "rule_declaration"}
            ),
        },
        "receiptVerified": True,
        "loadVerified": loaded,
        "receipt": {"uri": Path(report["receipt"]).as_uri(), "sha256": report["receipt_sha256"]},
        "branchCoverage": [],
        "boundaryCoverage": [],
        "scope": {"caseIds": [task.task_id], "agent": "claude_code", "mode": "controlled_task"},
    }
    validation = validate_report(candidate, raw_validation)
    projections = []
    for event in events:
        if event["kind"] in {
            "tool_started",
            "tool_completed",
            "assistant_text",
            "rule_declaration",
        }:
            display = evidence_projection({"value": event["payload"]})
            projections.append(
                {
                    "id": event["event_id"],
                    "kind": event["kind"],
                    "value": {"display": display["value"], "redacted": display["display_redacted"]},
                }
            )
    plan = {
        "candidateRevision": candidate["revision"],
        "caseId": task.task_id,
        "caseDigest": suite_digest(task),
        "eligibleBranches": manifest["branchIntents"][task.suite_id],
    }
    assessment = {
        "schemaVersion": 6,
        "collectorVersion": "release-review-1",
        "collectorDigest": collector_digest(),
        **{
            k: validation[k]
            for k in (
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
            )
        },
        "reportSha256": report_hash,
        "eventsSha256": report["events_sha256"],
        "candidateSnapshot": final_id,
        "captureComplete": bool(capture),
        "loadVerified": loaded,
        "behaviorChanged": changed,
        "originalStatus": final_receipt["status"],
        "verification": {
            "status": final_receipt["status"],
            "receiptSha256": report["receipt_sha256"],
            "protocol": PROTOCOL,
            "matches": True,
            "sameFinalPatch": True,
            "posthoc": False,
        },
        "initialVerification": {
            "status": initial_receipt["status"],
            "receiptSha256": report["initial_receipt_sha256"],
            "protocol": PROTOCOL,
            "matches": True,
            "candidateSnapshot": initial_id,
            "checks": [
                {"name": c["name"], "status": c["status"]} for c in initial_receipt["checks"]
            ],
        },
        "publicVerification": public,
        "checks": [{"name": c["name"], "status": c["status"]} for c in final_receipt["checks"]],
        "events": projections,
        "branchCandidates": manifest["branchIntents"][task.suite_id],
        "coveragePlanHash": hash_json(plan),
        "batchId": manifest["experimentId"],
        "taskDigest": suite_digest(task),
        "suiteId": task.suite_id,
        "checkMapping": list(task.checks),
        "processVerification": process,
        "intermediateVerifications": intermediate,
        "executionQualification": execution_qualification,
        "taskOutcome": trial["task_outcome"],
    }
    assessment["outcome"] = governance.assessment_outcome(assessment)
    governance.validate_assessment(candidate, validation, assessment)
    require(
        _sha(batch_root / "manifest.json") == manifest_hash,
        "release_manifest_changed_during_read",
        409,
    )
    for retained in (calibration, trial):
        require(
            all(_sha(path) == digest for path, digest in retained["tracked"].items()),
            "release_evidence_changed_during_read",
            409,
        )
    return {"validation": raw_validation, "assessment": assessment}
