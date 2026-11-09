"""G0 state tests use real files; no Claude or transport success is simulated."""

import hashlib
import json

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from memos.skill_release_v2.capture import verify_tool_delivery
from memos.skill_release_v2.gate import (
    GateDeniedError,
    HostGate,
    RunIdentity,
    VerifiedResult,
    digest_json,
)


IDENTITY = RunIdentity("run-1", "attempt-1", "session-1")
RULES = {"entry": ("repair", "exit:needs_evidence"), "repair": ("exit:success",)}


@pytest.fixture
def gate(tmp_path):
    (tmp_path / "TASK.md").write_text("Repair the caller according to this contract.\n")
    (tmp_path / "caller.py").write_text("wrong()\n")

    def verify(request, payload):
        # A trusted transport verifier supplies this object. Agent declarations cannot.
        if not isinstance(payload, VerifiedResult):
            raise ValueError("unverified native envelope")
        return payload

    return HostGate(
        workspace=tmp_path,
        identity=IDENTITY,
        rules=RULES,
        entry_rule="entry",
        allowed_paths=("caller.py", "new.py"),
        request_path="TASK.md",
        verify_result=verify,
    )


def finish(gate, request, *, result=None, error=False):
    return gate.complete_tool(
        IDENTITY,
        request.tool_use_id,
        VerifiedResult(request.digest, digest_json(result or {}), error),
    )


def observe(gate, name, tool, args, *, error=False):
    request = gate.begin_tool(IDENTITY, name, tool, args)
    return finish(gate, request, error=error)


def evidence(gate, *, error=False):
    return [
        observe(gate, "read", "Read", {"file_path": "TASK.md"}).reference,
        observe(gate, "check", "run_visible_checks", {}, error=error).reference,
    ]


def declaration(rule, phase, refs, branch=None):
    return {
        "rule_id": rule,
        "phase": phase,
        "branch_index": branch,
        "observation": "Observed current request and public result.",
        "decision": "Apply the declared rule.",
        "evidence_refs": refs,
    }


def declare(gate, name, value, *, commit=True):
    request = gate.begin_tool(IDENTITY, name, "record_rule", value)
    receipt = gate.receive_declaration(value)
    if commit:
        finish(gate, request, result=receipt.as_dict())
    return request, receipt


def ready(gate):
    refs = evidence(gate)
    declare(gate, "entry", declaration("entry", "select", refs, 0))
    declare(gate, "target", declaration("repair", "select", refs))
    declare(gate, "act", declaration("repair", "act", refs))
    return refs


def edit(gate, name="edit", **overrides):
    args = {"file_path": "caller.py", "old_string": "wrong()", "new_string": "right()"}
    args.update(overrides)
    return gate.begin_tool(IDENTITY, name, "Edit", args)


def apply(gate, request):
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    (gate.workspace / request.path).write_text("right()\n")
    return finish(gate, request)


def test_edit_without_declarations_is_denied_and_does_not_write(gate):
    before = (gate.workspace / "caller.py").read_bytes()
    with pytest.raises(GateDeniedError, match="declaration_required"):
        apply(gate, edit(gate))
    assert (gate.workspace / "caller.py").read_bytes() == before
    assert [a.event for a in gate.audit()] == ["attempted", "denied"]


def test_committed_selection_action_allow_one_actual_edit(gate):
    ready(gate)
    request = edit(gate)
    assert request.permit.snapshot == gate.state().snapshot
    assert len(request.permit.declarations) == 3
    assert len(request.permit.evidence_refs) == 2
    apply(gate, request)
    assert (gate.workspace / "caller.py").read_text() == "right()\n"
    assert gate.state().busy_tool is None
    assert gate.audit()[-1].event == "executed"
    with pytest.raises(GateDeniedError, match="permit_consumed"):
        gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)


def test_pending_action_does_not_authorize_parallel_edit(gate):
    refs = evidence(gate)
    declare(gate, "entry", declaration("entry", "select", refs, 0))
    declare(gate, "target", declaration("repair", "select", refs))
    request, receipt = declare(gate, "act", declaration("repair", "act", refs), commit=False)
    assert gate.state().pending_declarations == (receipt.declaration_id,)
    with pytest.raises(GateDeniedError, match="declaration_required"):
        edit(gate)
    finish(gate, request, result=receipt.as_dict())
    apply(gate, edit(gate, "second-edit"))


