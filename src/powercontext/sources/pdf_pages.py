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

"""Parse every physical PDF page and retrieve page-level Memory citations."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import re
import threading
import unicodedata
import zlib
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator
from typing_extensions import override

from powercontext.artifacts.models import MemoryCitation
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, Source, SourceMaterialization
from powercontext.sources.projections import TextEvidence

_TOKEN = re.compile(r"[a-z]+|\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BM25_K1 = 1.2
_BM25_B = 0.75
_SEARCH_POOL = 80

_STOPWORDS = frozenset({
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "i",
    "we",
    "you",
    "it",
    "its",
    "our",
    "your",
    "this",
    "that",
    "these",
    "those",
    "please",
    "has",
    "have",
    "had",
    "any",
    "if",
    "then",
    "else",
    "not",
    "no",
    "nor",
    "so",
    "than",
    "into",
    "over",
    "under",
    "between",
    "about",
    "after",
    "before",
    "during",
    "within",
    "using",
    "use",
    "used",
    "only",
    "also",
    "such",
    "their",
    "there",
    "them",
    "they",
    "but",
    "both",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "same",
    "too",
    "very",
    "just",
    "will",
    "may",
    "might",
    "must",
    "shall",
    "per",
    "via",
    "including",
    "include",
    "included",
    "based",
    "following",
    "give",
    "given",
    "show",
    "shown",
    "detail",
    "details",
    "response",
    "question",
    "answer",
    "rely",
    "relying",
    "assume",
    "assuming",
    "define",
    "defined",
    "calculate",
    "calculation",
    "respond",
    "directly",
    "explicitly",
    "outlined",
    "outline",
    "state",
    "company",
})

# Offline financial-language expansions. Weights are relative to original query terms.
_SYNONYMS: dict[str, tuple[tuple[str, float], ...]] = {
    "ppne": (("property", 1.0), ("plant", 1.0), ("equipment", 1.0)),
    "ppe": (("property", 1.0), ("plant", 1.0), ("equipment", 1.0)),
    "cogs": (("cost", 1.0), ("goods", 1.0), ("sold", 1.0), ("sales", 0.8)),
    "retiree": (("retirement", 0.8), ("postretirement", 0.8), ("benefits", 0.5)),
    "retirees": (("retirement", 0.8), ("postretirement", 0.8), ("benefits", 0.5)),
    "geography": (("geographic", 1.0), ("geographical", 1.0)),
    "geographies": (("geographic", 1.0), ("geographical", 1.0)),
    "geographic": (("geographical", 0.8), ("geography", 0.6)),
    "geographical": (("geographic", 0.8),),
    "region": (("geographic", 0.5), ("regions", 0.4)),
    "regions": (("geographic", 0.5), ("region", 0.4)),
    "country": (("countries", 0.4), ("geographic", 0.4)),
    "countries": (("country", 0.4), ("geographic", 0.4)),
    "legal": (
        ("litigation", 1.1),
        ("proceeding", 1.0),
        ("proceedings", 1.1),
        ("lawsuit", 0.8),
        ("lawsuits", 0.8),
        ("contingency", 0.9),
        ("contingencies", 1.0),
    ),
    "battle": (("litigation", 1.1), ("lawsuit", 0.9), ("proceeding", 0.9)),
    "battles": (("litigation", 1.1), ("lawsuits", 0.9), ("proceedings", 1.0), ("contingencies", 0.9)),
    "lawsuit": (("litigation", 0.9), ("proceeding", 0.7)),
    "lawsuits": (("litigation", 0.9), ("proceedings", 0.8)),
    "litigation": (("proceedings", 0.7), ("contingencies", 0.6)),
    "ebitda": (("operating", 0.8), ("depreciation", 1.0), ("amortization", 1.0), ("amortisation", 0.8)),
    "capex": (("capital", 0.7), ("expenditure", 1.0), ("expenditures", 1.0)),
    "expenditure": (("expenditures", 0.8), ("capital", 0.4)),
    "expenditures": (("expenditure", 0.8), ("capital", 0.4)),
    "restructuring": (("restructure", 0.6), ("severance", 0.7), ("charges", 0.5)),
    "restructure": (("restructuring", 0.8),),
    "cost": (("costs", 0.4), ("charge", 0.45), ("charges", 0.45), ("expense", 0.35), ("expenses", 0.35)),
    "costs": (("cost", 0.4), ("charges", 0.55), ("expenses", 0.4), ("charge", 0.4)),
    "revenue": (("revenues", 0.7), ("sales", 0.45)),
    "revenues": (("revenue", 0.7), ("sales", 0.45)),
    "sales": (("revenue", 0.4), ("revenues", 0.4)),
    "income": (("earnings", 0.35),),
    "earnings": (("income", 0.35),),
    "margin": (("margins", 0.4),),
    "depreciation": (("amortization", 0.6), ("amortisation", 0.5)),
    "amortization": (("depreciation", 0.6), ("amortisation", 0.5)),
    "amortisation": (("depreciation", 0.6), ("amortization", 0.6)),
    "debt": (("borrowings", 0.7), ("indebtedness", 0.6)),
    "borrowings": (("debt", 0.6),),
    "cash": (("flows", 0.25),),
    "flow": (("flows", 0.7),),
    "flows": (("flow", 0.5),),
    "dividend": (("dividends", 0.7),),
    "dividends": (("dividend", 0.7),),
    "segment": (("segments", 0.5),),
    "segments": (("segment", 0.5),),
    "goodwill": (("impairment", 0.4),),
    "tax": (("taxes", 0.5),),
    "taxes": (("tax", 0.5),),
    "lease": (("leases", 0.6),),
    "leases": (("lease", 0.6),),
    "pension": (("retirement", 0.4),),
    "inventory": (("inventories", 0.6),),
    "receivable": (("receivables", 0.6),),
    "payable": (("payables", 0.6),),
    "asset": (("assets", 0.5),),
    "assets": (("asset", 0.4),),
    "liability": (("liabilities", 0.6),),
    "liabilities": (("liability", 0.5),),
    "equity": (("stockholders", 0.4), ("shareholders", 0.4)),
    "operating": (("operations", 0.35),),
    "operations": (("operating", 0.35),),
    "operates": (("operate", 0.4), ("operations", 0.35), ("operating", 0.25)),
    "operate": (("operates", 0.4), ("operations", 0.3)),
}

_INCOME_TITLES = (
    "statement of income",
    "statements of income",
    "statement of operations",
    "statements of operations",
    "statement of earnings",
    "statements of earnings",
)
_CASH_TITLES = (
    "statement of cash flows",
    "statements of cash flows",
)
_POSITION_TITLES = (
    "balance sheet",
    "balance sheets",
    "statement of financial position",
    "statements of financial position",
)
_INCOME_QUERY = frozenset({
    "income",
    "ebitda",
    "margin",
    "operating",
    "earnings",
    "restructuring",
    "revenue",
    "revenues",
    "cogs",
    "turnover",
})
_CASH_QUERY = frozenset({
    "cash",
    "capex",
    "depreciation",
    "amortization",
    "amortisation",
    "expenditure",
    "expenditures",
    "ebitda",
})
_POSITION_QUERY = frozenset({
    "balance",
    "asset",
    "assets",
    "liability",
    "liabilities",
    "equity",
    "debt",
    "position",
    "ppne",
    "ppe",
    "property",
    "equipment",
    "inventory",
    "inventories",
    "turnover",
    "quick",
    "liquidity",
})

_PHRASE_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bp\s*p\s*&\s*e\b", re.IGNORECASE), " ppe property plant equipment "),
    (re.compile(r"\bcapital[\s-]+intensive\b", re.IGNORECASE), " capital intensive property plant equipment revenue "),
    (re.compile(r"\bgross\s+margin\b", re.IGNORECASE), " gross margin cost goods sold sales "),
    (re.compile(r"\binventory\s+turnover\b", re.IGNORECASE), " inventory turnover cost goods sold "),
    (re.compile(r"\bp\s*&\s*l\b", re.IGNORECASE), " income statement profit loss "),
    (re.compile(r"\bd\s*&\s*a\b", re.IGNORECASE), " depreciation amortization "),
    (re.compile(r"\bfy\s*((?:19|20)\d{2})\b", re.IGNORECASE), r" \1 "),
    (re.compile(r"\bcash\s+flows?\b", re.IGNORECASE), " cash flows "),
    (re.compile(r"\bincome\s+statements?\b", re.IGNORECASE), " income statement "),
    (re.compile(r"\bbalance\s+sheets?\b", re.IGNORECASE), " balance sheet financial position "),
    (
        re.compile(r"\brestructuring\s+(?:costs?|charges?|expenses?)\b", re.IGNORECASE),
        " restructuring charges costs ",
    ),
    (
        re.compile(r"\blegal\s+(?:battles?|proceedings?|matters?|cases?)\b", re.IGNORECASE),
        " legal proceedings litigation contingencies ",
    ),
)


class PDFDocumentInput(BaseModel):
    """Complete original PDF bytes identified by a caller-supplied document id."""

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def reject_blank_document_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """One physical page, numbered from 1, and the digest of the original PDF."""

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @field_validator("document_id")
    @classmethod
    def reject_blank_document_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value

    @field_validator("document_sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError("document_sha256 must be a lowercase SHA-256 hex digest")  # noqa: TRY003
        return value


class PDFPageSource(Source):
    """Captured text of one physical PDF page."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSourceAdapter:
    """Resolve a page record into a stable captured Source and read it back."""

    input_class = PDFPageInput
    name = "pdf-page"
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        page = PDFPageInput.model_validate(value)
        return PDFPageSource(
            name=_source_name(page.document_id, page.document_sha256, page.page_number),
            definition_version="1",
            materialization=SourceMaterialization.CAPTURED,
            document_id=page.document_id,
            document_sha256=page.document_sha256,
            page_number=page.page_number,
            text=page.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Project one page as standard text evidence anchored to the physical page."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    version="1",
    projections=(PDFPageTextEvidenceProjection(),),
)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Return every physical page of one complete PDF, or raise ``ValueError``."""

    document = value if isinstance(value, PDFDocumentInput) else PDFDocumentInput.model_validate(value)
    if not document.document_id.strip():
        raise ValueError("document_id must not be blank")  # noqa: TRY003
    if not isinstance(document.content, (bytes, bytearray)) or not document.content:
        raise ValueError("PDF content is empty or unreadable")  # noqa: TRY003
    content = bytes(document.content)
    texts = _extract_page_texts(content)
    digest = hashlib.sha256(content).hexdigest()
    return tuple(
        PDFPageInput(
            document_id=document.document_id,
            document_sha256=digest,
            page_number=page_number,
            text=_fit_page_text(document.document_id, _prepare_page_text(text)),
        )
        for page_number, text in enumerate(texts, start=1)
    )


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory and return at most ``limit`` distinct page citations."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return ()
    if not isinstance(question, str) or not question.strip():
        return ()
    records = await _list_page_records(memory)
    if not records:
        records = await _records_from_search(memory, question, limit)
    if not records:
        return ()
    records = _question_documents(records, question)
    scores = _score_texts(tuple(record.text for record in records), question)
    ranks = await _search_ranks(memory, question, records, limit)
    order = sorted(
        range(len(records)),
        key=lambda index: (-(scores[index] + ranks.get(records[index].identity, 0.0)), records[index].source_id),
    )
    selected: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    for index in order:
        if len(selected) >= limit:
            break
        record = records[index]
        if scores[index] + ranks.get(record.identity, 0.0) <= 0:
            continue
        if record.source_id in seen_pages:
            continue
        seen_pages.add(record.source_id)
        selected.append(record.citation)
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class _PageRecord:
    citation: MemoryCitation
    text: str
    source_id: str
    identity: tuple[str, str]


def _source_name(document_id: str, document_sha256: str, page_number: int) -> str:
    document_bytes = document_id.encode("utf-8")
    payload = b"\0".join((
        b"pdf-page-v1",
        str(len(document_bytes)).encode("ascii"),
        document_bytes,
        document_sha256.encode("utf-8"),
        str(page_number).encode("ascii"),
    ))
    return "p1" + hashlib.sha256(payload).hexdigest()


def _extract_page_texts(content: bytes) -> tuple[str, ...]:
    failures = _PDFExtractionFailures()
    logger = logging.getLogger("pypdf")
    logger.addHandler(failures)
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")  # noqa: TRY003
        page_count = len(reader.pages)
        if page_count < 1:
            raise ValueError("PDF contains no pages")  # noqa: TRY003
        texts: list[str] = []
        for index in range(page_count):
            page = reader.pages[index]
            _validate_text_streams(page.get("/Contents"), page.get("/Resources", {}), reader, set(), set())
            extracted = page.extract_text(extraction_mode="plain")
            texts.append("" if extracted is None else extracted)
        if len(texts) != page_count or failures.failed:
            raise ValueError("PDF page extraction was incomplete")  # noqa: TRY003
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF could not be parsed completely") from error  # noqa: TRY003
    finally:
        logger.removeHandler(failures)
    return tuple(texts)


class _PDFExtractionFailures(logging.Handler):
    """Detect explicit pypdf failures that it otherwise converts into partial output."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.thread_id = threading.get_ident()
        self.failed = False

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        message = record.getMessage()
        self.failed |= (
            record.levelno >= logging.ERROR
            or message.startswith((
                "Error ",
                "Impossible to decode XFormObject ",
                "EOF marker seems truncated",
                "EOF marker not found",
                "CAUTION: startxref found while searching for %%EOF.",
                "found startxref pointing to a previous revision after a corrupt one",
                "Invalid/Truncated xref table.",
                "XRef object at ",
                "Detected cyclic form XObject reference, skipping ",
                "Exceeded ",
                "PdfReadError(",
                "PdfStreamError(",
            ))
            or (record.name == "pypdf._reader" and message.startswith("Object ") and message.endswith(" not defined."))
        )


