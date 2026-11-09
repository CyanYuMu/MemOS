"""Load one locked suite without binding unrelated future suites to its identity."""

from __future__ import annotations

import json
import re

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from memos.skill_drafting.domain import hash_json


VARIANTS = (
    "initial",
    "reference",
    "unsafe_empty_read",
    "unsafe_update_arguments",
    "unsafe_dream_caller",
    "unsafe_merge_sideeffect",
)
MODES = ("public", "hidden")


@dataclass(frozen=True, kw_only=True)
class ExtensionSuite:
    suite_id: str
    task_id: str
    revision: str
    lineage_id: str
    initial_files: dict[str, str]
    variants: dict[str, dict[str, str]]
    editable_paths: tuple[str, ...]
    checks: tuple[dict[str, str], ...]
    oracle: str
    public_program: str
    expected_failures: dict[str, dict[str, tuple[str, ...]]]
    target_branches: tuple[str, ...]
    provenance: dict
    purpose: str = "branch_extension_asset"


def suite_digest(suite: ExtensionSuite) -> str:
    return hash_json(asdict(suite))


def suite_manifest(suite: ExtensionSuite) -> dict:
    """Maintainer lock construction; normal loading never rewrites a lock."""
    if set(suite.variants) != set(VARIANTS) or suite.variants["initial"] != suite.initial_files:
        raise ValueError("invalid_extension_variants")
    if not suite.editable_paths or not set(suite.editable_paths) < set(suite.initial_files):
        raise ValueError("invalid_extension_editable_paths")
    check_ids = [row["id"] for row in suite.checks]
    if len(set(check_ids)) != len(check_ids) or not check_ids:
        raise ValueError("invalid_extension_checks")
    for row in suite.checks:
        if (
            set(row) != {"id", "group", "ruleCheck"}
            or row["group"] not in {"scoped", "dream"}
            or row["id"] != row["group"] + "." + row["ruleCheck"]
            or not re.fullmatch(r"[a-z_]+", row["ruleCheck"])
        ):
            raise ValueError("invalid_extension_check_mapping")
    if suite.public_program != suite.initial_files.get("visible_checks.py"):
        raise ValueError("extension_public_program_mismatch")
    for variant, files in suite.variants.items():
        if set(files) != set(suite.initial_files):
            raise ValueError("extension_file_inventory_changed")
        for name, content in files.items():
            path = PurePosixPath(name)
            if (
                len(path.parts) != 1
                or path.as_posix() != name
                or "\\" in name
                or name in {"", ".", ".."}
                or not isinstance(content, str)
                or any(ord(c) < 32 for c in name)
            ):
                raise ValueError("invalid_extension_asset")
            if name not in suite.editable_paths and content != suite.initial_files[name]:
                raise ValueError("extension_protected_asset_changed")
        expectations = suite.expected_failures[variant]
        if set(expectations) != set(MODES):
            raise ValueError("invalid_extension_expectations")
        for failed in expectations.values():
            if tuple(name for name in check_ids if name in failed) != failed:
                raise ValueError("invalid_extension_expected_checks")
    return {
        "schemaVersion": 1,
        "purpose": "branch_extension_asset_freeze",
        "suiteId": suite.suite_id,
        "taskId": suite.task_id,
        "lineageId": suite.lineage_id,
        "digest": suite_digest(suite),
    }


def lock_path(suite_id: str) -> Path:
    if suite_id != "scoped-healthy-dream-c2-v1":
        raise ValueError("unknown_extension_suite")
    return Path(__file__).with_name("locks") / (suite_id + ".json")


def get_suite(suite_id: str) -> ExtensionSuite:
    path = lock_path(suite_id)
    from memos.skill_branch_c2.healthy import healthy_suite

    suite = healthy_suite()
    if json.loads(path.read_text()) != suite_manifest(suite):
        raise ValueError("extension_suite_lock_changed")
    return suite


def materialize(suite_id: str, destination: Path) -> Path:
    """The public exporter deliberately has no variant argument."""
    suite = get_suite(suite_id)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in suite.initial_files.items():
        path = destination / name
        path.write_text(content)
        path.chmod(0o644)
    return destination
