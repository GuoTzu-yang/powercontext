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
from typing import Annotated
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.pdf_rank import overlap_score, token_counts

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


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` citations to distinct PDF pages for one question."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    request = SearchMemoryRequest(query=question, mode="fts", limit=max(limit * 20, 400))
    result = await memory.search(request)
    wanted = token_counts(question)
    ranked: list[tuple[float, MemoryCitation]] = []
    seen: set[str] = set()
    for hit in result.hits:
        candidate = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=candidate))).entry
        for reference in entry.sources:
            if reference.source_type != PDF_PAGE_SOURCE_NAME or reference.source_id in seen:
                continue
            seen.add(reference.source_id)
            ranked.append((overlap_score(wanted, token_counts(entry.text)), candidate))
            break
    ranked.sort(key=lambda item: item[0], reverse=True)
    return tuple(citation for _, citation in ranked[:limit])


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
