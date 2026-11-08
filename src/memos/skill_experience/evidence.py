"""Seal checked source observations without authenticating arbitrary imported JSON.

The trusted collector and its artifact directories are the security boundary.
Content hashes detect later changes; they are not signatures or proof that an
untrusted caller actually ran Claude. P0/P1a artifacts are never rewritten.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
import tempfile

from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from memos.skill_adapter.events import sanitize_public
from memos.skill_adapter.runner import (
    ADAPTER_VERSION,
    PINNED_CLI_VERSION,
    RunConfig,
    _adapter_digest,
    context_matches,
    validate_config,
)
from memos.skill_evaluation.snapshots import SnapshotStore, snapshot_fingerprint
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import check_receipt


_QUALIFIED = {"qualified_success", "qualified_failure"}
_DIGEST = re.compile(r"[a-f0-9]{64}")
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,80}")
_MAX_JSON_BYTES = 32 * 1024 * 1024


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(code: str) -> None:
    raise ValueError("evidence_" + code)


def _require(condition: object, code: str) -> None:
    if not condition:
        _fail(code)


def _read(path: Path) -> bytes:
    _require(path.is_file() and not path.is_symlink(), "artifact_unavailable")
    _require(path.stat().st_size <= _MAX_JSON_BYTES, "artifact_too_large")
    return path.read_bytes()


def _json(path: Path) -> dict:
    value = json.loads(_read(path))
    _require(isinstance(value, dict), "invalid_document")
    return value


def _inside(path: Path | str, root: Path) -> Path:
    """References must stay in the claimed run, including through symbolic links."""
    path = Path(path).absolute()
    _require(path.is_relative_to(root), "path_escape")
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        _require(part not in {"..", "."}, "path_escape")
        cursor /= part
        _require(not cursor.is_symlink(), "path_escape")
    _require(path.resolve().is_relative_to(root), "path_escape")
    return path


def _root(path: Path) -> Path:
    # /tmp may itself be a platform symlink. Canonicalize the root once, then
    # reject symlinks within the artifact hierarchy rather than platform aliases.
    path = Path(path).resolve()
    _require(path.is_dir(), "root_unavailable")
    return path


def _reference(path: Path) -> dict:
    data = _read(path)
    return {"uri": path.as_uri(), "sha256": _sha(data), "size": len(data)}


def _uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    _require(
        parsed.scheme == "file" and not parsed.netloc and not parsed.query and not parsed.fragment,
        "invalid_uri",
    )
    path = Path(unquote(parsed.path))
    _require(path.is_absolute(), "invalid_uri")
    return path


def _time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    _require(timestamp.tzinfo is not None, "invalid_timestamp")
    return timestamp


def _snapshot(store: SnapshotStore, root: Path, identity: str) -> dict:
    _require(isinstance(identity, str) and _DIGEST.fullmatch(identity), "invalid_snapshot")
    snapshot = store.verify(identity)
    return {
        "snapshot_id": snapshot.snapshot_id,
        "store_uri": root.as_uri(),
        "manifest": _reference(root / identity / "manifest.json"),
    }


def _receipt(path: Path, candidate: Path, artifact_root: Path, case) -> dict:
    checked = check_receipt(path, candidate, artifact_root)
    # Do not propagate receipt errors: filenames or subprocess output may be private.
    _require(checked.matches, "receipt_invalid")
    receipt = _json(path)
    _require(
        receipt["case"]
        == {
            "case_id": case.case_id,
            "revision": case.revision,
            "split": case.split,
            "lineage_id": case.lineage_id,
        },
        "receipt_identity_mismatch",
    )
    _require(isinstance(receipt["execution"].get("isolation"), dict), "unguarded_receipt")
    _require(_time(receipt["started_at"]) <= _time(receipt["finished_at"]), "receipt_time_mismatch")
    return receipt


def _receipt_facts(path: Path, artifact_root: Path, receipt: dict) -> dict:
    return {
        "receipt": _reference(path),
        "receipt_hash": _reference(path.with_suffix(".sha256")),
        "artifact_root_uri": artifact_root.as_uri(),
        "candidate_snapshot": receipt["candidate_snapshot"],
        "run_id": receipt["run_id"],
        "attempt_id": receipt["attempt_id"],
        "status": receipt["status"],
        "checks": receipt["checks"],
        "reasons": receipt["reasons"],
        "started_at": receipt["started_at"],
        "finished_at": receipt["finished_at"],
        "environment": receipt["environment"],
        "environment_digest": receipt["environment_digest"],
        "execution": receipt["execution"],
        "artifacts": {
            name: _reference(_inside(artifact_root / reference["path"], artifact_root))
            for name, reference in receipt["artifacts"].items()
        },
    }


def _preflight(
    root: Path, case, initial: str, first_native: datetime | None, gaps: list[str]
) -> dict:
    path = root / "preflight.json"
    if not path.exists():
        gaps.append("preflight_missing")
        return {}
    data = _json(path)
    _require(
        data.get("schema_version") == 1
        and data.get("case_id") == case.case_id
        and data.get("case_revision") == case.revision
        and data.get("asset_digest") == case_digest(case),
        "preflight_identity_mismatch",
    )
    created = _time(data["created_at"])
    variants = data["variants"]
    _require(
        isinstance(variants, dict) and not set(variants) - {"initial", "baseline"},
        "invalid_preflight",
    )
    facts = {"manifest": _reference(path), "variants": {}}
    for variant in ("baseline", "initial"):
        if variant not in variants:
            gaps.append(variant + "_receipt_missing")
            continue
        reference = variants[variant]
        candidate = _inside(reference["candidate"], root)
        artifacts = _inside(reference["artifact_root"], root)
        receipt_path = _inside(reference["receipt"], artifacts)
        if not receipt_path.exists() or not candidate.exists():
            gaps.append(variant + "_receipt_missing")
            continue
        receipt = _receipt(receipt_path, candidate, artifacts, case)
        finished = _time(receipt["finished_at"])
        if first_native is not None:
            _require(created <= first_native and finished <= first_native, "preflight_after_agent")
        # A passing but different input is not a healthy baseline for this case.
        with tempfile.TemporaryDirectory(prefix="memos-evidence-input-") as temporary:
            expected = materialize_case(case.case_id, Path(temporary) / "candidate", variant)
            _require(
                snapshot_fingerprint(expected) == receipt["candidate_snapshot"],
                "preflight_variant_mismatch",
            )
        checks = receipt["checks"]
        _require(
            {item["name"] for item in checks} == set(case.required_checks),
            "preflight_checks_mismatch",
        )
        _require(
            all(item["status"] in {"passed", "failed"} for item in checks),
            "preflight_checks_mismatch",
        )
        if variant == "baseline":
            _require(receipt["status"] == "passed", "baseline_failed")
        else:
            _require(receipt["candidate_snapshot"] == initial, "initial_failure_mismatch")
            failed = {item["name"] for item in checks if item["status"] == "failed"}
            # Registry expectations are required failures, not an exhaustive list:
            # S2 intentionally also fails its scope-generalization assertion.
            _require(
                receipt["status"] == "failed"
                and set(case.expected_failure_checks["initial"]) <= failed,
                "initial_failure_unexpected",
            )
        facts["variants"][variant] = _receipt_facts(receipt_path, artifacts, receipt)
    return facts


def _events(root: Path, report: dict, gaps: list[str]) -> tuple[dict, list[dict], datetime | None]:
    path = root / "events.jsonl"
    data = _read(path)
    _require(_sha(data) == report["events_sha256"], "event_hash_mismatch")
    events = [json.loads(line) for line in data.splitlines()]
    context, session = report["run_context"], report["session_id"]
    _require(isinstance(session, str) and session, "invalid_session")
    phases: dict[tuple[str, str], list[str]] = {}
    starts: dict[tuple[str, str], dict] = {}
    previous = None
    native_times = []
    results = []
    native_hooks = set()
    allowed_stream = {
        "session_init",
        "informational",
        "native_result",
        "assistant_text",
        "user_text",
        "tool_started",
        "tool_completed",
        "hook_started",
        "hook_response",
        "hook_progress",
    }
    allowed_hooks = {
        "SessionStart",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
    }
    for sequence, event in enumerate(events, 1):
        _require(isinstance(event, dict), "invalid_event")
        _require(
            event.get("schema_version") == 1
            and event.get("sequence") == sequence
            and event.get("event_id")
            == f"{context['run_id']}:{context['attempt_id']}:{sequence:06d}"
            and event.get("context") == context,
            "event_identity_mismatch",
        )
        timestamp = _time(event["timestamp"])
        _require(previous is None or previous <= timestamp, "event_order_mismatch")
        previous = timestamp
        producer, kind, payload = event["producer"], event["kind"], event["payload"]
        _require(isinstance(payload, dict), "invalid_event")
        _require(
            producer in {"native_stream", "native_hook", "collector", "agent_declaration"},
            "unknown_event_producer",
        )
        if producer in {"native_stream", "native_hook"}:
            _require(event["native_session_id"] in (None, session), "event_session_mismatch")
            native_times.append(timestamp)
            if producer == "native_stream":
                _require(kind in allowed_stream, "unknown_stream_event")
            else:
                _require(
                    kind in allowed_hooks and payload.get("hook_event_name") == kind,
                    "unknown_hook_event",
                )
                native_hooks.add(kind)
            if (
                kind in {"session_init", "native_result", "tool_started", "tool_completed"}
                or producer == "native_hook"
            ):
                _require(event["native_session_id"] == session, "event_session_missing")
            if (
                producer == "native_stream"
                and kind.startswith("hook_")
                and (
                    payload.get("exit_code") not in (None, 0)
                    or payload.get("outcome") in {"error", "failed", "cancelled"}
                )
            ):
                gaps.append("hook_execution_failed")
        elif producer == "collector":
            _require(kind == "capture_issue", "invalid_collector_event")
            gaps.append("capture_issue")
        else:
            _require(kind == "rule_declaration", "invalid_declaration_event")
            gaps.append("source_rule_declaration")
        if producer == "native_stream" and kind == "native_result":
            _require(
                type(payload.get("is_error")) is bool and isinstance(payload.get("subtype"), str),
                "invalid_native_result",
            )
            results.append(payload)
        tool_phase = {
            ("native_stream", "tool_started"): ("stream", "pre"),
            ("native_stream", "tool_completed"): ("stream", "post"),
            ("native_hook", "PreToolUse"): ("hook", "pre"),
            ("native_hook", "PostToolUse"): ("hook", "post"),
            ("native_hook", "PostToolUseFailure"): ("hook", "post"),
        }.get((producer, kind))
        if tool_phase:
            channel, phase = tool_phase
            identity = event["native_tool_call_id"]
            _require(isinstance(identity, str) and identity, "tool_identity_missing")
            phases.setdefault((channel, identity), []).append(phase)
            if phase == "pre":
                _require(isinstance(payload.get("tool_name"), str), "tool_name_missing")
                starts[channel, identity] = payload
    if not {"SessionStart", "UserPromptSubmit", "Stop"} <= native_hooks:
        gaps.append("required_hook_missing")
    if any(value != ["pre", "post"] for value in phases.values()):
        gaps.append("unpaired_tool_observation")
    stream_ids = {identity for channel, identity in phases if channel == "stream"}
    hook_ids = {identity for channel, identity in phases if channel == "hook"}
    if stream_ids != hook_ids:
        gaps.append("tool_channel_mismatch")
    for identity in stream_ids & hook_ids:
        stream, hook = starts.get(("stream", identity)), starts.get(("hook", identity))
        if stream and hook:
            _require(stream["tool_name"] == hook["tool_name"], "tool_channel_identity_mismatch")
            # The pinned native hook projects name/id, not tool arguments. Compare
            # arguments only if a producer actually supplied that observation.
            if "tool_input" in hook:
                _require(
                    stream.get("tool_input") == hook["tool_input"], "tool_channel_input_mismatch"
                )
    _require(len(results) <= 1, "multiple_native_results")
    native_result = results[0] if results else None
    if native_result is None:
        gaps.append("missing_native_result")
    outcome = (
        "unknown"
        if native_result is None
        else "reported_error"
        if native_result["is_error"]
        else "reported_success"
    )
    capture = report["capture"]
    _require(
        capture.get("native_result") == native_result and capture.get("native_outcome") == outcome,
        "native_result_report_mismatch",
    )
    for key, value in {
        "event_count": len(events),
        "tool_action_count": len(stream_ids | hook_ids),
        "tool_observation_count": sum(len(set(value)) for value in phases.values()),
        "discovered": [event for event in events if event["kind"] == "session_init"],
        "native_expansions": [event for event in events if event["kind"] == "UserPromptExpansion"],
        "rule_declarations": [event for event in events if event["kind"] == "rule_declaration"],
    }.items():
        _require(capture.get(key) == value, "capture_report_mismatch")
    if capture.get("status") != "complete" or capture.get("reasons") or capture.get("error_code"):
        gaps.append("capture_partial")
    return (
        {
            "artifact": _reference(path),
            "event_count": len(events),
            "tool_action_count": len(stream_ids | hook_ids),
            "native_session_id": session,
            "native_outcome": outcome,
            "native_result": native_result,
            "last_native_at": max(native_times).isoformat() if native_times else None,
            "observed_tools": sorted({value["tool_name"] for value in starts.values()}),
        },
        events,
        min(native_times) if native_times else None,
    )


def _changes(store_root: Path, before: str, after: str) -> dict:
    store = SnapshotStore(store_root)
    old = store.verify(before).manifest
    new = store.verify(after).manifest
    left = {entry["path"]: entry for entry in old["entries"]}
    right = {entry["path"]: entry for entry in new["entries"]}
    files, diffs = [], []
    for name in sorted(set(left) | set(right)):
        a, b = left.get(name), right.get(name)
        if a == b:
            continue
        kind = "added" if a is None else "deleted" if b is None else "modified"
        files.append({"path": name, "change": kind, "before": a, "after": b})
        if all(entry is None or entry["kind"] == "file" for entry in (a, b)):
            contents = []
            for identity, entry in ((before, a), (after, b)):
                data = (
                    b"" if entry is None else (store_root / identity / "tree" / name).read_bytes()
                )
                if len(data) > 100_000 or b"\x00" in data:
                    break
                try:
                    contents.append(data.decode("utf-8").splitlines(keepends=True))
                except UnicodeDecodeError:
                    break
            if len(contents) == 2:
                diff = "".join(
                    difflib.unified_diff(
                        *contents, fromfile="before/" + name, tofile="after/" + name
                    )
                )
                diffs.append(
                    {
                        "path": name,
                        "diff": sanitize_public(diff[:32000]),
                        "truncated": len(diff) > 32000,
                    }
                )
    return {
        "files": files,
        "root_executable_before": old["root_executable"],
        "root_executable_after": new["root_executable"],
        "text_diffs": diffs,
        "text_diff_is_auxiliary": True,
    }


def _visible_checks(root: Path, report: dict) -> list[dict]:
    facts = []
    for index, record in enumerate(report.get("visible_checks", []), 1):
        _require(record.get("check_index") == index, "visible_check_identity_mismatch")
        reference = root / "visible-checks" / f"check-{index}.json"
        _require(_json(reference) == record, "visible_check_mismatch")
        if record.get("candidate_snapshot"):
            SnapshotStore(root / "visible-checks" / "snapshots").verify(
                record["candidate_snapshot"]
            )
        facts.append({"artifact": _reference(reference), "result": record})
    return facts


def _derive(run_root: Path, preflight_root: Path, trusted_collection: bool) -> dict:
    _require(type(trusted_collection) is bool, "invalid_trust_flag")
    run_root, preflight_root = _root(run_root), _root(preflight_root)
    report_path = run_root / "report.json"
    report = _json(report_path)
    context = report["run_context"]
    case = get_case(context["case_id"])
    _require(
        report.get("schema_version") == 1
        and report.get("adapter_version") == ADAPTER_VERSION
        and report.get("purpose") == "adapter_validation"
        and report.get("qualified_agent_experience") is False,
        "unsupported_report",
    )
    _require(
        context.get("case_revision") == case.revision
        and context.get("split") == case.split
        and context.get("lineage_id") == case.lineage_id
        and report.get("case_asset_digest") == case_digest(case),
        "registered_identity_mismatch",
    )
    for key in ("run_id", "attempt_id"):
        _require(
            isinstance(context.get(key), str) and _IDENTITY.fullmatch(context[key]),
            "invalid_run_identity",
        )
    _require(
        report["initial_snapshot"] == context.get("initial_snapshot"), "initial_identity_mismatch"
    )
    _require(report.get("adapter_sha256") == _adapter_digest(), "adapter_changed")
    gaps, exclusions = [], []
    for key in ("cli_sha256", "profile_sha256"):
        value = report.get(key)
        if value is None:
            gaps.append(key + "_missing")
        else:
            _require(isinstance(value, str) and _DIGEST.fullmatch(value), "invalid_runtime_digest")
    if not trusted_collection:
        exclusions.append("untrusted_collection")
    if case.split != "source":
        exclusions.append("registered_non_source")
    if report.get("skill_export") or context.get("skill_id") or context.get("skill_revision"):
        exclusions.append("skill_assisted_source")
    store_root = run_root / "snapshots"
    store = SnapshotStore(store_root)
    snapshots = {"initial": _snapshot(store, store_root, report["initial_snapshot"])}
    if report.get("control_snapshot"):
        snapshots["control"] = _snapshot(store, store_root, report["control_snapshot"])
        control_context = _json(store_root / report["control_snapshot"] / "tree" / "context.json")
        _require(
            all(control_context.get(key) == value for key, value in context.items()),
            "control_context_mismatch",
        )
        if control_context.get("runtime_alias") or control_context.get("rule_ids"):
            exclusions.append("skill_assisted_control")
    else:
        gaps.append("control_snapshot_missing")
    event_facts, events, first_native = _events(run_root, report, gaps)
    config = RunConfig(
        cli_path=Path(sys.executable),
        model=report["requested_model"],
        expected_version=report["cli_version"],
        tools=tuple(report["tools"]),
        timeout_seconds=report["timeout_seconds"],
        max_turns=report["max_turns"],
        max_budget_usd=report["max_budget_usd"],
        effort=report["effort"],
    )
    validate_config(config)
    # Native init declares available tools; the actual calls must independently
    # respect that capability set. Matching stream/hook claims alone do not suffice.
    if not set(event_facts["observed_tools"]) <= set(config.tools) | {
        "mcp__memos_run__run_visible_checks"
    }:
        gaps.append("unapproved_tool_observed")
    matching_context = context_matches(events, config, None)
    reported_models = sorted(
        {
            model
            for event in events
            if event["kind"] == "native_result"
            for model in event["payload"].get("modelUsage", {})
        }
    )
    _require(report.get("reported_models", []) == reported_models, "model_report_mismatch")
    if reported_models and reported_models != [config.model]:
        matching_context = False
    if (
        not matching_context
        or report.get("context_matches") is not True
        or report["cli_version"] != PINNED_CLI_VERSION
    ):
        gaps.append("runtime_context_mismatch")
    if (
        report.get("execution", {}).get("timed_out")
        or report.get("execution", {}).get("returncode") is None
    ):
        gaps.append("execution_incomplete")
    isolation = {}
    if (run_root / "isolation.json").exists():
        isolation = {
            "artifact": _reference(run_root / "isolation.json"),
            "probe": _json(run_root / "isolation.json"),
        }
    if isolation.get("probe", {}).get("passed") is not True:
        gaps.append("isolation_missing")
    preflight = (
        _preflight(preflight_root, case, report["initial_snapshot"], first_native, gaps)
        if case.split == "source"
        else {}
    )
    final, changes = {}, {"files": [], "text_diffs": [], "text_diff_is_auxiliary": True}
    candidate_snapshot = report.get("candidate_snapshot")
    if candidate_snapshot:
        snapshots["candidate"] = _snapshot(store, store_root, candidate_snapshot)
        _require(
            snapshot_fingerprint(run_root / "candidate") == candidate_snapshot, "candidate_changed"
        )
        changes = _changes(store_root, report["initial_snapshot"], candidate_snapshot)
    else:
        gaps.append("candidate_missing")
    if report.get("receipt") and candidate_snapshot:
        artifacts = run_root / "validation"
        receipt_path = _inside(report["receipt"], artifacts)
        receipt = _receipt(receipt_path, run_root / "candidate", artifacts, case)
        _require(_sha(_read(receipt_path)) == report["receipt_sha256"], "receipt_hash_mismatch")
        _require(
            receipt["run_id"] == context["run_id"]
            and receipt["attempt_id"] == context["attempt_id"]
            and receipt["candidate_snapshot"] == candidate_snapshot,
            "final_binding_mismatch",
        )
        if case.split == "source":
            _require(
                receipt["initial_snapshot"] == report["initial_snapshot"],
                "registered_initial_mismatch",
            )
        if event_facts["last_native_at"]:
            _require(
                _time(event_facts["last_native_at"]) <= _time(receipt["started_at"]),
                "final_before_agent_completed",
            )
        _require(
            report.get("task_status") == receipt["status"]
            and report.get("receipt_matches") is True
            and report.get("task_accepted") is (receipt["status"] == "passed"),
            "final_report_mismatch",
        )
        final = _receipt_facts(receipt_path, artifacts, receipt)
    else:
        gaps.append("final_receipt_missing")
    gaps = sorted(set(gaps))
    if exclusions:
        qualification = "excluded"
    elif gaps:
        qualification = "needs_evidence"
    elif (
        final["status"] == "passed"
        and event_facts["native_outcome"] == "reported_success"
        and report["execution"]["returncode"] == 0
    ):
        qualification = "qualified_success"
    else:
        qualification = "qualified_failure"
    return {
        "schema_version": 1,
        "implementation_sha256": _sha(Path(__file__).read_bytes()),
        "qualification": qualification,
        "gaps": gaps,
        "exclusion_reasons": exclusions,
        "collection": {
            "trusted_local_collection": trusted_collection,
            "trust_boundary": "local_host_and_artifact_directories",
            "external_authentication": False,
        },
        "locations": {
            "run_root_uri": run_root.as_uri(),
            "preflight_root_uri": preflight_root.as_uri(),
        },
        "context": context,
        "case": {
            "case_id": case.case_id,
            "revision": case.revision,
            "lineage_id": case.lineage_id,
            "split": case.split,
            "asset_digest": case_digest(case),
            "contract": case.contract,
            "editable_paths": list(case.editable_paths),
            "required_checks": list(case.required_checks),
            "expected_initial_failures": list(case.expected_failure_checks.get("initial", ())),
            "provenance": case.provenance,
        },
        "report": _reference(report_path),
        "snapshots": snapshots,
        "preflight": preflight,
        "events": event_facts,
        "final": final,
        "changes": changes,
        "visible_checks": _visible_checks(run_root, report),
        "environment": {
            key: report.get(key)
            for key in (
                "adapter_version",
                "adapter_sha256",
                "cli_version",
                "cli_sha256",
                "requested_model",
                "reported_models",
                "tools",
                "max_turns",
                "max_budget_usd",
                "effort",
                "timeout_seconds",
                "profile_sha256",
                "execution",
            )
        },
        "isolation": isolation,
        "unknowns": {
            "root_cause": "unknown",
            "failure_attribution": "unknown",
            "user_feedback": "not_collected",
            "causal_skill_effect": "not_evaluated",
        },
    }


def seal_evidence(
    run_root: Path, preflight_root: Path, destination: Path, *, trusted_collection: bool = False
) -> Path:
    """Seal a local run; only the host's direct collector may grant source eligibility."""
    try:
        _require(type(trusted_collection) is bool, "invalid_trust_flag")
        destination = Path(destination).absolute()
        _require(not destination.exists() and not destination.is_symlink(), "destination_exists")
        evidence = _derive(run_root, preflight_root, trusted_collection)
        evidence["evidence_revision"] = _sha(_json_bytes(evidence))
        destination.mkdir(parents=True, exist_ok=False)
        path = destination / "evidence.json"
        with path.open("xb") as stream:
            stream.write(_json_bytes(evidence))
        return path
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith("evidence_"):
            raise
        raise ValueError("evidence_invalid_artifact") from None


