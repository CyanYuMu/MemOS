"""Fixed candidate projection and admission around the preserved Claude adapter.

This boundary checks startup and result admission. It cannot erase an already
loaded Skill or interrupt every tool action after a concurrent state change.
"""

from __future__ import annotations

import hashlib
import json
import tempfile

from pathlib import Path

from memos.skill_adapter.artifacts import SkillRevision, export_skill, runtime_alias, verify_export
from memos.skill_adapter.runner import run_case
from memos.skill_drafting.artifacts import load_candidate
from memos.skill_evaluation.cases import list_cases
from memos.skill_evaluation.tasks import case_digest
from memos.skill_evaluation.verifier import check_receipt


PROJECTION_VERSION = "management-native-1"


def dev_case(case_id: str):
    # Do not enumerate the broader registry: it includes hidden holdout assets.
    for case in list_cases():
        if case.case_id == case_id and case.split == "dev":
            return case
    raise ValueError("managed_case_not_allowed")


def case_options() -> list[dict]:
    return [
        {"caseId": c.case_id, "title": c.description, "split": "dev"}
        for c in list_cases()
        if c.split == "dev"
    ]


def native_skill(candidate: dict) -> SkillRevision:
    lines = [f"# {candidate['title']}", "", candidate["goal"]]
    for key in ("applicability", "exclusions", "prerequisites", "constraints"):
        lines += ["", "## " + key, "", *["- " + value for value in candidate[key]]]
    lines += ["", "## Rules", "", "Entry: " + candidate["entry_rule"]]
    for rule in candidate["rules"]:
        lines += ["", "### " + rule["rule_id"]]
        for key in ("basis", "condition", "observation", "action", "expected_result"):
            lines += [key + ": " + rule[key]]
        lines += ["- " + b["when"] + " → " + b["target"] for b in rule["branches"]]
    lines += [
        "",
        "## Exits",
        "",
        *[key + ": " + candidate["exits"][key] for key in sorted(candidate["exits"])],
    ]
    for key in ("acceptance", "limitations"):
        lines += ["", "## " + key, "", *["- " + value for value in candidate[key]]]
    spec = SkillRevision(
        skill_id=candidate["skill_id"],
        revision=candidate["revision"],
        body="\n".join(lines) + "\n",
        rule_ids=tuple(r["rule_id"] for r in candidate["rules"]),
        requires=tuple(candidate["requires"]),
    )
    runtime_alias(spec)  # Preserve the adapter's limits; never truncate a rule ID.
    return spec


