"""MMR 去重依赖向量：fine 路径不能只在 dedup=sim 时才带 embedding。"""

import memos.api.handlers.add_handler  # noqa: F401  先打破 single_cube ↔ add_handler 的循环导入

from memos.multi_mem_cube.single_cube import _includes_embedding


def test_mmr_needs_embeddings():
    assert _includes_embedding("mmr") is True


def test_sim_needs_embeddings():
    assert _includes_embedding("sim") is True


def test_no_dedup_does_not_need_embeddings():
    assert _includes_embedding("no") is False
    assert _includes_embedding(None) is False
