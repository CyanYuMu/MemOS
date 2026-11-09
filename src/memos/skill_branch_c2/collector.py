"""Host-only re-reading of C2 healthy-Dream evidence; no judgement is manufactured."""

from __future__ import annotations

import tempfile

from pathlib import Path
from types import SimpleNamespace

from memos.skill_branch_c2 import batch, governance, runner
from memos.skill_branch_c2.broker import check_visible_record
from memos.skill_branch_c2.process import (
    build_process_evidence,
    initial_checks_match,
)
from memos.skill_branch_c2.qualification import require_qualification
from memos.skill_branch_c2.tasks import get_suite, materialize, suite_digest
from memos.skill_branch_c2.verifier import PROTOCOL, check_receipt
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_experience import evidence
from memos.skill_experience.evidence import _events
from memos.skill_governance.collector import _inside, _loaded, _read, _sha
from memos.skill_management.domain import require, validate_report
from memos.skill_management.service import evidence_projection


def collector_digest() -> str:
    return hash_json(
        {
            "collector": _sha(Path(__file__)),
            "contract": _sha(Path(governance.__file__)),
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


def _manifest(candidate, export_hash, directory, qualification_directory):
    manifest = _read(directory / "manifest.json")
    identity = {k: v for k, v in manifest.items() if k != "experimentId"}
    require(manifest["experimentId"] == hash_json(identity), "extension_manifest_changed", 409)
    require(
        set(identity)
        == {
            "protocol",
            "batchDigest",
            "adapterDigest",
            "configurationDigest",
            "qualification",
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
        identity["protocol"] == "c2-batch-1"
        and identity["batchDigest"] == _sha(Path(batch.__file__))
        and identity["adapterDigest"] == runner.adapter_digest(),
        "extension_protocol_changed",
        409,
    )
    require(
        identity["candidateRevision"] == candidate["revision"] == CANDIDATE_REVISION
        and identity["contentHash"] == candidate["content_hash"]
        and identity["exportHash"] == export_hash,
        "extension_candidate_changed",
        409,
    )
    require(
        identity["qualification"]
        == require_qualification(qualification_directory, suite_id="scoped-healthy-dream-c2-v1"),
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
            for name, suite_id, group in batch.UNITS
        ]
        and identity["maxAgentRuns"] == 2
        and identity["newIndependentSamples"] == 0
        and identity["branchCoverage"] == []
        and identity["effectivenessEvaluation"] is False,
        "extension_plan_changed",
        409,
    )
    return manifest


def _intermediate(task, report, root, artifacts, records, context, allowed):
    """C2 does not accept C1 midpoint receipts, even if their hashes are valid."""
    require(
        report.get("intermediate_verifications") == [], "c2_intermediate_inventory_changed", 409
    )
    return []


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
    """Return unreviewed Validation and schema-4 facts after full artifact recheck."""
    allowed = Path(allowed_root).resolve()
    root, batch_root = _inside(run_directory, allowed), _inside(batch_directory, allowed)
    qualification_directory = _inside(qualification_directory, allowed)
    manifest = _manifest(candidate, export_hash, batch_root, qualification_directory)
    unit = next((u for u in manifest["units"] if root == batch_root / "runs" / u["id"]), None)
    require(
        unit is not None and unit["group"] == "skill", "extension_calibration_not_independent", 422
    )
    report_path = _inside(root / "report.json", allowed)
    report_hash, manifest_hash = _sha(report_path), _sha(batch_root / "manifest.json")
    report = _read(report_path)
    task = get_suite(unit["suiteId"])
    context = report["run_context"]
    expected_context = {
        "case_id": task.task_id,
        "case_revision": task.revision,
        "lineage_id": task.lineage_id,
        "split": "holdout",
        "skill_id": candidate["skill_id"],
        "skill_revision": candidate["revision"],
    }
    require(
        all(context.get(k) == v for k, v in expected_context.items()), "extension_task_changed", 409
    )
    require(
        report["adapter_version"] == runner.ADAPTER_VERSION
        and report["adapter_sha256"] == manifest["adapterDigest"]
        and report["experiment_id"] == manifest["experimentId"]
        and report["verification_qualification"] == manifest["qualification"]
        and report["purpose"] == "branch_extension_validation"
        and report["group"] == "skill"
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
    _, events, _ = _events(root, report, gaps)
    runtime = manifest["runtime"]
    observed_context = runner.context_matches(
        events,
        SimpleNamespace(
            model=runtime["model"],
            expected_version=runtime["cliVersion"],
            tools=tuple(runtime["tools"]),
        ),
        report["skill_export"]["runtime_alias"],
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
    records_verified = bool(len(records) >= 2)
    for index, record in enumerate(records, 1):
        path = _inside(root / "visible-checks" / f"check-{index}.json", allowed)
        require(
            record["check_index"] == index and _read(path) == record,
            "extension_visible_changed",
            409,
        )
        with tempfile.TemporaryDirectory(prefix="memos-branch-visible-") as temporary:
            sealed = visible_store.restore(
                record["candidate_snapshot"], Path(temporary) / "candidate"
            )
            require(check_visible_record(path, sealed), "extension_visible_unverified", 422)
        records_verified = records_verified and record["result"].get("execution_error") is None
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
    loaded = _loaded(store, report, candidate, {"exportHash": export_hash}, events)
    capture = (
        not set(gaps) - {"source_rule_declaration"}
        and observed_context
        and runner._declarations_complete(events)
    )
    public = {
        "initialExitCode": records[0]["result"]["exit_code"] if records else None,
        "finalExitCode": records[-1]["result"]["exit_code"] if records else None,
        "recordsVerified": bool(records_verified),
        "deliveriesVerified": bool(deliveries),
        "deliveryRefs": [
            {
                "checkIndex": d["check_index"],
                "startedEvent": d["started_event"],
                "completedEvent": d["completed_event"],
            }
            for d in deliveries or []
        ],
    }
    accepted = (
        initial_receipt["status"] == "failed"
        and initial_checks_match(task.suite_id, initial_receipt["checks"])
        and bool(process["checkpoints"])
        and initial_checks_match(task.suite_id, process["checkpoints"][0]["checks"])
        and final_receipt["status"] == "passed"
        and changed
        and records_verified
        and process["complete"]
        and public["initialExitCode"] == 1
        and public["finalExitCode"] == 0
    )
    require(
        report["task_accepted"] is bool(accepted)
        and report["behavior_changed"] is changed
        and report["initial_status"] == initial_receipt["status"]
        and report["task_status"] == final_receipt["status"],
        "extension_result_changed",
        409,
    )
    raw_validation = {
        "kind": "independent_task",
        "purpose": "actual",
        "status": "passed"
        if report["status"] == "passed" and accepted and capture and loaded
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
            "accepted": bool(accepted),
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
        "eligibleBranches": list(task.target_branches),
    }
    assessment = {
        "schemaVersion": 4,
        "collectorVersion": "c2-review-1",
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
        "branchCandidates": list(task.target_branches),
        "coveragePlanHash": hash_json(plan),
        "batchId": manifest["experimentId"],
        "taskDigest": suite_digest(task),
        "suiteId": task.suite_id,
        "checkMapping": list(task.checks),
        "processVerification": process,
        "intermediateVerifications": intermediate,
    }
    assessment["outcome"] = governance.assessment_outcome(assessment)
    governance.validate_assessment(candidate, validation, assessment)
    require(
        _sha(report_path) == report_hash
        and _sha(batch_root / "manifest.json") == manifest_hash
        and _sha(root / "events.jsonl") == report["events_sha256"],
        "extension_evidence_changed_during_read",
        409,
    )
    return {"validation": raw_validation, "assessment": assessment}
