"""Map reviewed assertions, not check-name similarity, to bounded obligations."""

import copy
import json

from pathlib import Path

from memos.skill_drafting.domain import hash_json


def task_identity(case_id):
    if case_id.startswith("transfer-"):
        from memos.skill_transfer.tasks import case_digest, get_case
        from memos.skill_transfer.verification import verifier_digest

        return {"taskDigest": case_digest(get_case(case_id)), "verifierDigest": verifier_digest()}
    if case_id == "p5-update-repair-r1":
        from memos.skill_p5.tasks import get_suite, suite_digest
        from memos.skill_p5.verifier import implementation_digest

        return {
            "taskDigest": suite_digest(get_suite("update-repair-r1")),
            "verifierDigest": implementation_digest(),
        }
    raise ValueError("contract_unknown_task")


def catalog():
    return json.loads(Path(__file__).with_name("catalog.json").read_text())


def contract(case_id):
    value = catalog()
    if case_id not in value["tasks"]:
        raise ValueError("contract_unknown_task")
    profile = copy.deepcopy(value["tasks"][case_id])
    if profile["identity"] != task_identity(case_id):
        raise ValueError("contract_implementation_changed")
    return {"caseId": case_id, **profile}


def assess_checks(profile, rows, *, visibility):
    """This assesses trusted normalized observations, not receipt authenticity.

    The audit caller must first bind receipt, final snapshot and native delivery.
    A complete check inventory and complete semantics are separate facts: P5's
    public checks omit some inputs that only its hidden verifier exercises.
    """
    if profile != contract(profile["caseId"]):
        raise ValueError("contract_mapping_changed")
    if visibility not in {"public", "hidden"}:
        raise ValueError("contract_invalid_visibility")
    mapping = profile[visibility]
    valid = isinstance(rows, list) and all(
        isinstance(row, dict)
        and set(row) == {"name", "status"}
        and isinstance(row["name"], str)
        and row["name"] in mapping
        and row["status"] in ("passed", "failed", "error")
        for row in rows
    )
    names = [row["name"] for row in rows] if valid else []
    valid = valid and len(names) == len(set(names))
    complete = valid and set(names) == set(mapping)
    all_passed = complete and all(row["status"] == "passed" for row in rows)
    supported = set()
    if valid:
        for row in rows:
            if row["status"] == "passed":
                supported.update(mapping[row["name"]])
    missing = sorted(set(catalog()["obligations"]) - supported)
    return {
        "visibility": visibility,
        "observationsValid": valid,
        "inventoryComplete": complete,
        "allPassed": all_passed,
        "supportedObligations": sorted(supported),
        "missingObligations": missing,
        "semanticsComplete": all_passed and not missing,
        "mappingDigest": hash_json(profile),
        "scopeDifferences": profile["scopeDifferences"],
    }
