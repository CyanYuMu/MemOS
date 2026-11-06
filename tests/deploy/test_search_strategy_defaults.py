"""Tests for configurable search strategy defaults."""

import pytest

from memos.api.config import APIConfig


SWITCHES = {
    "BM25_CALL": "bm25",
    "VEC_COT_CALL": "cot",
    "FULLTEXT_CALL": "fulltext",
}


def _strategy():
    cfg = APIConfig.get_default_cube_config()
    return cfg.text_mem.config.search_strategy


def test_code_defaults_enable_the_cloud_aligned_recall_paths(monkeypatch):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)

    strategy = _strategy()

    for key in SWITCHES.values():
        assert strategy[key] is True


@pytest.mark.parametrize("env_name,key", SWITCHES.items())
def test_env_false_turns_each_path_off(monkeypatch, env_name, key):
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_name, "false")

    strategy = _strategy()

    assert strategy[key] is False
    # Switching one path off leaves the other two on.
    assert all(strategy[other] is True for other in SWITCHES.values() if other != key)
