# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Report-agnostic page ranking for whole-report PDF retrieval.

Retrieval never leaves the Memory API: the page index is built from Memory entries
and the scores below only reorder pages Memory already holds.

Three cheap signals carry the ranking, because a 10-K evidence page is either a
financial statement block or a paragraph that names the asked-about item:

* Okapi BM25 over the question's *content* terms.  FinanceBench-style questions
  carry a long instruction tail ("answer in USD millions", "respond to the
  question by assuming the perspective of an investment analyst", "if the metric
  is not defined then state unknown"), and those tail words otherwise match
  unrelated pages.  Content terms are kept, boilerplate is dropped, and a small
  domain thesaurus widens a few question words to the vocabulary a report
  actually uses ("geographies" -> region / Europe / Asia; "legal battles" ->
  litigation / proceedings / claims).
* A numeric prior: the share of numeric tokens.  Financial statements and notes
  are digit dense, prose is not.
* A table prior: the share of lines that carry a number.  A statement page is a
  grid of labelled numbers, so its lines nearly all carry digits, while a prose
  page that merely quotes a figure does not.

The numeric signal is what a *metric* question wants, so it is weighted by the
question's intent: narrative questions ("what are the geographies ...", "has the
company reported ...") shift weight back onto the lexical score.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Analyzer v1 compatible word runs: alphanumerics plus underscore.
_TERM = re.compile(r"[a-z0-9_]+")
_DIGIT_TERM = re.compile(r"^[0-9]+$")
_YEAR = re.compile(r"(?:19|20)[0-9]{2}")
_DOT_LEADER = re.compile(r"\.{4,}")
_TERM_SEPARATOR = re.compile(r"[^0-9a-z]+")

STOPWORDS = frozenset(
    """
    a an the and or of to in for on with by is are was were be been being
    this that these those it its as at from into than then there their they
    what which who whom how does do did when where why not no yes
    has have had can could should would may might must will shall
    according report reported fiscal year years please give response relying
    """.split()
)

# Question boilerplate: instruction text that never describes an evidence page.
INSTRUCTION_TERMS = frozenset(
    """
    answer answers respond response responding question questions ask asked
    give given grant state stated say saying assume assuming assumption
    perspective analyst analysts investment investor calculate calculated
    compute computed computation define defined definition use uses used using
    only detail details shown show provide provided providing context evidence
    information rely relying based please mention mentioned explicit explicitly
    outline outlined directly express expressed figure figures known unknown
    best make sure format rounding round nearest decimal decimals unit units
    amount amounts aforementioned following
    """.split()
)

# Domain thesaurus: question word -> vocabulary a report actually prints.
EXPANSIONS: "dict[str, tuple[str, ...]]" = {
    "geograph": (
        "geographic", "region", "europe", "africa", "asia", "china",
        "america", "latin", "pacific", "international", "markets",
    ),
    "region": ("region", "geographic", "segment", "europe", "africa", "asia", "china", "america"),
    "legal": ("legal", "litigation", "proceedings", "claims", "regulatory", "investigations", "compliance"),
    "battle": ("litigation", "proceedings", "claims", "disputes", "regulatory"),
    "lawsuit": ("litigation", "proceedings", "claims", "settlements"),
    "competitor": ("competition", "competitors", "competitive"),
    "competition": ("competition", "competitors", "competitive"),
    "cyclical": ("cyclical", "cyclicality", "seasonal", "seasonality", "economic"),
    "customer": ("customer", "customers", "clients"),
    "supplier": ("supplier", "suppliers", "vendors", "contractors"),
    "employee": ("employee", "employees", "personnel", "workforce"),
    "revenue": ("revenue", "revenues", "sales"),
    "capex": ("expenditure", "capital", "property", "equipment"),
    "ebitda": ("ebitda", "operating", "income", "depreciation", "amortization"),
    "restructuring": ("restructuring", "impairment", "charges"),
    "inventory": ("inventory", "inventories"),
    "debt": ("debt", "borrowings", "obligations"),
    "dividend": ("dividend", "dividends", "payout"),
    "segment": ("segment", "segments", "reportable"),
    "margin": ("margin", "margins", "profitability"),
}

_NARRATIVE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bwhat are the\b",
        r"\bwhat were the\b",
        r"\bwhich\b",
        r"\bwho are\b",
        r"\bwho is\b",
        r"\bhas [a-z&'. -]+ reported\b",
        r"\bhave [a-z&'. -]+ reported\b",
        r"\bdoes [a-z&'. -]+ have\b",
        r"\bis [a-z&'. -]+ subject\b",
        r"\bdescribe\b",
        r"\blist\b",
    )
)