def test_native_declaration_receipt_must_match_exact_pending_response(gate):
    refs = evidence(gate)
    request, _ = declare(gate, "entry", declaration("entry", "select", refs, 0), commit=False)
    with pytest.raises(GateDeniedError, match="receipt_mismatch"):
        finish(gate, request, result={"status": "committed"})
    assert not gate.state().committed_declarations


def test_duplicate_native_requests_cannot_be_matched_from_agent_parameters(gate):
    value = declaration("entry", "select", evidence(gate), 0)
    gate.begin_tool(IDENTITY, "one", "record_rule", value)
    gate.begin_tool(IDENTITY, "two", "record_rule", value)
    with pytest.raises(GateDeniedError, match="ambiguous_declaration"):
        gate.receive_declaration(value)
    assert not gate.state().pending_declarations


def test_mcp_without_native_request_or_with_spoofed_id_is_rejected(gate):
    value = declaration("entry", "select", ["not-yet-observed"], 0)
    with pytest.raises(GateDeniedError, match="native_request_missing"):
        gate.receive_declaration(value)
    value["tool_use_id"] = "invented"
    with pytest.raises(GateDeniedError, match="declaration_shape"):
        gate.receive_declaration(value)


@pytest.mark.parametrize("field", ["run_id", "attempt_id", "session_id"])
def test_cross_identity_is_denied(gate, field):
    with pytest.raises(GateDeniedError, match="identity_mismatch"):
        gate.begin_tool(
            replace(IDENTITY, **{field: "other"}), "read", "Read", {"file_path": "TASK.md"}
        )


def test_unverified_or_foreign_receipt_never_creates_observation(gate):
    request = gate.begin_tool(IDENTITY, "read", "Read", {"file_path": "TASK.md"})
    with pytest.raises(GateDeniedError, match="unverified_result"):
        gate.complete_tool(IDENTITY, request.tool_use_id, {"I_read_the_document": True})
    assert not gate.state().observations
    other = gate.begin_tool(IDENTITY, "other", "Read", {"file_path": "TASK.md"})
    with pytest.raises(GateDeniedError, match="receipt_mismatch"):
        gate.complete_tool(
            IDENTITY, "other", VerifiedResult(request.digest, digest_json({}), False)
        )
    assert other.digest != request.digest


def test_observation_refs_cannot_be_invented_or_partial_request_reads(gate):
    refs = [observe(gate, "read", "Read", {"file_path": "TASK.md", "limit": 1}).reference]
    refs.append(observe(gate, "check", "run_visible_checks", {}).reference)
    with pytest.raises(GateDeniedError, match="request_observation_missing"):
        declare(gate, "entry", declaration("entry", "select", refs, 0))
    with pytest.raises(GateDeniedError, match="unknown_evidence"):
        declare(gate, "invented", declaration("entry", "select", ["fake"], 0))


def test_action_needs_target_selection_not_only_entry_or_late_check(gate):
    refs = evidence(gate)
    declare(gate, "entry", declaration("entry", "select", refs, 0))
    with pytest.raises(GateDeniedError, match="target_selection_required"):
        declare(gate, "act", declaration("repair", "act", refs))
    with pytest.raises(GateDeniedError, match="target_selection_required"):
        declare(gate, "late", declaration("repair", "check", refs))


def test_error_delivery_can_support_stop_but_never_edit(gate):
    refs = evidence(gate, error=True)
    declare(gate, "entry", declaration("entry", "select", refs, 1))
    declare(gate, "exit", declaration("entry", "exit", refs, 1))
    assert gate.state().exited
    assert gate.state().observations[-1].is_error
    with pytest.raises(GateDeniedError, match="exited"):
        edit(gate)


def test_error_delivery_does_not_become_successful_action_evidence(gate):
    refs = evidence(gate, error=True)
    declare(gate, "entry", declaration("entry", "select", refs, 0))
    declare(gate, "target", declaration("repair", "select", refs))
    with pytest.raises(GateDeniedError, match="successful_check_required"):
        declare(gate, "act", declaration("repair", "act", refs))


def test_healthy_exit_needs_no_fabricated_edit_or_action(gate):
    refs = evidence(gate)
    declare(gate, "entry", declaration("entry", "select", refs, 0))
    declare(gate, "target", declaration("repair", "select", refs))
    declare(gate, "exit", declaration("repair", "exit", refs, 0))
    assert gate.state().exited
    assert not any(a.event == "executed" and a.tool_name == "Edit" for a in gate.audit())


