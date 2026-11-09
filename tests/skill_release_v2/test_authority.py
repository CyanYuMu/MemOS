"""Project contracts cannot masquerade as Memory or silently accept drift."""

import copy
import subprocess

from pathlib import Path

import pytest

from memos.skill_release_v2.authority import (
    CONTRACT_PATH,
    extract_contract_context,
    recheck_contract_context,
    validate_contract_context,
)
from memos.skill_release_v2.gate import digest_json


SOURCE = '''from abc import abstractmethod

class BaseGraphDB:
    @abstractmethod
    def update_node(self, id: str, fields: dict, user_name=None):
        """Update fields for a node; this does not promise transactional behavior."""

    def unrelated(self):
        return "not in the model context"
'''


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    source = root / CONTRACT_PATH
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE)
    git(root, "init", "-q")
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "fixed contract",
    )
    return root, git(root, "rev-parse", "HEAD")


def context(repository):
    root, revision = repository
    return extract_contract_context(root, project_id="project-test", repository_revision=revision)


def test_context_binds_complete_method_without_adding_a_memory_sample(repository):
    item = context(repository)
    validate_contract_context(item)
    assert item["empirical_sample"] is False
    assert item["kind"] == "project_contract_context"
    assert item["authority_kind"] == "project_interface_contract"
    assert item["repository_relative_path"] == CONTRACT_PATH
    assert "@abstractmethod" in item["excerpt"] and "transactional" in item["excerpt"]
    assert "unrelated" not in item["excerpt"] and "not in the model context" not in item["excerpt"]
    assert not {"run_id", "source_id", "qualification", "lineage_id"} & item.keys()
    result = recheck_contract_context(item, authority_bindings={"project-test": repository[0]})
    assert result == {"context_id": item["context_id"], "status": "healthy"}


@pytest.mark.parametrize("revision", ["HEAD", "main", "a" * 39, "--help", "a" * 64])
def test_extraction_requires_a_full_fixed_commit(repository, revision):
    with pytest.raises(ValueError, match="authority_revision"):
        extract_contract_context(
            repository[0], project_id="project-test", repository_revision=revision
        )


def test_uncommitted_contract_cannot_be_attributed_to_old_commit(repository):
    path = repository[0] / CONTRACT_PATH
    path.write_text(SOURCE + "\n# current bytes differ\n")
    with pytest.raises(ValueError, match="authority_commit_file_mismatch"):
        context(repository)


@pytest.mark.parametrize(
    "field,value",
    [
        ("empirical_sample", True),
        ("source_id", "fake-memory"),
        ("repository_relative_path", "../../private"),
        ("symbol", "Other.update_node"),
        ("excerpt", "def update_node(): pass\n"),
        ("ast_sha256", "a" * 64),
        ("context_id", "b" * 64),
        ("file_sha256", "not-a-digest"),
    ],
)
def test_context_shape_and_nested_bindings_are_verified(repository, field, value):
    item = context(repository)
    item[field] = value
    if field != "context_id":
        item["context_id"] = digest_json({k: v for k, v in item.items() if k != "context_id"})
    with pytest.raises(ValueError, match="authority_"):
        validate_contract_context(item)


def test_whole_file_drift_is_rejected_even_if_method_stays_equal(repository):
    item = context(repository)
    path = repository[0] / CONTRACT_PATH
    path.write_text(SOURCE.replace('return "not in the model context"', 'return "changed"'))
    with pytest.raises(ValueError, match="authority_file_changed"):
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})
    path.write_text(SOURCE)
    assert (
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})["status"]
        == "healthy"
    )


def test_new_head_does_not_rebase_frozen_contract(repository):
    item = context(repository)
    git(
        repository[0],
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "unrelated change",
    )
    assert git(repository[0], "rev-parse", "HEAD") != item["repository_revision"]
    assert (
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})["status"]
        == "healthy"
    )


def test_trusted_project_binding_is_required(repository):
    with pytest.raises(ValueError, match="authority_binding_missing"):
        recheck_contract_context(context(repository), authority_bindings={"other": repository[0]})


def test_symlinked_authority_is_rejected_even_when_bytes_are_identical(repository, tmp_path):
    item = context(repository)
    path = repository[0] / CONTRACT_PATH
    target = tmp_path / "same.py"
    target.write_text(SOURCE)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="authority_unsafe_path"):
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})


def test_duplicate_methods_are_not_silently_selected(repository):
    root, _ = repository
    path = root / CONTRACT_PATH
    path.write_text(SOURCE + "\n    def update_node(self):\n        pass\n")
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "ambiguous contract",
    )
    with pytest.raises(ValueError, match="authority_symbol"):
        extract_contract_context(
            root, project_id="project-test", repository_revision=git(root, "rev-parse", "HEAD")
        )


def test_forged_full_file_hash_is_rejected_by_live_readback(repository):
    item = copy.deepcopy(context(repository))
    item["file_sha256"] = "a" * 64
    item["context_id"] = digest_json({k: v for k, v in item.items() if k != "context_id"})
    validate_contract_context(item)  # Self-consistency is not live authenticity.
    with pytest.raises(ValueError, match="authority_file_changed"):
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})


def test_tree_object_cannot_be_mislabeled_as_repository_commit(repository):
    root, _ = repository
    tree = git(root, "rev-parse", "HEAD^{tree}")
    with pytest.raises(ValueError, match="authority_revision_type"):
        extract_contract_context(root, project_id="project-test", repository_revision=tree)


