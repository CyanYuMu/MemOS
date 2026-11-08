"""Task identities, exports and grading inputs must stay bound to their registry."""

from dataclasses import replace

import pytest

from memos.skill_evaluation.cases import get_case as get_dev_case
from memos.skill_evaluation.tasks import (
    candidate_policy_violations,
    case_digest,
    validate_specs,
)


def task(**changes):
    return replace(
        get_dev_case("dev-strict-signature"),
        split="source",
        editable_paths=("caller.py", "fake_graph.py"),
        **changes,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"assessment_kind": "manual_boundary"},
        {"manual_rubric": ("independent evidence",)},
        {"required_checks": ("different check",)},
        {"provenance": {"commit": "changed"}},
        {"related_lineages": ("changed-lineage",)},
        {"imported_modules": {"caller": "elsewhere.py"}},
    ],
)
def test_case_digest_binds_more_than_code_and_oracle(changes):
    original = task()
    assert case_digest(original) != case_digest(replace(original, **changes))


def test_registry_rejects_duplicate_id_and_holdout_ancestry_overlap():
    source = task()
    with pytest.raises(ValueError, match="Duplicate"):
        validate_specs((source, source))
    holdout = replace(
        source,
        case_id="other-id",
        split="holdout",
        lineage_id="renamed-lineage",
        related_lineages=(source.lineage_id,),
    )
    with pytest.raises(ValueError, match="lineage"):
        validate_specs((source, holdout))


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", ".git/config", ".", ""])
def test_registry_rejects_unsafe_export_paths(path):
    with pytest.raises(ValueError, match="path"):
        validate_specs((task(initial_files={path: "secret"}),))


@pytest.mark.parametrize("path", ["visible_checks.py", ".hidden", ".git/config", "x/y.txt"])
def test_candidate_cannot_change_protected_tests_or_add_hidden_inputs(tmp_path, path):
    spec = task()
    for name, content in spec.reference_files.items():
        (tmp_path / name).write_text(content)
    changed = tmp_path / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("changed")
    assert candidate_policy_violations(spec, tmp_path)


def test_candidate_accepts_alternative_edit_but_rejects_deleted_contract_and_symlink(tmp_path):
    spec = task()
    for name, content in spec.reference_files.items():
        (tmp_path / name).write_text(content)
    (tmp_path / "caller.py").write_text("# another implementation\n")
    assert candidate_policy_violations(spec, tmp_path) == []
    (tmp_path / "visible_checks.py").unlink()
    assert candidate_policy_violations(spec, tmp_path)
    (tmp_path / "visible_checks.py").symlink_to("caller.py")
    assert candidate_policy_violations(spec, tmp_path)


def test_locked_registry_detects_relabeling_and_content_drift(tmp_path, monkeypatch):
    import json

    from memos.skill_evaluation import tasks

    spec = task(case_id="source-test")
    monkeypatch.setattr(tasks, "_p0b_specs", lambda: (spec,))
    lock = tmp_path / "registry.json"
    lock.write_text(json.dumps(tasks.registry_manifest((spec,))))
    monkeypatch.setattr(tasks, "LOCK_PATH", lock)
    assert tasks.get_case("source-test").split == "source"
    for changed in (
        replace(spec, split="holdout", lineage_id="relabelled"),
        replace(spec, contract="silently changed"),
        replace(spec, editable_paths=(*spec.editable_paths, "visible_checks.py")),
    ):
        monkeypatch.setattr(tasks, "_p0b_specs", lambda changed=changed: (changed,))
        with pytest.raises(ValueError, match="lock mismatch|lineage"):
            tasks.get_case("source-test")


def test_registry_rejects_transitive_holdout_lineage_overlap():
    source = task(case_id="source-test", lineage_id="a", related_lineages=("b",))
    bridge = replace(
        source, case_id="dev-bridge", split="dev", lineage_id="b", related_lineages=("c",)
    )
    holdout = replace(
        source, case_id="holdout-test", split="holdout", lineage_id="c", related_lineages=()
    )
    with pytest.raises(ValueError, match="lineage"):
        validate_specs((source, bridge, holdout))
