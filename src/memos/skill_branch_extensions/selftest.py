"""Host-only asset checks preserve partial outcomes without qualifying Agent runs."""

from __future__ import annotations

import json
import stat

from pathlib import Path

from memos.skill_branch_extensions.tasks import MODES, VARIANTS, get_suite, suite_digest
from memos.skill_evaluation.snapshots import (
    SnapshotIntegrityError,
    SnapshotStore,
    snapshot_fingerprint,
)
from memos.skill_verification.executor import REPORT_LIMIT, execute_program, implementation_digest


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def _same_suite(suite_id: str, expected_digest: str) -> bool:
    try:
        return suite_digest(get_suite(suite_id)) == expected_digest
    except (OSError, ValueError, TypeError, KeyError):
        # Preserve executed evidence, but never qualify it against a changed lock.
        return False


def _candidate_policy(suite, candidate: Path) -> None:
    """Check names, file kinds and protected content before any candidate import."""
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("extension_candidate_policy_changed")
    paths = list(candidate.iterdir())
    if {p.name for p in paths} != set(suite.initial_files):
        raise ValueError("extension_candidate_policy_changed")
    for path in paths:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o7111:
            raise ValueError("extension_candidate_policy_changed")
        if (
            path.name not in suite.editable_paths
            and path.read_text() != suite.initial_files[path.name]
        ):
            raise ValueError("extension_candidate_policy_changed")


def evaluate_candidate(
    suite_id: str, candidate: Path, output: Path, *, mode: str, timeout: float = 15
) -> dict:
    """Evaluate fixed checks against one workspace, not against caller-supplied code.

    This host helper supports negative tests, but the public CLI only audits the
    locked six variants. It never returns a reusable execution qualification.
    """
    suite = get_suite(suite_id)
    fixed_digest = suite_digest(suite)
    if mode not in MODES:
        raise ValueError("unknown_extension_check_mode")
    candidate = Path(candidate)
    _candidate_policy(suite, candidate)
    candidate = candidate.resolve()
    output = Path(output).resolve()
    if output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise ValueError("extension_audit_roots_overlap")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    store = SnapshotStore(output / "snapshots")
    snapshot = store.capture(candidate)
    executor_digest = implementation_digest()
    program = suite.public_program if mode == "public" else suite.oracle
    execution = execute_program(program, candidate, output / "execution", timeout)
    path = output / "execution" / "report.json"
    try:
        report = (
            json.loads(path.read_text())
            if not path.is_symlink() and path.is_file() and path.stat().st_size <= REPORT_LIMIT
            else {}
        )
    except (ValueError, UnicodeError, RecursionError):
        report = {}
    try:
        intact = snapshot_fingerprint(candidate) == snapshot.snapshot_id
        store.verify(snapshot.snapshot_id)
        _candidate_policy(suite, candidate)
    except (ValueError, OSError, SnapshotIntegrityError):
        intact = False
    expected_ids = [row["id"] for row in suite.checks]
    raw_checks = report.get("checks") if isinstance(report, dict) else None
    checks = raw_checks if isinstance(raw_checks, list) else []
    valid = (
        isinstance(report, dict)
        and set(report) == {"checks", "imported"}
        and isinstance(raw_checks, list)
        and all(isinstance(row, dict) for row in checks)
        and [row.get("name") for row in checks] == expected_ids
        and all(
            isinstance(row.get("status"), str) and row["status"] in {"passed", "failed"}
            for row in checks
        )
        and report.get("imported")
        == {name: str(candidate / (name + ".py")) for name in ("caller", "fake_graph")}
        and type(execution.get("returncode")) is int
        and execution["returncode"] in (0, 1)
        and execution.get("timed_out") is False
        and execution.get("output_limited") is False
        and execution.get("cancelled") is False
        and execution.get("background_processes") is False
        and execution.get("limit_reasons") == []
        and "start_error" not in execution
        and intact
        and executor_digest == implementation_digest()
        and suite_digest(suite) == fixed_digest
        and _same_suite(suite_id, fixed_digest)
    )
    failed = [
        row["name"]
        for row in checks
        if isinstance(row, dict)
        and row.get("status") == "failed"
        and isinstance(row.get("name"), str)
    ]
    valid = valid and execution["returncode"] == (1 if failed else 0)
    status = ("failed" if failed else "passed") if valid else "error"
    # A malformed whole report cannot lend a green status to one group.
    group_status = {
        group: (
            "failed"
            if any(row["group"] == group and row["id"] in failed for row in suite.checks)
            else "passed"
        )
        if valid
        else "error"
        for group in ("scoped", "dream")
    }
    result = {
        "schemaVersion": 1,
        "purpose": "branch_extension_asset_selftest",
        "suiteId": suite.suite_id,
        "suiteDigest": fixed_digest,
        "taskId": suite.task_id,
        "lineageId": suite.lineage_id,
        "executorDigest": executor_digest,
        "mode": mode,
        "status": status,
        "checks": checks if isinstance(checks, list) else [],
        "checkMapping": list(suite.checks),
        "failedChecks": failed,
        "groupStatus": group_status,
        "candidateSnapshot": snapshot.snapshot_id,
        "candidateUnchanged": intact,
        "snapshotManifest": str(store.root / snapshot.snapshot_id / "manifest.json"),
        "executionRecord": str(output / "execution" / "execution.json"),
        "qualifiedAgentExperience": False,
        "branchCoverage": [],
        "modelCalls": 0,
    }
    _write(output / "result.json", result)
    return result


