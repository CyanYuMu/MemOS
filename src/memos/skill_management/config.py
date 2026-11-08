"""Private host configuration; never accepted through the browser API."""

from __future__ import annotations

import fcntl
import json
import tempfile

from contextlib import contextmanager
from pathlib import Path

from memos.skill_management.domain import require


def read_config(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    path = Path(path).resolve()
    require(path.is_file() and path.stat().st_size <= 1000000)
    require(path.stat().st_mode & 0o077 == 0, "private_config_required")
    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    require(
        isinstance(value, dict)
        and set(value)
        == {"serviceToken", "listen", "graph", "memory", "projects", "runtime", "artifactsRoot"}
    )
    require(isinstance(value["serviceToken"], str) and len(value["serviceToken"]) >= 32)
    require(not any(c.isspace() for c in value["serviceToken"]))
    require(set(value["listen"]) == {"host", "port"} and value["listen"]["host"] == "127.0.0.1")
    require(type(value["listen"]["port"]) is int and 1024 <= value["listen"]["port"] <= 65535)
    require(set(value["graph"]) == {"uri", "user", "password", "database", "memory_database_id"})
    require(
        set(value["memory"])
        in ({"url", "ledger", "snapshots"}, {"url", "ledger", "snapshots", "token"})
    )
    require(isinstance(value["projects"], list) and 0 < len(value["projects"]) <= 20)
    seen, graph_projects = set(), set()
    for project in value["projects"]:
        require(
            {"projectId", "graphProject", "candidates"}
            <= set(project)
            <= {"projectId", "graphProject", "candidates", "experiments"}
        )
        experiments = project.get("experiments", [])
        require(isinstance(experiments, list) and len(experiments) <= 20)
        require(all(isinstance(path, str) and Path(path).is_absolute() for path in experiments))
        require(len(set(experiments)) == len(experiments))
        require(
            isinstance(project["projectId"], str)
            and project["projectId"]
            and project["projectId"] not in seen
        )
        seen.add(project["projectId"])
        require(
            isinstance(project["graphProject"], str)
            and project["graphProject"] not in graph_projects
        )
        graph_projects.add(project["graphProject"])
        require(isinstance(project["candidates"], list) and 0 < len(project["candidates"]) <= 100)
        for candidate in project["candidates"]:
            require(set(candidate) == {"directory", "challenges"})
            require(
                Path(candidate["directory"]).is_absolute()
                and isinstance(candidate["challenges"], list)
            )
    runtime = value["runtime"]
    require(
        set(runtime)
        == {"cliPath", "model", "timeoutSeconds", "maxTurns", "maxBudgetUsd", "providerEnvironment"}
    )
    provider = runtime["providerEnvironment"]
    require(
        isinstance(provider, dict)
        and set(provider)
        <= {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        }
    )
    require(all(isinstance(v, str) and v for v in provider.values()))
    require(
        not provider.get("ANTHROPIC_BASE_URL", "").rstrip("/").endswith("/v1/messages"),
        "provider_base_url_required",
    )
    require(Path(value["artifactsRoot"]).is_absolute())
    return value


@contextmanager
def service_lock(config):
    """One local owner per graph database, including alternate config files.

    P3 is a single-host service. Multi-host workers need a distributed lease
    before recovery can safely distinguish an abandoned job from a live one.
    """
    # Ignore endpoint aliases: one local authority owns all P3 job recovery.
    path = Path(tempfile.gettempdir()) / "memos-skill-management-authority.lock"
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("service_already_running") from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def build_service(config):
    from memos.skill_adapter.runner import RunConfig, validate_config
    from memos.skill_drafting.artifacts import load_candidate
    from memos.skill_drafting.graph_bundle import import_candidate
    from memos.skill_drafting.sources import recheck_packet
    from memos.skill_experience.bridge import MemoryBridge
    from memos.skill_experience.client import ProductMemoryClient
    from memos.skill_management.repository import ManagementRepository
    from memos.skill_management.runtime import export_identity
    from memos.skill_management.service import Project, SkillService

    runtime = config["runtime"]
    run_config = RunConfig(
        cli_path=Path(runtime["cliPath"]),
        model=runtime["model"],
        timeout_seconds=runtime["timeoutSeconds"],
        max_turns=runtime["maxTurns"],
        max_budget_usd=runtime["maxBudgetUsd"],
    )
    validate_config(run_config)
    memory = config["memory"]
    require(Path(memory["ledger"]).is_file() and Path(memory["snapshots"]).is_dir())
    client = ProductMemoryClient(
        memory["url"],
        timeout=20,
        headers={"Authorization": "Bearer " + memory["token"]} if memory.get("token") else None,
    )
    projects = {}
    repositories = []

    def sources(detail):
        loaded = load_candidate(Path(detail["artifactDirectory"]))
        require(loaded["candidate"] == detail["candidate"], "artifact_mismatch", 409)
        packet = loaded["packet"]
        scope = packet["scope"]
        require(client.service_identity == scope["service_identity"], "source_scope_mismatch", 409)
        bridge = MemoryBridge(
            client,
            Path(memory["ledger"]),
            Path(memory["snapshots"]),
            cube_id=scope["cube_id"],
            user_id=scope["user_id"],
            namespace=scope["namespace"],
        )
        recheck_packet(bridge, packet)
        return packet

    try:
        for setting in config["projects"]:
            repo = ManagementRepository(project_id=setting["graphProject"], **config["graph"])
            repositories.append(repo)
            repo.initialize()
            candidates = []
            for registration in setting["candidates"]:
                directory = Path(registration["directory"]).resolve()
                loaded = load_candidate(directory)
                packet = loaded["packet"]
                scope = packet["scope"]
                require(client.service_identity == scope["service_identity"])
                bridge = MemoryBridge(
                    client,
                    Path(memory["ledger"]),
                    Path(memory["snapshots"]),
                    cube_id=scope["cube_id"],
                    user_id=scope["user_id"],
                    namespace=scope["namespace"],
                )
                import_candidate(directory, bridge, repo)
                repo.register_candidate(
                    loaded["candidate"],
                    loaded["manifest"],
                    directory,
                    challenges=registration["challenges"],
                )
                candidates.append(loaded["candidate"])
            repo.recover_jobs()
            for candidate in candidates:
                detail = repo.get_revision(candidate["skill_id"], candidate["revision"])
                projected = export_identity(candidate)
                if (
                    detail["management"].get("exportHash") != projected["exportHash"]
                    or detail["management"].get("projectionVersion")
                    != projected["projectionVersion"]
                ):
                    repo.bind_export(
                        candidate["skill_id"],
                        candidate["revision"],
                        export_hash=projected["exportHash"],
                        projection_version=projected["projectionVersion"],
                        expected_sequence=detail["management"]["sequence"],
                        operation_id="export-"
                        + projected["projectionVersion"]
                        + "-"
                        + projected["exportHash"],
                        actor="host_export_registration",
                        reason="Bind the fixed host projection; prior validation records remain immutable.",
                    )
            projects[setting["projectId"]] = Project(repo, sources)
        service = SkillService(
            projects, Path(config["artifactsRoot"]), run_config, runtime["providerEnvironment"]
        )
        service.configure_experiments(
            {p["projectId"]: p.get("experiments", []) for p in config["projects"]}
        )
        return service
    except Exception:
        for repo in repositories:
            repo.close()
        raise