def _validate_text_streams(value: Any, resources: Any, reader: Any, visited: set[int], active: set[int]) -> None:
    """Check actual page/Form content without decoding unrelated image streams."""

    from pypdf.generic import NullObject

    if value is None:
        return
    stream = value.get_object()
    if isinstance(stream, NullObject):
        return
    identity = id(stream)
    if identity in active:
        raise ValueError("PDF content contains a cyclic Form reference")  # noqa: TRY003
    if identity in visited:
        return
    active.add(identity)
    try:
        _validate_called_forms(stream, resources, reader, visited, active)
    finally:
        active.remove(identity)
    visited.add(identity)


def _validate_called_forms(stream: Any, resources: Any, reader: Any, visited: set[int], active: set[int]) -> None:
    from pypdf.generic import ContentStream

    _validate_content_filters(stream)
    content = ContentStream(stream, reader)
    if hasattr(resources, "get_object"):
        resources = resources.get_object()
    for operands, operator in content.operations:
        if operator != b"Do" or not operands:
            continue
        xobjects = resources.get("/XObject", {})
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        target = xobjects.get(operands[0])
        if target is None:
            raise ValueError("PDF content refers to a missing XObject")  # noqa: TRY003
        form = target.get_object()
        if form.get("/Subtype") != "/Image":
            _validate_text_streams(form, form.get("/Resources", resources), reader, visited, active)


