"""Read-only host verification of retained evidence before human adjudication.

Paths are supplied only by a private host registry, never by a browser command.
The collector seals observations, not human conclusions or branch coverage.
"""

from __future__ import annotations

import hashlib
import json
import tempfile

from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, verify_export
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_evaluation.verifier import check_receipt as legacy_check
from memos.skill_experience.evidence import _events, _visible_checks
from memos.skill_management.domain import require
from memos.skill_management.service import evidence_projection
from memos.skill_verification.verifier import check_receipt as current_check


def collector_digest() -> str:
    from memos.skill_experience import evidence

    return hash_json(
        {
            "collector": _sha(Path(__file__)),
            "decisions": _sha(Path(__file__).with_name("domain.py")),
            "event_verifier": _sha(Path(evidence.__file__)),
        }
    )


def _inside(path: Path, root: Path) -> Path:
    path = Path(path).absolute()
    require(not any(part.is_symlink() for part in (path, *path.parents)), "evidence_path_invalid")
    require(path.resolve().is_relative_to(root), "evidence_path_invalid")
    return path.resolve()


def _sha(path: Path) -> str:
    require(path.is_file() and path.stat().st_size <= 8_000_000, "evidence_size_invalid")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    _sha(path)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_evidence_field")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    require(isinstance(value, dict), "invalid_evidence")
    return value


def _receipt(path, candidate, artifacts, case):
    value = _read(path)
    checker = current_check if value.get("schema_version") == 3 else legacy_check
    checked = checker(path, candidate, artifacts)
    require(checked.matches, "receipt_unverified", 422)
    require(
        value["case"]["case_id"] == case.case_id and value["asset_digest"] == case_digest(case),
        "assessment_binding_mismatch",
        409,
    )
    return value


def _loaded(store, report, candidate, validation, events):
    manifest = report.get("skill_export")
    require(
        isinstance(manifest, dict)
        and manifest.get("skill_id") == candidate["skill_id"]
        and manifest.get("revision") == candidate["revision"]
        and manifest.get("export_hash") == validation["exportHash"],
        "export_mismatch",
        409,
    )
    store.verify(report["control_snapshot"])
    with tempfile.TemporaryDirectory(prefix="memos-governance-control-") as temporary:
        control = store.restore(report["control_snapshot"], Path(temporary).resolve() / "control")
        alias = manifest["runtime_alias"]
        require(isinstance(alias, str) and Path(alias).name == alias, "export_mismatch", 409)
        directory = control / "skills" / alias
        body = (directory / "SKILL.md").read_text().split("\n---\n\n", 1)
        require(len(body) == 2, "export_mismatch", 409)
        spec = SkillRevision(
            candidate["skill_id"],
            candidate["revision"],
            body[1],
            tuple(manifest["rule_ids"]),
            tuple(manifest["requires"]),
        )
        require(verify_export(spec, directory) == manifest, "export_mismatch", 409)
    expanded = any(
        e["kind"] == "UserPromptExpansion"
        and e["payload"].get("command_name") == alias
        and e["payload"].get("gate") == "allowed"
        for e in events
    )
    discovered = any(
        e["kind"] == "session_init" and alias in e["payload"].get("slash_commands", [])
        for e in events
    )
    return expanded and discovered and report.get("loading", {}).get("status") == "verified"


