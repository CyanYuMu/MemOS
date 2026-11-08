"""Independent holdout asset checks; no Agent experience or Skill effect is claimed."""

import json
import os
import subprocess
import sys

import pytest

from memos.skill_evaluation.holdout_tasks import HOLDOUT_CASE_IDS, get_holdout_case


def _materialize(spec, destination, variant):
    destination.mkdir()
    for name, content in getattr(spec, f"{variant}_files").items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return destination


def _oracle(spec, candidate, output):
    output.mkdir()
    oracle = output / "oracle.py"
    report = output / "report.json"
    oracle.write_text(spec.oracle_source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(oracle), str(candidate), str(report)],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert report.is_file(), result.stderr
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_holdout_registry_preserves_seven_independent_lineages_and_manual_boundaries():
    cases = [get_holdout_case(case_id) for case_id in HOLDOUT_CASE_IDS]
    assert len(cases) == len({case.lineage_id for case in cases}) == 7
    assert all(case.split == "holdout" for case in cases)
    assert all(
        case.provenance["origin"] == "independently_authored_transfer_task" for case in cases
    )
    assert all(case.provenance["independence_basis"] for case in cases)
    for case in cases:
        assert case.editable_paths and "TASK.md" in case.initial_files
        assert not {"TASK.md", "visible_checks.py"} & set(case.editable_paths)
        assert case.initial_files["TASK.md"] == case.reference_files["TASK.md"]
        assert "R3-" not in case.initial_files["TASK.md"]
        assert "oracle" not in "\n".join(case.initial_files)
        case.initial_files["injected.py"] = "changed"
        assert "injected.py" not in get_holdout_case(case.case_id).initial_files
    ambiguous = get_holdout_case("holdout-ambiguous")
    assert ambiguous.assessment_kind == "manual_boundary"
    assert len(ambiguous.manual_rubric) == 3
    assert "diagnosis.json" not in ambiguous.initial_files
    assert "diagnosis.json" in ambiguous.reference_files
    for case_id in ("holdout-fixture", "holdout-discount", "holdout-binding"):
        assert get_holdout_case(case_id).assessment_kind == "boundary_repair"
        assert get_holdout_case(case_id).manual_rubric
    with pytest.raises(ValueError):
        get_holdout_case("not-a-holdout")