def _validate_content_filters(stream: Any) -> None:
    """An array of content streams is one program; only its byte filters are independent."""

    from pypdf.generic import ArrayObject, NullObject

    resolved = stream.get_object()
    if isinstance(resolved, ArrayObject):
        for part in resolved:
            _validate_content_filters(part)
    elif not isinstance(resolved, NullObject):
        _validate_flate_stream(resolved)


def _validate_flate_stream(stream: Any) -> None:
    """pypdf's recovery decoder accepts truncated DEFLATE streams; require their end marker."""

    from pypdf.filters import decode_stream_data
    from pypdf.generic import ArrayObject, NameObject

    filters = stream.get("/Filter", ())
    if hasattr(filters, "get_object"):
        filters = filters.get_object()
    if not isinstance(filters, (ArrayObject, tuple)):
        filters = (filters,)
    for index, name in enumerate(filters):
        if name not in {"/FlateDecode", "/Fl"}:
            continue
        preceding = copy(stream)
        preceding[NameObject("/Filter")] = ArrayObject(filters[:index])
        data = decode_stream_data(preceding)
        decoder = zlib.decompressobj()
        decoder.decompress(data, 65536)
        while decoder.unconsumed_tail and not decoder.eof:
            decoder.decompress(decoder.unconsumed_tail, 65536)
        if not decoder.eof:
            raise ValueError("PDF content has a truncated compressed stream")  # noqa: TRY003