@pytest.mark.parametrize("change", ["input", "snapshot", "path", "session", "permit"])
def test_edit_permission_is_bound_to_exact_request_and_current_state(gate, change):
    ready(gate)
    request = edit(gate)
    identity, args, permit = IDENTITY, request.input, request.permit
    if change == "input":
        args["new_string"] = "different()"
    elif change == "snapshot":
        (gate.workspace / "caller.py").write_text("external()\n")
    elif change == "path":
        args["file_path"] = "TASK.md"
    elif change == "session":
        identity = replace(IDENTITY, session_id="other")
    else:
        permit = replace(permit, snapshot="f" * 64)
    with pytest.raises(GateDeniedError):
        gate.consume_edit(permit, identity, request.tool_use_id, args)
    assert not any(a.event == "executed" and a.tool_name == "Edit" for a in gate.audit())


@pytest.mark.parametrize("path", ["TASK.md", "../escape.py", "/etc/passwd", "sub/../caller.py"])
def test_protected_and_traversing_paths_are_rejected(gate, path):
    ready(gate)
    with pytest.raises(GateDeniedError, match="path"):
        edit(gate, file_path=path)


def test_symlink_swap_after_permission_is_denied_without_touching_target(gate, tmp_path):
    ready(gate)
    request = edit(gate)
    (tmp_path / "caller.py").unlink()
    (tmp_path / "caller.py").symlink_to(tmp_path / "TASK.md")
    before = (tmp_path / "TASK.md").read_bytes()
    with pytest.raises(GateDeniedError, match="symlink"):
        apply(gate, request)
    assert (tmp_path / "TASK.md").read_bytes() == before


def test_edit_and_checks_cannot_overlap_and_duplicate_tool_id_cannot_replay(gate):
    ready(gate)
    request = edit(gate)
    for name, tool, args in [
        ("check2", "run_visible_checks", {}),
        ("edit2", "Write", {"file_path": "new.py", "content": "x"}),
    ]:
        with pytest.raises(GateDeniedError, match="operation_in_progress"):
            gate.begin_tool(IDENTITY, name, tool, args)
    gate.abort_tool(IDENTITY, request.tool_use_id, "hook_timeout")
    with pytest.raises(GateDeniedError, match="duplicate_tool_use_id"):
        edit(gate)
    assert gate.audit()[-1].event == "denied"


def test_pending_check_blocks_edit(gate):
    ready(gate)
    gate.begin_tool(IDENTITY, "checking", "run_visible_checks", {})
    with pytest.raises(GateDeniedError, match="operation_in_progress"):
        edit(gate)


def test_concurrent_edit_requests_have_only_one_permission(gate):
    ready(gate)

    def attempt(n):
        try:
            return edit(gate, f"edit-{n}")
        except GateDeniedError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(x is not None for x in pool.map(attempt, range(2))) == 1


def test_completion_without_consumption_or_after_hook_timeout_is_not_execution(gate):
    ready(gate)
    request = edit(gate)
    with pytest.raises(GateDeniedError, match="permit_not_consumed"):
        finish(gate, request)
    assert not any(a.event == "executed" and a.tool_name == "Edit" for a in gate.audit())
    request = edit(gate, "second")
    gate.abort_tool(IDENTITY, request.tool_use_id, "hook_no_response")
    with pytest.raises(GateDeniedError, match="request_not_pending"):
        finish(gate, request)


def test_actual_write_to_protected_file_invalidates_gate(gate):
    ready(gate)
    request = edit(gate)
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    (gate.workspace / "TASK.md").write_text("tampered")
    with pytest.raises(GateDeniedError, match="unexpected_file_change"):
        finish(gate, request)
    assert gate.state().invalidated
    assert gate.audit()[-1].event == "failed"


def test_edit_invalidates_old_check_and_action_until_new_observation(gate):
    ready(gate)
    apply(gate, edit(gate))
    with pytest.raises(GateDeniedError, match="current_check_required"):
        edit(gate, "again")


def test_failed_edit_with_changes_preserves_facts_and_requires_recheck(gate):
    ready(gate)
    request = edit(gate)
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    (gate.workspace / "caller.py").write_text("partial()\n")
    finish(gate, request, error=True)
    assert gate.audit()[-1].event == "failed"
    assert json.loads(gate.audit()[-1].details_json)["changed_paths"] == ["caller.py"]
    with pytest.raises(GateDeniedError, match="current_check_required"):
        edit(gate, "again")


