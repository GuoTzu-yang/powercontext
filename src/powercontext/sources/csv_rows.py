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

"""Row-level CSV Sources with stable identity and text-evidence projection.

Each parsed CSV record becomes one ``CAPTURED`` Source identified by the pair
``(document_id, primary_key_value)``.  Parsing is atomic: a document either
yields every row or raises ``ValueError`` without exposing partial output.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Final
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME: Final[str] = "csv-row"
NonEmptyText = Annotated[str, Field(min_length=1)]


def _is_blank(value: str) -> bool:
    return not value.strip()


def _validate_document_id(document_id: str) -> str:
    if not document_id or _is_blank(document_id):
        raise ValueError("document_id must not be empty or blank")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")
    return document_id


class CSVDocumentInput(BaseModel):
    """A complete offline CSV document to be parsed into row Sources."""

    document_id: str
    primary_key: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("primary_key")
    @classmethod
    def validate_primary_key(cls, value: str) -> str:
        if not value or _is_blank(value):
            raise ValueError("primary_key must not be empty or blank")
        return value


class CSVRowInput(BaseModel):
    """One CSV record expressed as a row Source payload."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _validate_document_id(value)

    @field_validator("columns", "values")
    @classmethod
    def require_non_empty_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("columns and values must not be empty")
        return value

    @field_validator("values")
    @classmethod
    def require_matching_width(
        cls, value: tuple[str, ...], info
    ) -> tuple[str, ...]:
        columns = info.data.get("columns")
        if columns is not None and len(value) != len(columns):
            raise ValueError("values must match the header width")
        return value

    @field_validator("primary_key_value")
    @classmethod
    def validate_primary_key_value(cls, value: str) -> str:
        if _is_blank(value):
            raise ValueError("primary_key_value must not be empty or blank")
        return value


class CSVRowSource(Source):
    """Captured CSV row that can be read back and projected as evidence."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the delimiter-safe identity of one CSV row Source."""

    _validate_document_id(document_id)
    if _is_blank(primary_key_value):
        raise ValueError("primary_key_value must not be empty or blank")
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("csv row source identity exceeds the maximum length")
    return source_id


def _decode(content: bytes) -> str:
    if not content:
        raise ValueError("csv document is empty")
    return content.decode("utf-8-sig")


def _read_records(text: str) -> list[list[str]]:
    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=",",
        quotechar='"',
        doublequote=True,
        strict=True,
    )
    try:
        return [record for record in reader]
    except csv.Error as error:
        raise ValueError(f"malformed csv document: {error}") from error


def parse_csv_document(document: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate one CSV document into row inputs, atomically."""

    records = _read_records(_decode(document.content))
    if not records:
        raise ValueError("csv document has no header")
    header = records[0]
    if not header:
        raise ValueError("csv header is empty")
    if any(_is_blank(field) for field in header):
        raise ValueError("csv header fields must not be empty or blank")
    if len(set(header)) != len(header):
        raise ValueError("csv header fields must be unique")
    if document.primary_key not in header:
        raise ValueError("primary_key must match a header field exactly")
    key_index = header.index(document.primary_key)
    columns = tuple(header)

    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError("csv record width does not match the header")
        primary_key_value = record[key_index]
        if _is_blank(primary_key_value):
            raise ValueError("csv primary key value must not be empty or blank")
        if primary_key_value in seen:
            raise ValueError("csv primary key values must be unique within a document")
        seen.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document.document_id,
                primary_key=document.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve row payloads into Sources and read them back verbatim."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as text evidence without losing column order."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        content = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=content,
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)