def assess_run(
    candidate: dict,
    validation: dict,
    run_directory: Path,
    *,
    allowed_root: Path,
    reassessment: tuple[Path, Path] | None = None,
    coverage_plan: dict | None = None,
) -> dict:
    """Recheck facts; a posthoc receipt explains the same patch, never a new sample."""
    allowed = Path(allowed_root).resolve()
    root = _inside(run_directory, allowed)
    report_path = _inside(root / "report.json", allowed)
    before_hash = _sha(report_path)
    report = _read(report_path)
    context = report["run_context"]
    for source, target in (
        ("run_id", "runId"),
        ("attempt_id", "attemptId"),
        ("case_id", "caseId"),
        ("case_revision", "caseRevision"),
        ("lineage_id", "lineageId"),
        ("split", "split"),
    ):
        require(context[source] == validation[target], "assessment_binding_mismatch", 409)
    require(
        context.get("skill_id") == candidate["skill_id"]
        and context.get("skill_revision") == candidate["revision"],
        "assessment_binding_mismatch",
        409,
    )
    case = get_case(validation["caseId"])
    require(report["case_asset_digest"] == case_digest(case), "assessment_binding_mismatch", 409)
    final = _inside(root / "candidate", allowed)
    old_receipt_path = _inside(Path(report["receipt"]), root / "validation")
    require(
        _sha(old_receipt_path) == report["receipt_sha256"] == validation["receipt"]["sha256"],
        "receipt_unverified",
        422,
    )
    original = _receipt(old_receipt_path, final, root / "validation", case)
    require(
        original["run_id"] == context["run_id"] and original["attempt_id"] == context["attempt_id"],
        "assessment_binding_mismatch",
        409,
    )
    require(
        snapshot_fingerprint(final)
        == report["candidate_snapshot"]
        == original["candidate_snapshot"],
        "assessment_binding_mismatch",
        409,
    )
    receipt_path, receipt = old_receipt_path, original
    if reassessment is not None:
        receipt_path = _inside(reassessment[0], allowed)
        artifacts = _inside(reassessment[1], allowed)
        receipt = _receipt(receipt_path, final, artifacts, case)
        require(
            receipt["candidate_snapshot"] == original["candidate_snapshot"],
            "assessment_binding_mismatch",
            409,
        )
    gaps = []
    _, events, _ = _events(root, report, gaps)
    _visible_checks(root, report)
    store = SnapshotStore(root / "snapshots")
    initial = store.verify(report["initial_snapshot"])
    final_snapshot = store.verify(report["candidate_snapshot"])
    require(
        context["initial_snapshot"] == report["initial_snapshot"],
        "assessment_binding_mismatch",
        409,
    )
    loaded = _loaded(store, report, candidate, validation, events)

    # Only the registered manual-boundary diagnosis is explanatory output.
    # Do not exempt arbitrary JSON/Markdown files from behavior-change checks.
    def behavior(snapshot):
        return [
            entry
            for entry in snapshot.manifest["entries"]
            if not (case.assessment_kind == "manual_boundary" and entry["path"] == "diagnosis.json")
        ]

    plan = coverage_plan or {
        "candidateRevision": candidate["revision"],
        "caseId": case.case_id,
        "caseDigest": case_digest(case),
        "eligibleBranches": [],
    }
    require(
        isinstance(plan, dict)
        and set(plan) == {"candidateRevision", "caseId", "caseDigest", "eligibleBranches"}
    )
    require(
        plan["candidateRevision"] == candidate["revision"]
        and plan["caseId"] == case.case_id
        and plan["caseDigest"] == case_digest(case),
        "coverage_plan_mismatch",
        409,
    )
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
    require(
        _sha(report_path) == before_hash and _sha(root / "events.jsonl") == report["events_sha256"],
        "evidence_changed_during_read",
        409,
    )
    return {
        "schemaVersion": 1,
        "collectorVersion": "host-review-1",
        "collectorDigest": collector_digest(),
        **{
            key: validation[key]
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
            )
        },
        "reportSha256": before_hash,
        "eventsSha256": report["events_sha256"],
        "candidateSnapshot": original["candidate_snapshot"],
        "captureComplete": not set(gaps) - {"source_rule_declaration"}
        and report.get("context_matches") is True,
        "loadVerified": loaded,
        "behaviorChanged": behavior(initial) != behavior(final_snapshot),
        "originalStatus": original["status"],
        "verification": {
            "status": receipt["status"],
            "receiptSha256": _sha(receipt_path),
            "protocol": receipt["validator_version"],
            "matches": True,
            "sameFinalPatch": True,
            "posthoc": reassessment is not None,
        },
        "checks": [{"name": item["name"], "status": item["status"]} for item in receipt["checks"]],
        "events": projections,
        "branchCandidates": plan["eligibleBranches"],
        "coveragePlanHash": hash_json(plan),
    }
