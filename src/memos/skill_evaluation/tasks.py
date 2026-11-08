"""Trusted task registry and visible export policy, not an Agent security boundary."""

from __future__ import annotations

import hashlib
import json
import re

from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from memos.skill_evaluation import cases as dev_cases


if TYPE_CHECKING:
    from memos.skill_evaluation.cases import CaseSpec


LOCK_PATH = Path(__file__).parent / "assets" / "tasks-lock.json"
VARIANTS = ("baseline", "initial", "reference", "unsafe")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def case_digest(case: CaseSpec) -> str:
    """Bind every task field, including policy, scoring, provenance and all variants."""
    return _digest(
        json.dumps(asdict(case), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def variant_files(case: CaseSpec, variant: str) -> dict[str, str]:
    if variant not in VARIANTS:
        raise ValueError(f"Unknown task variant: {variant}")
    if variant == "baseline":
        return case.reference_files if case.baseline_files is None else case.baseline_files
    return getattr(case, f"{variant}_files")


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and value != "."
        and "\\" not in value
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", "..", ".git"} for part in path.parts)
    )


def validate_specs(specs: tuple[CaseSpec, ...]) -> None:
    """Reject invalid exports and declared overlap with any holdout lineage."""
    ids = set()
    families = []
    for case in specs:
        if case.case_id in ids:
            raise ValueError(f"Duplicate case ID: {case.case_id}")
        ids.add(case.case_id)
        if not re.fullmatch(r"[a-z0-9-]+", case.case_id):
            raise ValueError("Invalid case ID")
        if case.split not in {"dev", "source", "holdout"}:
            raise ValueError("Invalid task split")
        if not case.lineage_id or not case.revision:
            raise ValueError("Missing task lineage or revision")
        if case.assessment_kind not in {"repair", "boundary_repair", "manual_boundary"}:
            raise ValueError("Invalid assessment kind")
        if case.assessment_kind != "repair" and not case.manual_rubric:
            raise ValueError("Boundary tasks require a manual rubric")
        if not case.required_checks or len(set(case.required_checks)) != len(case.required_checks):
            raise ValueError("Invalid required check set")
        for paths in (
            *(variant_files(case, variant) for variant in VARIANTS),
            case.editable_paths,
            case.imported_modules.values(),
        ):
            if any(not isinstance(path, str) or not _safe_path(path) for path in paths):
                raise ValueError("Invalid task export path")
        for variant in VARIANTS:
            files = variant_files(case, variant)
            if any(not isinstance(content, str) for content in files.values()):
                raise ValueError("Task files must be text")
            for name in files:
                if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
                    raise ValueError("Conflicting task export path")
        family = {case.lineage_id, *case.related_lineages}
        for previous, ancestors in families:
            if "holdout" in (case.split, previous.split) and family & ancestors:
                raise ValueError(f"Holdout lineage overlap: {case.case_id}/{previous.case_id}")
        families.append((case, family))


def _p0b_specs() -> tuple[CaseSpec, ...]:
    # Lazy imports preserve the small, public P0a entry points.
    from memos.skill_evaluation.holdout_tasks import HOLDOUT_CASE_IDS, get_holdout_case
    from memos.skill_evaluation.source_tasks import SOURCE_CASE_IDS, get_source_case

    return tuple(get_source_case(name) for name in SOURCE_CASE_IDS) + tuple(
        get_holdout_case(name) for name in HOLDOUT_CASE_IDS
    )


def registry_manifest(specs: tuple[CaseSpec, ...]) -> dict:
    """Build a reviewable maintenance manifest; never silently refresh the shipped lock."""
    validate_specs((*dev_cases.list_cases(), *specs))
    return {
        "schema_version": 1,
        "purpose": "task_asset_freeze",
        "cases": [
            {
                "case_id": case.case_id,
                "revision": case.revision,
                "split": case.split,
                "lineage_id": case.lineage_id,
                "related_lineages": list(case.related_lineages),
                "assessment_kind": case.assessment_kind,
                "asset_digest": case_digest(case),
                "provenance": case.provenance,
                "variants": {
                    variant: {
                        name: _digest(content.encode())
                        for name, content in sorted(variant_files(case, variant).items())
                    }
                    for variant in VARIANTS
                },
            }
            for case in sorted(specs, key=lambda spec: spec.case_id)
        ],
    }


def _locked_specs() -> tuple[CaseSpec, ...]:
    specs = _p0b_specs()
    expected = registry_manifest(specs)
    if json.loads(LOCK_PATH.read_text(encoding="utf-8")) != expected:
        raise ValueError("Task registry lock mismatch: review the revision and frozen assets")
    return specs


def list_cases(split: str = "dev") -> tuple[CaseSpec, ...]:
    """Read authoritative task purposes; there is no relabel operation."""
    if split not in {"dev", "source", "holdout", "all"}:
        raise ValueError("Unknown task split")
    if split == "dev":
        return dev_cases.list_cases()
    cases = (*dev_cases.list_cases(), *_locked_specs())
    return tuple(case for case in cases if split == "all" or case.split == split)


def get_case(case_id: str) -> CaseSpec:
    if case_id in {case.case_id for case in dev_cases.list_cases()}:
        return dev_cases.get_case(case_id)
    for case in _locked_specs():
        if case.case_id == case_id:
            return case
    raise ValueError(f"Unknown registered task: {case_id}")


def materialize_case(case_id: str, destination: Path, variant: str = "initial") -> Path:
    """Maintainer export; the CLI only exposes initial tasks, never reference variants."""
    case = get_case(case_id)
    if case.split == "dev":
        return dev_cases.materialize_case(
            case_id, destination, "reference" if variant == "baseline" else variant
        )
    files = variant_files(case, variant)
    validate_specs((case,))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return destination


def candidate_policy_violations(case: CaseSpec, candidate: Path) -> list[str]:
    """Enforce P0b's positive file list and byte-exact protected inputs."""
    if case.split == "dev":
        return []  # Preserve the original public probe contract.
    allowed = set(case.initial_files) | set(case.editable_paths)
    directories = {
        parent.as_posix()
        for name in allowed
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    violations = []
    actual = set()
    for path in sorted(candidate.rglob("*")):
        name = path.relative_to(candidate).as_posix()
        if path.is_symlink():
            violations.append(f"symlink:{name}")
        elif path.is_dir():
            if name not in directories:
                violations.append(f"unknown_directory:{name}")
        elif name not in allowed or not path.is_file():
            violations.append(f"unknown_file:{name}")
        else:
            actual.add(name)
            if name not in case.editable_paths and (
                path.read_bytes() != case.initial_files[name].encode()
                or path.stat().st_mode & 0o111
            ):
                violations.append(f"protected_file_changed:{name}")
    violations.extend(f"missing_file:{name}" for name in sorted(set(case.initial_files) - actual))
    return violations


def score_result(case: CaseSpec, status: str) -> dict:
    """Keep automatic behavior separate from unobserved diagnosis and manual judgement."""
    return {
        "repair_pass": (
            None
            if case.assessment_kind == "manual_boundary" or status == "error"
            else int(status == "passed")
        ),
        "boundary_pass": None,
        "branch_supported": None,
        "critical_violation": None,
        "review_required": case.assessment_kind != "repair",
        "manual_rubric": list(case.manual_rubric),
        "outcome": (
            "needs_review"
            if status == "needs_review"
            else "verified_fixed"
            if status == "passed"
            else "unknown"
            if status == "error"
            else "unresolved"
        ),
    }
