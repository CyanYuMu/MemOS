"""The deterministic embedder used by prompt dry runs."""

import pytest

from memos.configs.embedder import EmbedderConfigFactory
from memos.embedders.factory import EmbedderFactory


def _embedder(dims: int = 8):
    config = EmbedderConfigFactory.model_validate(
        {"backend": "placeholder", "config": {"embedding_dims": dims}}
    )
    return EmbedderFactory.from_config(config)


def test_vectors_are_stable_and_batched():
    embedder = _embedder()
    first = embedder.embed(["蓝绿发布", "blue green deploy"])
    second = embedder.embed(["蓝绿发布", "blue green deploy"])
    assert first == second
    assert len(first) == 2
    assert all(len(vector) == 8 for vector in first)
    assert first[0] != first[1]


def test_dimension_is_configurable():
    assert len(_embedder(dims=4).embed(["x"])[0]) == 4


def test_empty_text_still_produces_a_vector():
    vector = _embedder().embed([""])[0]
    assert len(vector) == 8


@pytest.mark.parametrize("text", ["用户喜欢靠窗的座位", "the migration is blocked"])
def test_vectors_are_not_trivially_uniform(text):
    vector = _embedder().embed([text])[0]
    assert len(set(vector)) > 1