def test_state_and_audit_are_immutable_and_hash_chained(gate):
    ready(gate)
    audit = gate.audit()
    with pytest.raises(FrozenInstanceError):
        audit[0].event = "executed"
    with pytest.raises(FrozenInstanceError):
        gate.state().exited = True
    for index, event in enumerate(audit):
        assert event.sequence == index + 1
        assert event.previous_hash == (audit[index - 1].digest if index else "0" * 64)
        assert event.digest == hashlib.sha256(event.canonical_json.encode()).hexdigest()


@pytest.mark.parametrize("value", [None, True, -1, 9])
def test_entry_branch_requires_valid_integer(gate, value):
    with pytest.raises(GateDeniedError, match="branch_index"):
        declare(gate, "entry", declaration("entry", "select", evidence(gate), value))


def test_host_failure_or_explicit_invalidation_is_fail_closed(gate):
    ready(gate)
    request = edit(gate)
    gate.invalidate("hook_disconnected")
    with pytest.raises(GateDeniedError, match="invalidated"):
        gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    assert (gate.workspace / "caller.py").read_text() == "wrong()\n"


def test_new_error_check_invalidates_previous_success_and_action(gate):
    ready(gate)
    observe(gate, "new-error", "run_visible_checks", {}, error=True)
    with pytest.raises(GateDeniedError, match="current_check_required"):
        edit(gate)


def test_old_success_cannot_mask_latest_error_in_new_action_evidence(gate):
    refs = ready(gate)
    error = observe(gate, "latest-error", "run_visible_checks", {}, error=True).reference
    before = (gate.workspace / "caller.py").read_bytes()
    with pytest.raises(GateDeniedError, match="successful_check_required"):
        declare(gate, "new-act", declaration("repair", "act", [*refs, error]))
        apply(gate, edit(gate, "after-error"))
    assert (gate.workspace / "caller.py").read_bytes() == before
    assert not any(a.event == "executed" and a.tool_name == "Edit" for a in gate.audit())


def test_aborted_new_check_cannot_fall_back_to_old_success_for_actual_edit(gate):
    ready(gate)
    gate.begin_tool(IDENTITY, "latest-check", "run_visible_checks", {})
    gate.abort_tool(IDENTITY, "latest-check", "native_timeout")
    before = (gate.workspace / "caller.py").read_bytes()
    with pytest.raises(GateDeniedError, match="invalidated"):
        apply(gate, edit(gate, "after-timeout"))
    assert (gate.workspace / "caller.py").read_bytes() == before
    assert gate.state().invalidated
    assert not any(a.event == "executed" and a.tool_name == "Edit" for a in gate.audit())
    with pytest.raises(GateDeniedError, match="invalidated"):
        observe(gate, "pretend-recovered-check", "run_visible_checks", {})


def test_unverified_new_check_cannot_fall_back_to_old_success_for_actual_edit(gate):
    ready(gate)
    request = gate.begin_tool(IDENTITY, "latest-check", "run_visible_checks", {})
    with pytest.raises(GateDeniedError, match="unverified_result"):
        gate.complete_tool(IDENTITY, request.tool_use_id, {"unverified": "native response"})
    before = (gate.workspace / "caller.py").read_bytes()
    with pytest.raises(GateDeniedError, match="invalidated"):
        apply(gate, edit(gate, "after-missing-capture"))
    assert (gate.workspace / "caller.py").read_bytes() == before
    assert gate.state().invalidated
    assert not any(a.event == "executed" and a.tool_name == "Edit" for a in gate.audit())


def test_error_read_does_not_register_request_evidence(gate):
    refs = [observe(gate, "read", "Read", {"file_path": "TASK.md"}, error=True).reference]
    refs.append(observe(gate, "check", "run_visible_checks", {}).reference)
    with pytest.raises(GateDeniedError, match="request_observation_missing"):
        declare(gate, "entry", declaration("entry", "select", refs, 0))


@pytest.mark.parametrize(
    "field,value",
    [("rule_id", []), ("phase", {}), ("evidence_refs", [None]), ("observation", None)],
)
def test_declaration_boundary_types_fail_with_semantic_error(gate, field, value):
    data = declaration("entry", "select", ["unobserved"], 0)
    data[field] = value
    with pytest.raises(GateDeniedError):
        gate.begin_tool(IDENTITY, "invalid", "record_rule", data)
    assert gate.audit()[-1].event == "denied"