def export_identity(candidate: dict) -> dict:
    spec = native_skill(candidate)
    with tempfile.TemporaryDirectory(prefix="memos-skill-projection-") as root:
        destination = Path(root).resolve() / runtime_alias(spec)
        manifest = export_skill(spec, destination)
        verify_export(spec, destination)
    return {
        "projectionVersion": PROJECTION_VERSION,
        "contentHash": candidate["content_hash"],
        "exportHash": manifest["export_hash"],
        "runtimeAlias": manifest["runtime_alias"],
        "manifest": manifest,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("managed_invalid_artifact")
    result = json.loads(path.read_bytes())
    if not isinstance(result, dict):
        raise ValueError("managed_invalid_artifact")
    return result


def _inspection(
    output: Path, report: dict, candidate: dict, case, identity: dict
) -> tuple[dict, dict, bool, dict | None, dict]:
    context = report.get("run_context", {})
    expected = {
        "case_id": case.case_id,
        "case_revision": case.revision,
        "split": "dev",
        "lineage_id": case.lineage_id,
        "skill_id": candidate["skill_id"],
        "skill_revision": candidate["revision"],
    }
    if not all(context.get(k) == v for k, v in expected.items()) or report.get(
        "case_asset_digest"
    ) != case_digest(case):
        raise ValueError("managed_context_mismatch")
    if report.get("skill_export") != identity["manifest"]:
        raise ValueError("managed_export_mismatch")
    if any(not isinstance(context.get(k), str) or not context[k] for k in ("run_id", "attempt_id")):
        raise ValueError("managed_context_mismatch")
    agent = output / "agent"
    events = agent / "events.jsonl"
    if events.is_symlink() or report.get("events_sha256") != _sha(events):
        raise ValueError("managed_event_mismatch")
    declarations = set()
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if (
            event.get("kind") == "rule_declaration"
            and event.get("payload", {}).get("rule_id") in identity["manifest"]["rule_ids"]
        ):
            declarations.add(event["payload"]["rule_id"])
    receipt_ref = None
    receipt_matches, verification_status = False, "unknown"
    if report.get("receipt"):
        receipt_path = Path(report["receipt"]).resolve()
        if not receipt_path.is_relative_to((agent / "validation").resolve()):
            raise ValueError("managed_receipt_scope_mismatch")
        receipt = _json(receipt_path)
        if report.get("receipt_sha256") != _sha(receipt_path):
            raise ValueError("managed_receipt_digest_mismatch")
        if (
            receipt.get("run_id") != context["run_id"]
            or receipt.get("attempt_id") != context["attempt_id"]
            or receipt.get("case", {}).get("case_id") != case.case_id
        ):
            raise ValueError("managed_receipt_context_mismatch")
        binding = check_receipt(receipt_path, agent / "candidate", agent / "validation")
        receipt_matches, verification_status = binding.matches, binding.verification_status
        receipt_ref = {"uri": receipt_path.as_uri(), "sha256": _sha(receipt_path)}
    accepted = (
        receipt_matches and verification_status == "passed" and report.get("task_accepted") is True
    )
    task = {
        "status": verification_status if verification_status in {"passed", "failed"} else "unknown",
        "accepted": accepted,
        "receiptMatches": receipt_matches,
    }
    usage = {
        "loading": "verified"
        if report.get("loading", {}).get("status") == "verified"
        else "unknown",
        "capture": "complete"
        if report.get("capture", {}).get("status") == "complete"
        else "partial",
        "usageReview": "unreviewed",
        "declaredRuleIds": sorted(declarations),
    }
    return context, task, receipt_matches, receipt_ref, usage


def execute_task(
    candidate_directory: Path,
    case_id: str,
    output: Path,
    config,
    provider: dict,
    *,
    mode: str,
    authorize,
) -> dict:
    if mode not in {"normal", "candidate_evaluation"}:
        raise ValueError("managed_invalid_mode")
    case = dev_case(case_id)
    loaded = load_candidate(candidate_directory)
    candidate = loaded["candidate"]
    identity = export_identity(candidate)
    before = authorize()
    if before.get("allowed") is not True:
        raise ValueError("managed_admission_denied")
    if type(before.get("sequence")) is not int or before["sequence"] < 0:
        raise ValueError("managed_admission_mismatch")
    if mode == "normal" and not (isinstance(before.get("releaseId"), str) and before["releaseId"]):
        raise ValueError("managed_admission_mismatch")
    for key in ("contentHash", "exportHash"):
        if before.get(key) != identity[key]:
            raise ValueError("managed_admission_mismatch")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    spec = native_skill(candidate)
    export_skill(spec, output / "native")
    result = {"status": "unknown", "errorCode": None, "validation": None, "projection": identity}
    try:
        report_path = run_case(
            case_id, output / "agent", config, skill=spec, provider_environment=provider
        )
        if Path(report_path).resolve() != output / "agent/report.json":
            raise ValueError("managed_report_scope_mismatch")
        report = _json(report_path)
        verify_export(spec, output / "native")
        context, task, receipt_matches, receipt_ref, usage = _inspection(
            output, report, candidate, case, identity
        )
        try:
            after = authorize()
        except Exception:
            after = {"allowed": False}
        still_allowed = (
            after.get("allowed") is True
            and after.get("sequence") == before.get("sequence")
            and all(
                after.get(k) == before.get(k) for k in ("releaseId", "contentHash", "exportHash")
            )
        )
        usage["endedAdmission"] = "allowed" if still_allowed else "needs_review"
        status = "unknown"
        if (
            still_allowed
            and receipt_matches
            and usage["loading"] == "verified"
            and usage["capture"] == "complete"
        ):
            status = "passed" if task["accepted"] and report.get("status") == "passed" else "failed"
        validation = {
            "kind": "isolated_dev",
            "status": status,
            "contentHash": candidate["content_hash"],
            "exportHash": identity["exportHash"],
            "caseId": case_id,
            "caseRevision": case.revision,
            "lineageId": case.lineage_id,
            "split": "dev",
            "runId": context["run_id"],
            "attemptId": context["attempt_id"],
            "taskResult": task,
            "skillUsage": usage,
            "receiptVerified": receipt_matches,
            "loadVerified": usage["loading"] == "verified",
            "branchCoverage": [],
            "boundaryCoverage": [],
            "scope": {"caseIds": [case_id], "agent": "claude_code", "mode": "controlled_task"},
            "purpose": "actual",
        }
        if receipt_ref is not None:
            validation["receipt"] = receipt_ref
        result.update(
            status=status,
            validation=validation,
            errorCode=None if still_allowed else "admission_changed",
        )
    except Exception:
        # Never expose provider bodies, local paths or exception text to the API.
        result["errorCode"] = "execution_unverified"
    (output / "managed-result.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    return result
