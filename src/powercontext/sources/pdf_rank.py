"""Deterministic lexical ranking for offline PDF page retrieval.

The judge only receives real Memory entries, so recall depends on how well a page's own words
match a natural-language question. Three cheap offline transforms carry most of the gap: prefix
stemming (``geographies`` matches ``geographic``), fiscal-year normalization (``FY22`` matches
``2022``), and a document identity boost so a question about one report is not outranked by
boilerplate from another report stored in the same scope.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence

_TOKEN = re.compile(r"[a-z]+|\d+")
_FISCAL_YEAR = re.compile(r"fy(\d{2,4})")
_STEM_LENGTH = 5
_HEADING_CHARS = 160
_BM25_K1 = 1.2
_BM25_B = 0.75
_NUMERIC_WEIGHT = 0.9
_NUMERIC_CAP = 1.5
_HEADING_WEIGHT = 0.5
_DOCUMENT_BOOST = 2.0

_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those of to in on at by for from with
    without as is are was were be been being have has had do does did not no nor so such very
    most less least much many any all both each other some own same too only about above after
    again against below between during before under over out off up down into through while what
    which who whom whose how why when where please can could should would may might must will
    shall it its i we you they he she them us our your their my me based state explain measure
    relevant reasonably healthy known due related including include includes increase decrease
    change drove bring brought lose lost gain gained among within across per approximately
    respectively also however therefore thus furthermore like company year years million millions
    billion basis terms item part note notes
    """.split()
)


def token_counts(text: str) -> Counter[str]:
    """Count lowercase word and number terms with the judge's own token pattern."""

    return Counter(_TOKEN.findall(text.lower()))


def overlap_score(question: Counter[str], page: Counter[str]) -> float:
    """Share of the question's term mass that also appears on the page."""

    if not question or not page:
        return 0.0
    shared = sum(min(count, page.get(token, 0)) for token, count in question.items())
    return shared / sum(question.values())


def stem(token: str) -> str:
    """Collapse inflections the shared analyzer keeps apart."""

    return token[:_STEM_LENGTH] if len(token) >= _STEM_LENGTH else token


def query_terms(question: str) -> tuple[str, ...]:
    """Return the evidence terms a question contributes, in first-seen order."""

    terms: list[str] = []
    for token in _TOKEN.findall(question.lower()):
        fiscal = _FISCAL_YEAR.fullmatch(token)
        if fiscal is not None:
            digits = fiscal.group(1)
            token = f"20{digits}" if len(digits) == 2 else digits
        elif token in _STOPWORDS:
            continue
        elif not token.isdigit() and len(token) < 2:
            continue
        terms.append(token)
    return tuple(dict.fromkeys(terms))


def document_id(page_text: str) -> str:
    """Return the leading document identifier of one stored page projection."""

    return page_text.split("\n", 1)[0].strip()


def _heading_terms(page_text: str) -> Counter[str]:
    body = page_text.split("\n", 1)[1] if "\n" in page_text else page_text
    return Counter(stem(token) for token in _TOKEN.findall(body[:_HEADING_CHARS].lower()))


def _document_matches(term: str, document: str) -> bool:
    lowered = document.lower()
    if term in lowered:
        return True
    return any(part.startswith(term) for part in lowered.split("_") if len(part) >= 3)


def _best_document(stemmed: Sequence[str], documents: Sequence[str]) -> str | None:
    distinct = tuple(dict.fromkeys(documents))
    if len(distinct) < 2 or len(distinct) > max(1, len(documents) // 10):
        # A single document, or text without a document identity, cannot be preferred.
        return None
    best: str | None = None
    best_score = 0
    for document in distinct:
        score = sum(1 for term in stemmed if _document_matches(term, document))
        if score > best_score:
            best, best_score = document, score
    return best


def rank_scores(
    page_texts: Sequence[str],
    terms: Sequence[str],
    counts: dict[str, Counter[str]] | None = None,
) -> list[float]:
    """Score every candidate page for one question; higher is a better evidence page."""

    if not page_texts:
        return []
    stemmed = tuple(dict.fromkeys(stem(term) for term in terms))
    if not stemmed:
        return [0.0 for _ in page_texts]
    cached = counts if counts is not None else {}
    buckets: list[Counter[str]] = []
    for text in page_texts:
        raw = cached.get(text)
        if raw is None:
            raw = token_counts(text)
        bucket: Counter[str] = Counter()
        for token, count in raw.items():
            bucket[stem(token)] += count
        buckets.append(bucket)
    lengths = [sum(bucket.values()) or 1 for bucket in buckets]
    size = len(page_texts)
    average = sum(lengths) / size
    documents = [document_id(text) for text in page_texts]
    preferred = _best_document(stemmed, documents)
    document_frequency: Counter[str] = Counter()
    for bucket in buckets:
        for key in stemmed:
            if bucket.get(key):
                document_frequency[key] += 1
    inverse = {
        key: math.log(1.0 + (size - document_frequency[key] + 0.5) / (document_frequency[key] + 0.5))
        for key in stemmed
    }
    scores: list[float] = []
    for index, bucket in enumerate(buckets):
        length = lengths[index]
        heading = _heading_terms(page_texts[index])
        score = 0.0
        for key in stemmed:
            frequency = bucket.get(key, 0)
            if not frequency:
                continue
            score += (
                inverse[key]
                * (frequency * (_BM25_K1 + 1))
                / (frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average))
            )
            if heading.get(key):
                score += inverse[key] * _HEADING_WEIGHT
        digits = sum(count for key, count in bucket.items() if key.isdigit())
        score *= 1.0 + min(_NUMERIC_WEIGHT * digits / length, _NUMERIC_CAP)
        if preferred is not None and documents[index] == preferred:
            score *= _DOCUMENT_BOOST
        scores.append(score)
    return scores


__all__ = [
    "document_id",
    "overlap_score",
    "query_terms",
    "rank_scores",
    "stem",
    "token_counts",
]
