"""Freeze, run and score a none-vs-skill comparison on every fixed P5 suite.

The published revision was qualified on these suites with the Skill loaded, but
only the repair suite also had a no-experience arm. This protocol runs both arms
on every suite in one frozen batch so boundary behaviour (stop, do not edit a
healthy caller, do not work around a read-only dependency) can be compared.

It reuses the frozen P5 runner, qualification and outcome rules unchanged. It
never calls the management service, issues no execution tickets and therefore
cannot change a Release, admission state or branch coverage.
"""

import hashlib
import json
import time
import uuid

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_experiments.__main__ import runtime
from memos.skill_management.config import read_config
from memos.skill_management.domain import require
from memos.skill_management.runtime import export_identity, native_skill
from memos.skill_p5.execution_policy import policy_identity, report_eligibility
from memos.skill_p5.qualification import require_qualification
from memos.skill_p5.runner import adapter_digest, run_extension_task
from memos.skill_p5.tasks import SUITES, get_suite, suite_digest


PROTOCOL = "p5-boundary-ab-1"
GROUPS = ("none", "skill")
# Issues that make a run non-comparable. Missed stops, extra edits or an
# exceeded repair budget are behaviour and remain ordinary failures.
EXECUTION_ISSUES = frozenset(
    {
        "invalid_group",
        "capture_incomplete",
        "loading_unverified",
        "process_incomplete",
        "invalid_cycle_limit",
        "invalid_cycle_count",
        "policy_mismatch",
    }
)

__all__ = ["aggregate", "binding", "freeze", "plan_units", "policy_identity", "run", "score"]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    Path(path).chmod(0o600)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plan_units(repetitions=1):
    """Both arms on every suite; the arm that runs first alternates by suite and repetition."""
    require(
        type(repetitions) is int and 1 <= repetitions <= 3,
        "boundary_invalid_repetitions",
    )
    units = []
    for repetition in range(1, repetitions + 1):
        for index, suite in enumerate(SUITES):
            order = GROUPS if (index + repetition) % 2 else GROUPS[::-1]
            units.extend(
                {
                    "id": f"{suite}-{group}-r{repetition}",
                    "suiteId": suite,
                    "group": group,
                    "repetition": repetition,
                }
                for group in order
            )
    return units


def binding(config_path, candidate_directory, qualification_directory, *, repetitions, nonce):
    service = read_config(config_path)
    config = runtime(service)
    candidate = load_candidate(candidate_directory)["candidate"]
    projection = export_identity(candidate)
    identity = {
        "protocol": PROTOCOL,
        "batchNonce": nonce,
        "experimentCode": _sha(__file__),
        "adapterDigest": adapter_digest(),
        "executionPolicy": policy_identity(),
        "skillId": candidate["skill_id"],
        "revision": candidate["revision"],
        "contentHash": candidate["content_hash"],
        "exportHash": projection["exportHash"],
        "qualification": {
            suite: require_qualification(Path(qualification_directory) / suite, suite_id=suite)[
                "qualificationId"
            ]
            for suite in SUITES
        },
        "runtime": {
            "cliHash": _sha(config.cli_path.resolve()),
            "cliVersion": config.expected_version,
            "model": config.model,
            "effort": config.effort,
            "tools": list(config.tools),
            "timeoutSeconds": config.timeout_seconds,
            "maxTurns": config.max_turns,
            "maxBudgetUsd": config.max_budget_usd,
        },
        "repetitions": repetitions,
        "units": [
            {
                **unit,
                "taskId": get_suite(unit["suiteId"]).task_id,
                "taskDigest": suite_digest(get_suite(unit["suiteId"])),
            }
            for unit in plan_units(repetitions)
        ],
        "managementTickets": False,
        "publicationEvidence": False,
        "branchCoverage": [],
    }
    identity["maxAgentRuns"] = len(identity["units"])
    identity = {**identity, "experimentId": hash_json(identity)}
    return identity, service, config, native_skill(candidate)


