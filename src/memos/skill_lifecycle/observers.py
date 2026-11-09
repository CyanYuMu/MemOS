"""Read-only host probes. Private locators never enter the public projection."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys

from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from memos.skill_drafting.domain import hash_json
from memos.skill_experience.client import MemoryClientError
from memos.skill_management.domain import require


DETECTOR_VERSION = "lifecycle-observer-1"


def memory_subject(snapshot):
    locator = {
        k: snapshot[k] for k in ("service_identity", "cube_id", "user_id", "namespace", "memory_id")
    }
    record = {
        "id": snapshot["memory_id"],
        "memory": snapshot["memory"],
        "metadata": snapshot["metadata"],
    }
    return {
        "subjectId": hash_json(["memory", locator]),
        "kind": "memory",
        "expectedDigest": hash_json(record),
        "locator": locator,
        "baseline": record,
        "snapshotId": snapshot["snapshot_id"],
    }


def observe_memory(client, subject):
    result = {
        "subjectId": subject["subjectId"],
        "status": "unavailable",
        "observedDigest": None,
        "reason": "memory_unavailable",
    }
    locator = subject["locator"]
    if client.service_identity != locator["service_identity"]:
        return {**result, "status": "invalid", "reason": "memory_service_mismatch"}
    try:
        response = client.get(locator["memory_id"], locator["cube_id"])
    except MemoryClientError as error:
        if str(error) in {"invalid_response", "response_too_large"}:
            return {**result, "status": "invalid", "reason": "memory_response_invalid"}
        return result
    except (OSError, RuntimeError, ValueError):
        return result
    record = response.get("data") if isinstance(response, dict) else None
    if not isinstance(record, dict):
        return {**result, "status": "invalid", "reason": "memory_shape_invalid"}
    metadata = record.get("metadata")
    if (
        record.get("id") != locator["memory_id"]
        or not isinstance(record.get("memory"), str)
        or not record["memory"].strip()
        or not isinstance(metadata, dict)
    ):
        return {**result, "status": "invalid", "reason": "memory_shape_invalid"}
    expected = subject["baseline"]["metadata"]
    # Ownership and immutable provenance must remain valid even when content drifts.
    protected = {
        "user_id",
        "owner_id",
        "cube_id",
        "skill_namespace",
        "skill_owner",
        "skill_operation_id",
        "skill_evidence_revision",
        "skill_case_id",
        "skill_lineage_id",
        "skill_run_id",
        "skill_attempt_id",
        "skill_split",
    }
    if not expected.keys() <= metadata.keys() or any(
        metadata.get(k) != expected[k] for k in protected & expected.keys()
    ):
        return {**result, "status": "invalid", "reason": "memory_provenance_invalid"}
    observed = hash_json({k: record[k] for k in ("id", "memory", "metadata")})
    equal = observed == subject["expectedDigest"]
    return {
        **result,
        "status": "healthy" if equal else "changed",
        "observedDigest": observed,
        "reason": "memory_matches" if equal else "memory_content_changed",
    }


def _tool_fingerprint(locator):
    cli = Path(locator["cliPath"])
    binary_hash = hashlib.sha256(cli.read_bytes()).hexdigest()
    process = subprocess.run(
        [str(cli), "--version"],
        capture_output=True,
        timeout=5,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
    )
    if process.returncode != 0 or not process.stdout.strip() or len(process.stdout) > 4096:
        raise ValueError("invalid_tool_probe")
    version = process.stdout.decode("utf-8").strip()
    implementation = [
        {"name": Path(p).name, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()}
        for p in locator["implementationPaths"]
    ]
    return hash_json(
        {
            "binary": binary_hash,
            "version": version,
            "implementation": implementation,
            "python": sys.version,
            "platform": platform.system(),
            "capabilities": locator["capabilities"],
            "probe": "fixed-version-command-succeeded",
        }
    )


def tool_subject(cli_path, *, implementation_paths=(), capabilities=None):
    locator = {
        "cliPath": str(Path(cli_path).absolute()),
        "implementationPaths": [str(Path(p).absolute()) for p in implementation_paths],
        "capabilities": capabilities
        or ["Read", "Edit", "Write", "run_visible_check", "record_rule"],
    }
    require(Path(locator["cliPath"]).is_file(), "tool_unavailable", 503)
    return {
        "subjectId": hash_json(["tool", locator]),
        "kind": "tool",
        "expectedDigest": _tool_fingerprint(locator),
        "locator": locator,
    }


def observe_tool(subject):
    result = {"subjectId": subject["subjectId"], "observedDigest": None}
    try:
        observed = _tool_fingerprint(subject["locator"])
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "status": "unavailable", "reason": "tool_unavailable"}
    except (ValueError, UnicodeError):
        return {**result, "status": "invalid", "reason": "tool_probe_invalid"}
    equal = observed == subject["expectedDigest"]
    return {
        **result,
        "observedDigest": observed,
        "status": "healthy" if equal else "changed",
        "reason": "tool_matches" if equal else "tool_contract_changed",
    }


class HostObserver:
    """Bindings contain trusted host paths; callers can request only registered IDs."""

    def __init__(self, client, *, budget_seconds=10, max_workers=4):
        require(type(budget_seconds) in (int, float) and 0 < budget_seconds <= 15)
        require(type(max_workers) is int and 1 <= max_workers <= 8)
        self.client = client
        self.budget_seconds, self.max_workers = budget_seconds, max_workers

    def __call__(self, subjects):
        # Shared provenance is common across revisions. Probe each fixed subject
        # once, with bounded concurrency and one budget for the entire closure.
        unique = {hash_json(subject): subject for subject in subjects}
        if not unique:
            return []
        executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="skill-probe"
        )
        futures = {}
        try:
            for key, subject in unique.items():
                futures[key] = (
                    executor.submit(observe_memory, self.client, subject)
                    if subject["kind"] == "memory"
                    else executor.submit(observe_tool, subject)
                )
            complete, _ = wait(futures.values(), timeout=self.budget_seconds)
            results = {}
            for key, future in futures.items():
                if future not in complete:
                    future.cancel()
                    results[key] = {
                        "subjectId": unique[key]["subjectId"],
                        "status": "unavailable",
                        "observedDigest": None,
                        "reason": "probe_budget_exhausted",
                    }
                else:
                    results[key] = future.result()
            return [results[hash_json(subject)] for subject in subjects]
        finally:
            # Running probes own bounded HTTP/subprocess timeouts; do not make a
            # public request wait for work whose total observation budget expired.
            executor.shutdown(wait=False, cancel_futures=True)


def subjects_for_packet(packet, cli_path):
    """Build the first immutable host baseline from a verified source packet."""
    root = Path(__file__).resolve().parents[1]
    implementations = sorted((root / "skill_adapter").glob("*.py"))
    implementations += sorted((root / "skill_release").glob("*.py"))
    implementations += [root / "skill_management" / "runtime.py"]
    return [memory_subject(source["snapshot"]) for source in packet["sources"]] + [
        tool_subject(cli_path, implementation_paths=implementations)
    ]
