"""Normal jobs require registered cases, not merely an accepted prefix."""

import pytest

from memos.skill_management.domain import ManagementError
from memos.skill_management.repository import ManagementRepository


@pytest.mark.parametrize("prefix", ["release", "p5"])
def test_normal_job_request_accepts_exact_registered_task(prefix):
    value = ManagementRepository._job_request(
        "skill",
        "a" * 64,
        case_id=prefix + "-update-repair-r1",
        actor="test",
        export_hash="b" * 64,
        mode="normal",
    )
    assert value["caseId"] == prefix + "-update-repair-r1"
    assert value["mode"] == "normal"


@pytest.mark.parametrize("case", ["p5-unknown", "p5-check-unavailable-r1", "release-unknown"])
def test_independent_or_unknown_tasks_are_not_normal_jobs(case):
    with pytest.raises(ManagementError):
        ManagementRepository._job_request(
            "skill",
            "a" * 64,
            case_id=case,
            actor="test",
            export_hash="b" * 64,
            mode="normal",
        )