def _prepare_page_text(extracted: str) -> str:
    """Normalize layout whitespace without removing any page content."""

    return unicodedata.normalize("NFC", " ".join(extracted.split()))


# canonical 层的单条文本上限是 8192 UTF-8 字节；超限会让整份文档作废，
# 代价远大于尾部丢几个词。留出余量后只在超限页上做压缩/截断。
_ENTRY_TEXT_LIMIT = 8100


def _fit_page_text(document_id: str, text: str) -> str:
    """Keep the stored entry under the canonical 8192-byte limit.

    The text projection stores ``f"{document_id}\n{text}"``.  Only pages that
    would overflow are compacted (non-alphanumerics collapsed to one space,
    which preserves the token multiset) and, if still too long, truncated on a
    byte boundary.  Normal pages are returned unchanged so their token counts
    stay identical to the frozen baseline.
    """

    head = document_id + "\n"
    if len(head.encode("utf-8")) + len(text.encode("utf-8")) <= _ENTRY_TEXT_LIMIT:
        return text
    compact = re.sub(r"[^A-Za-z0-9]+", " ", text).strip()
    if len(head.encode("utf-8")) + len(compact.encode("utf-8")) <= _ENTRY_TEXT_LIMIT:
        return compact
    budget = max(_ENTRY_TEXT_LIMIT - len(head.encode("utf-8")), 0)
    return compact.encode("utf-8")[:budget].decode("utf-8", "ignore")