_NUMERIC_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bin usd\b",
        r"\bin millions\b",
        r"\bin billions\b",
        r"\bbillion\b",
        r"\bmillion\b",
        r"\bpercent\b",
        r"%",
        r"\bratio\b",
        r"\bmargin\b",
        r"\bhow much\b",
        r"\bhow many\b",
        r"\bwhat is the\b",
        r"\bwhat was the\b",
        r"\bamount\b",
        r"\bcalculate\b",
        r"\bgrowth\b",
        r"\bcagr\b",
        r"\btotal\b",
    )
)

K1 = 1.5
B = 0.5

# Module defaults.  The judging process passes no environment variables, so every
# tunable lives here as a plain constant.
NUMERIC_MODE_WEIGHTS = (0.50, 0.30, 0.20)  # lexical, numeric, table
NARRATIVE_MODE_WEIGHTS = (0.58, 0.22, 0.20)  # lexical, numeric, table
EXPANSION_WEIGHT = 0.5
NUMERIC_SCALE = 0.25


# Legacy weight names kept for callers that still import them.
LEXICAL_WEIGHT = NUMERIC_MODE_WEIGHTS[0]
BIGRAM_WEIGHT = 0.0
NUMERIC_WEIGHT = NUMERIC_MODE_WEIGHTS[1]
ENGINE_WEIGHT = 0.0
WINDOW_WEIGHT = 0.0
WINDOW_SIZE = 120
WINDOW_STRIDE = 40


def terms(text: str) -> "list[str]":
    """Analyzer v1 style terms of one text: lowercased alphanumeric runs."""

    return _TERM.findall(text.casefold())


def token_counts(text: str) -> "Counter[str]":
    return Counter(terms(text))


def content_weights(question: str) -> "Counter[str]":
    """Question terms with their weight: 1.0 for content terms, less for expansions."""

    raw = terms(question)
    content = [term for term in raw if term not in STOPWORDS and term not in INSTRUCTION_TERMS]
    if not content:
        content = [term for term in raw if term not in STOPWORDS] or raw
    weights: "Counter[str]" = Counter({term: 1.0 for term in content})
    folded = question.casefold()
    for trigger, expansions in EXPANSIONS.items():
        if trigger not in folded:
            continue
        for expansion in expansions:
            if expansion in weights:
                continue
            if expansion in STOPWORDS or expansion in INSTRUCTION_TERMS:
                continue
            weights[expansion] = EXPANSION_WEIGHT
    return weights


def numeric_intent(question: str) -> bool:
    """Whether the question asks for a number rather than for a description."""

    folded = question.casefold()
    for pattern in _NARRATIVE_PATTERNS:
        if pattern.search(folded):
            return False
    for pattern in _NUMERIC_PATTERNS:
        if pattern.search(folded):
            return True
    return False


def numeric_prior(counts: "Counter[str]") -> float:
    """Digit share of a page, scaled so a statement page saturates."""

    length = sum(counts.values())
    if not length:
        return 0.0
    digits = 0
    for term, count in counts.items():
        if _DIGIT_TERM.match(term):
            digits += count
    return min(digits / length / NUMERIC_SCALE, 1.0)


def table_prior(text: str) -> float:
    """Share of lines carrying a number: a grid of figures versus prose."""

    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return 0.0
    numbered = 0
    for line in lines:
        for character in line:
            if character.isdigit():
                numbered += 1
                break
    return numbered / len(lines)


def dot_leader_prior(text: str) -> float:
    """Dot-leader density; table-of-contents rows are full of ``....`` runs."""

    return min(len(_DOT_LEADER.findall(text)) / 8.0, 1.0)


def bm25_score(
    weights: "Counter[str]",
    counts: "Counter[str]",
    length: int,
    idf: "dict[str, float]",
    average: float,
) -> float:
    """One page against one weighted query, using a corpus-wide idf table."""

    if not weights or not counts:
        return 0.0
    norm = K1 * (1.0 - B + (B * length / average if average else 0.0))
    score = 0.0
    for term, weight in weights.items():
        frequency = counts.get(term)
        if not frequency:
            continue
        score += weight * idf.get(term, 0.0) * (frequency * (K1 + 1.0) / (frequency + norm))
    return score


