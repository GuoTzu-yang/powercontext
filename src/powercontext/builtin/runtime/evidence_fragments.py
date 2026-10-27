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

"""Select literal evidence spans from recalled Memory entries under one byte budget.

Retrieval returns whole Memory entries, but the answer to the current query usually lives in a
few sentences. This module scores every sentence of the retrieved entries against the query,
keeps the exact original character span, and packs the best spans into the caller's byte budget.
The budget is measured on the complete rendered output, so citations and envelope overhead are
paid for before any span is admitted.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
TRUST_POLICY_ID = "untrusted_history"

_MIN_SPAN_CHARS = 32
_SENTENCE = re.compile(r"[^.!?\u3002\uff01\uff1f\n]+[.!?\u3002\uff01\uff1f]?")
_TOKEN = re.compile(r"[0-9a-z]+|[\u4e00-\u9fff]")


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


@dataclass(frozen=True)
class PreparedEvidenceFragment:
    """One literal span of one Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Sentence:
    document: int
    order: int
    start: int
    end: int
    text: str
    score: float = 0.0


@dataclass(frozen=True)
class _Document:
    address: MemoryEntryAddress
    text: str


def tokens(value: str) -> tuple[str, ...]:
    """Split one span into matchable word and CJK-character terms."""

    return tuple(_TOKEN.findall(value.lower()))


def _sentences(document: int, text: str) -> tuple[_Sentence, ...]:
    spans: list[_Sentence] = []
    order = 0
    for match in _SENTENCE.finditer(text):
        start, end = match.span()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end - start < 2:
            continue
        spans.append(_Sentence(document=document, order=order, start=start, end=end, text=text[start:end]))
        order += 1
    if not spans and text:
        trimmed = text.strip()
        if trimmed:
            start = text.index(trimmed)
            spans.append(_Sentence(document=document, order=0, start=start, end=start + len(trimmed), text=trimmed))
    return tuple(spans)


def _score(spans: list[_Sentence], query: str) -> None:
    query_terms = tuple(dict.fromkeys(tokens(query)))
    if not query_terms:
        return
    wanted = set(query_terms)
    frequency: dict[str, int] = {}
    for span in spans:
        for term in set(tokens(span.text)):
            frequency[term] = frequency.get(term, 0) + 1
    total = len(spans)
    inverse = {term: math.log(1.0 + total / (1.0 + count)) for term, count in frequency.items()}
    for index, span in enumerate(spans):
        present = set(tokens(span.text)) & wanted
        if not present:
            continue
        weight = sum(inverse[term] for term in present)
        coverage = len(present) / len(wanted)
        spans[index] = _Sentence(
            document=span.document,
            order=span.order,
            start=span.start,
            end=span.end,
            text=span.text,
            score=weight * (0.5 + coverage),
        )


def _ranked(spans: Sequence[_Sentence]) -> list[_Sentence]:
    """Order relevant spans first, then the rest, so spare budget still buys literal evidence."""

    relevant = [span for span in spans if span.score > 0]
    relevant.sort(key=lambda span: (-span.score, span.document, span.start))
    if not relevant:
        return sorted(spans, key=lambda span: (span.document, span.start))
    matched = {span.document for span in relevant}
    filler = [span for span in spans if span.score <= 0 and span.document in matched]
    filler.sort(key=lambda span: (span.document, span.start))
    return relevant + filler


def _diverse(spans: Sequence[_Sentence]) -> list[_Sentence]:
    """Represent every candidate document before spending budget on a second span."""

    ordered: list[_Sentence] = []
    seen: set[int] = set()
    for span in spans:
        if span.document in seen:
            continue
        seen.add(span.document)
        ordered.append(span)
    remaining = [span for span in spans if span not in ordered]
    return ordered + remaining


