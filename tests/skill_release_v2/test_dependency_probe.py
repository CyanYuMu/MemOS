"""Dependency qualification uses copied modules and the real OS boundary."""

import hashlib
import json
import sys

import pytest

from memos.skill_release_v2 import dependency_probe
from memos.skill_release_v2.dependency_probe import (
    DependencyProbeError,
    build_manifest,
    copy_manifest,
    run_probe,
)


def dependency_repository(tmp_path):
    repository = tmp_path / "repository"
    files = {
        "memos/__init__.py": "INITIALIZED = True\n",
        "memos/mem_feedback/feedback.py": (
            "from memos import INITIALIZED\n"
            "from memos.helpers import helper\n"
            "assert INITIALIZED and helper.VALUE == 7\n"
            "class MemFeedback: pass\n"
        ),
        "memos/helpers/__init__.py": "from . import helper\n",
        "memos/helpers/helper.py": "VALUE = 7\n",
        "memos/skill_release/hidden.py": "raise AssertionError('must stay hidden')\n",
    }
    for name, content in files.items():
        path = repository / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repository / ".env").write_text("PROBE_CANARY=not-a-secret\n")
    return repository


def test_manifest_follows_parent_and_relative_modules_without_unrelated_files(tmp_path):
    repository = dependency_repository(tmp_path)
    manifest = build_manifest(repository / "src")
    assert set(manifest["modules"]) == {
        "memos",
        "memos.mem_feedback.feedback",
        "memos.helpers",
        "memos.helpers.helper",
    }
    assert manifest["resources"] == []
    for entry in manifest["files"]:
        raw = (repository / "src" / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert ".env" not in json.dumps(manifest)
    assert "hidden.py" not in json.dumps(manifest)


@pytest.mark.parametrize(
    "resource", ["../.env", "memos/.env", "memos/.ENV", "memos/skill_release/hidden.py"]
)
def test_resources_cannot_copy_secrets_or_skill_assets(tmp_path, resource):
    repository = dependency_repository(tmp_path)
    with pytest.raises(DependencyProbeError):
        build_manifest(repository / "src", resources=(resource,))


def test_skill_import_is_blocking_instead_of_silently_removed(tmp_path):
    repository = dependency_repository(tmp_path)
    target = repository / "src/memos/mem_feedback/feedback.py"
    target.write_text("from memos.skill_release import hidden\n")
    with pytest.raises(DependencyProbeError, match="forbidden_module"):
        build_manifest(repository / "src")


def test_copy_detects_source_change_and_does_not_reuse_an_existing_bundle(tmp_path):
    repository = dependency_repository(tmp_path)
    manifest = build_manifest(repository / "src")
    target = repository / "src/memos/helpers/helper.py"
    target.write_text("VALUE = 8\n")
    with pytest.raises(DependencyProbeError, match="source_changed"):
        copy_manifest(repository / "src", tmp_path / "copy", manifest)
    refreshed = build_manifest(repository / "src")
    copy_manifest(repository / "src", tmp_path / "fresh", refreshed)
    with pytest.raises(DependencyProbeError, match="destination_exists"):
        copy_manifest(repository / "src", tmp_path / "fresh", refreshed)


def test_manifest_refuses_symlinked_modules_and_resources(tmp_path):
    repository = dependency_repository(tmp_path)
    target = repository / "src/memos/helpers/helper.py"
    target.unlink()
    target.symlink_to(repository / ".env")
    with pytest.raises(DependencyProbeError, match="symlink"):
        build_manifest(repository / "src")


def test_explicit_dynamic_modules_are_copied_without_executing_them(tmp_path):
    repository = dependency_repository(tmp_path)
    resource = repository / "src/memos/helpers/late.py"
    resource.write_text("raise RuntimeError('manifest building must not import code')\n")
    default = build_manifest(repository / "src")
    assert "memos.helpers.late" not in default["modules"]
    explicit = build_manifest(repository / "src", extra_modules=("memos.helpers.late",))
    assert explicit["modules"]["memos.helpers.late"] == "memos/helpers/late.py"


@pytest.mark.parametrize("timeout", [None, True, 0, float("nan"), 121, "20"])
def test_invalid_timeout_is_rejected_before_creating_artifacts(tmp_path, timeout):
    repository = dependency_repository(tmp_path)
    with pytest.raises(DependencyProbeError, match="invalid_timeout"):
        run_probe(repository, tmp_path / "probe", timeout=timeout)
    assert not (tmp_path / "probe").exists()


@pytest.mark.parametrize("health", [None, 1, "yes"])
def test_health_mode_requires_a_boolean(tmp_path, health):
    repository = dependency_repository(tmp_path)
    with pytest.raises(DependencyProbeError, match="invalid_health"):
        run_probe(repository, tmp_path / "probe", health=health)
    assert not (tmp_path / "probe").exists()


def test_health_mode_is_fixed_to_feedback_and_ninety_seconds(tmp_path):
    repository = dependency_repository(tmp_path)
    with pytest.raises(DependencyProbeError, match="invalid_health"):
        run_probe(repository, tmp_path / "target", target="memos.helpers.helper", health=True)
    with pytest.raises(DependencyProbeError, match="invalid_health"):
        run_probe(repository, tmp_path / "timeout", timeout=91, health=True)
    assert not (tmp_path / "target").exists()
    assert not (tmp_path / "timeout").exists()


def test_case_variant_dotenv_is_rejected_even_when_it_exists(tmp_path):
    repository = dependency_repository(tmp_path)
    (repository / "src/memos/.ENV").write_text("PUBLIC_PROBE=1")
    with pytest.raises(DependencyProbeError, match="forbidden_source_path"):
        build_manifest(repository / "src", resources=("memos/.ENV",))


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the qualified macOS backend")
def test_real_os_probe_imports_complete_package_and_denies_host_access(tmp_path, monkeypatch):
    repository = dependency_repository(tmp_path)
    monkeypatch.setenv("DEPENDENCY_PROBE_SECRET_CANARY", "must-not-be-inherited")
    result = run_probe(repository, tmp_path / "probe", timeout=20)
    assert result["passed"] is True
    assert result["child"]["imported"] is True
    assert all(result["child"]["isolationChecks"].values())
    assert "DEPENDENCY_PROBE_SECRET_CANARY" not in result["child"]["environmentKeys"]
    assert result["child"]["pythonFlags"] == {"isolated": 1, "noSite": 1, "dontWriteBytecode": 1}
    modules = result["child"]["modules"]
    assert modules["memos"] == "memos/__init__.py"
    assert modules["memos.mem_feedback.feedback"] == "memos/mem_feedback/feedback.py"
    assert result["bundleUnchanged"] is True
    assert (tmp_path / "probe/isolation.sb").is_file()
    assert (tmp_path / "probe/access.json").is_file()
    assert (tmp_path / "probe/manifest.json").is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the qualified macOS backend")
def test_missing_import_is_a_retained_failure_not_a_wider_mount(tmp_path):
    repository = dependency_repository(tmp_path)
    target = repository / "src/memos/mem_feedback/feedback.py"
    target.write_text("import dependency_probe_missing_package\n")
    result = run_probe(repository, tmp_path / "probe", timeout=20)
    assert result["passed"] is False
    assert result["child"]["failure"]["type"] == "ModuleNotFoundError"
    assert result["child"]["failure"]["module"] == "dependency_probe_missing_package"
    assert result["bundleUnchanged"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the qualified macOS backend")
def test_dotenv_os_rule_does_not_block_a_normal_environment_module(tmp_path):
    repository = dependency_repository(tmp_path)
    helper = repository / "src/memos/helpers/helper.py"
    helper.write_text("from ._environment import VALUE\n")
    (helper.parent / "_environment.py").write_text("VALUE = 7\n")
    result = run_probe(repository, tmp_path / "probe", timeout=20)
    assert result["passed"] is True
    assert result["child"]["isolationChecks"]["dotenvDenied"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the qualified macOS backend")
def test_truncated_access_log_blocks_qualification(tmp_path, monkeypatch):
    repository = dependency_repository(tmp_path)
    monkeypatch.setattr(dependency_probe, "MAX_ACCESS_RECORDS", 2)
    result = run_probe(repository, tmp_path / "probe", timeout=20)
    assert result["passed"] is False
    assert result["child"]["accessComplete"] is False
    assert result["child"]["accessEventCount"] > len(result["child"]["access"])


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the qualified macOS backend")
def test_required_resource_needs_explicit_hash_bound_whitelist(tmp_path):
    repository = dependency_repository(tmp_path)
    helper = repository / "src/memos/helpers/helper.py"
    helper.write_text(
        "from pathlib import Path\n"
        "VALUE = int(Path(__file__).with_name('public-rules.txt').read_text())\n"
    )
    (helper.parent / "public-rules.txt").write_text("7")
    missing = run_probe(repository, tmp_path / "missing", timeout=20)
    assert missing["passed"] is False
    assert missing["child"]["failure"]["type"] == "FileNotFoundError"
    present = run_probe(
        repository,
        tmp_path / "present",
        timeout=20,
        resources=("memos/helpers/public-rules.txt",),
    )
    assert present["passed"] is True
    access = present["child"]["access"]
    assert any(row.get("path") == "memos/helpers/public-rules.txt" for row in access)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the qualified macOS backend")
def test_health_assertion_failure_blocks_an_otherwise_successful_import(tmp_path, monkeypatch):
    repository = dependency_repository(tmp_path)
    producer = tmp_path / "producer/src/memos/skill_release_v2/dependency_probe.py"
    producer.parent.mkdir(parents=True)
    producer.write_text("# public test producer\n")
    producer.with_name("dependency_health.py").write_text(
        "def run_health_checks():\n    raise AssertionError('public health calibration failure')\n"
    )
    monkeypatch.setattr(dependency_probe, "__file__", str(producer))
    result = run_probe(repository, tmp_path / "probe", timeout=20, health=True)
    assert result["passed"] is False
    assert result["child"]["imported"] is True
    assert result["child"]["health"] is None
    assert result["child"]["failure"]["type"] == "AssertionError"
    assert set(result["producerSources"]) == {"dependency_probe.py", "dependency_health.py"}
