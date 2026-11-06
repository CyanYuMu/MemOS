"""Tokenization helpers for lexical (BM25) retrieval.

The tokenizer is deliberately dependency-tolerant:

* ``jieba`` is used for CJK runs when it can be imported.  When it is missing we
  fall back to character uni/bi-grams, which keeps Chinese retrieval usable
  without adding a hard dependency.
* Latin/digit runs are lower-cased and split with a small regular expression so
  identifiers such as ``user_name`` match both the whole token and its parts.
* Stopwords are applied to both documents and queries so that BM25's document
  length normalisation is not skewed by function words.

Nothing here mutates global jieba state; the tokenizer is a pure function.
"""

from __future__ import annotations

import re
import unicodedata

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable


# CJK unified ideographs plus the common extensions/radicals.  We keep the
# ranges explicit instead of relying on ``\w`` so that punctuation is a natural
# separator and Latin tokenisation stays predictable.
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f]+")
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Small, curated stopword lists.  They intentionally stay short: over-filtering
# hurts short memory snippets more than it helps ranking.
_ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "for",
        "from",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "more",
        "most",
        "my",
        "no",
        "not",
        "of",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
    ]
)

_CHINESE_STOPWORDS: frozenset[str] = frozenset(
    [
        "的",
        "了",
        "着",
        "是",
        "在",
        "和",
        "与",
        "及",
        "或",
        "也",
        "都",
        "就",
        "而",
        "被",
        "把",
        "对",
        "从",
        "到",
        "为",
        "以",
        "于",
        "之",
        "其",
        "这",
        "那",
        "哪",
        "什么",
        "怎么",
        "如何",
        "为什么",
        "一个",
        "一些",
        "我们",
        "你们",
        "他们",
        "她们",
        "它们",
        "我",
        "你",
        "他",
        "她",
        "它",
        "有",
        "没有",
        "不",
        "很",
        "吗",
        "呢",
        "吧",
        "啊",
        "呀",
        "哦",
        "嗯",
        "请",
        "帮",
        "一下",
        "可以",
        "需要",
        "应该",
    ]
)

_DEFAULT_STOPWORDS: frozenset[str] = _ENGLISH_STOPWORDS | _CHINESE_STOPWORDS

_jieba_module = None
_jieba_checked = False


def _get_jieba():
    """Return the ``jieba`` module if importable, otherwise ``None``.

    Import is lazy and cached so that importing ``memos`` never depends on an
    optional package and missing jieba only costs one failed import attempt.
    """
    global _jieba_module, _jieba_checked
    if not _jieba_checked:
        _jieba_checked = True
        try:
            import jieba

            jieba.setLogLevel(60)  # silence the dictionary build banner
            _jieba_module = jieba
        except ImportError:
            _jieba_module = None
    return _jieba_module


def is_jieba_available() -> bool:
    """Whether Chinese word segmentation is available."""
    return _get_jieba() is not None


def _tokenize_latin(segment: str) -> list[str]:
    """Tokenize a Latin/digit segment, keeping snake_case parts searchable."""
    tokens: list[str] = []
    for raw in _LATIN_TOKEN_RE.findall(segment):
        token = raw.strip("_")
        if not token:
            continue
        # Single Latin characters carry little signal; keep single digits.
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.append(token)
        if "_" in token:
            parts = [part for part in token.split("_") if len(part) > 1 or part.isdigit()]
            tokens.extend(parts)
    return tokens


def _tokenize_cjk(run: str, use_jieba: bool) -> list[str]:
    """Tokenize a CJK run with jieba, falling back to uni/bi-grams."""
    if use_jieba:
        jieba = _get_jieba()
        if jieba is not None:
            return [token.strip() for token in jieba.lcut(run) if token.strip()]

    # Fallback: unigrams for single-character runs, bigrams (plus unigrams for
    # recall) for longer runs.  Deterministic and dependency-free.
    if len(run) <= 2:
        return list(run)
    tokens: list[str] = [run[i : i + 2] for i in range(len(run) - 1)]
    tokens.extend(run)
    return tokens


def tokenize(
    text: str | None,
    *,
    use_jieba: bool = True,
    stopwords: Iterable[str] | None = None,
    max_tokens: int | None = None,
) -> list[str]:
    """Tokenize ``text`` for BM25 indexing or querying.

    Args:
        text: Input text. ``None``/empty input yields an empty list.
        use_jieba: Use jieba for CJK runs when available.  When jieba is not
            installed the function transparently falls back to n-grams.
        stopwords: Extra stopwords on top of the built-in Chinese/English sets.
        max_tokens: Hard cap, applied after stopword removal.  This keeps
            pathological queries from dominating index-build or scoring cost.

    Returns:
        A list of lower-cased tokens in original order.
    """
    if not text or not isinstance(text, str):
        return []

    normalized = unicodedata.normalize("NFKC", text).lower()
    blocked = _DEFAULT_STOPWORDS
    if stopwords:
        blocked = blocked | frozenset(word.lower() for word in stopwords if word)

    tokens: list[str] = []
    cursor = 0
    for match in _CJK_RUN_RE.finditer(normalized):
        tokens.extend(_tokenize_latin(normalized[cursor : match.start()]))
        tokens.extend(_tokenize_cjk(match.group(), use_jieba))
        cursor = match.end()
    tokens.extend(_tokenize_latin(normalized[cursor:]))

    filtered = [token for token in tokens if token and token not in blocked]
    if max_tokens is not None and max_tokens >= 0 and len(filtered) > max_tokens:
        return filtered[:max_tokens]
    return filtered
