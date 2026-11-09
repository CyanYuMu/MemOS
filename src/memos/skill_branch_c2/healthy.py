"""C2 derives one scoped defect while Dream starts contract compliant."""

from memos.skill_branch_c2.tasks import ExtensionSuite
from memos.skill_branch_extensions.combined import (
    BROKEN_CALLER,
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


# The frozen C1 programs exercise shared-state contracts with separate public and
# hidden inputs. C2 changes the starting defect, not the acceptance oracle.
C2_CONTRACT = CONTRACT.replace("# Combined MemOS contract task", "# MemOS shared contract task")
MERGE_BROKEN = CALLER.replace(
    '{"status": "archived", "merged_into": new_id}',
    '{"status": "activated", "merged_into": new_id}',
)


def healthy_suite() -> ExtensionSuite:
    common = {"TASK.md": C2_CONTRACT, "visible_checks.py": PUBLIC}

    def files(caller, fake):
        return {**common, "caller.py": caller, "fake_graph.py": fake}

    variants = {
        "initial": files(CALLER, READ_BROKEN),
        "reference": files(CALLER, FAKE),
        "unsafe_empty_read": files(CALLER, EMPTY_READ),
        "unsafe_update_arguments": files(CALLER, WIDE_ARGUMENTS),
        "unsafe_dream_caller": files(BROKEN_CALLER, FAKE),
        "unsafe_merge_sideeffect": files(MERGE_BROKEN, FAKE),
    }
    read_failures = ("scoped.fake_scope_contract", "scoped.scope_generalization")
    expected = {
        "initial": read_failures,
        "reference": (),
        "unsafe_empty_read": (*read_failures, "scoped.caller_with_fake"),
        "unsafe_update_arguments": ("dream.fake_contract",),
        "unsafe_dream_caller": ("dream.caller_contract", "dream.caller_with_fake"),
        "unsafe_merge_sideeffect": ("dream.other_actions",),
    }
    return ExtensionSuite(
        suite_id="scoped-healthy-dream-c2-v1",
        task_id="branch-scoped-healthy-dream",
        revision="1",
        lineage_id="combined-shared-state-20261109",
        initial_files=variants["initial"],
        variants=variants,
        editable_paths=("caller.py", "fake_graph.py"),
        checks=CHECKS,
        oracle=ORACLE,
        public_program=PUBLIC,
        expected_failures={
            name: {"public": failed, "hidden": failed} for name, failed in expected.items()
        },
        target_branches=(
            "confirm_contract#3",
            "repair_scoped_reads#0",
            "verify_scoped_reads#1",
            "verify_dream_update#0",
        ),
        provenance={
            "kind": "derived_contract_composition",
            "sourceMemoryConsumed": False,
            "derivedFromTasks": [
                "branch-scoped-table",
                "branch-dream-journal",
                "branch-combined-state",
            ],
            "composition": "same shared-state contracts with scoped defect and initially healthy Dream",
            "independentSourceSamples": 0,
            "scenario": "scoped_read_defect_with_healthy_dream",
            "limitations": [
                "offline controlled state; not a production API replica",
                "asset qualification grants no Agent or branch evidence",
                "shares C1 lineage; not an additional independent support sample",
            ],
        },
    )