def freeze(config_path, candidate_directory, qualification_directory, output, *, repetitions):
    identity = binding(
        config_path,
        candidate_directory,
        qualification_directory,
        repetitions=repetitions,
        nonce=uuid.uuid4().hex,
    )[0]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    _write(output / "manifest.json", identity)
    return {
        "experimentId": identity["experimentId"],
        "maxAgentRuns": len(identity["units"]),
        "modelCalls": 0,
    }


def _native(run_directory):
    path = Path(run_directory) / "events.jsonl"
    results = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("kind") == "native_result":
                results.append(event.get("payload") or {})
    return results[0] if len(results) == 1 else {}


def score(report_path, group, elapsed):
    report_path = Path(report_path)
    report = _read(report_path)
    facts = report_eligibility(report, group)
    issues = sorted(set(facts["issues"]) & EXECUTION_ISSUES)
    behaviour = report.get("status")
    outcome = report.get("task_outcome") if isinstance(report.get("task_outcome"), dict) else {}
    native = _native(report_path.parent)
    reasons = list(outcome.get("reasons") or [])
    process = report.get("process_evidence")
    process = process if isinstance(process, dict) else {}
    capture = report.get("capture") if isinstance(report.get("capture"), dict) else {}
    # Secondary view only: when the sole capture gap is the order of checks and
    # edits, the retained patch result is still meaningful. The primary status
    # stays unknown so neither arm gains from an unprovable process.
    loading = report.get("loading") if isinstance(report.get("loading"), dict) else {}
    process_only = (
        set(issues) <= {"capture_incomplete", "process_incomplete", "loading_unverified"}
        and set(capture.get("reasons") or ()) <= {"process_evidence_incomplete"}
        and bool(reasons)
        and isinstance(outcome.get("kind"), str)
        # The P5 runner withholds "verified" whenever capture is partial, so the
        # Skill arm reports loading_unverified for this same gap. Require that the
        # Skill was actually seen being discovered and expanded.
        and (group == "none" or (loading.get("discovered") and loading.get("native_expanded")))
    )
    if behaviour in ("passed", "failed") and not issues:
        secondary = behaviour
    elif process_only:
        secondary = "failed" if set(reasons) - {"process_incomplete"} else "passed"
    else:
        secondary = None
    return {
        "status": behaviour if behaviour in ("passed", "failed") and not issues else "unknown",
        "outcomeIgnoringProcess": secondary,
        "processIssues": sorted(process.get("issues") or []),
        "taskAccepted": report.get("task_accepted") is True,
        "outcomeKind": outcome.get("kind"),
        "outcomeReasons": reasons,
        "executionIssues": issues,
        "capture": facts["captureComplete"],
        "loading": facts["loading"],
        "cyclesUsed": facts["cyclesUsed"],
        "elapsedSeconds": round(elapsed, 3),
        "reportedCostUsd": native.get("total_cost_usd"),
        "numTurns": native.get("num_turns"),
        "reportSha256": _sha(report_path),
    }


def _pairs(rows, key):
    cells = {}
    for row in rows:
        # A unit that never produced a report cannot contribute to either view.
        if "failureType" in row:
            continue
        cells.setdefault((row["suiteId"], row["repetition"]), {})[row["group"]] = row.get(key)
    names = {
        ("passed", "passed"): "bothPassed",
        ("failed", "failed"): "bothFailed",
        ("failed", "passed"): "skillOnlyPassed",
        ("passed", "failed"): "noneOnlyPassed",
    }
    pairs = dict.fromkeys(("comparable", *names.values(), "withUnknown"), 0)
    for cell in cells.values():
        outcome = (cell.get("none"), cell.get("skill"))
        if outcome not in names:
            pairs["withUnknown"] += 1
            continue
        pairs["comparable"] += 1
        pairs[names[outcome]] += 1
    return pairs