def _rewrite_question(question: str) -> str:
    rewritten = question
    for pattern, replacement in _PHRASE_REWRITES:
        rewritten = pattern.sub(replacement, rewritten)
    return rewritten


def _question_documents(records: Sequence[_PageRecord], question: str) -> tuple[_PageRecord, ...]:
    """Respect report names explicitly present in the question before ranking pages.

    The standard projection prefixes every page with its document id. Using this
    shared label prevents a page from another report winning only because its
    narrative repeats more of the question's financial vocabulary.
    """

    question_words = re.findall(r"[a-z0-9]+", question.lower())
    keys_by_label = _document_query_keys(records, _explicit_query_symbols(question))
    matched: set[str] = set()
    matched_names: set[str] = set()
    for label, keys in keys_by_label.items():
        if not keys:
            continue
        longest = max(map(len, keys))
        for start in range(len(question_words)):
            phrase = ""
            for word in question_words[start:]:
                phrase += word
                if phrase in keys:
                    matched.add(label)
                    matched_names.add(phrase)
                    break
                if len(phrase) >= longest:
                    break
            if label in matched:
                break
    comparison = {"compare", "compared", "comparison", "versus", "vs"}.intersection(question_words)
    if not matched or (comparison and len(matched_names) < 2):
        return tuple(records)
    return tuple(record for record in records if record.text.partition("\n")[0] in matched)


def _document_query_keys(records: Sequence[_PageRecord], symbols: set[str]) -> dict[str, set[str]]:
    """Collect issuer names and disclosed trading symbols from stored page evidence."""

    keys_by_label: dict[str, set[str]] = {}
    for record in records:
        label = record.text.partition("\n")[0]
        if label not in keys_by_label:
            keys_by_label[label] = _document_name_keys(label)
        if symbols:
            keys_by_label[label].update(_document_trading_symbols(record.text).intersection(symbols))
    return keys_by_label


