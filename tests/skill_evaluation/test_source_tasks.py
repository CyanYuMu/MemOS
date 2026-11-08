"""Checks for fixed production-derived source tasks, not Agent experience."""

import ast
import hashlib
import json
import subprocess
import sys

from pathlib import Path

import pytest

from memos.skill_evaluation.source_tasks import SOURCE_CASE_IDS, get_source_case


def _materialize(spec, destination, variant):
    destination.mkdir()
    for name, content in getattr(spec, f"{variant}_files").items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return destination


def _oracle(spec, candidate, directory):
    directory.mkdir()
    script = directory / "oracle.py"
    report = directory / "report.json"
    script.write_text(spec.oracle_source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(script), str(candidate), str(report)],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert report.is_file(), result.stderr
    return result, json.loads(report.read_text())


@pytest.mark.parametrize("case_id", SOURCE_CASE_IDS)
def test_fixed_production_symbols_are_exact_copies_of_registered_commit(case_id):
    spec = get_source_case(case_id)
    provenance = spec.provenance
    assert provenance["commit"] == "5a595b4a06676c8d259412ae75048fdb668c482c"
    assert provenance["adaptations"]
    root = Path(__file__).resolve().parents[2]
    for entry in provenance["symbols"]:
        original = subprocess.check_output(
            ["git", "show", f"{provenance['commit']}:{entry['path']}"], cwd=root
        ).decode()
        assert hashlib.sha256(original.encode()).hexdigest() == entry["file_sha256"]
        node = next(
            node
            for node in ast.parse(original).body
            if getattr(node, "name", None) == entry["symbol"]
            or (
                isinstance(node, ast.Assign)
                and any(getattr(target, "id", None) == entry["symbol"] for target in node.targets)
            )
            or (
                isinstance(node, ast.AnnAssign)
                and getattr(node.target, "id", None) == entry["symbol"]
            )
        )
        excerpt = "".join(original.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])
        assert hashlib.sha256(excerpt.encode()).hexdigest() == entry["symbol_sha256"]
        assert excerpt in spec.reference_files[entry["destination"]]


def test_source_registry_has_fixed_scope_and_returns_independent_values():
    specs = [get_source_case(case_id) for case_id in SOURCE_CASE_IDS]
    assert len(specs) == 2
    assert len({spec.lineage_id for spec in specs}) == 2
    for spec in specs:
        assert spec.split == "source"
        assert spec.related_lineages
        assert spec.editable_paths == ("caller.py", "fake_graph.py")
        assert spec.imported_modules == {
            "caller": "caller.py",
            "fake_graph": "fake_graph.py",
            "support": "support.py",
        }
        spec.provenance["commit"] = "changed"
        assert get_source_case(spec.case_id).provenance["commit"] != "changed"
    with pytest.raises(ValueError):
        get_source_case("not-registered")


@pytest.mark.parametrize("case_id", SOURCE_CASE_IDS)
@pytest.mark.parametrize("variant", ("initial", "reference", "unsafe"))
def test_source_oracle_accepts_repair_and_rejects_fault_and_unsafe(tmp_path, case_id, variant):
    spec = get_source_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", variant)
    process, report = _oracle(spec, candidate, tmp_path / "trusted")
    expected = 0 if variant == "reference" else 1
    assert process.returncode == expected, (process.stderr, report)
    assert {row["name"] for row in report["checks"]} == set(spec.required_checks)
    assert all(row["status"] != "error" for row in report["checks"]), report
    failed = {row["name"] for row in report["checks"] if row["status"] == "failed"}
    assert set(spec.expected_failure_checks.get(variant, ())) <= failed
    assert report["imported_files"] == {
        name: str((candidate / path).resolve()) for name, path in spec.imported_modules.items()
    }