def test_snapshot_change_between_declaration_receive_and_native_return_cannot_commit(gate):
    refs = evidence(gate)
    request, receipt = declare(gate, "entry", declaration("entry", "select", refs, 0), commit=False)
    (gate.workspace / "caller.py").write_text("external()\n")
    with pytest.raises(GateDeniedError, match="stale_snapshot"):
        finish(gate, request, result=receipt.as_dict())
    assert not gate.state().committed_declarations


def test_check_does_not_register_if_workspace_changes_while_it_runs(gate):
    request = gate.begin_tool(IDENTITY, "check", "run_visible_checks", {})
    (gate.workspace / "caller.py").write_text("external()\n")
    with pytest.raises(GateDeniedError, match="stale_snapshot"):
        finish(gate, request)
    assert not gate.state().observations


def test_second_cycle_requires_new_check_and_new_action(gate):
    ready(gate)
    apply(gate, edit(gate))
    latest = observe(gate, "new-check", "run_visible_checks", {}).reference
    with pytest.raises(GateDeniedError, match="current_check_required"):
        edit(gate, "old-action")
    refs = [gate.state().observations[0].reference, latest]
    declare(gate, "new-act", declaration("repair", "act", refs))
    request = edit(gate, "new-edit", old_string="right()", new_string="better()")
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    (gate.workspace / "caller.py").write_text("better()\n")
    finish(gate, request)


def test_write_new_allowlisted_file_and_absolute_native_path(gate):
    ready(gate)
    request = gate.begin_tool(
        IDENTITY,
        "write",
        "Write",
        {"file_path": str(gate.workspace / "new.py"), "content": "new()\n"},
    )
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    (gate.workspace / "new.py").write_text("new()\n")
    finish(gate, request)
    assert gate.audit()[-1].event == "executed"


def test_mutating_supplied_inputs_and_returned_copies_cannot_change_native_request(gate):
    value = {"file_path": "TASK.md"}
    request = gate.begin_tool(IDENTITY, "read", "Read", value)
    value["file_path"] = "caller.py"
    copied = request.input
    copied["file_path"] = "caller.py"
    assert request.input == {"file_path": "TASK.md"}
    assert finish(gate, request).kind == "request"


def test_successful_tool_return_cannot_hide_different_actual_edit(gate):
    ready(gate)
    request = edit(gate)
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    (gate.workspace / "caller.py").write_text("undeclared_change()\n")
    with pytest.raises(GateDeniedError, match="edit_result_mismatch"):
        finish(gate, request)
    assert gate.state().invalidated


def test_consumed_edit_with_missing_callback_closes_gate_even_without_observed_change(gate):
    ready(gate)
    request = edit(gate)
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    gate.abort_tool(IDENTITY, request.tool_use_id, "native_timeout")
    assert gate.state().invalidated
    with pytest.raises(GateDeniedError, match="invalidated"):
        edit(gate, "next")


def test_empty_or_ambiguous_replacement_cannot_be_reported_as_success(gate):
    ready(gate)
    request = edit(gate, old_string="not present")
    gate.consume_edit(request.permit, IDENTITY, request.tool_use_id, request.input)
    with pytest.raises(GateDeniedError, match="edit_result_mismatch"):
        finish(gate, request)


def test_success_after_error_requires_new_verified_check_and_new_action(gate):
    refs = ready(gate)
    observe(gate, "error-check", "run_visible_checks", {}, error=True)
    new_ref = observe(gate, "recovered-check", "run_visible_checks", {}).reference
    declare(gate, "new-act", declaration("repair", "act", [refs[0], new_ref]))
    apply(gate, edit(gate))
    assert any(o.is_error for o in gate.state().observations)


