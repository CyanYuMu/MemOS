"""Run the real isolated executor; qualification is not an Agent success."""

import json

import pytest

from memos.skill_release.broker import check_visible_record
from memos.skill_release.qualification import audit_protocol, require_qualification


@pytest.mark.parametrize(
    "suite_id",
    [
        "update-repair-r1",
        "update-healthy-r1",
        "scope-outside-r1",
        "contract-missing-r1",
        "update-unresolved-r1",
        "check-unavailable-r1",
    ],
)
def test_fixed_matrix_has_real_hidden_receipts_and_public_delivery(tmp_path, suite_id):
    root = tmp_path / suite_id
    result = audit_protocol(suite_id, root)
    assert result["success"], result
    assert result["modelCalls"] == 0
    assert result["branchCoverage"] == []
    assert (
        require_qualification(root, suite_id=suite_id)["qualificationId"]
        == result["qualificationId"]
    )
    if suite_id == "check-unavailable-r1":
        record_path = root / "visible" / suite_id / "initial/check-2.json"
        record = json.loads(record_path.read_text())
        assert record["fault"] == {"checkIndex": 2, "code": "registered_check_unavailable"}
        assert record["execution"] is None
        assert record["checks"] == []
        candidate = root / "candidates" / suite_id / "initial"
        assert check_visible_record(record_path, candidate)
        record["result"]["execution_error"] = None
        record_path.write_text(json.dumps(record))
        assert not check_visible_record(record_path, candidate)
        with pytest.raises(ValueError, match="qualification_invalid"):
            require_qualification(root, suite_id=suite_id)