def render_evidence(fragments: Sequence[PreparedEvidenceFragment]) -> str:
    """Render selected spans with their exact citation identity inside the trust envelope."""

    envelope = {
        "trust": TRUST_POLICY_ID,
        "items": [_item(fragment) for fragment in fragments],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{BEGIN_MARKER}\n{encoded}\n{END_MARKER}"))


def _item(fragment: PreparedEvidenceFragment) -> dict[str, object]:
    artifact = fragment.address.memory.artifact
    return {
        "citation": {
            "scope_id": fragment.address.memory.scope_id,
            "memory_ref": artifact.model_dump(mode="json"),
            "entry_id": fragment.address.entry_id,
            "entry_version_id": fragment.address.entry_version_id,
        },
        "content": fragment.text,
        "start": fragment.start,
        "end": fragment.end,
    }


def _document(
    scope_id: str,
    memory_ref: object,
    entry_id: str,
    entry_version_id: str,
    text: str,
) -> _Document:
    return _Document(
        address=MemoryEntryAddress(
            memory=ArtifactAddress(scope_id=scope_id, artifact=memory_ref),
            entry_id=entry_id,
            entry_version_id=entry_version_id,
        ),
        text=text,
    )


def select_evidence(
    documents: Iterable[tuple[str, object, str, str, str]],
    *,
    query: str,
    max_bytes: int,
) -> tuple[tuple[PreparedEvidenceFragment, ...], str | None]:
    """Pick literal spans from ``documents`` under ``max_bytes``.

    Each document is ``(scope_id, memory_ref, entry_id, entry_version_id, text)``. Whole spans are
    admitted first so evidence stays complete; a span is only shortened when nothing else fits the
    remaining room. Returns the fragments in delivery order with the rendered context, or
    ``(none, None)`` when no span fits the budget at all.
    """

    entries = tuple(_document(*document) for document in documents)
    spans: list[_Sentence] = []
    for index, entry in enumerate(entries):
        spans.extend(_sentences(index, entry.text))
    _score(spans, query)
    ordered = _diverse(_ranked(spans))
    chosen: list[tuple[int, PreparedEvidenceFragment]] = []
    deferred: list[tuple[int, _Sentence]] = []
    seen: set[tuple[int, int, int]] = set()
    for priority, span in enumerate(ordered):
        identity = (span.document, span.start, span.end)
        if identity in seen:
            continue
        seen.add(identity)
        document = entries[span.document]
        fragment = _fragment(document.address, span.start, span.end, document.text)
        if _fits((*(item for _, item in chosen), fragment), max_bytes):
            chosen.append((priority, fragment))
        else:
            deferred.append((priority, span))
    selected = [item for _, item in chosen]
    for priority, span in deferred:
        document = entries[span.document]
        shortened = _shorten(document, span, selected, max_bytes)
        if shortened is None:
            continue
        span_length = span.end - span.start
        if len(shortened.text) * 10 < span_length * 7:
            # An honestly incomplete span is worse than leaving the room to a later whole span.
            continue
        chosen.append((priority, shortened))
        selected.append(shortened)
    if not chosen:
        return (), None
    chosen.sort(key=lambda item: item[0])
    fragments = tuple(item for _, item in chosen)
    return fragments, render_evidence(fragments)


def _fragment(address: MemoryEntryAddress, start: int, end: int, text: str) -> PreparedEvidenceFragment:
    return PreparedEvidenceFragment(address=address, start=start, end=end, text=text[start:end])


def _fits(fragments: Sequence[PreparedEvidenceFragment], max_bytes: int) -> bool:
    return len(render_evidence(fragments).encode("utf-8")) <= max_bytes


def _shorten(
    document: _Document,
    span: _Sentence,
    selected: Sequence[PreparedEvidenceFragment],
    max_bytes: int,
) -> PreparedEvidenceFragment | None:
    """Fit a literal prefix of one span when the whole span does not fit."""

    low = min(_MIN_SPAN_CHARS, span.end - span.start)
    high = span.end - span.start
    best: PreparedEvidenceFragment | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = _fragment(document.address, span.start, span.start + middle, document.text)
        if _fits((*selected, candidate), max_bytes):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best