def test_git_replace_cannot_reassign_source_to_an_old_commit(repository):
    root, original = repository
    (root / CONTRACT_PATH).write_text(SOURCE.replace("fields: dict", "unexpected: dict"))
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "replacement",
    )
    replacement = git(root, "rev-parse", "HEAD")
    git(root, "replace", original, replacement)
    assert b"unexpected" in subprocess.check_output(
        ["git", "-C", str(root), "show", original + ":" + CONTRACT_PATH]
    )
    with pytest.raises(ValueError, match="authority_commit_file_mismatch"):
        extract_contract_context(root, project_id="project-test", repository_revision=original)


@pytest.mark.parametrize(
    "binding",
    [
        "    async def update_node(self):\n        pass\n",
        "    update_node = lambda self: None\n",
        "    if True:\n        def update_node(self):\n            pass\n",
    ],
)
def test_rebound_method_is_not_presented_as_the_original_contract(repository, binding):
    root, _ = repository
    (root / CONTRACT_PATH).write_text(SOURCE + "\n" + binding)
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "rebound method",
    )
    with pytest.raises(ValueError, match="authority_symbol"):
        extract_contract_context(
            root, project_id="project-test", repository_revision=git(root, "rev-parse", "HEAD")
        )


def test_extractor_identity_requires_the_current_approved_producer(repository):
    item = context(repository)
    item["extractor_digest"] = "a" * 64
    item["context_id"] = digest_json({k: v for k, v in item.items() if k != "context_id"})
    validate_contract_context(item)
    with pytest.raises(ValueError, match="authority_extractor_changed"):
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})


def test_ambient_git_directory_cannot_override_host_binding(repository, monkeypatch):
    item = context(repository)
    monkeypatch.setenv("GIT_DIR", "/nonexistent-git-override")
    monkeypatch.setenv("GIT_WORK_TREE", "/nonexistent-worktree-override")
    assert (
        recheck_contract_context(item, authority_bindings={"project-test": repository[0]})["status"]
        == "healthy"
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "\nBaseGraphDB.update_node = lambda self: None\n",
        "\nBaseGraphDB = type('BaseGraphDB', (), {'update_node': lambda self: None})\n",
        "\n    from builtins import str as update_node\n",
    ],
)
def test_dynamic_symbol_rebinding_is_outside_static_selector(repository, suffix):
    root, _ = repository
    (root / CONTRACT_PATH).write_text(SOURCE + suffix)
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "dynamic binding",
    )
    with pytest.raises(ValueError, match="authority_symbol"):
        extract_contract_context(
            root, project_id="project-test", repository_revision=git(root, "rev-parse", "HEAD")
        )


def test_nested_copy_cannot_borrow_ancestor_repository_history(repository):
    root, original = repository
    nested = root / "not-a-repository"
    copy = nested / CONTRACT_PATH
    copy.parent.mkdir(parents=True)
    copy.write_text(SOURCE)
    (root / CONTRACT_PATH).write_text(SOURCE.replace("fields: dict", "unexpected: dict"))
    with pytest.raises(ValueError, match="authority_repository_root"):
        extract_contract_context(nested, project_id="project-test", repository_revision=original)


def test_actual_project_interface_fits_the_declarative_selector(repository):
    root, _ = repository
    source = Path(__file__).resolve().parents[2] / CONTRACT_PATH
    (root / CONTRACT_PATH).write_bytes(source.read_bytes())
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "full project interface",
    )
    item = extract_contract_context(
        root, project_id="project-test", repository_revision=git(root, "rev-parse", "HEAD")
    )
    assert "fields: dict[str, Any]" in item["excerpt"]
    assert (
        recheck_contract_context(item, authority_bindings={"project-test": root})["status"]
        == "healthy"
    )


@pytest.mark.parametrize(
    "transform",
    [
        lambda source: source
        + '\nCONSTANT = setattr(globals()["BaseGraphDB"], "update_node", lambda self: None)\n',
        lambda source: source + '\nCONSTANT = (other := "dynamic")\n',
        lambda source: source.replace(
            "class BaseGraphDB:",
            '@(lambda cls: type("BaseGraphDB", (), {"update_node": lambda self: None}))\nclass BaseGraphDB:',
        ),
        lambda source: source.replace(
            "class BaseGraphDB:", 'class BaseGraphDB(type("Other", (), {})):'
        ),
        lambda source: source.replace(
            "def unrelated(self):", 'def unrelated(self, side_effect=print("during-definition")):'
        ),
    ],
)
def test_executable_declaration_headers_and_constants_are_rejected(repository, transform):
    root, _ = repository
    (root / CONTRACT_PATH).write_text(transform(SOURCE))
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "unsupported declaration",
    )
    with pytest.raises(ValueError, match="authority_symbol"):
        extract_contract_context(
            root, project_id="project-test", repository_revision=git(root, "rev-parse", "HEAD")
        )


def test_class_method_cannot_shadow_an_authorized_decorator(repository):
    root, _ = repository
    source = SOURCE.replace(
        "class BaseGraphDB:",
        "class BaseGraphDB:\n    def abstractmethod(original):\n        return lambda self: None\n",
    )
    (root / CONTRACT_PATH).write_text(source)
    git(root, "add", CONTRACT_PATH)
    git(
        root,
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.invalid",
        "commit",
        "-qm",
        "shadowed decorator",
    )
    with pytest.raises(ValueError, match="authority_symbol"):
        extract_contract_context(
            root, project_id="project-test", repository_revision=git(root, "rev-parse", "HEAD")
        )
