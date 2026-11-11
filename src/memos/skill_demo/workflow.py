"""Reuse trusted collectors and Memory readback without importing private history."""

import importlib.util
import os
import re
import shutil
import sys
import uuid

from pathlib import Path

from memos.skill_contracts.audit import read_json
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_experience.evidence import verify_evidence
from memos.skill_transfer.verification import write


SOURCES = ("source-dream-update", "source-scoped-update")
PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
ENV_GROUPS = {
    "memory": ("MEMOS_SKILL_API_URL",),
    "generator": ("MEMOS_SKILL_LLM_BASE_URL", "MEMOS_SKILL_LLM_API_KEY", "MEMOS_SKILL_LLM_MODEL"),
    "graph": (
        "MEMOS_SKILL_GRAPH_URI",
        "MEMOS_SKILL_GRAPH_USER",
        "MEMOS_SKILL_GRAPH_PASSWORD",
        "MEMOS_SKILL_GRAPH_DATABASE",
        "MEMOS_SKILL_GRAPH_MEMORY_DB_ID",
    ),
}


def require(condition, code="demo_invalid_input"):
    if not condition:
        raise ValueError(code)


def load_config(path):
    value = read_json(path)
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "schemaVersion",
            "projectId",
            "skillId",
            "cliPath",
            "agentModel",
            "cubeId",
            "userId",
        }
    )
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1)
    require(
        all(
            isinstance(v, str) and v.strip() and len(v) <= 500 and "\x00" not in v
            for k, v in value.items()
            if k != "schemaVersion"
        )
    )
    for key in ("projectId", "skillId"):
        require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", value[key]) is not None)
    require(Path(value["cliPath"]).is_absolute())
    return value


def _manifest(config):
    return {
        "protocol": "fresh-skill-demo-1",
        "configDigest": hash_json(config),
        "projectId": config["projectId"],
        "sourceCases": [
            {"caseId": case, "taskDigest": case_digest(get_case(case))} for case in SOURCES
        ],
        "maxSourceAgentRuns": 2,
        "maxDraftCalls": 1,
        "automaticPublication": False,
    }


