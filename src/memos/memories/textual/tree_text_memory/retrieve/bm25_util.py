import hashlib
import threading

import numpy as np

from sklearn.feature_extraction.text import TfidfVectorizer

from memos.dependency import require_python_package
from memos.log import get_logger
from memos.memories.textual.tree_text_memory.retrieve.retrieve_utils import FastTokenizer
from memos.utils import timed


logger = get_logger(__name__)
# 进程级缓存：cache_key -> BM25Okapi（或 None 表示该语料没有可索引的文本）
_BM25_CACHE = None
_CACHE_MAX_SIZE = 100
_CACHE_LOCK = threading.Lock()


def _get_cache():
    global _BM25_CACHE
    if _BM25_CACHE is None:
        from cachetools import LRUCache

        _BM25_CACHE = LRUCache(maxsize=_CACHE_MAX_SIZE)
    return _BM25_CACHE


def _corpus_fingerprint(corpus: list[str]) -> str:
    """语料内容指纹，用来判断缓存里的 BM25 索引是否还对应当前这批文档。"""
    digest = hashlib.sha1()
    for doc in corpus:
        digest.update(doc.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


class EnhancedBM25:
    """Enhanced BM25 with Spacy tokenization and TF-IDF reranking"""

    @require_python_package(import_name="cachetools", install_command="pip install cachetools")
    def __init__(self, tokenizer=None, en_model="en_core_web_sm", zh_model="zh_core_web_sm"):
        """
        Initialize Enhanced BM25 with memory management
        """
        if tokenizer is None:
            # 只有 BM25 检索启用 jieba 搜索模式; 其它模块仍用上游精确分词。
            self.tokenizer = FastTokenizer(search_mode=True)
        else:
            self.tokenizer = tokenizer
        self._current_tfidf = None

        # 缓存是进程级的：每 new 一个 EnhancedBM25 就重置一次会让索引反复重建。
        _get_cache()

    def _tokenize_doc(self, text):
        """
        Tokenize a single document using SpacyTokenizer
        """
        return self.tokenizer.tokenize_mixed(text, lang="auto")

    @staticmethod
    def _cache_key(corpus_name: str, corpus: list[str]) -> tuple[str, str]:
        """缓存键 = 语料名 + 内容指纹。

        只按 corpus_name 缓存时，同一用户新增/删除记忆后仍会命中旧快照，
        BM25 的下标会映射到旧语料的顺序上。指纹是 O(N)，远低于重新分词与建索引。
        """
        return (corpus_name, _corpus_fingerprint(corpus))

    @require_python_package(import_name="rank_bm25", install_command="pip install rank-bm25")
    def _prepare_corpus_data(self, corpus, corpus_name="default"):
        from rank_bm25 import BM25Okapi

        cache_key = self._cache_key(corpus_name, corpus)
        with _CACHE_LOCK:
            cache = _get_cache()
            if cache_key in cache:
                logger.debug("[bm25] corpus cache hit: %s (%d docs)", corpus_name, len(corpus))
                return cache[cache_key]

            logger.debug("bm25 cache miss: %s (%d docs)", corpus_name, len(corpus))
            tokenized_corpus = [self._tokenize_doc(doc) for doc in corpus]
            logger.debug("[bm25] corpus cache miss: %s (%d docs)", corpus_name, len(corpus))
            tokenized_corpus = [self._tokenize_doc(doc) for doc in corpus]
            if not any(tokenized_corpus):
                # 全是空文档时 BM25Okapi 的 avgdl 为 0，会算出 NaN 分数
                cache[cache_key] = None
                return None

            bm25_model = BM25Okapi(tokenized_corpus)
            cache[cache_key] = bm25_model
            return bm25_model

    def clear_cache(self, corpus_name=None):
        """清掉某个语料名的全部版本；不传则清空整个进程级缓存。"""
        with _CACHE_LOCK:
            cache = _get_cache()
            if corpus_name is None:
                cache.clear()
                return
            for key in [k for k in cache if k[0] == corpus_name]:
                del cache[key]

    def get_cache_info(self):
        """Get current cache information"""
        with _CACHE_LOCK:
            cache = _get_cache()
            return {
                "cache_size": len(cache),
                "max_cache_size": _CACHE_MAX_SIZE,
                "cached_corpora": sorted({key[0] for key in cache}),
            }

    def _search_docs(
        self,
        query: str,
        corpus: list[str],
        corpus_name="test",
        top_k=50,
        use_tfidf=False,
        rerank_candidates_multiplier=2,
        cleanup=False,
    ):
        """
        Args:
            query: Search query string
            corpus: List of document texts
            top_k: Number of top results to return
            rerank_candidates_multiplier: Multiplier for candidate selection
            cleanup: 检索结束后是否释放临时 TF-IDF 矩阵
        """
        if not corpus:
            return []

        logger.info(f"Searching {len(corpus)} documents for query: '{query}'")

        try:
            # Prepare BM25 model
            bm25_model = self._prepare_corpus_data(corpus, corpus_name=corpus_name)
            if bm25_model is None:
                return []

            tokenized_query = self._tokenize_doc(query)
            tokenized_query = list(dict.fromkeys(tokenized_query))

            # Get BM25 scores
            bm25_scores = bm25_model.get_scores(tokenized_query)

            # argsort 对全 0 分数会按原始下标返回：与查询毫无词重叠的文档必须剔掉，
            # 否则每次查询都会往候选池里塞一批无关记忆，白耗 rerank 预算。
            # 这里按词重叠判断而不是按 score > 0 判断——某个词的 idf 恰好为 0 时
            # （df == corpus_size / 2）分数也是 0，那是合法命中。
            query_terms = set(tokenized_query)
            matched = np.array(
                [
                    bool(query_terms.intersection(freqs))
                    for freqs in getattr(bm25_model, "doc_freqs", [{}] * len(corpus))
                ]
            )

            # Select candidates
            candidate_count = min(top_k * rerank_candidates_multiplier, len(corpus))
            candidate_indices = np.argsort(bm25_scores)[-candidate_count:][::-1]
            candidate_indices = candidate_indices[matched[candidate_indices]]
            combined_scores = bm25_scores[candidate_indices]

            if use_tfidf:
                # Create TF-IDF for this search
                tfidf = TfidfVectorizer(
                    tokenizer=self._tokenize_doc, lowercase=False, token_pattern=None
                )
                tfidf_matrix = tfidf.fit_transform(corpus)

                # TF-IDF reranking
                query_vec = tfidf.transform([query])
                tfidf_similarities = (
                    (tfidf_matrix[candidate_indices] * query_vec.T).toarray().flatten()
                )

                # Combine scores
                combined_scores = 0.7 * bm25_scores[candidate_indices] + 0.3 * tfidf_similarities

            sorted_candidate_indices = candidate_indices[np.argsort(combined_scores)[::-1][:top_k]]
            sorted_combined_scores = np.sort(combined_scores)[::-1][:top_k]

            # build result list
            bm25_recalled_results = []
            for rank, (doc_idx, combined_score) in enumerate(
                zip(sorted_candidate_indices, sorted_combined_scores, strict=False), 1
            ):
                bm25_score = bm25_scores[doc_idx]

                candidate_pos = np.where(candidate_indices == doc_idx)[0][0]
                tfidf_score = tfidf_similarities[candidate_pos] if use_tfidf else 0

                bm25_recalled_results.append(
                    {
                        "text": corpus[doc_idx],
                        "bm25_score": float(bm25_score),
                        "tfidf_score": float(tfidf_score),
                        "combined_score": float(combined_score),
                        "rank": rank,
                        "doc_index": int(doc_idx),
                    }
                )

            logger.debug(f"Search completed: found {len(bm25_recalled_results)} results")
            return bm25_recalled_results

        except Exception as e:
            logger.error(f"BM25 search failed: {e}")
            return []
        finally:
            if cleanup:
                self._cleanup_memory()

    @staticmethod
    def _node_to_doc(node_dict: dict) -> str:
        """把图节点摊平成 BM25 文档。

        生产图里只有一部分节点带 ``key``（preference / topic 类节点只有 topic 或
        memory），所以按 key -> topic -> memory 逐级退化；不能因为个别节点缺字段
        就让整批召回丢失。文档顺序必须与 node_dicts 一一对应（doc_index 靠它回查），
        因此这里不过滤空文档。
        """
        metadata = node_dict.get("metadata") or {}
        head = metadata.get("key") or metadata.get("topic") or node_dict.get("memory") or ""
        tags = metadata.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        return " ".join([str(head), *(str(tag) for tag in tags)]).strip()

    def _cleanup_memory(self):
        """释放本次检索临时持有的 TF-IDF 矩阵。"""
        self._current_tfidf = None

    @timed
    def search(self, query: str, node_dicts: list[dict], corpus_name="default", **kwargs):
        """
        Search with BM25 and optional TF-IDF reranking
        """
        try:
            corpus_list = [self._node_to_doc(node_dict) for node_dict in node_dicts]
            recalled_results = self._search_docs(
                query, corpus_list, corpus_name=corpus_name, **kwargs
            )
            return [node_dicts[item["doc_index"]] for item in recalled_results]
        except Exception as e:
            logger.error(f"Error in bm25 search: {e}")
            return []
