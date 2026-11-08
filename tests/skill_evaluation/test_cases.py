"""Behavioral checks for public dev probes, not collected Agent experience."""

from __future__ import annotations

import json
import subprocess
import sys

from dataclasses import FrozenInstanceError

import pytest

from memos.skill_evaluation.cases import get_case, list_cases, materialize_case


CASE_IDS = ("dev-strict-signature", "dev-tenant-isolation")


def _oracle(case_id, candidate, workdir):
    workdir.mkdir()
    spec = get_case(case_id)
    oracle = workdir / "trusted_oracle.py"
    report_path = workdir / "report.json"
    oracle.write_text(spec.oracle_source, encoding="utf-8")
    # This unrelated module must never be imported from the process cwd.
    (workdir / "caller.py").write_text("raise RuntimeError('wrong caller')\n", encoding="utf-8")
    process = subprocess.run(
        [sys.executable, "-I", "-B", str(oracle), str(candidate), str(report_path)],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert report_path.is_file(), process.stderr
    return process, json.loads(report_path.read_text(encoding="utf-8"))


def _visible(candidate):
    # -I excludes the script directory, so the visible checker adds its own root.
    return subprocess.run(
        [sys.executable, "-I", "-B", str(candidate / "visible_checks.py")],
        cwd=candidate.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_registry_is_dev_only_and_does_not_share_mutable_fixture_state():
    cases = list_cases()
    assert isinstance(cases, tuple)
    assert tuple(case.case_id for case in cases) == CASE_IDS
    assert len({case.lineage_id for case in cases}) == 2
    for case in cases:
        assert case.split == "dev"
        assert case.revision and case.contract
        assert set(case.expected_failure_checks) == {"initial", "unsafe"}
        for expected in case.expected_failure_checks.values():
            assert expected and set(expected) <= set(case.required_checks)
        with pytest.raises(FrozenInstanceError):
            case.split = "source"
        case.initial_files["injected.py"] = "not a fixture"
        assert "injected.py" not in get_case(case.case_id).initial_files


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_prepare_exports_only_task_code_and_visible_checks(tmp_path, case_id):
    destination = tmp_path / "candidate"
    assert materialize_case(case_id, destination) == destination
    assert {path.name for path in destination.iterdir()} == {
        "TASK.md",
        "caller.py",
        "fake_graph.py",
        "visible_checks.py",
    }
    task = (destination / "TASK.md").read_text(encoding="utf-8")
    assert "dev" in task and get_case(case_id).contract in task
    assert get_case(case_id).oracle_source not in task
    with pytest.raises(FileExistsError):
        materialize_case(case_id, destination, variant="reference")
    assert (destination / "caller.py").read_text(encoding="utf-8") == (
        get_case(case_id).initial_files["caller.py"]
    )


def test_prepare_rejects_unknown_cases_and_variants_without_creating_directory(tmp_path):
    with pytest.raises(ValueError):
        get_case("source-case")
    for case_id, variant in (("unknown", "initial"), (CASE_IDS[0], "patched")):
        destination = tmp_path / "unused"
        with pytest.raises(ValueError):
            materialize_case(case_id, destination, variant=variant)
        assert not destination.exists()


@pytest.mark.parametrize("case_id", CASE_IDS)
@pytest.mark.parametrize("variant", ("initial", "reference", "unsafe"))
def test_oracle_distinguishes_behavior_and_confirms_candidate_imports(tmp_path, case_id, variant):
    candidate = materialize_case(case_id, tmp_path / "candidate", variant=variant)
    before = {path.name: path.read_bytes() for path in candidate.iterdir()}
    process, report = _oracle(case_id, candidate, tmp_path / "oracle")
    checks = {check["name"]: check for check in report["checks"]}
    spec = get_case(case_id)
    assert set(checks) == set(spec.required_checks)
    assert {check["status"] for check in checks.values()} <= {"passed", "failed"}
    assert all(isinstance(check["detail"], str) for check in checks.values())
    assert report["imported_files"] == {
        name: str((candidate / f"{name}.py").resolve()) for name in ("caller", "fake_graph")
    }
    if variant == "reference":
        assert process.returncode == 0, report
        assert all(check["status"] == "passed" for check in checks.values())
    else:
        assert process.returncode == 1, report
        for name in spec.expected_failure_checks[variant]:
            assert checks[name]["status"] == "failed", report
    assert {path.name: path.read_bytes() for path in candidate.iterdir()} == before
    visible = _visible(candidate)
    assert (visible.returncode == 0) == (variant != "initial"), visible.stderr


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_removing_visible_assertions_cannot_change_external_rejection(tmp_path, case_id):
    candidate = materialize_case(case_id, tmp_path / "candidate")
    (candidate / "visible_checks.py").write_text("# assertions removed\n", encoding="utf-8")
    assert _visible(candidate).returncode == 0
    process, report = _oracle(case_id, candidate, tmp_path / "oracle")
    assert process.returncode == 1
    assert any(check["status"] == "failed" for check in report["checks"])


@pytest.mark.parametrize("damage", ("missing", "syntax"))
def test_import_failure_is_infrastructure_error_not_a_semantic_rejection(tmp_path, damage):
    candidate = materialize_case(CASE_IDS[0], tmp_path / "candidate", variant="reference")
    caller = candidate / "caller.py"
    if damage == "missing":
        caller.unlink()
    else:
        caller.write_text("def broken(:\n", encoding="utf-8")
    process, report = _oracle(CASE_IDS[0], candidate, tmp_path / "oracle")
    assert process.returncode == 2
    assert any(check["status"] == "error" for check in report["checks"])


def test_legal_candidate_call_raising_is_a_behavior_failure(tmp_path):
    candidate = materialize_case(CASE_IDS[0], tmp_path / "candidate", variant="reference")
    (candidate / "caller.py").write_text(
        "def update_memory(*args, **kwargs):\n    raise RuntimeError('broken caller')\n",
        encoding="utf-8",
    )
    process, report = _oracle(CASE_IDS[0], candidate, tmp_path / "oracle")
    assert process.returncode == 1
    checks = {item["name"]: item["status"] for item in report["checks"]}
    assert checks["caller_contract"] == "failed"


def test_removed_candidate_interface_is_a_contract_failure(tmp_path):
    candidate = materialize_case(CASE_IDS[0], tmp_path / "candidate", variant="reference")
    (candidate / "caller.py").write_text("# required entry point removed\n", encoding="utf-8")
    process, report = _oracle(CASE_IDS[0], candidate, tmp_path / "oracle")
    assert process.returncode == 1
    checks = {item["name"]: item["status"] for item in report["checks"]}
    assert checks["caller_contract"] == "failed"


@pytest.mark.parametrize(
    "lookup",
    (
        'self.nodes.get((user_name or "tenant-1", id))',
        'self.nodes.get((user_name, id)) or self.nodes.get((user_name, "mem-1"))',
    ),
    ids=("missing-tenant-fallback", "missing-node-fallback"),
)
def test_tenant_oracle_rejects_missing_scope_or_node_fallback(tmp_path, lookup):
    case_id = "dev-tenant-isolation"
    candidate = materialize_case(case_id, tmp_path / "candidate", variant="reference")
    fake_path = candidate / "fake_graph.py"
    source = fake_path.read_text(encoding="utf-8")
    fake_path.write_text(
        source.replace("self.nodes.get((user_name, id))", lookup), encoding="utf-8"
    )
    # These wrong implementations still satisfy the visible wrong-tenant example.
    assert _visible(candidate).returncode == 0
    process, report = _oracle(case_id, candidate, tmp_path / "oracle")
    assert process.returncode == 1
    checks = {item["name"]: item["status"] for item in report["checks"]}
    assert checks["fake_rejects_foreign_tenant"] == "failed"
