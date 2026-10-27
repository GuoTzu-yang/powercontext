"""Token-overlap re-ranking for offline PDF page retrieval."""

from __future__ import annotations

import re
from collections import Counter

_TOKEN = re.compile(r"[a-z]+|\d+")


def token_counts(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(text.lower()))


def overlap_score(question: Counter[str], page: Counter[str]) -> float:
    if not question or not page:
        return 0.0
    shared = sum(min(count, page.get(token, 0)) for token, count in question.items())
    return shared / sum(question.values())


__all__ = ["overlap_score", "token_counts"]