def test_capture_verifier_accepts_real_error_envelope_without_losing_error_identity(gate):
    retained = {}

    def verified(request, payload):
        if request.tool_name == "Read":
            return payload
        host_result, is_error = retained[request.tool_use_id]
        result = verify_tool_delivery(
            payload,
            expected_tool_use_id=request.tool_use_id,
            expected_is_error=is_error,
            host_result=host_result,
        )
        return VerifiedResult(request.digest, result.result_sha256, result.is_error)

    actual = HostGate(
        workspace=gate.workspace,
        identity=IDENTITY,
        rules=RULES,
        entry_rule="entry",
        allowed_paths=("caller.py",),
        request_path="TASK.md",
        verify_result=verified,
    )
    doc = observe(actual, "read", "Read", {"file_path": "TASK.md"})
    check = actual.begin_tool(IDENTITY, "error-check", "run_visible_checks", {})
    body = {"checks": [], "status": "error", "execution_error": "registered_check_unavailable"}
    retained[check.tool_use_id] = (body, True)
    observation = actual.complete_tool(
        IDENTITY,
        check.tool_use_id,
        {"tool_use_id": check.tool_use_id, "is_error": True, "tool_result": json.dumps(body)},
    )
    assert observation.is_error
    refs = [doc.reference, observation.reference]
    for name, phase in [("entry", "select"), ("exit", "exit")]:
        value = declaration("entry", phase, refs, 1)
        actual.begin_tool(IDENTITY, name, "record_rule", value)
        receipt = actual.receive_declaration(value)
        retained[name] = (receipt.as_dict(), False)
        actual.complete_tool(
            IDENTITY,
            name,
            {
                "tool_use_id": name,
                "is_error": False,
                "tool_result": [{"type": "text", "text": json.dumps(receipt.as_dict())}],
            },
        )
    assert actual.state().exited
    assert actual.state().observations[-1].is_error


def test_changed_authority_cannot_be_re_read_to_bypass_fixed_public_request(gate):
    (gate.workspace / "TASK.md").write_text("Changed authority after run was frozen.\n")
    ready(gate)
    with pytest.raises(GateDeniedError, match="protected_workspace_changed"):
        edit(gate)


def test_native_tool_id_and_tool_name_invalid_types_are_safe_denials(gate):
    for tool_id, name in [(None, "Read"), ("invalid", []), ("another", None)]:
        with pytest.raises(GateDeniedError):
            gate.begin_tool(IDENTITY, tool_id, name, {"file_path": "TASK.md"})
    assert gate.audit()[-1].event == "denied"


def test_first_unchanged_task_read_cannot_supply_request_authority(gate):
    request = gate.begin_tool(IDENTITY, "cached", "Read", {"file_path": "TASK.md"})
    cached = gate.complete_tool(
        IDENTITY,
        "cached",
        VerifiedResult(
            request.digest,
            digest_json({"type": "file_unchanged"}),
            False,
            read_content_delivered=False,
        ),
    )
    assert cached.kind == "read" and cached.document_sha256 is None
    check = observe(gate, "check", "run_visible_checks", {})
    value = declaration("entry", "select", [cached.reference, check.reference], 0)
    gate.begin_tool(IDENTITY, "entry", "record_rule", value)
    with pytest.raises(GateDeniedError, match="request_observation_missing"):
        gate.receive_declaration(value)
    assert not gate.state().committed_declarations
    executed = next(
        row for row in gate.audit() if row.event == "executed" and row.tool_use_id == "cached"
    )
    assert json.loads(executed.details_json)["read_content_delivered"] is False


def test_unchanged_read_does_not_erase_prior_delivered_request_evidence(gate):
    original = observe(gate, "original", "Read", {"file_path": "TASK.md"})
    request = gate.begin_tool(IDENTITY, "cached", "Read", {"file_path": "TASK.md"})
    cached = gate.complete_tool(
        IDENTITY,
        "cached",
        VerifiedResult(
            request.digest,
            digest_json({"type": "file_unchanged"}),
            False,
            read_content_delivered=False,
        ),
    )
    check = observe(gate, "check", "run_visible_checks", {})
    declare(gate, "entry", declaration("entry", "select", [original.reference, check.reference], 0))
    assert original.kind == "request" and cached.kind == "read"
    assert len(gate.state().committed_declarations) == 1


@pytest.mark.parametrize("flag", [None, 0, 1, "false", []])
def test_content_delivery_flag_is_strict_boolean(gate, flag):
    request = gate.begin_tool(IDENTITY, "read", "Read", {"file_path": "TASK.md"})
    with pytest.raises(GateDeniedError, match="receipt_mismatch"):
        gate.complete_tool(
            IDENTITY,
            "read",
            VerifiedResult(
                request.digest,
                digest_json({}),
                False,
                read_content_delivered=flag,
            ),
        )
    assert not gate.state().observations