def _document_name_keys(label: str) -> set[str]:
    report_words = {
        "annual",
        "financial",
        "fiscal",
        "form",
        "fy",
        "pdf",
        "quarterly",
        "report",
        "statement",
        "statements",
        "co",
    }
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", label.lower())
        if len(word) > 1
        and not word.isdecimal()
        and word not in report_words
        and re.fullmatch(r"(?:fy)?\d{2,4}(?:q[1-4])?|q[1-4](?:\d{2,4})?|[1-4]q(?:\d{2,4})?|\d+[kq]", word) is None
    ]
    key = "".join(words)
    if len(key) < 2 or (len(key) == 2 and key.isalpha()):
        return set()
    keys = {key}
    for suffix in ("incorporated", "corporation", "company", "limited", "corp", "inc", "ltd", "llc", "plc"):
        if key.endswith(suffix) and len(key) - len(suffix) >= 3:
            keys.add(key[: -len(suffix)])
    return keys


def _explicit_query_symbols(question: str) -> set[str]:
    words = re.findall(r"\b[A-Za-z][A-Za-z0-9]{1,7}\b", question)
    symbols = {word.lower() for word in words if sum(char.isupper() for char in word) >= 2}
    explicit = {word.lower() for word in re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,7})\b", question)}
    financial = {"usd", "fy", "roa", "roe", "ebit", "ebitda", "ppne", "ppe", "cogs", "dpo", "dso", "eps", "gaap", "sec"}
    return symbols.difference(financial, _SYNONYMS, _STOPWORDS, _INCOME_QUERY, _POSITION_QUERY, _CASH_QUERY) | explicit


def _document_trading_symbols(text: str) -> set[str]:
    heading = re.search(r"\btrading\s+symbols?(?:\s*\(s\))?", text, re.IGNORECASE)
    if heading is None:
        return set()
    table = text[heading.end() : heading.end() + 5000]
    return {
        symbol.lower()
        for symbol in re.findall(r"\b([A-Z][A-Z0-9]{1,7})\s+(?:The\s+)?(?:New\s+York|NASDAQ|Nasdaq|NYSE)\b", table)
    }


