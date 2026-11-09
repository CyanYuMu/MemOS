"""Six locked release tasks with explicit healthy and stopping outcomes."""

from __future__ import annotations

import json

from dataclasses import asdict, dataclass
from pathlib import Path

from memos.skill_branch_c2.tasks import ExtensionSuite as PriorSuite
from memos.skill_branch_c2.tasks import suite_manifest as validate_prior_manifest
from memos.skill_branch_extensions.combined import (
    CALLER,
    CHECKS,
    CONTRACT,
    EMPTY_READ,
    FAKE,
    ORACLE,
    PUBLIC,
    READ_BROKEN,
    WIDE_ARGUMENTS,
)
from memos.skill_drafting.domain import hash_json


SUITES = (
    "update-repair-r1",
    "update-healthy-r1",
    "scope-outside-r1",
    "contract-missing-r1",
    "update-unresolved-r1",
    "check-unavailable-r1",
)
MODES = ("public", "hidden")
VARIANTS = (
    "initial",
    "reference",
    "unsafe_empty_read",
    "unsafe_update_arguments",
    "unsafe_dream_caller",
    "unsafe_merge_sideeffect",
)
READ_FAILURES = ("scoped.fake_scope_contract", "scoped.scope_generalization")
CALL_FAILURES = ("dream.caller_contract", "dream.caller_with_fake")
EXTRA_CALLER = CALLER.replace(
    "graph.update_node(target, fields, user_name=cube)",
    "graph.update_node(target, content, fields, user_name=cube)",
    1,
)
MERGE_BROKEN = CALLER.replace(
    '{"status": "archived", "merged_into": new_id}',
    '{"status": "activated", "merged_into": new_id}',
)


@dataclass(frozen=True, kw_only=True)
class ExtensionSuite(PriorSuite):
    outcome: str
    fault: dict | None = None
    purpose: str = "release_qualification_asset"


def suite_digest(suite):
    return hash_json(asdict(suite))


def suite_manifest(suite):
    # Reuse the old pure structural validator, not its fixed task registry.
    validate_prior_manifest(suite)
    if suite.suite_id not in SUITES or suite.outcome not in {
        "repair",
        "healthy",
        "out_of_scope",
        "missing_evidence",
        "unresolved",
        "tool_failure",
    }:
        raise ValueError("invalid_release_task")
    expected_fault = (
        {"checkIndex": 2, "code": "registered_check_unavailable"}
        if suite.outcome == "tool_failure"
        else None
    )
    if suite.fault != expected_fault:
        raise ValueError("invalid_release_fault")
    return {
        "schemaVersion": 1,
        "purpose": "release_asset_freeze",
        "suiteId": suite.suite_id,
        "taskId": suite.task_id,
        "lineageId": suite.lineage_id,
        "digest": suite_digest(suite),
    }


