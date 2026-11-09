"""Native event simulations use the real isolated public and hidden verifier."""

import pytest

from memos.skill_adapter.artifacts import SkillRevision, export_skill
from memos.skill_p5.runner import _validate_inputs
from memos.skill_p5.tasks import get_suite, suite_digest


def test_runner_binds_any_fixed_candidate_and_export(tmp_path):
    task = get_suite("update-repair-r1")
    skill = SkillRevision(
        "synthetic-new-candidate", "a" * 64, "Synthetic test body.", ("rule_one",)
    )
    exported = export_skill(skill, tmp_path / "export")
    assert (
        _validate_inputs(
            task, "skill", "b" * 64, suite_digest(task), skill, exported["export_hash"]
        )
        == exported
    )
    with pytest.raises(ValueError, match="export_hash"):
        _validate_inputs(task, "skill", "b" * 64, suite_digest(task), skill, "c" * 64)