def _focused_terms(question: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN.findall(_rewrite_question(question).lower()):
        if token in _STOPWORDS or token in seen:
            continue
        if len(token) == 1 or (token.isdigit() and len(token) < 2):
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _query_weights(terms: Sequence[str]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for term in terms:
        weights[term] = max(weights.get(term, 0.0), 1.0)
        for synonym, weight in _SYNONYMS.get(term, ()):
            if synonym not in _STOPWORDS:
                weights[synonym] = max(weights.get(synonym, 0.0), weight)
    return weights


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _score_texts(texts: Sequence[str], question: str) -> list[float]:
    terms = _focused_terms(question)
    if not texts or not terms:
        return [0.0] * len(texts)
    weights = _query_weights(terms)
    documents = [_tokenize(text) for text in texts]
    frequencies: list[dict[str, int]] = []
    lengths: list[int] = []
    document_frequency: dict[str, int] = {}
    for tokens in documents:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        frequencies.append(counts)
        lengths.append(len(tokens))
        for token in counts:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    count = len(documents)
    average_length = sum(lengths) / count if count else 0.0
    inverse_document_frequency = {
        token: math.log(1.0 + (count - seen + 0.5) / (seen + 0.5)) for token, seen in document_frequency.items()
    }
    present = set(document_frequency)
    scores: list[float] = []
    query_term_set = set(terms)
    for text, tokens, counts, length in zip(texts, documents, frequencies, lengths, strict=True):
        lexical = 0.0
        for token, weight in weights.items():
            frequency = counts.get(token, 0)
            if frequency <= 0 or token not in present or average_length <= 0:
                continue
            inverse = inverse_document_frequency[token]
            denominator = frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length / average_length)
            lexical += weight * inverse * frequency * (_BM25_K1 + 1.0) / denominator
        coverage = sum(inverse_document_frequency[token] for token in terms if token in counts)
        adjustment = (
            _bigram_bonus(counts, terms, inverse_document_frequency)
            + _title_bonus(tokens, query_term_set)
            + _concept_bonus(text, query_term_set)
        )
        scores.append(lexical + 0.15 * coverage + adjustment)
    return scores


def _bigram_bonus(counts: Mapping[str, int], terms: Sequence[str], inverse: Mapping[str, float]) -> float:
    bonus = 0.0
    for left, right in zip(terms, terms[1:], strict=False):
        if left in counts and right in counts:
            bonus += 0.25 * (inverse.get(left, 0.0) + inverse.get(right, 0.0))
    bonus += _section_bonus(set(counts), set(terms))
    return bonus


def _title_bonus(tokens: Sequence[str], query_terms: set[str]) -> float:
    """Prefer the face of a financial statement when the question names that statement."""

    head = "".join(tokens[:80])
    bonus = 0.0
    if query_terms & _INCOME_QUERY and any(title.replace(" ", "") in head for title in _INCOME_TITLES):
        bonus += 12.0
    if query_terms & _CASH_QUERY and any(title.replace(" ", "") in head for title in _CASH_TITLES):
        bonus += 12.0
    if query_terms & _POSITION_QUERY and any(title.replace(" ", "") in head for title in _POSITION_TITLES):
        bonus += 8.0
    return bonus


def _section_bonus(tokens: set[str], query_terms: set[str]) -> float:
    bonus = 0.0
    wants_income = bool(query_terms & _INCOME_QUERY)
    wants_cash = bool(query_terms & _CASH_QUERY)
    wants_position = bool(query_terms & _POSITION_QUERY)
    wants_legal = bool(
        query_terms
        & {
            "legal",
            "litigation",
            "lawsuit",
            "lawsuits",
            "battle",
            "battles",
            "contingency",
            "contingencies",
            "proceeding",
            "proceedings",
        }
    )
    wants_geography = bool(
        query_terms
        & {"geography", "geographies", "geographic", "geographical", "region", "regions", "country", "countries"}
    )
    consolidated = "consolidated" in tokens
    if wants_income and consolidated and tokens & {"income", "operations", "earnings"}:
        bonus += 1.4
    if wants_cash and "cash" in tokens and "flows" in tokens:
        bonus += 1.4
    if wants_position and ({"assets", "liabilities"} <= tokens or {"financial", "position"} <= tokens):
        bonus += 1.1
    if wants_legal and tokens & {"litigation", "proceedings", "contingencies", "lawsuit", "lawsuits"}:
        bonus += 1.6
    if wants_geography and tokens & {"geographic", "geographical", "geography"}:
        bonus += 1.6
    return bonus


def _concept_bonus(text: str, query_terms: set[str]) -> float:
    """Lift the financial statement or disclosure that a question is actually about.

    Lexical overlap ranks the narrative that repeats the question words. Quick-ratio, tax-rate,
    and segment-revenue questions are answered from a statement page that barely shares those words.
    """

    lowered = text.lower()
    tokens = _TOKEN.findall(lowered)
    if not tokens:
        return 0.0
    # Years are everywhere in a 10-K. Other numbers mark a statement or a quantified disclosure.
    number_count = sum(1 for token in tokens if token.isdigit() and len(token) != 4)
    density = number_count / len(tokens)
    bonus = 0.0
    consolidated = "consolidated" in lowered
    head = "".join(tokens[:80])
    balance_sheet = "balance sheet" in lowered or any(title.replace(" ", "") in head for title in _POSITION_TITLES)
    operations = any(
        phrase in lowered
        for phrase in (
            "statements of operations",
            "statement of operations",
            "statements of income",
            "statement of income",
            "statements of earnings",
            "statement of earnings",
        )
    )
    wants_balance = bool(query_terms & _POSITION_QUERY)
    if wants_balance and consolidated and balance_sheet:
        bonus += 12.0 + 45.0 * density
    wants_results = bool(query_terms & (_INCOME_QUERY | {"tax", "taxes", "inventory"}))
    if wants_results and consolidated and operations:
        bonus += 10.0 + 35.0 * density
    asks_categories = bool(query_terms & {"product", "products", "service", "services", "category", "categories"})
    if asks_categories and "segment" in lowered and ("revenue" in lowered or "revenues" in lowered):
        bonus += 8.0 + 25.0 * density
    if (
        {"product", "products"} & query_terms
        and {"service", "services"} & query_terms
        and "item 1" in lowered
        and "business" in lowered
    ):
        bonus += 6.0
    asks_customers = "customer" in query_terms or "customers" in query_terms
    if asks_customers and "customer" in lowered and ("accounted for" in lowered or "percent of" in lowered):
        bonus += 6.0
    if "tax" in query_terms and "effective tax" in lowered:
        bonus += 8.0
    if query_terms & {"revenue", "revenues"} and "net revenue" in lowered:
        bonus += 3.5
    if query_terms & {"margin", "operating"} and "operating income" in lowered:
        bonus += 4.0
    if ("retain" in query_terms or "retention" in query_terms) and "retention" in lowered:
        bonus += 6.0
    if query_terms & {"geography", "geographies", "geographic", "geographical"} and any(
        region in lowered for region in ("africa", "asia pacific", "europe", "latin america")
    ):
        bonus += 4.0
    return bonus


async def _list_page_records(memory: Any) -> tuple[_PageRecord, ...]:
    try:
        page = await memory.list()
    except Exception:
        return ()
    records: list[_PageRecord] = []
    for record in getattr(page, "entries", ()) or ():
        converted = _record_from_entry(record)
        if converted is not None:
            records.append(converted)
    return tuple(records)


async def _records_from_search(memory: Any, question: str, limit: int) -> tuple[_PageRecord, ...]:
    hits = await _run_search(memory, question, max(limit, _SEARCH_POOL))
    records: list[_PageRecord] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        try:
            loaded = await memory.get(GetMemoryEntryRequest(citation=citation))
        except Exception:
            continue
        converted = _record_from_entry(loaded)
        if converted is None or converted.identity in seen:
            continue
        seen.add(converted.identity)
        records.append(converted)
    return tuple(records)


async def _search_ranks(
    memory: Any,
    question: str,
    records: Sequence[_PageRecord],
    limit: int,
) -> dict[tuple[str, str], float]:
    known = {record.identity for record in records}
    hits = await _run_search(memory, question, max(limit, _SEARCH_POOL))
    ranks: dict[tuple[str, str], float] = {}
    for rank, hit in enumerate(hits, start=1):
        identity = (hit.entry_id, hit.entry_version_id)
        if identity not in known:
            continue
        ranks[identity] = ranks.get(identity, 0.0) + 1.5 / (60.0 + rank)
    return ranks


async def _run_search(memory: Any, question: str, limit: int) -> tuple[Any, ...]:
    query = " ".join(_focused_terms(question)[:8])
    if not query:
        return ()
    try:
        page = await memory.search(SearchMemoryRequest(query=query, limit=max(limit, 1), mode="fts"))
    except Exception:
        return ()
    return tuple(getattr(page, "hits", ()) or ())


def _record_from_entry(record: Any) -> _PageRecord | None:
    entry = getattr(record, "entry", None)
    citation = getattr(record, "citation", None)
    if entry is None or citation is None:
        return None
    state = getattr(record, "state", "active")
    if state not in {"active", None}:
        return None
    sources = tuple(getattr(entry, "sources", ()) or ())
    if len(sources) != 1:
        return None
    source = sources[0]
    if getattr(source, "source_type", None) != "pdf-page":
        return None
    source_id = getattr(source, "source_id", None)
    text = getattr(entry, "text", None)
    if not isinstance(source_id, str) or not source_id or not isinstance(text, str):
        return None
    return _PageRecord(
        citation=citation,
        text=text,
        source_id=source_id,
        identity=(entry.entry_id, entry.entry_version_id),
    )


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "parse_pdf_document",
    "search_pdf_pages",
]
