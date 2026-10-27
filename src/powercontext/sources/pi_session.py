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

"""Pi session history: selected-branch parsing and incremental capture."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunStatus,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
_SUPPORTED_VERSION = 3
NonEmptyText = Annotated[str, Field(min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class PiSessionInput(_Frozen):
    """One Pi JSONL session file and the branch leaf to follow."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(_Frozen):
    """One visible event of the selected branch with its complete record."""

    session_id: NonEmptyText
    event_id: NonEmptyText
    parent_id: str | None
    event_type: NonEmptyText
    role: NonEmptyText
    content: NonEmptyText
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured Pi event that can be read back and projected as evidence."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable identity of one Pi event Source."""

    source_id = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("pi event source identity exceeds the maximum length")
    return source_id


def _decode(content: bytes) -> str:
    if not content:
        raise ValueError("pi session archive is empty")
    return content.decode("utf-8-sig")


def _load_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError("pi session record must be a JSON object")
        records.append(parsed)
    if not records:
        raise ValueError("pi session archive has no records")
    return records


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text_blocks(content: Any) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    tool_calls: list[str] = []
    if isinstance(content, str):
        if content:
            texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
            elif kind == "toolCall":
                name = block.get("name") or ""
                call_id = block.get("id") or ""
                arguments = block.get("arguments")
                tool_calls.append(f"[tool_call {name} {call_id}] {_compact(arguments)}")
    return texts, tool_calls


def _event_text(record: dict[str, Any]) -> tuple[str, str] | None:
    record_type = record.get("type")
    if record_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "bashExecution":
            command = message.get("command") or ""
            output = message.get("output") or ""
            return role, f"$ {command}\n{output}" if output else f"$ {command}"
        texts, tool_calls = _text_blocks(message.get("content"))
        if role == "toolResult":
            tool_name = message.get("toolName") or ""
            call_id = message.get("toolCallId") or ""
            body = "\n".join(texts)
            if not body:
                return None
            return role, f"[tool_result {tool_name} {call_id}] {body}"
        parts = [*texts, *tool_calls]
        if not parts:
            return None
        return role, "\n".join(parts)
    if record_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return record_type, summary
    if record_type == "custom_message":
        texts, _ = _text_blocks(record.get("content"))
        if not texts:
            return None
        return "custom", "\n".join(texts)
    return None


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return every visible event of the selected branch, ordered root first."""

    records = _load_records(_decode(value.content))
    header = records[0]
    if header.get("type") != "session":
        raise ValueError("pi session archive must start with a session header")
    if header.get("version") != _SUPPORTED_VERSION:
        raise ValueError("unsupported pi session version")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("pi session header must declare a non-empty id")

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("pi session record must declare a non-empty id")
        if event_id in nodes:
            raise ValueError("pi session record ids must be unique")
        parent_id = record.get("parentId")
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in nodes):
            raise ValueError("pi session parent must reference an earlier record")
        nodes[event_id] = record
        order.append(event_id)
    if not order:
        raise ValueError("pi session archive has no records")

    leaf_id = value.leaf_id if value.leaf_id is not None else order[-1]
    if leaf_id not in nodes:
        raise ValueError("pi session leaf is missing from the archive")

    chain: list[str] = []
    cursor: str | None = leaf_id
    while cursor is not None:
        chain.append(cursor)
        parent = nodes[cursor].get("parentId")
        cursor = parent if isinstance(parent, str) else None
    chain.reverse()

    events: list[PiEventInput] = []
    for event_id in chain:
        record = nodes[event_id]
        rendered = _event_text(record)
        if rendered is None:
            continue
        role, body = rendered
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=record.get("parentId"),
                event_type=str(record.get("type")),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


class PiEventAdapter:
    """Resolve parsed events into Sources and read them back verbatim."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi event as text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_ADAPTER = PiEventAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


def _record_digest(record: dict[str, JsonValue]) -> str:
    payload = _compact(record).encode()
    return hashlib.sha256(payload).hexdigest()


class PiSessionConnector:
    """Import the selected Pi branch incrementally with a durable checkpoint."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | os.PathLike[str], leaf_id: str | None = None) -> None:
        self._path = path
        self._leaf_id = leaf_id

    async def run(self, session) -> ConnectorRunCompletion:
        content = open(self._path, "rb").read()
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))

        previous = session.checkpoint if isinstance(session.checkpoint, dict) else {}
        imported_raw = previous.get("imported")
        imported: dict[str, str] = dict(imported_raw) if isinstance(imported_raw, dict) else {}

        for event in events:
            digest = _record_digest(event.record)
            known = imported.get(event.event_id)
            if known is not None:
                if known != digest:
                    raise ValueError("persisted pi event cannot be rewritten")
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            imported[event.event_id] = digest

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"imported": imported},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