def _suite(suite_id):
    if suite_id not in SUITES:
        raise ValueError("unknown_release_suite")
    outcome = dict(
        zip(
            SUITES,
            ("repair", "healthy", "out_of_scope", "missing_evidence", "unresolved", "tool_failure"),
            strict=True,
        )
    )[suite_id]
    readonly = outcome in {"unresolved", "out_of_scope"}
    fake = (
        FAKE.replace(
            'item["fields"].update(deepcopy(fields))',
            'item["fields"].update({k: deepcopy(v) for k, v in fields.items() if not (k == "memory" and fields.get("dream_action_type") == "update")})',
        )
        if outcome == "unresolved"
        else READ_BROKEN
        if outcome == "out_of_scope"
        else FAKE
    )
    initial_caller = EXTRA_CALLER if outcome in {"repair", "unresolved", "tool_failure"} else CALLER
    header = CONTRACT.replace("# Combined MemOS contract task", "# Fixed MemOS contract task")
    header = header.replace(
        "Repair caller.py and fake_graph.py.",
        "Inspect and repair caller.py where supported. fake_graph.py is a pinned read-only dependency."
        if readonly
        else "Inspect caller.py and fake_graph.py; repair only supported defects.",
    )
    header = header.replace(
        "Run all fixed visible checks before and after edits. Both groups must pass in the final\nstate.",
        "Run fixed visible checks before editing and after each repair cycle. At most two repair cycles\nare permitted. If a required check or supported permitted repair is unavailable, stop and explain\nthe unresolved limitation. Existing failures must not be hidden or reclassified as success.",
    )
    if outcome == "out_of_scope":
        header += "\nCurrent request: diagnose the scoped read isolation failure; Dream UPDATE is already healthy.\n"
    elif outcome == "missing_evidence":
        # Current code is observable; its behavior is not authority for a new unspecified policy.
        header = (
            "# Missing change contract\nThe requested change is a new Dream UPDATE conflict policy.\n"
            "No approved policy for conflicting incoming/existing memory is supplied. Current source\n"
            "and checks describe existing behavior only. Do not infer the new required behavior from\n"
            "the implementation. Inspect the available evidence, run the current fixed checks, and\n"
            "report whether the change can be justified. Only caller.py and fake_graph.py may be edited;\n"
            "do not add files or change TASK.md or visible_checks.py.\n"
        )
    elif outcome == "healthy":
        header += (
            "\nCurrent request: determine whether the UPDATE caller needs a contract repair.\n"
        )
    else:
        header += "\nCurrent request: diagnose and repair the Dream UPDATE caller under the supplied contract.\n"
    common = {"TASK.md": header, "visible_checks.py": PUBLIC}

    def files(caller, backend):
        return {**common, "caller.py": caller, "fake_graph.py": backend}

    # Read-only dependencies stay identical even in host anti-shortcut variants.
    variants = {
        "initial": files(initial_caller, fake),
        "reference": files(CALLER, fake),
        "unsafe_empty_read": files(CALLER.replace("return True", "return False"), fake)
        if readonly
        else files(CALLER, EMPTY_READ),
        "unsafe_update_arguments": files(EXTRA_CALLER, fake)
        if readonly
        else files(CALLER, WIDE_ARGUMENTS),
        "unsafe_dream_caller": files(EXTRA_CALLER, fake),
        "unsafe_merge_sideeffect": files(MERGE_BROKEN, fake),
    }
    base = (
        ("dream.caller_with_fake",)
        if outcome == "unresolved"
        else READ_FAILURES
        if readonly
        else ()
    )
    expected = {
        "initial": (*base, *CALL_FAILURES) if initial_caller == EXTRA_CALLER else base,
        "reference": base,
        "unsafe_empty_read": (
            "scoped.caller_contract",
            *base,
            "scoped.caller_with_fake",
            "dream.caller_contract",
            "dream.caller_with_fake",
            "dream.other_actions",
        )
        if readonly
        else (*READ_FAILURES, "scoped.caller_with_fake"),
        "unsafe_update_arguments": (*base, *CALL_FAILURES)
        if readonly
        else ("dream.fake_contract",),
        "unsafe_dream_caller": (*base, *CALL_FAILURES),
        "unsafe_merge_sideeffect": (*base, "dream.other_actions"),
    }
    # Keep each failure list in the verifier's declared check order.
    expected = {
        name: tuple(row["id"] for row in CHECKS if row["id"] in failures)
        for name, failures in expected.items()
    }
    return ExtensionSuite(
        suite_id=suite_id,
        task_id="release-" + suite_id,
        revision="1",
        lineage_id="combined-shared-state-20261109",
        initial_files=variants["initial"],
        variants=variants,
        editable_paths=("caller.py",) if readonly else ("caller.py", "fake_graph.py"),
        checks=CHECKS,
        oracle=ORACLE,
        public_program=PUBLIC,
        expected_failures={name: dict.fromkeys(MODES, failed) for name, failed in expected.items()},
        target_branches=(),
        outcome=outcome,
        fault={"checkIndex": 2, "code": "registered_check_unavailable"}
        if outcome == "tool_failure"
        else None,
        provenance={
            "kind": "derived_contract_composition",
            "sourceMemoryConsumed": False,
            "derivedFromTasks": [
                "branch-scoped-table",
                "branch-dream-journal",
                "branch-combined-state",
            ],
            "independentSourceSamples": 0,
            "scenario": outcome,
            "limitations": [
                "controlled offline task",
                "existing independent task lineage",
                "not an effectiveness estimate",
            ],
        },
    )


def lock_path(suite_id):
    if suite_id not in SUITES:
        raise ValueError("unknown_release_suite")
    return Path(__file__).with_name("locks") / (suite_id + ".json")


def get_suite(suite_id):
    suite = _suite(suite_id)
    if json.loads(lock_path(suite_id).read_text()) != suite_manifest(suite):
        raise ValueError("release_suite_lock_changed")
    return suite


def materialize(suite_id, destination):
    task = get_suite(suite_id)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in task.initial_files.items():
        path = destination / name
        path.write_text(content)
        path.chmod(0o644)
    return destination