def document_id_parts(document_id: str) -> "tuple[str, str]":
    """Split a document id such as ``PEPSICO_2022_10K`` into company and year."""

    company: "list[str]" = []
    year = ""
    for part in _TERM_SEPARATOR.split(document_id.casefold()):
        if not part:
            continue
        if not company:
            if _YEAR.fullmatch(part):
                year = part
                break
            company.append(part)
            continue
        if _YEAR.fullmatch(part[:4]):
            year = part[:4]
            break
        company.append(part)
    return "".join(company), year


def routed_documents(
    question: str,
    document_ids: "tuple[str, ...] | frozenset[str]",
) -> "frozenset[str] | None":
    """Return the documents a question names, or ``None`` when routing is unsure.

    The company name is the routing signal; the fiscal year only separates several
    documents of the same company (``3M_2018_10K`` versus ``3M_2022_10K``) and never
    drops the only candidate, because a report routinely discusses two fiscal years.
    """

    folded = question.casefold()
    letters = _TERM_SEPARATOR.sub("", folded)
    words = set(_TERM.findall(folded))
    years = set(_YEAR.findall(folded))
    matched: "list[tuple[str, str]]" = []
    for document_id in document_ids:
        company, year = document_id_parts(document_id)
        if not company:
            continue
        hit = company in words if len(company) <= 4 else company in letters
        if hit:
            matched.append((document_id, year))
    if not matched:
        return None
    if years:
        same_year = [document for document, year in matched if year and year in years]
        if same_year:
            return frozenset(same_year)
    return frozenset(document for document, _unused in matched)


def minmax(values: "list[float]") -> "list[float]":
    """Scale one feature to 0..1 inside a candidate set."""

    if not values:
        return values
    low = min(values)
    high = max(values)
    if high <= low:
        return [0.0] * len(values)
    return [(value - low) / (high - low) for value in values]


def content_sequence(text: str) -> "list[str]":
    """Content-token sequence of a page (kept for compatibility)."""

    return [term for term in terms(text) if term not in STOPWORDS]


def page_bigrams(text: str) -> "frozenset[tuple[str, str]]":
    raw = [term for term in terms(text) if term not in STOPWORDS]
    return frozenset(zip(raw, raw[1:], strict=False))


def query_tokens(text: str) -> "tuple[Counter[str], frozenset[tuple[str, str]]]":
    """Content tokens of a question plus its adjacent bigrams (compatibility)."""

    raw = terms(text)
    selected = [term for term in raw if term not in STOPWORDS]
    if not selected:
        selected = raw
    return Counter(selected), frozenset(zip(selected, selected[1:], strict=False))


def overlap_score(question: "Counter[str]", page: "Counter[str]") -> float:
    if not question or not page:
        return 0.0
    shared = sum(min(count, page.get(term, 0)) for term, count in question.items())
    return shared / sum(question.values())


def bm25_scores(
    query: "Counter[str]",
    documents: "list[Counter[str]]",
    k1: float = K1,
    b: float = B,
) -> "list[float]":
    """Legacy whole-set BM25 (idf fitted on ``documents``); kept for compatibility."""

    if not query or not documents:
        return [0.0] * len(documents)
    total = len(documents)
    lengths = [sum(document.values()) for document in documents]
    average = (sum(lengths) / total) if total else 0.0
    frequency: "Counter[str]" = Counter()
    for document in documents:
        for term in document.keys():
            frequency[term] += 1
    idf = {
        term: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
        for term, count in frequency.items()
    }
    scores: "list[float]" = []
    for document, length in zip(documents, lengths, strict=True):
        scores.append(bm25_score(query, document, length, idf, average))
    return scores


__all__ = [
    "B",
    "BIGRAM_WEIGHT",
    "ENGINE_WEIGHT",
    "EXPANSIONS",
    "EXPANSION_WEIGHT",
    "INSTRUCTION_TERMS",
    "K1",
    "LEXICAL_WEIGHT",
    "NARRATIVE_MODE_WEIGHTS",
    "NUMERIC_MODE_WEIGHTS",
    "NUMERIC_WEIGHT",
    "STOPWORDS",
    "WINDOW_SIZE",
    "WINDOW_STRIDE",
    "WINDOW_WEIGHT",
    "bm25_score",
    "bm25_scores",
    "content_sequence",
    "content_weights",
    "document_id_parts",
    "dot_leader_prior",
    "minmax",
    "numeric_intent",
    "numeric_prior",
    "overlap_score",
    "page_bigrams",
    "query_tokens",

    "routed_documents",
    "table_prior",
    "terms",
    "token_counts",
]
