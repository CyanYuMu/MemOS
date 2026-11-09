"""Private CLI wiring for server-issued execution tickets; no browser paths."""

from contextlib import contextmanager

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_experience.client import ProductMemoryClient
from memos.skill_lifecycle.execution import make_execution_guard
from memos.skill_lifecycle.observers import HostObserver
from memos.skill_management.config import read_config
from memos.skill_management.domain import require
from memos.skill_management.repository import ManagementRepository


@contextmanager
def execution_guard(config_path, project_id, candidate_directory):
    config = read_config(config_path)
    settings = next((p for p in config["projects"] if p["projectId"] == project_id), None)
    require(settings is not None, "project_not_found", 404)
    candidate = load_candidate(candidate_directory)["candidate"]
    memory = config["memory"]
    client = ProductMemoryClient(
        memory["url"],
        timeout=3,
        headers={"Authorization": "Bearer " + memory["token"]} if memory.get("token") else None,
    )
    repo = ManagementRepository(project_id=settings["graphProject"], **config["graph"])
    try:
        detail = repo.get_revision(candidate["skill_id"], candidate["revision"])
        require(detail["candidate"] == candidate, "execution_candidate_mismatch", 409)
        yield make_execution_guard(repo, HostObserver(client), candidate)
    finally:
        repo.close()
