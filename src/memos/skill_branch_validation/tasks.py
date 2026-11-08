"""Explicit task assets and lock, independent from the frozen P4 registry."""

from __future__ import annotations

import json

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from memos.skill_drafting.domain import hash_json


@dataclass(frozen=True, kw_only=True)
class BranchTask:
    task_id: str
    revision: str
    lineage_id: str
    purpose: str
    initial_files: dict[str, str]
    reference_files: dict[str, str]
    unsafe_files: dict[str, str]
    editable_paths: tuple[str, ...]
    oracle: str
    required_checks: tuple[str, ...]
    expected_failures: dict[str, tuple[str, ...]]
    target_branches: tuple[str, ...]
    provenance: dict


LOCK = Path(__file__).with_name("tasks-lock.json")
CANDIDATE_REVISION = "f92905541710def3172d16e9c78d7226bf22e646b4c8baa73da499bf826e2a4c"


def task_digest(task: BranchTask) -> str:
    return hash_json(asdict(task))


def _tasks() -> tuple[BranchTask, ...]:
    from memos.skill_branch_validation.dream import dream_task
    from memos.skill_branch_validation.scoped import scoped_task

    return scoped_task(), dream_task()


def manifest() -> dict:
    """Maintainer-only lock construction; loading never rewrites the lock."""
    tasks = _tasks()
    if len({task.lineage_id for task in tasks}) != len(tasks):
        raise ValueError("branch_lineage_overlap")
    for task in tasks:
        if task.purpose != "branch_validation" or not task.required_checks:
            raise ValueError("invalid_branch_task")
        for files in (task.initial_files, task.reference_files, task.unsafe_files):
            if set(files) != set(task.initial_files):
                raise ValueError("branch_asset_inventory_changed")
            for name, value in files.items():
                path = PurePosixPath(name)
                if len(path.parts) != 1 or name in {"", ".", ".."} or not isinstance(value, str):
                    raise ValueError("invalid_branch_asset")
            if any(
                files[name] != task.initial_files[name]
                for name in files
                if name not in task.editable_paths
            ):
                raise ValueError("branch_protected_asset_changed")
    return {
        "schemaVersion": 1,
        "purpose": "branch_asset_freeze",
        "candidateRevision": CANDIDATE_REVISION,
        "tasks": [
            {"taskId": task.task_id, "lineageId": task.lineage_id, "digest": task_digest(task)}
            for task in tasks
        ],
    }


def list_tasks() -> tuple[BranchTask, ...]:
    if json.loads(LOCK.read_text()) != manifest():
        raise ValueError("branch_task_lock_changed")
    return _tasks()


def get_task(task_id: str) -> BranchTask:
    for task in list_tasks():
        if task.task_id == task_id:
            return task
    raise ValueError("unknown_branch_task")


def materialize(task_id: str, destination: Path, variant: str = "initial") -> Path:
    task = get_task(task_id)
    if variant not in {"initial", "reference", "unsafe"}:
        raise ValueError("unknown_branch_variant")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in getattr(task, variant + "_files").items():
        path = destination / name
        path.write_text(content)
        path.chmod(0o644)
    return destination
