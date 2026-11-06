"""BM25 缓存与中文分词的回归测试。

这几条覆盖的是 master 上的真实缺陷:
- 缓存只按 corpus_name 命中,写入新记忆后 BM25 仍返回旧快照;
- 每次 new EnhancedBM25 都会重建全局 LRU,缓存被其它实例清空;
- cleanup=True 调用了不存在的 _cleanup_memory,异常被吞后返回空结果;
- jieba 精确模式不产子词,复合词查询召回不到。
"""

import pytest

from memos.memories.textual.tree_text_memory.retrieve.retrieve_utils import FastTokenizer


pytest.importorskip("rank_bm25")
pytest.importorskip("jieba")

from memos.memories.textual.tree_text_memory.retrieve.bm25_util import EnhancedBM25


def _bm25() -> EnhancedBM25:
    # EnhancedBM25 默认给 BM25 语料开启 jieba 搜索模式,这里保持一致。
    tokenizer = FastTokenizer(use_stopwords=False, search_mode=True)
    model = EnhancedBM25(tokenizer=tokenizer)
    model.clear_cache()
    return model


def test_bm25_default_tokenizer_enables_search_mode_only_here():
    model = EnhancedBM25()
    assert model.tokenizer.search_mode is True
    # 其它模块直接实例化的分词器保持上游精确模式。
    assert FastTokenizer().search_mode is False


def test_cache_invalidates_when_corpus_changes():
    bm25 = _bm25()
    first = bm25._search_docs("alpha", ["alpha beta", "gamma delta"], corpus_name="user-1", top_k=2)
    assert first and first[0]["text"] == "alpha beta"

    # 同一 corpus_name、同样的文档数,但内容已经变了。旧实现会命中旧模型,
    # “epsilon”在旧语料里没有词频,新文档不会被召回。
    second = bm25._search_docs(
        "epsilon", ["epsilon zeta", "gamma delta"], corpus_name="user-1", top_k=2
    )
    assert second, "corpus 变化后 BM25 仍返回空结果"
    assert second[0]["text"] == "epsilon zeta"


def test_cache_is_shared_across_instances():
    bm25 = _bm25()
    bm25._search_docs("alpha", ["alpha beta"], corpus_name="shared", top_k=1)
    info_before = bm25.get_cache_info()
    assert info_before["cache_size"] == 1

    # 新实例不应把进程级缓存清空。
    other = EnhancedBM25(tokenizer=FastTokenizer(use_stopwords=False))
    info_after = other.get_cache_info()
    assert info_after["cache_size"] == 1
    assert "shared" in info_after["cached_corpora"]


def test_clear_cache_removes_all_versions_of_a_corpus():
    bm25 = _bm25()
    bm25._search_docs("alpha", ["alpha"], corpus_name="c1", top_k=1)
    bm25._search_docs("beta", ["beta"], corpus_name="c1", top_k=1)
    bm25._search_docs("gamma", ["gamma"], corpus_name="c2", top_k=1)
    assert bm25.get_cache_info()["cache_size"] == 3

    bm25.clear_cache("c1")
    info = bm25.get_cache_info()
    assert info["cache_size"] == 1
    assert info["cached_corpora"] == ["c2"]


def test_cleanup_flag_does_not_break_search():
    bm25 = _bm25()
    # 旧实现里 cleanup=True 会调用不存在的 _cleanup_memory,异常被外层
    # except 吞掉后返回 [],看起来像“没搜到”。
    result = bm25._search_docs(
        "alpha", ["alpha beta", "gamma"], corpus_name="cleanup", top_k=1, cleanup=True
    )
    assert result and result[0]["text"] == "alpha beta"


def test_chinese_tokenizer_can_use_search_mode():
    tokenizer = FastTokenizer(use_stopwords=False, search_mode=True)
    tokens = tokenizer.tokenize_mixed("中华人民共和国")
    # 搜索模式会同时产出长词和子词,精确模式只有整词。
    assert "中华人民共和国" in tokens
    assert any(token in tokens for token in ("中华", "人民", "共和"))


def test_chinese_tokenizer_defaults_to_exact_mode():
    tokenizer = FastTokenizer(use_stopwords=False)
    tokens = tokenizer.tokenize_mixed("中华人民共和国")
    assert tokens == ["中华人民共和国"]


def test_chinese_tokenizer_can_fall_back_to_exact_mode():
    tokenizer = FastTokenizer(use_stopwords=False, search_mode=False)
    tokens = tokenizer.tokenize_mixed("中华人民共和国")
    assert tokens == ["中华人民共和国"]


def test_english_tokenization_is_unchanged():
    tokenizer = FastTokenizer(use_stopwords=False)
    assert tokenizer.tokenize_mixed("Hello, World! 42") == ["hello", "world", "42"]
