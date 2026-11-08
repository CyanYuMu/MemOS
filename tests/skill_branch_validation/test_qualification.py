"""A complete current protocol matrix is required, not just a success boolean."""

import json
import sys

import pytest

from memos.skill_branch_validation import qualification
from memos.skill_branch_validation.qualification import audit_protocol, require_qualification
from memos.skill_drafting.domain import hash_json


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")


@pytest.fixture
def qualified(tmp_path):
    root = tmp_path / "qualification"
    result = audit_protocol(root)
    assert result["success"]
    return root, result


def test_complete_protocol_matrix_rechecks(qualified):
    root, result = qualified
    assert len(result["hidden"]) == 6 and len(result["visible"]) == 4
    assert result["modelCalls"] == 0 and result["branchCoverage"] == []
    assert require_qualification(root)["qualificationId"] == result["qualificationId"]


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "identity", "candidate", "record", "path"]
)
def test_incomplete_or_changed_qualification_blocks_even_rehashed(qualified, change):
    root, result = qualified
    if change == "missing":
        result["hidden"].pop()
    elif change == "duplicate":
        result["hidden"][-1] = result["hidden"][0]
    elif change == "identity":
        result["binding"]["protocol"] = "old"
    elif change == "candidate":
        candidate = root / result["hidden"][0]["candidate"] / "fake_graph.py"
        candidate.write_text("# replacement\n")
    elif change == "record":
        result["visible"][0]["exitCode"] = 0
    else:
        result["hidden"][0]["receipt"] = "../receipt.json"
    result["qualificationId"] = hash_json(
        {k: v for k, v in result.items() if k != "qualificationId"}
    )
    (root / "summary.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="branch_qualification_invalid"):
        require_qualification(root)


def test_host_success_with_failed_client_delivery_cannot_qualify(tmp_path, monkeypatch):
    request = qualification._request_visible_checks

    def lost_response(context):
        request(context)  # The host still produces a valid execution record.
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "timed_out": False,
            "execution_error": "test_broker_failed",
        }

    monkeypatch.setattr(qualification, "_request_visible_checks", lost_response)
    result = audit_protocol(tmp_path / "qualification")
    assert result["success"] is False
    with pytest.raises(ValueError, match="branch_qualification_invalid"):
        require_qualification(tmp_path / "qualification")