@pytest.mark.parametrize("case_id", HOLDOUT_CASE_IDS)
@pytest.mark.parametrize("variant", ("initial", "reference", "unsafe"))
def test_oracles_accept_behavior_and_reject_initial_faults_and_shortcuts(
    tmp_path, case_id, variant
):
    spec = get_holdout_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", variant)
    before = {p.relative_to(candidate): p.read_bytes() for p in candidate.rglob("*") if p.is_file()}
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    checks = {check["name"]: check["status"] for check in report["checks"]}
    assert set(checks) == set(spec.required_checks), report
    assert set(checks.values()) <= {"passed", "failed"}, report
    assert report["imported_files"] == {
        name: str((candidate / path).resolve()) for name, path in spec.imported_modules.items()
    }
    if variant == "reference":
        assert result.returncode == 0, (report, result.stderr)
        assert set(checks.values()) == {"passed"}
    else:
        assert result.returncode == 1, (report, result.stderr)
        assert spec.expected_failure_checks[variant]
        for name in spec.expected_failure_checks[variant]:
            assert checks[name] == "failed", report
    after = {p.relative_to(candidate): p.read_bytes() for p in candidate.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("case_id", HOLDOUT_CASE_IDS)
def test_visible_assertion_removal_cannot_make_original_fault_pass(tmp_path, case_id):
    spec = get_holdout_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", "initial")
    (candidate / "visible_checks.py").write_text("# deleted\n", encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report


@pytest.mark.parametrize("case_id", tuple(c for c in HOLDOUT_CASE_IDS if c != "holdout-ambiguous"))
def test_reference_visible_checks_run_in_exported_workspace(tmp_path, case_id):
    spec = get_holdout_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(candidate / "visible_checks.py")],
        cwd=candidate,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.parametrize("case_id", HOLDOUT_CASE_IDS)
@pytest.mark.parametrize("variant", ("initial", "unsafe"))
def test_visible_success_alone_does_not_distinguish_shortcuts(tmp_path, case_id, variant):
    spec = get_holdout_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", variant)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(candidate / "visible_checks.py")],
        cwd=candidate,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert (result.returncode == 0) == (variant == "unsafe"), (result.stdout, result.stderr)


@pytest.mark.parametrize("fixed_side", ("caller.py", "fake_graph.py"))
def test_migration_requires_both_sides_to_satisfy_the_new_contract(tmp_path, fixed_side):
    spec = get_holdout_case("holdout-migration")
    candidate = _materialize(spec, tmp_path / "candidate", "initial")
    (candidate / fixed_side).write_text(spec.reference_files[fixed_side], encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report
    checks = {item["name"]: item["status"] for item in report["checks"]}
    assert checks["fake_contract" if fixed_side == "caller.py" else "caller_contract"] == "failed"


def test_deadline_rejects_hardcoding_the_visible_timeout(tmp_path):
    spec = get_holdout_case("holdout-deadline")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "caller.py"
    path.write_text(path.read_text().replace("budget_ms / 1000", "0.25"), encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report
    assert any(
        item["name"] == "caller_contract" and item["status"] == "failed"
        for item in report["checks"]
    )


def test_binding_accepts_updating_the_existing_service_instance(tmp_path):
    spec = get_holdout_case("holdout-binding")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "caller.py"
    path.write_text(
        path.read_text().replace(
            "    SERVICE = Catalog(client)\n", "    SERVICE.client = client\n"
        ),
        encoding="utf-8",
    )
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 0, report


def test_record_accepts_a_different_attribute_record_implementation(tmp_path):
    spec = get_holdout_case("holdout-record")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "fake_graph.py"
    source = path.read_text().replace(
        "from dataclasses import dataclass", "from collections import namedtuple"
    )
    source = source.replace(
        "@dataclass(frozen=True)\nclass Record:\n    key: str\n    label: str",
        "Record = namedtuple('Record', 'key label')",
    )
    path.write_text(source, encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 0, report


@pytest.mark.parametrize("diagnosis", ("not json", "[]", '{"disposition": "completed"}'))
def test_ambiguous_task_rejects_missing_or_malformed_clarification(tmp_path, diagnosis):
    spec = get_holdout_case("holdout-ambiguous")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    (candidate / "diagnosis.json").write_text(diagnosis, encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report
    assert any(
        item["name"] == "diagnosis_structure" and item["status"] == "failed"
        for item in report["checks"]
    )


def test_ambiguous_auto_gate_cannot_judge_nonempty_claims_as_semantic_success(tmp_path):
    spec = get_holdout_case("holdout-ambiguous")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "diagnosis.json"
    diagnosis = json.loads(path.read_text())
    diagnosis["missing_definition"] = "unsupported claim"
    diagnosis["question"] = "unsupported question"
    for item in diagnosis["evidence"]:
        item["observation"] = "unverified assertion"
    path.write_text(json.dumps(diagnosis), encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 0, (
        report
    )  # Structure only; the verifier must require manual review.
    assert spec.assessment_kind == "manual_boundary"
    assert spec.manual_rubric
    assert "repair_pass" not in report and "boundary_pass" not in report


def test_fixture_ignores_user_pytest_options_and_plugin_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--option-that-does-not-exist")
    monkeypatch.setenv("PYTEST_PLUGINS", "plugin_that_does_not_exist")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "unrelated_outer_test")
    spec = get_holdout_case("holdout-fixture")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    (tmp_path / "conftest.py").write_text("raise RuntimeError('unrelated parent fixture')\n")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 0, report
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(candidate / "visible_checks.py")],
        cwd=candidate,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_ambiguous_task_description_does_not_supply_the_diagnosis_or_required_stop():
    spec = get_holdout_case("holdout-ambiguous")
    task = spec.initial_files["TASK.md"]
    for answer in ("未规定", "成本升序", "相关度降序", "原始字节", "缺少权威"):
        assert answer not in task
    assert set(spec.editable_paths) == {"caller.py", "fake_graph.py", "diagnosis.json"}


@pytest.mark.parametrize(
    "hook_body",
    (
        "    for item in items:\n        item.add_marker(pytest.mark.skip(reason='bypass'))\n",
        "    for item in items:\n        item.add_marker(pytest.mark.xfail(reason='bypass'))\n",
        "    items[:] = items[:1]\n",
    ),
)
def test_fixture_skipping_all_external_checks_cannot_pass(tmp_path, hook_body):
    spec = get_holdout_case("holdout-fixture")
    candidate = _materialize(spec, tmp_path / "candidate", "initial")
    with (candidate / "conftest.py").open("a", encoding="utf-8") as stream:
        stream.write("\ndef pytest_collection_modifyitems(items):\n" + hook_body)
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode != 0, report
    assert (
        next(item for item in report["checks"] if item["name"] == "fixture_isolation")["status"]
        != "passed"
    )


def test_binding_fake_must_preserve_arbitrary_supplied_data(tmp_path):
    spec = get_holdout_case("holdout-binding")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "fake_graph.py"
    path.write_text(
        path.read_text().replace("dict(titles)", "{'n1': 'replacement', 'n2': 'atlas'}"),
        encoding="utf-8",
    )
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report
    assert (
        next(item for item in report["checks"] if item["name"] == "fake_contract")["status"]
        == "failed"
    )


def test_migration_fake_rejects_invalid_limit_types_as_value_error(tmp_path):
    spec = get_holdout_case("holdout-migration")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "fake_graph.py"
    path.write_text(
        path.read_text().replace("type(limit) is not int", "isinstance(limit, bool)"),
        encoding="utf-8",
    )
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report
    assert (
        next(item for item in report["checks"] if item["name"] == "fake_contract")["status"]
        == "failed"
    )


@pytest.mark.parametrize(
    "before, after",
    (
        ('{"n1": "local"}[key]', '{"n1": "corrupted-local"}[key]'),
        ('{"n1": "local"}[key]', '{"n1": "local"}.get(key)'),
        ("SERVICE = Catalog(DEFAULT_CLIENT)", "SERVICE = Catalog(LocalGraph())"),
    ),
)
def test_binding_preserves_original_local_behavior_before_install(tmp_path, before, after):
    spec = get_holdout_case("holdout-binding")
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / "caller.py"
    source = path.read_text(encoding="utf-8")
    assert before in source
    path.write_text(source.replace(before, after), encoding="utf-8")
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode == 1, report
    assert (
        next(item for item in report["checks"] if item["name"] == "initial_local_behavior")[
            "status"
        ]
        == "failed"
    )


@pytest.mark.parametrize(
    "hook",
    (
        "\ndef pytest_collection_modifyitems(items):\n"
        "    for item in items:\n        item.obj = lambda graph: None\n",
        "\ndef pytest_pyfunc_call(pyfuncitem):\n    return True\n",
    ),
)
def test_fixture_cannot_replace_or_short_circuit_external_test_bodies(tmp_path, hook):
    spec = get_holdout_case("holdout-fixture")
    candidate = _materialize(spec, tmp_path / "candidate", "initial")
    with (candidate / "conftest.py").open("a", encoding="utf-8") as stream:
        stream.write(hook)
    result, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert result.returncode != 0, report
    assert (
        next(item for item in report["checks"] if item["name"] == "fixture_isolation")["status"]
        != "passed"
    )