def check_variant(
    suite_id: str, variant: str, mode: str, output: Path, timeout: float = 15
) -> dict:
    suite = get_suite(suite_id)
    fixed_digest = suite_digest(suite)
    if variant not in VARIANTS:
        raise ValueError("unknown_extension_variant")
    if mode not in MODES:
        raise ValueError("unknown_extension_check_mode")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Private variant export is deliberately confined to this host audit path.
    candidate = output / "candidate"
    candidate.mkdir(mode=0o700)
    for name, content in suite.variants[variant].items():
        path = candidate / name
        path.write_text(content)
        path.chmod(0o644)
    result = evaluate_candidate(
        suite_id, candidate, output / "verification", mode=mode, timeout=timeout
    )
    expected = suite.expected_failures[variant][mode]
    binding_intact = (
        result["suiteDigest"] == fixed_digest
        and suite_digest(suite) == fixed_digest
        and _same_suite(suite_id, fixed_digest)
    )
    if not binding_intact:
        result["status"] = "error"
        result["groupStatus"] = dict.fromkeys(("scoped", "dream"), "error")
    result.update(
        variant=variant,
        expectedFailures=list(expected),
        expectedSuiteDigest=fixed_digest,
        suiteBindingIntact=binding_intact,
        matchesExpectation=result["status"] != "error"
        and binding_intact
        and result["failedChecks"] == list(expected),
    )
    _write(output / "selftest.json", result)
    return result


def audit_suite(suite_id: str, output: Path, timeout: float = 15) -> dict:
    suite = get_suite(suite_id)
    fixed_digest = suite_digest(suite)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    results = [
        check_variant(suite_id, variant, mode, output / variant / mode, timeout)
        for variant in VARIANTS
        for mode in MODES
    ]
    binding_intact = (
        suite_digest(suite) == fixed_digest
        and _same_suite(suite_id, fixed_digest)
        and all(
            row["suiteDigest"] == fixed_digest
            and row["expectedSuiteDigest"] == fixed_digest
            and row["suiteBindingIntact"]
            for row in results
        )
    )
    result = {
        "schemaVersion": 1,
        "purpose": "branch_extension_asset_selftest",
        "suiteId": suite.suite_id,
        "suiteDigest": fixed_digest,
        "bindingIntact": binding_intact,
        "passed": binding_intact and all(row["matchesExpectation"] for row in results),
        "results": results,
        "qualifiedAgentExperience": False,
        "branchCoverage": [],
        "modelCalls": 0,
        "humanApprovals": 0,
    }
    _write(output / "summary.json", result)
    return result