def verify_evidence(path: Path) -> dict:
    """Recheck content, current implementations, registered identities and receipts."""
    try:
        path = Path(path).absolute()
        evidence = _json(path)
        _require(_read(path) == _json_bytes(evidence), "document_changed")
        payload = {key: value for key, value in evidence.items() if key != "evidence_revision"}
        _require(evidence["evidence_revision"] == _sha(_json_bytes(payload)), "revision_mismatch")
        locations = evidence["locations"]
        expected = _derive(
            _uri_path(locations["run_root_uri"]),
            _uri_path(locations["preflight_root_uri"]),
            evidence["collection"]["trusted_local_collection"],
        )
        _require(payload == expected, "content_changed")
        return evidence
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith("evidence_"):
            raise
        raise ValueError("evidence_invalid_artifact") from None


def load_source_summary(path: Path) -> dict:
    """Return checked factual input for the bridge, never an unverified pass claim."""
    evidence = verify_evidence(path)
    if evidence["qualification"] not in _QUALIFIED or evidence["case"]["split"] != "source":
        raise ValueError("source_ineligible")
    context, case = evidence["context"], evidence["case"]
    initial = evidence["preflight"]["variants"]["initial"]
    failed = [check["name"] for check in initial["checks"] if check["status"] == "failed"]
    final = evidence["final"]
    content = "\n".join(
        [
            f"MemOS controlled Python repair case: {case['case_id']} ({case['revision']}).",
            f"Task contract: {case['contract']}",
            f"Initial observed failed checks: {', '.join(failed)}. Healthy baseline passed.",
            f"Observed native tools: {', '.join(evidence['events']['observed_tools'])}.",
            "Observed file changes: "
            + "; ".join(
                f"{item['change']} {item['path']}" for item in evidence["changes"]["files"]
            ),
            "Final independent checks: "
            + "; ".join(f"{item['name']}={item['status']}" for item in final["checks"]),
            f"Qualification: {evidence['qualification']}. Final receipt status: {final['status']}.",
            "Root cause and failure attribution: unknown; user feedback: not collected; causal Skill effect: not evaluated.",
            f"Evidence revision: {evidence['evidence_revision']}. Evidence URI: {Path(path).resolve().as_uri()}.",
        ]
    )
    content = str(sanitize_public(content))[:16000]
    return {
        "evidence_revision": evidence["evidence_revision"],
        "content": content,
        "qualification": evidence["qualification"],
        **{key: context[key] for key in ("case_id", "lineage_id", "run_id", "attempt_id", "split")},
        "info": {
            "skill_evidence_uri": Path(path).resolve().as_uri(),
            "skill_case_revision": case["revision"],
            "skill_initial_snapshot": evidence["snapshots"]["initial"]["snapshot_id"],
            "skill_candidate_snapshot": evidence["snapshots"]["candidate"]["snapshot_id"],
            "skill_final_receipt_sha256": final["receipt"]["sha256"],
        },
    }
