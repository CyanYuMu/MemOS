"""检索默认模式：生产不允许静默落到 fast。"""

import importlib

from memos.api import product_models
from memos.types import SearchMode


def _default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MEMOS_SEARCH_MODE", raising=False)
    else:
        monkeypatch.setenv("MEMOS_SEARCH_MODE", value)
    importlib.reload(product_models)
    return product_models.APISearchRequest(
        user_id="u", query="q", readable_cube_ids=["c"]
    ).mode


def test_default_is_fine(monkeypatch):
    assert _default(monkeypatch, None) == SearchMode.FINE


def test_env_can_pick_any_mode(monkeypatch):
    assert _default(monkeypatch, "mixture") == SearchMode.MIXTURE
    assert _default(monkeypatch, "fast") == SearchMode.FAST


def test_invalid_env_falls_back_to_fine(monkeypatch):
    assert _default(monkeypatch, "turbo") == SearchMode.FINE
