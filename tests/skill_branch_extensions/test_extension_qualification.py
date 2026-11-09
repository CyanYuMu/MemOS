"""A full retained matrix, including client delivery, is required for admission."""

import json
import sys

import pytest

from memos.skill_branch_extensions.qualification import audit_protocol, require_qualification
from memos.skill_drafting.domain import hash_json


SUITE = "combined-c1-v1"
MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed no-fork executor")


@MACOS
def test_protocol_qualification_has_twelve_real_checks_and_no_model_or_coverage(tmp_path):
    result = audit_protocol(SUITE, tmp_path / "qualification")
    assert result["success"]
    assert len(result["hidden"]) == len(result["visible"]) == 6
    assert result["modelCalls"] == result["humanApprovals"] == 0
    assert result["branchCoverage"] == []
    assert (
        require_qualification(tmp_path / "qualification", suite_id=SUITE)["qualificationId"]
        == result["qualificationId"]
    )


@MACOS
def test_rehashed_missing_matrix_entry_cannot_qualify(tmp_path):
    root = tmp_path / "qualification"
    result = audit_protocol(SUITE, root)
    result["visible"].pop()
    result["qualificationId"] = hash_json(
        {k: v for k, v in result.items() if k != "qualificationId"}
    )
    (root / "summary.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="extension_qualification_invalid"):
        require_qualification(root, suite_id=SUITE)


@MACOS
@pytest.mark.parametrize(
    "damage", ["duplicate", "wrong_variant", "client_missing", "client_changed", "groups"]
)
def test_rehashed_summary_cannot_hide_variant_delivery_or_group_damage(tmp_path, damage):
    root = tmp_path / "qualification"
    result = audit_protocol(SUITE, root)
    assert result["success"]
    if damage == "duplicate":
        result["hidden"][1] = result["hidden"][0]
    elif damage == "wrong_variant":
        target = root / "candidates" / SUITE / "reference" / "caller.py"
        target.write_text((root / "candidates" / SUITE / "initial" / "caller.py").read_text())
    elif damage == "groups":
        result["visible"][0]["groupStatus"] = {"scoped": "passed", "dream": "passed"}
    else:
        target = root / "visible" / SUITE / "reference" / "client-response.json"
        if damage == "client_missing":
            target.unlink()
        else:
            response = json.loads(target.read_text())
            response["evidence_ref"]["check_index"] = 2
            target.write_text(json.dumps(response))
    result["qualificationId"] = hash_json(
        {k: v for k, v in result.items() if k != "qualificationId"}
    )
    (root / "summary.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="extension_qualification_invalid"):
        require_qualification(root, suite_id=SUITE)


@MACOS
def test_old_qualification_protocol_cannot_accept_extension_matrix(tmp_path):
    from memos.skill_branch_validation.qualification import require_qualification as old_require

    root = tmp_path / "qualification"
    assert audit_protocol(SUITE, root)["success"]
    with pytest.raises(ValueError, match="branch_qualification_invalid"):
        old_require(root)


def test_cancelled_qualification_does_not_begin_matrix(tmp_path):
    import threading

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValueError, match="extension_qualification_cancelled"):
        audit_protocol(SUITE, tmp_path / "qualification", cancellation_event=cancelled)
    assert not list((tmp_path / "qualification").rglob("execution.json"))


@MACOS
def test_qualification_schema_rejects_boolean_numeric_alias(tmp_path):
    root = tmp_path / "qualification"
    result = audit_protocol(SUITE, root)
    assert result["success"]
    result["schemaVersion"] = True
    result["qualificationId"] = hash_json(
        {k: v for k, v in result.items() if k != "qualificationId"}
    )
    (root / "summary.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="extension_qualification_invalid"):
        require_qualification(root, suite_id=SUITE)
