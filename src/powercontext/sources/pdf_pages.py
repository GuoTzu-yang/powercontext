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
import math
from typing import Annotated
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.pdf_rank import (
    NARRATIVE_MODE_WEIGHTS,
    NUMERIC_MODE_WEIGHTS,
    STATEMENT_WEIGHT,
    bm25_score,
    content_weights,
    minmax,
    numeric_intent,
    numeric_prior,
    page_statement_types,
    routed_documents,
    statement_types,
    table_prior,
    token_counts,
)

PDF_PAGE_SOURCE_NAME = "pdf-page"
_IDENTITY_PREFIX = "pdf:"
_NARROW_SEARCH_LIMIT = 4000
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


_SEARCH_CACHE: "dict[int, dict[str, object]]" = {}


def _cache_for(memory: object) -> "dict[str, object]":
    """Per-Memory page index; one Memory identity owns one scope's pages."""

    cache = _SEARCH_CACHE.get(id(memory))
    if cache is None:
        cache = {
            "loaded": False,
            "dirty": True,
            "citations": [],
            "documents": [],
            "pages": [],
            "tokens": [],
            "lengths": [],
            "numeric": [],
            "tables": [],
            "statements": [],
            "idf": {},
            "average": 0.0,
            "entities": set(),
            "entries": set(),
            "documents_seen": set(),
            "fallback": False,
        }
        _SEARCH_CACHE[id(memory)] = cache
    return cache


def _source_location(source_id: str) -> "tuple[str, int | None]":
    """Recover the document id and page number from a page Source identity."""

    raw = source_id[len(_IDENTITY_PREFIX):] if source_id.startswith(_IDENTITY_PREFIX) else source_id
    parts = raw.rsplit(":", 2)
    if len(parts) != 3:
        return "", None
    try:
        page_number = int(parts[2])
    except ValueError:
        return "", None
    return unquote(parts[0]), page_number


def _remember_page(cache: "dict[str, object]", citation, source_id: str, text: str) -> bool:
    """Append one page to the local index unless that page is already indexed."""

    if source_id in cache["entities"]:
        return False
    document, page_number = _source_location(source_id)
    if page_number is None:
        return False
    cache["entities"].add(source_id)
    body = text.split("\n", 1)[1] if "\n" in text else text
    counts = token_counts(body)
    cache["citations"].append(citation)
    cache["documents"].append(document)
    cache["pages"].append(page_number)
    cache["tokens"].append(counts)
    cache["lengths"].append(sum(counts.values()))
    cache["numeric"].append(numeric_prior(counts))
    cache["tables"].append(table_prior(body))
    cache["statements"].append(page_statement_types(body))
    cache["documents_seen"].add(document)
    cache["dirty"] = True
    return True


def _refresh_statistics(cache: "dict[str, object]") -> None:
    """Fit one idf table and average length over every indexed page."""

    if not cache["dirty"]:
        return
    cache["dirty"] = False
    tokens = cache["tokens"]
    lengths = cache["lengths"]
    total = len(tokens)
    frequency: "dict[str, int]" = {}
    for counts in tokens:
        for term in counts:
            frequency[term] = frequency.get(term, 0) + 1
    total_length = sum(lengths)
    cache["average"] = (total_length / total) if total else 0.0
    cache["idf"] = {
        term: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
        for term, count in frequency.items()
    }


async def _index_scope(memory: object, cache: "dict[str, object]") -> None:
    """Enumerate every page this scope holds so ranking never depends on question order."""

    if cache["loaded"]:
        return
    cache["loaded"] = True
    try:
        listing = await memory.list()
    except Exception:  # noqa: BLE001 - a Memory without listing falls back to search
        cache["fallback"] = True
        return
    if not listing.entries:
        cache["fallback"] = True
        return
    for record in listing.entries:
        entry = record.entry
        for reference in entry.sources:
            if reference.source_type != PDF_PAGE_SOURCE_NAME:
                continue
            citation = MemoryCitation(
                memory_ref=record.memory_ref,
                entry_id=entry.entry_id,
                entry_version_id=entry.entry_version_id,
            )
            _remember_page(cache, citation, reference.source_id, entry.text)
            # one citation must resolve to exactly one page
            break


async def _absorb_pages(memory: object, question: str, cache: "dict[str, object]") -> None:
    """Fallback: add unseen pages from one broad lexical search of the question."""

    request = SearchMemoryRequest(query=question, mode="fts", limit=_NARROW_SEARCH_LIMIT)
    result = await memory.search(request)
    for hit in result.hits:
        if hit.entry_id in cache["entries"]:
            continue
        cache["entries"].add(hit.entry_id)
        candidate = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        try:
            entry = (await memory.get(GetMemoryEntryRequest(citation=candidate))).entry
        except Exception:  # a stale head is simply not a candidate
            continue
        for reference in entry.sources:
            if reference.source_type != PDF_PAGE_SOURCE_NAME:
                continue
            _remember_page(cache, candidate, reference.source_id, entry.text)
            break


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` citations to distinct PDF pages for one question."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if limit < 1:
        raise ValueError("limit must be a positive integer")

    cache = _cache_for(memory)
    await _index_scope(memory, cache)
    if cache["fallback"] or not cache["citations"]:
        # a Memory that cannot be enumerated is queried question by question
        await _absorb_pages(memory, question, cache)
    citations = cache["citations"]
    if not citations:
        return ()

    _refresh_statistics(cache)
    idf = cache["idf"]
    average = cache["average"]
    weights = content_weights(question)
    if numeric_intent(question):
        lexical_weight, numeric_weight, table_weight = NUMERIC_MODE_WEIGHTS
    else:
        lexical_weight, numeric_weight, table_weight = NARRATIVE_MODE_WEIGHTS
    documents = cache["documents"]
    pages = cache["pages"]
    tokens = cache["tokens"]
    lengths = cache["lengths"]
    routed = routed_documents(question, frozenset(cache["documents_seen"]))

    total = len(citations)
    lexical = minmax(
        [bm25_score(weights, tokens[index], lengths[index], idf, average) for index in range(total)]
    )
    numbers = minmax(cache["numeric"])
    tables = minmax(cache["tables"])
    wanted_statements = statement_types(question)
    statements = cache["statements"]

    # Pages of the document the question names win over every other document's pages,
    # then the weighted signals decide the order inside that partition.
    ranked: "list[tuple[int, float, str, int, int]]" = []
    for index in range(total):
        preferred = 0 if routed is not None and documents[index] not in routed else 1
        statement_bonus = (
            STATEMENT_WEIGHT
            if wanted_statements and (wanted_statements & statements[index])
            else 0.0
        )
        combined = (
            lexical_weight * lexical[index]
            + numeric_weight * numbers[index]
            + table_weight * tables[index]
            + statement_bonus
        )
        ranked.append((preferred, combined, documents[index], pages[index], index))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))

    chosen: "list[MemoryCitation]" = []
    seen: "set[tuple[str, int]]" = set()
    for _, _, document, page_number, index in ranked:
        if (document, page_number) in seen:
            continue
        seen.add((document, page_number))
        chosen.append(citations[index])
        if len(chosen) >= limit:
            break
    return tuple(chosen)


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
