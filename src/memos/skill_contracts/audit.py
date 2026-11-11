"""Re-read frozen transfer artifacts without rerunning or upgrading old scores."""

import json

from pathlib import Path

from memos.skill_contracts.mapping import assess_checks, catalog, contract
from memos.skill_drafting.domain import hash_json
from memos.skill_p5.delivery import _visible_deliveries
from memos.skill_transfer.__main__ import UNITS, score
from memos.skill_transfer.runner import adapter_digest
from memos.skill_transfer.verification import check_receipt, sha, write


def require(condition, code="contract_artifact_mismatch"):
    if not condition:
        raise ValueError(code)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "contract_duplicate_key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("contract_nonfinite_json")


def parse(data):
    return json.loads(data, object_pairs_hook=_unique, parse_constant=_constant)


def read_json(path):
    sha(path)  # Bounded regular file; no link following.
    return parse(Path(path).read_text())


def verified_receipt(root):
    root = Path(root)
    path = root / "validation/receipt.json"
    require(
        check_receipt(path, root / "candidate", root / "validation").matches,
        "contract_receipt_unverified",
    )
    return read_json(path)


def _rows(checks):
    require(isinstance(checks, list))
    require(all(type(row.get("passed")) is bool for row in checks))
    return [
        {"name": row["name"], "status": "passed" if row["passed"] else "failed"} for row in checks
    ]


def audit_batch(batch, output):
    batch, output = Path(batch).resolve(), Path(output).resolve()
    require(not output.exists() and not output.is_symlink(), "contract_output_exists")
    require(not output.is_relative_to(batch), "contract_output_inside_history")
    manifest = read_json(batch / "manifest.json")
    results = read_json(batch / "results.json")
    require(
        manifest["experimentId"]
        == hash_json({k: v for k, v in manifest.items() if k != "experimentId"})
        and manifest["protocol"] == "transfer-six-units-1"
        and manifest["adapterDigest"] == adapter_digest()
        and manifest["revision"] == catalog()["skillRevision"]
        and manifest["projection"]["exportHash"] == catalog()["exportHash"]
        and results["experimentId"] == manifest["experimentId"]
        and results["complete"] is True
        and results["publicationEvidence"] is False
        and len(results["rows"]) == len(UNITS)
        and [(u["caseId"], u["group"]) for u in manifest["units"]] == UNITS
    )
    rows = []
    for unit, old in zip(manifest["units"], results["rows"], strict=True):
        require(unit["id"] == unit["caseId"] + "-" + unit["group"])
        require(all(old[k] == value for k, value in unit.items()))
        root = batch / "runs" / unit["id"]
        require(sha(root / "report.json") == old["reportSha256"])
        report = read_json(root / "report.json")
        profile = contract(unit["caseId"])
        require(unit["taskDigest"] == profile["identity"]["taskDigest"])
        require(
            report["experiment_id"] == manifest["experimentId"] and report["group"] == unit["group"]
        )
        require(report["adapter_sha256"] == manifest["adapterDigest"])
        for report_key, manifest_key in (
            ("cli_sha256", "cliHash"),
            ("cli_version", "cliVersion"),
            ("requested_model", "model"),
            ("max_turns", "maxTurns"),
            ("timeout_seconds", "timeoutSeconds"),
            ("max_budget_usd", "maxBudgetUsd"),
            ("effort", "effort"),
            ("tools", "tools"),
        ):
            require(report[report_key] == manifest["runtime"][manifest_key])
        require(
            report["skill_export"]
            == (manifest["projection"]["manifest"] if unit["group"] == "skill" else None)
        )
        require(report["case_asset_digest"] == unit["taskDigest"])
        require(report["events_sha256"] == sha(root / "events.jsonl"))
        require(report["receipt_sha256"] == sha(root / "validation/receipt.json"))
        require(Path(report["receipt"]).resolve() == root / "validation/receipt.json")
        rescored = score(report, root, unit["group"], old["elapsedSeconds"])
        require(all(old[k] == value for k, value in rescored.items()))
        receipt = verified_receipt(root)
        context = report["run_context"]
        require(
            receipt["runId"] == context["run_id"] and receipt["attemptId"] == context["attempt_id"]
        )
        require(receipt["candidateSnapshot"] == report["candidate_snapshot"])
        events = [parse(line) for line in (root / "events.jsonl").read_text().splitlines()]
        require(all(e["context"] == context for e in events))
        require(len({e["event_id"] for e in events}) == len(events))
        records = report["visible_checks"]
        require(bool(records))
        for index, record in enumerate(records, 1):
            require(record["check_index"] == index)
            require(record == read_json(root / "visible-checks" / f"check-{index}.json"))
            phase = root / "visible-checks" / f"phase-{index}"
            require(
                (phase / "program.py").read_text()
                == (root / "candidate/visible_checks.py").read_text()
            )
            require(parse(record["result"]["stdout"]) == read_json(phase / "report.json"))
        deliveries = _visible_deliveries(records, events, ())
        require(deliveries is not None, "contract_delivery_unverified")
        final = records[-1]
        require(
            final["candidate_snapshot"] == receipt["candidateSnapshot"],
            "contract_final_check_stale",
        )
        require(not final["result"]["execution_error"] and not final["result"]["timed_out"])
        public = assess_checks(
            profile, _rows(parse(final["result"]["stdout"])["checks"]), visibility="public"
        )
        hidden = assess_checks(profile, _rows(receipt["checks"]), visibility="hidden")
        declarations = [
            {
                "eventId": e["event_id"],
                **{k: e["payload"][k] for k in ("rule_id", "phase", "observation", "decision")},
            }
            for e in events
            if e["kind"] == "rule_declaration"
        ]
        rows.append(
            {
                **unit,
                "originalStatus": old["status"],
                "reportSha256": old["reportSha256"],
                "eventsSha256": report["events_sha256"],
                "receiptSha256": report["receipt_sha256"],
                "candidateSnapshot": receipt["candidateSnapshot"],
                "patchAccepted": report["task_accepted"],
                "nativeDeliveries": deliveries,
                "declarations": declarations,
                "publicEvidence": public,
                "hostEvidence": hidden,
                "fullSkillAcceptance": "needs_evidence",
                "ruleUsageReview": "requires_semantic_review",
                "publicationEvidence": False,
                "branchCoverage": [],
            }
        )
    value = {
        "protocol": "transfer-contract-audit-1",
        "experimentId": manifest["experimentId"],
        "manifestSha256": sha(batch / "manifest.json"),
        "resultsSha256": sha(batch / "results.json"),
        "catalogDigest": hash_json(catalog()),
        "rows": rows,
        "historicalResultsChanged": False,
        "publicationEvidence": False,
        "trustBoundary": "Local retained host artifacts; hashes establish binding, not third-party signatures.",
    }
    output.mkdir(parents=True, mode=0o700)
    write(output / "audit.json", {**value, "auditId": hash_json(value)})
    return {"auditId": hash_json(value), "units": len(rows), "publicationEvidence": False}