def aggregate(rows):
    groups = []
    for group in GROUPS:
        selected = [r for r in rows if r["group"] == group]
        groups.append(
            {
                "group": group,
                "total": len(selected),
                **{
                    s: sum(r["status"] == s for r in selected)
                    for s in ("passed", "failed", "unknown")
                },
                "passedIgnoringProcess": sum(
                    r.get("outcomeIgnoringProcess") == "passed" for r in selected
                ),
                "processIssueRuns": sum(bool(r.get("processIssues")) for r in selected),
                "elapsedSeconds": round(sum(r.get("elapsedSeconds") or 0 for r in selected), 3),
                "reportedCostUsd": round(sum(r.get("reportedCostUsd") or 0 for r in selected), 6),
            }
        )
    return {
        "groups": groups,
        "pairs": _pairs(rows, "status"),
        "pairsIgnoringProcess": _pairs(rows, "outcomeIgnoringProcess"),
    }


def rescore(output):
    """Recompute the secondary view from retained reports; never rewrite results.json."""
    output = Path(output)
    original = _read(output / "results.json")
    rows = []
    for row in original["rows"]:
        if "failureType" in row:
            rows.append(row)
            continue
        try:
            fresh = score(
                output / "runs" / row["id"] / "report.json", row["group"], row["elapsedSeconds"]
            )
        except (OSError, ValueError):
            rows.append(row)
            continue
        rows.append({**row, "outcomeIgnoringProcess": fresh["outcomeIgnoringProcess"]})
    view = {
        "protocol": PROTOCOL,
        "experimentId": original["experimentId"],
        "revision": original.get("revision"),
        "originalResultsSha256": _sha(output / "results.json"),
        "rescoringVersion": _sha(__file__),
        "rows": rows,
        **aggregate(rows),
        "managementTickets": False,
        "publicationEvidence": False,
        "branchCoverage": [],
        "statisticalBenefitEstablished": False,
    }
    path = output / "rescored.json"
    _write(path, view)
    return view


def run(config_path, candidate_directory, qualification_directory, output, progress=None):
    """Run every frozen unit once; a started batch is never resumed or retried."""
    output = Path(output)
    manifest = _read(output / "manifest.json")

    def current():
        return binding(
            config_path,
            candidate_directory,
            qualification_directory,
            repetitions=manifest.get("repetitions", 1),
            nonce=manifest["batchNonce"],
        )

    identity, service, config, skill = current()
    require(identity == manifest, "boundary_inputs_changed")
    _write(output / "started.json", {"experimentId": identity["experimentId"]})
    rows, blocked = [], None
    for unit in identity["units"]:
        if current()[0] != manifest:
            blocked = "boundary_inputs_changed"
            break
        if progress:
            progress({"stage": "started", "unit": unit["id"]})
        root = output / "runs" / unit["id"]
        started = time.monotonic()
        try:
            report_path = run_extension_task(
                unit["suiteId"],
                root,
                config,
                qualification_directory=Path(qualification_directory) / unit["suiteId"],
                group=unit["group"],
                experiment_id=identity["experimentId"],
                expected_suite_digest=unit["taskDigest"],
                skill=skill if unit["group"] == "skill" else None,
                expected_export_hash=identity["exportHash"] if unit["group"] == "skill" else None,
                provider_environment=service["runtime"]["providerEnvironment"],
            )
            result = score(report_path, unit["group"], time.monotonic() - started)
        except Exception as error:
            # Categories only: exception text may contain provider details.
            result = {
                "status": "unknown",
                "failureType": type(error).__name__,
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "reportedCostUsd": None,
            }
        row = {**unit, **result}
        rows.append(row)
        _write(output / f"{unit['id']}-result.json", row)
        if progress:
            progress({"stage": "finished", **{k: row.get(k) for k in ("id", "status")}})
    summary = {
        "protocol": PROTOCOL,
        "experimentId": identity["experimentId"],
        "revision": identity["revision"],
        "complete": blocked is None and len(rows) == len(identity["units"]),
        "blockedReason": blocked,
        "rows": rows,
        **aggregate(rows),
        "managementTickets": False,
        "publicationEvidence": False,
        "branchCoverage": [],
        "statisticalBenefitEstablished": False,
    }
    _write(output / "results.json", summary)
    return summary
