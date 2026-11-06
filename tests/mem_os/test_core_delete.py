import uuid

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from memos.mem_os.core import MOSCore


def _mos_with_cube(cube_id: str) -> tuple[MOSCore, MagicMock]:
    text_mem = MagicMock()
    text_mem.delete = MagicMock()
    cube = MagicMock()
    cube.text_mem = text_mem

    mos = object.__new__(MOSCore)
    mos.user_id = "test_user"
    mos.mem_cubes = {cube_id: cube}
    mos._validate_cube_access = MagicMock()
    return mos, text_mem


class _ScopedTextMem:
    """Text memory whose delete_all accepts an explicit user scope."""

    def __init__(self):
        self.delete_all_calls: list[str | None] = []

    def delete_all(self, user_name: str | None = None) -> None:
        self.delete_all_calls.append(user_name)


class _UnscopedTextMem:
    """Per-cube text memory (naive/general) that takes no tenant scope."""

    def __init__(self):
        self.calls = 0

    def delete_all(self) -> None:
        self.calls += 1


def _mos_with_user(cube_id: str, user) -> tuple[MOSCore, _ScopedTextMem]:
    text_mem = _ScopedTextMem()
    cube = MagicMock()
    cube.text_mem = text_mem

    mos = object.__new__(MOSCore)
    mos.user_id = "test_user"
    mos.mem_cubes = {cube_id: cube}
    mos._validate_cube_access = MagicMock()
    mos.user_manager = MagicMock()
    mos.user_manager.get_user.return_value = user
    return mos, text_mem


def test_delete_forwards_a_single_id_as_a_list():
    memory_id = str(uuid.uuid4())
    mos, text_mem = _mos_with_cube("cube-a")

    mos.delete("cube-a", memory_id)

    # TreeTextMemory.delete iterates memory_ids; a bare string would be
    # iterated character by character and delete nothing.
    text_mem.delete.assert_called_once_with([memory_id])


def test_delete_all_scopes_to_the_target_user_name():
    mos, text_mem = _mos_with_user("cube-a", SimpleNamespace(user_name="alice"))

    mos.delete_all("cube-a")

    # The graph store must receive an explicit tenant; without it a shared
    # graph whose config.user_name is empty would clear every tenant.
    assert text_mem.delete_all_calls == ["alice"]


def test_delete_all_uses_the_explicit_user_id_scope():
    mos, text_mem = _mos_with_user("cube-a", SimpleNamespace(user_name="bob"))

    mos.delete_all("cube-a", user_id="other-user")

    mos._validate_cube_access.assert_called_with("other-user", "cube-a")
    mos.user_manager.get_user.assert_called_once_with("other-user")
    assert text_mem.delete_all_calls == ["bob"]


def test_delete_all_calls_unscoped_stores_without_a_tenant_argument():
    text_mem = _UnscopedTextMem()
    cube = MagicMock()
    cube.text_mem = text_mem

    mos = object.__new__(MOSCore)
    mos.user_id = "test_user"
    mos.mem_cubes = {"cube-a": cube}
    mos._validate_cube_access = MagicMock()
    mos.user_manager = MagicMock()
    mos.user_manager.get_user.return_value = SimpleNamespace(user_name="alice")

    mos.delete_all("cube-a")

    assert text_mem.calls == 1


def test_delete_all_resolves_the_default_cube_before_validating():
    mos, text_mem = _mos_with_user("cube-a", SimpleNamespace(user_name="alice"))
    mos.user_manager.get_user_cubes.return_value = [SimpleNamespace(cube_id="cube-a")]

    mos.delete_all()

    mos._validate_cube_access.assert_called_once_with("test_user", "cube-a")
    assert text_mem.delete_all_calls == ["alice"]


def test_delete_all_refuses_when_user_name_cannot_be_resolved():
    mos, text_mem = _mos_with_user("cube-a", None)

    with pytest.raises(ValueError, match="refusing"):
        mos.delete_all("cube-a")

    assert text_mem.delete_all_calls == []


def test_delete_all_refuses_when_user_name_is_empty():
    mos, text_mem = _mos_with_user("cube-a", SimpleNamespace(user_name=""))

    with pytest.raises(ValueError, match="refusing"):
        mos.delete_all("cube-a")

    assert text_mem.delete_all_calls == []
