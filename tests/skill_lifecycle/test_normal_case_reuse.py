"""A normal use of a qualified task must not rewrite its immutable case node."""

from copy import deepcopy

import pytest

from test_repository import _fixtures, register

from memos.skill_management.domain import ManagementError


@pytest.mark.parametrize("same_lineage", [True, False])
def test_normal_feedback_reuses_qualified_case_but_never_changes_lineage(repo, same_lineage):
    candidate = register(repo)
    holdout = _fixtures.report(candidate)
    repo.record_validation(
        candidate["skill_id"],
        candidate["revision"],
        report=holdout,
        actor="test",
        operation_id="independent",
    )
    original = deepcopy(repo.inspect())
    normal = _fixtures.report(candidate, kind="isolated_dev", suffix="normal")
    # Each real invocation has a distinct receipt; the generic fixture reuses
    # one synthetic digest and would otherwise test an unrelated evidence clash.
    normal["receipt"]["sha256"] = "f" * 64
    normal.update(caseId=holdout["caseId"], caseRevision=holdout["caseRevision"])
    normal["scope"]["caseIds"] = [normal["caseId"]]
    normal["lineageId"] = holdout["lineageId"] if same_lineage else "another-lineage"
    if not same_lineage:
        with pytest.raises(ManagementError, match="immutable_conflict"):
            repo.record_validation(
                candidate["skill_id"],
                candidate["revision"],
                report=normal,
                actor="test",
                operation_id="normal",
            )
        return
    result = repo.record_validation(
        candidate["skill_id"],
        candidate["revision"],
        report=normal,
        actor="test",
        operation_id="normal",
    )
    assert result["split"] == "dev" and result["branchCoverage"] == []
    before = [n for n in original["nodes"] if n["kind"] == "CaseRevision"]
    after = [n for n in repo.inspect()["nodes"] if n["kind"] == "CaseRevision"]
    assert before == after