def init_demo(config, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    write(output / "manifest.json", _manifest(config))
    return {"status": "initialized", "projectId": config["projectId"], "modelCalls": 0}


def existing(config, output):
    output = Path(output).resolve()
    require(read_json(output / "manifest.json") == _manifest(config), "demo_manifest_changed")
    return output


def env(name):
    value = os.environ.get(name)
    require(isinstance(value, str) and bool(value.strip()), "demo_configuration_missing")
    return value


def bind_identity(output, name, identity):
    """Pin a stage's non-secret identity before external side effects.

    A changed endpoint must not create another Memory after an uncertain write.
    Credential rotation is deliberately excluded from connection identity.
    """
    path = output / (name + "-binding.json")
    if path.exists():
        require(read_json(path) == identity, "demo_binding_changed")
    else:
        write(path, identity)


def run_config(config):
    from memos.skill_adapter.runner import RunConfig, validate_config

    value = RunConfig(
        cli_path=Path(config["cliPath"]),
        model=config["agentModel"],
        max_turns=20,
        timeout_seconds=300,
        max_budget_usd=2,
    )
    validate_config(value)
    return value


def doctor(config=None):
    dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in ("pytest", "openai", "neo4j", "pydantic")
    }
    local = {
        "pythonSupported": sys.version_info >= (3, 11),
        "macOSSandbox": sys.platform == "darwin" and shutil.which("sandbox-exec") is not None,
        "dependencies": dependencies,
    }
    result = {
        "status": "ready"
        if all((local["pythonSupported"], local["macOSSandbox"], all(dependencies.values())))
        else "unavailable",
        "local": local,
        "modelCalls": 0,
        "connectionChecksPerformed": False,
        "configuredEnvironmentGroups": {
            k: all(os.environ.get(n) for n in names) for k, names in ENV_GROUPS.items()
        },
    }
    if config is not None:
        # A version probe is local and carries no model/provider credentials.
        import subprocess

        from memos.skill_adapter.runner import PINNED_CLI_VERSION

        # Doctor exists to locate a broken environment. A missing CLI, an invalid
        # run configuration or a probe failure is a finding, not an internal error:
        # failing the command here would hide the diagnosis the caller asked for.
        matches = False
        try:
            run_config(config)
            # A version probe is local and carries no model/provider credentials.
            probe = subprocess.run(
                [config["cliPath"], "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
            matches = (
                probe.returncode == 0
                and probe.stdout.strip() == PINNED_CLI_VERSION + " (Claude Code)"
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            matches = False
        result["claudePinnedVersionMatches"] = matches
        if not matches:
            result["status"] = "unavailable"
    return result


def selfcheck(output, progress):
    from memos.skill_experience.collection import prepare_preflight
    from memos.skill_transfer.verification import audit

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for case in SOURCES:
        prepare_preflight(case, output / case)
        progress({"stage": "source_preflight", "caseId": case, "status": "passed", "modelCalls": 0})
    result = audit(output / "transfer")
    summary = {
        "status": "passed" if result["passed"] else "failed",
        "sourcePreflights": 2,
        "transferVariants": len(result["rows"]),
        "modelCalls": 0,
    }
    write(output / "result.json", summary)
    return summary


def collect(config, output, progress):
    from memos.skill_experience.collection import collect_source

    output = existing(config, output)
    runtime = run_config(config)
    provider = {name: os.environ[name] for name in PROVIDER_KEYS if os.environ.get(name)}
    require(any(provider.get(name) for name in PROVIDER_KEYS[:3]), "demo_provider_missing")
    sources = output / "sources"
    sources.mkdir(mode=0o700)  # Exclusive claim before either model invocation.
    rows = []
    for case in SOURCES:
        progress({"stage": "collect_started", "caseId": case})
        path = collect_source(case, sources / case, runtime, provider_environment=provider)
        result = read_json(path)
        row = {"caseId": case, "status": result["status"], "qualification": result["qualification"]}
        rows.append(row)
        progress({"stage": "collect_finished", **row})
    summary = {
        "status": "completed" if all(r["status"] == "collected" for r in rows) else "incomplete",
        "rows": rows,
        "automaticRetry": False,
    }
    write(output / "collection-summary.json", summary)
    return summary


def source_entries(output):
    # Check the complete fixed set before verifying or posting any source.
    raw = [read_json(Path(output) / "sources" / case / "collection.json") for case in SOURCES]
    require(
        all(
            r["status"] == "collected" and r["case_id"] == case and r.get("evidence")
            for r, case in zip(raw, SOURCES, strict=True)
        ),
        "demo_source_incomplete",
    )
    entries = []
    for result, case in zip(raw, SOURCES, strict=True):
        path = Path(result["evidence"]).resolve()
        require(path.is_relative_to((Path(output) / "sources" / case / "evidence").resolve()))
        evidence = verify_evidence(path)
        require(
            evidence["qualification"] in {"qualified_success", "qualified_failure"}
            and evidence["case"]["case_id"] == case
            and evidence["case"]["split"] == "source",
            "demo_source_incomplete",
        )
        entries.append(
            {"evidence_path": str(path), "evidence_revision": evidence["evidence_revision"]}
        )
    return entries


def bridge(config, output):
    from memos.skill_experience.bridge import MemoryBridge
    from memos.skill_experience.client import ProductMemoryClient

    token = os.environ.get("MEMOS_SKILL_API_TOKEN")
    client = ProductMemoryClient(
        env("MEMOS_SKILL_API_URL"), headers={"Authorization": "Bearer " + token} if token else None
    )
    bind_identity(output, "memory", {"serviceIdentity": client.service_identity})
    return MemoryBridge(
        client,
        output / "memory-ledger.sqlite",
        output / "snapshots",
        cube_id=config["cubeId"],
        user_id=config["userId"],
        namespace="public-demo-" + config["projectId"],
    )


def ingest(config, output, progress):
    from memos.skill_drafting.sources import collect_packet

    output = existing(config, output)
    entries = source_entries(output)
    client = bridge(config, output)
    observations, planned = [], []
    for entry in entries:
        result = client.ingest(Path(entry["evidence_path"]))
        observations.append(result)
        progress(
            {
                "stage": "memory_readback",
                "status": result["status"],
                "memoryCount": len(result["memory_ids"]),
            }
        )
        planned.append({**entry, "memory_ids": result["memory_ids"]})
    # Preserve all attempts, including partial ingestion. Repeating this stage
    # reconciles the SAME durable claims; it never retries a source/model run.
    write(output / ("readback-" + uuid.uuid4().hex + ".json"), {"observations": observations})
    require(all(r["status"] == "completed" for r in observations), "demo_memory_incomplete")
    plan = {
        "schema_version": 1,
        "project_id": config["projectId"],
        "scope": {
            "service_identity": client.client.service_identity,
            "cube_id": client.cube_id,
            "user_id": client.user_id,
            "namespace": client.namespace,
        },
        "sources": planned,
    }
    packet = collect_packet(client, plan)  # Enforces two lineages and fresh GETs.
    path = output / "source-plan.json"
    if path.exists():
        require(read_json(path) == plan, "demo_source_plan_changed")
    else:
        write(path, plan)
    bind_identity(output, "source-plan", {"planDigest": hash_json(plan)})
    return {
        "status": "completed",
        "sourceCount": len(packet["sources"]),
        "lineageCount": len(packet["independent_lineages"]),
        "planDigest": hash_json(plan),
    }


def draft(config, output):
    from memos.skill_drafting.generation import OpenAIJSONGenerator
    from memos.skill_drafting.pipeline import draft as generate

    output = existing(config, output)
    plan = read_json(output / "source-plan.json")
    require(
        read_json(output / "source-plan-binding.json") == {"planDigest": hash_json(plan)},
        "demo_source_plan_changed",
    )
    require(not (output / "draft").exists(), "demo_draft_already_attempted")
    generator = OpenAIJSONGenerator(
        model=env("MEMOS_SKILL_LLM_MODEL"),
        api_key=env("MEMOS_SKILL_LLM_API_KEY"),
        base_url=env("MEMOS_SKILL_LLM_BASE_URL"),
        timeout=180,
        max_tokens=16000,
    )
    try:
        path = generate(
            bridge(config, output), plan, generator, output / "draft", skill_id=config["skillId"]
        )
    finally:
        generator.close()
    job = read_json(path)
    return {
        "status": job["status"],
        "qualification": job["qualification"],
        "failureStage": job.get("failure_stage"),
        "errorCode": job.get("error_code"),
        "automaticPublication": False,
    }


def graph(config, output):
    from memos.skill_drafting.artifacts import load_candidate
    from memos.skill_drafting.graph import SkillGraphRepository
    from memos.skill_drafting.graph_bundle import import_candidate

    output = existing(config, output)
    directory = output / "draft/candidate"
    loaded = load_candidate(directory)
    require(loaded["packet"]["project_id"] == config["projectId"])
    bind_identity(
        output,
        "graph",
        {
            "connectionDigest": hash_json(
                [
                    env("MEMOS_SKILL_GRAPH_URI"),
                    env("MEMOS_SKILL_GRAPH_DATABASE"),
                    env("MEMOS_SKILL_GRAPH_MEMORY_DB_ID"),
                ]
            )
        },
    )
    repo = SkillGraphRepository(
        env("MEMOS_SKILL_GRAPH_URI"),
        user=env("MEMOS_SKILL_GRAPH_USER"),
        password=env("MEMOS_SKILL_GRAPH_PASSWORD"),
        database=env("MEMOS_SKILL_GRAPH_DATABASE"),
        memory_database_id=env("MEMOS_SKILL_GRAPH_MEMORY_DB_ID"),
        project_id=config["projectId"],
    )
    try:
        result = import_candidate(directory, bridge(config, output), repo)
        candidate = loaded["candidate"]
        traces = [
            repo.trace_rule(
                candidate["skill_id"] + "@" + candidate["revision"] + "#" + r["rule_id"]
            )
            for r in candidate["rules"]
        ]
        value = {
            "status": "completed",
            "revision": candidate["revision"],
            "ruleCount": len(traces),
            "semanticReview": "pending",
            "publicationStatus": "candidate",
            "import": result,
            "traces": traces,
        }
        write(output / ("graph-" + uuid.uuid4().hex + ".json"), value)
        return {
            k: value[k]
            for k in ("status", "revision", "ruleCount", "semanticReview", "publicationStatus")
        }
    finally:
        repo.close()
