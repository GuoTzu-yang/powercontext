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

"""Parse whole PDF reports into page Sources and retrieve page citations."""

from __future__ import annotations

import hashlib
import io
from collections import Counter
from typing import Annotated
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.pdf_rank import query_terms, rank_scores, token_counts

PDF_PAGE_SOURCE_NAME = "pdf-page"
_IDENTITY_PREFIX = "pdf:"
NonEmptyText = Annotated[str, Field(min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class PDFDocumentInput(_Frozen):
    """One complete PDF report identified by a caller-stable document id."""

    document_id: str
    content: bytes


class PDFPageInput(_Frozen):
    """One physical page and the text extracted from it."""

    document_id: NonEmptyText
    document_sha256: NonEmptyText
    page_number: int = Field(ge=1)
    text: str


class PDFPageSource(Source):
    """Captured PDF page that can be read back and projected as evidence."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the revision-aware identity of one PDF page Source."""

    base = f"{_IDENTITY_PREFIX}{quote(document_id, safe='')}"
    candidate = f"{base}:{document_sha256}:{page_number}"
    if len(candidate) <= MAX_SOURCE_ID_LENGTH:
        return candidate
    digest = hashlib.sha256(document_id.encode()).hexdigest()[:48]
    return f"{_IDENTITY_PREFIX}{digest}:{document_sha256}:{page_number}"


def _page_number_from_identity(source_id: str) -> int | None:
    parts = source_id.rsplit(":", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _validate_document_id(document_id: str) -> str:
    if not document_id or not document_id.strip():
        raise ValueError("document_id must not be empty or blank")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")
    return document_id


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF, failing atomically."""

    document_id = _validate_document_id(value.document_id)
    content = bytes(value.content)
    if not content:
        raise ValueError("pdf document is empty")
    if not content.lstrip()[:5].startswith(b"%PDF-"):
        raise ValueError("content is not a PDF document")
    digest = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            reader.decrypt("")
        pages: list[PDFPageInput] = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append(
                PDFPageInput(
                    document_id=document_id,
                    document_sha256=digest,
                    page_number=index,
                    text=text,
                )
            )
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - any parser failure is an invalid document
        raise ValueError(f"pdf document cannot be parsed: {error}") from error
    if not pages:
        raise ValueError("pdf document has no pages")
    return tuple(pages)


class PDFPageAdapter:
    """Resolve parsed pages into Sources and read them back verbatim."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one PDF page as text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_ADAPTER = PDFPageAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)


_PRIMARY_SEARCH_LIMIT = 400
_EXPANSION_SEARCH_LIMIT = 600
# Hard request budget per question. The acceptance suite asks every published question in one
# process, so widening recall must stay inside six Memory searches even on a multi-report scope.
_MAX_SEARCHES_PER_QUESTION = 6
_CITATION_SCAN_FACTOR = 4


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` citations to distinct PDF pages for one question."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    terms = query_terms(question)
    try:
        hits, counts = await _candidate_hits(memory, question, terms)
    except Exception:  # noqa: BLE001 - widening retrieval must never lose the plain search
        hits, counts = [], {}
    if not hits:
        return await _plain_citations(memory, question, limit)
    scores = rank_scores([hit.text for hit in hits], terms, counts)
    order = sorted(range(len(hits)), key=lambda index: (-scores[index], index))
    return await _citations(memory, hits, order, limit)


async def _candidate_hits(
    memory, question: str, terms: tuple[str, ...]
) -> tuple[list[MemoryHit], dict[str, Counter[str]]]:
    """Collect a page pool with at most ``_MAX_SEARCHES_PER_QUESTION`` Memory searches.

    One broad search covers the question as written. The remaining searches pair up the rarest
    remaining terms: Memory admits a candidate only when it covers a share of the query terms,
    so a two-term query still admits any page that carries either of them, which is what an
    evidence page that shares only part of the question's vocabulary needs.
    """

    counts: dict[str, Counter[str]] = {}

    def counts_for(text: str) -> Counter[str]:
        bucket = counts.get(text)
        if bucket is None:
            bucket = token_counts(text)
            counts[text] = bucket
        return bucket

    found: dict[str, MemoryHit] = {}
    primary = await memory.search(
        SearchMemoryRequest(query=question, mode="fts", limit=_PRIMARY_SEARCH_LIMIT)
    )
    for hit in primary.hits:
        found.setdefault(hit.entry_id, hit)
    searches = 1
    if terms and searches < _MAX_SEARCHES_PER_QUESTION:
        frequency: Counter[str] = Counter()
        for hit in found.values():
            seen = set(counts_for(hit.text))
            for term in terms:
                if term in seen:
                    frequency[term] += 1
        ordered = sorted(terms, key=lambda term: (frequency[term], term))
        budget = 2 * (_MAX_SEARCHES_PER_QUESTION - searches)
        for start in range(0, min(len(ordered), budget), 2):
            query = " ".join(ordered[start : start + 2]).strip()
            if not query:
                continue
            page = await memory.search(
                SearchMemoryRequest(query=query, mode="fts", limit=_EXPANSION_SEARCH_LIMIT)
            )
            searches += 1
            for hit in page.hits:
                found.setdefault(hit.entry_id, hit)
    return list(found.values()), counts


async def _plain_citations(memory, question: str, limit: int) -> tuple[MemoryCitation, ...]:
    """Fall back to the single-search behaviour when the widened pool is empty."""

    page = await memory.search(
        SearchMemoryRequest(query=question, mode="fts", limit=max(limit * 20, 400))
    )
    hits = list(page.hits)
    return await _citations(memory, hits, list(range(len(hits))), limit)


async def _citations(
    memory, hits: list[MemoryHit], order: list[int], limit: int
) -> tuple[MemoryCitation, ...]:
    """Resolve ranked hits into citations to distinct, real pages."""

    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    scanned = 0
    scan_budget = max(limit * _CITATION_SCAN_FACTOR, 64)
    for index in order:
        if len(citations) >= limit or scanned >= scan_budget:
            break
        scanned += 1
        hit = hits[index]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        for reference in entry.sources:
            if reference.source_type != PDF_PAGE_SOURCE_NAME or reference.source_id in seen:
                continue
            seen.add(reference.source_id)
            citations.append(citation)
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