@pytest.mark.parametrize("case_id", SOURCE_CASE_IDS)
@pytest.mark.parametrize("variant", ("initial", "reference", "unsafe"))
def test_visible_reproducer_exposes_fault_but_cannot_detect_unsafe_fix(tmp_path, case_id, variant):
    candidate = _materialize(get_source_case(case_id), tmp_path / "candidate", variant)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(candidate / "visible_checks.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert (result.returncode == 0) == (variant != "initial"), result.stderr


@pytest.mark.parametrize("case_id", SOURCE_CASE_IDS)
def test_visible_package_has_no_hidden_answers(case_id):
    spec = get_source_case(case_id)
    assert set(spec.initial_files) == {
        "caller.py",
        "fake_graph.py",
        "support.py",
        "visible_checks.py",
        "TASK.md",
    }
    assert spec.oracle_source not in "\n".join(spec.initial_files.values())
    assert "unsafe" not in spec.initial_files["TASK.md"]
    assert "R3-" not in spec.initial_files["TASK.md"]


@pytest.mark.parametrize("case_id", SOURCE_CASE_IDS)
def test_behavioral_oracle_accepts_equivalent_non_reference_patch(tmp_path, case_id):
    spec = get_source_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    if case_id == "source-dream-update":
        path = candidate / "caller.py"
        content = path.read_text()
        content = content.replace(
            "action.target_memory_id,\n                fields,",
            "action.target_memory_id,\n                fields=fields,",
        )
    else:
        path = candidate / "fake_graph.py"
        content = path.read_text()
        content = content.replace(
            'memory_id != self.node["id"] or kwargs.get("user_name") != self.tenant',
            '(memory_id, kwargs.get("user_name")) != (self.node["id"], self.tenant)',
        )
    assert content != path.read_text()
    path.write_text(content)
    process, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert process.returncode == 0, report


@pytest.mark.parametrize(
    "case_id,filename,before,after,expected_check",
    [
        (
            "source-dream-update",
            "caller.py",
            "action.target_memory_id,\n                fields,\n                user_name=cube_id,",
            "action.target_memory_id,\n                fields,\n                user_name=None,",
            "caller_contract",
        ),
        (
            "source-dream-update",
            "fake_graph.py",
            '        if not isinstance(fields, dict):\n            raise TypeError("fields must be a dictionary")\n',
            "        if not isinstance(fields, dict):\n            return None\n",
            "fake_contract",
        ),
        (
            "source-scoped-update",
            "fake_graph.py",
            'memory_id != self.node["id"] or kwargs.get("user_name") != self.tenant',
            'kwargs.get("user_name") != self.tenant',
            "fake_scope_contract",
        ),
        (
            "source-scoped-update",
            "fake_graph.py",
            'memory_id != self.node["id"] or kwargs.get("user_name") != self.tenant',
            'memory_id != "m-1" or kwargs.get("user_name") != "tenant-1"',
            "scope_generalization",
        ),
        (
            "source-scoped-update",
            "caller.py",
            "node = graph_db.get_node(req.memory_id, user_name=tenant)",
            'node = graph_db.get_node("m-1", user_name="tenant-1")',
            "scope_generalization",
        ),
        (
            "source-scoped-update",
            "caller.py",
            'logger.warning("Failed to embed updated memory content", exc_info=True)\n        return None',
            'logger.warning("Failed to embed updated memory content", exc_info=True)\n        return [0.0]',
            "embedding_failure",
        ),
    ],
)
def test_oracle_rejects_targeted_regressions(
    tmp_path, case_id, filename, before, after, expected_check
):
    spec = get_source_case(case_id)
    candidate = _materialize(spec, tmp_path / "candidate", "reference")
    path = candidate / filename
    content = path.read_text()
    assert content.count(before) == 1
    path.write_text(content.replace(before, after))
    process, report = _oracle(spec, candidate, tmp_path / "trusted")
    assert process.returncode == 1, report
    assert any(
        row["name"] == expected_check and row["status"] == "failed" for row in report["checks"]
    ), report
