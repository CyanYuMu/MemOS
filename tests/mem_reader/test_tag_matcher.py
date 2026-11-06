"""Semantic selection of ``custom_tags`` by embedding similarity."""

from types import SimpleNamespace

import pytest

from memos.mem_reader import tag_matcher
from memos.mem_reader.tag_matcher import apply_semantic_custom_tags, semantic_tag_match_enabled


VECTORS = {
    "travel": [1.0, 0.0, 0.0],
    "food": [0.0, 1.0, 0.0],
    "work": [0.0, 0.0, 1.0],
    "went to Paris": [0.9, 0.1, 0.0],
    "likes noodles": [0.1, 0.95, 0.0],
    "unrelated": [0.4, 0.4, 0.4],
}


class FakeEmbedder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [VECTORS[text] for text in texts]


def _item(text, tags=None, memory_type="LongTermMemory", embedding=None):
    return SimpleNamespace(
        memory=text,
        metadata=SimpleNamespace(tags=tags, memory_type=memory_type, embedding=embedding),
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tag_matcher._tag_vectors.clear()
    for name in (
        "CUSTOM_TAGS_SEMANTIC_MATCH",
        "CUSTOM_TAGS_MATCH_THRESHOLD",
        "CUSTOM_TAGS_MATCH_TOP_K",
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_by_default_and_does_not_touch_the_embedder():
    embedder = FakeEmbedder()
    item = _item("went to Paris", tags=["trip"])

    changed = apply_semantic_custom_tags([item], ["travel"], embedder)

    assert changed == 0
    assert item.metadata.tags == ["trip"]
    assert embedder.calls == []


def test_env_switch_enables_matching(monkeypatch):
    monkeypatch.setenv("CUSTOM_TAGS_SEMANTIC_MATCH", "true")

    assert semantic_tag_match_enabled() is True
    assert semantic_tag_match_enabled(False) is False
    assert semantic_tag_match_enabled(True) is True


def test_close_tags_are_merged_after_the_reader_tags():
    item = _item("went to Paris", tags=["trip"])

    changed = apply_semantic_custom_tags(
        [item], ["travel", "food", "work"], FakeEmbedder(), enabled=True
    )

    assert changed == 1
    assert item.metadata.tags == ["trip", "travel"]


def test_each_memory_gets_its_own_tags():
    paris, noodles = _item("went to Paris"), _item("likes noodles")

    apply_semantic_custom_tags(
        [paris, noodles], ["travel", "food", "work"], FakeEmbedder(), enabled=True
    )

    assert paris.metadata.tags == ["travel"]
    assert noodles.metadata.tags == ["food"]


def test_tags_below_the_threshold_are_not_added():
    item = _item("unrelated")

    changed = apply_semantic_custom_tags(
        [item], ["travel", "food", "work"], FakeEmbedder(), enabled=True, threshold=0.9
    )

    assert changed == 0
    assert not item.metadata.tags


def test_top_k_limits_how_many_tags_are_added():
    item = _item("unrelated")

    apply_semantic_custom_tags(
        [item], ["travel", "food", "work"], FakeEmbedder(), enabled=True, threshold=0.1, top_k=2
    )

    assert len(item.metadata.tags) == 2


def test_existing_tags_are_not_duplicated():
    item = _item("went to Paris", tags=["travel"])

    changed = apply_semantic_custom_tags([item], ["travel"], FakeEmbedder(), enabled=True)

    assert changed == 0
    assert item.metadata.tags == ["travel"]


def test_stored_embeddings_are_reused():
    embedder = FakeEmbedder()
    item = _item("went to Paris", embedding=VECTORS["went to Paris"])

    apply_semantic_custom_tags([item], ["travel"], embedder, enabled=True)

    assert embedder.calls == [["travel"]]
    assert item.metadata.tags == ["travel"]


def test_missing_embeddings_are_computed_in_one_batch():
    embedder = FakeEmbedder()
    items = [_item("went to Paris"), _item("likes noodles")]

    apply_semantic_custom_tags(items, ["travel"], embedder, enabled=True)

    assert embedder.calls == [["travel"], ["went to Paris", "likes noodles"]]


def test_candidate_vectors_are_cached_across_calls():
    embedder = FakeEmbedder()

    for _ in range(2):
        apply_semantic_custom_tags(
            [_item("went to Paris", embedding=VECTORS["went to Paris"])],
            ["travel"],
            embedder,
            enabled=True,
        )

    assert embedder.calls == [["travel"]]


def test_candidate_vectors_are_not_shared_between_embedders():
    class OtherEmbedder:
        def embed(self, texts):
            return [[0.0, 1.0] for _ in texts]

    apply_semantic_custom_tags(
        [_item("went to Paris", embedding=VECTORS["went to Paris"])],
        ["travel"],
        FakeEmbedder(),
        enabled=True,
    )
    other = _item("x", embedding=[0.0, 1.0])

    changed = apply_semantic_custom_tags([other], ["travel"], OtherEmbedder(), enabled=True)

    assert changed == 1
    assert other.metadata.tags == ["travel"]


def test_blank_and_duplicate_candidates_are_dropped():
    embedder = FakeEmbedder()
    item = _item("went to Paris", embedding=VECTORS["went to Paris"])

    apply_semantic_custom_tags([item], [" travel ", "travel", "", "  "], embedder, enabled=True)

    assert embedder.calls == [["travel"]]
    assert item.metadata.tags == ["travel"]


@pytest.mark.parametrize("memory_type", ["RawFileMemory", "PreferenceMemory"])
def test_non_text_memory_types_are_skipped(memory_type):
    item = _item("went to Paris", memory_type=memory_type)

    assert apply_semantic_custom_tags([item], ["travel"], FakeEmbedder(), enabled=True) == 0
    assert not item.metadata.tags


def test_embedder_failure_keeps_reader_tags():
    class Broken:
        def embed(self, texts):
            raise RuntimeError("down")

    item = _item("went to Paris", tags=["trip"])

    assert apply_semantic_custom_tags([item], ["travel"], Broken(), enabled=True) == 0
    assert item.metadata.tags == ["trip"]


def test_no_candidates_or_no_embedder_is_a_noop():
    item = _item("went to Paris")

    assert apply_semantic_custom_tags([item], None, FakeEmbedder(), enabled=True) == 0
    assert apply_semantic_custom_tags([item], ["travel"], None, enabled=True) == 0


def test_env_threshold_and_top_k_are_honoured(monkeypatch):
    monkeypatch.setenv("CUSTOM_TAGS_MATCH_THRESHOLD", "0.1")
    monkeypatch.setenv("CUSTOM_TAGS_MATCH_TOP_K", "1")
    item = _item("unrelated")

    apply_semantic_custom_tags([item], ["travel", "food", "work"], FakeEmbedder(), enabled=True)

    assert len(item.metadata.tags) == 1
