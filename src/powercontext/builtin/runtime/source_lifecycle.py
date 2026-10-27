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

"""Propagate Source lifecycle events through one Scope's evidence DAG.

A Scope registers Sources, the Memory entries derived from them, and the Topic Memory drafts
derived from those entries. Each registration names its parents and the ``all``/``any`` policy
that decides whether the registered body is still supported. A Source event then flips that
Source's availability and every dependent node is re-evaluated bottom-up, so unsupported content
leaves native search and Context while still being readable on its exact historical citation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory.models import Memory
from powercontext.builtin.artifacts.memory.service import MemoryService
from powercontext.builtin.persistence.source_lifecycle import SourceLifecycleStore
from powercontext.builtin.sources import validate_scope_id
from powercontext.builtin.records import BaseValueNotFoundError
from powercontext.errors import ArtifactNotFoundError, SourceNotFoundError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

SOURCE_LIFECYCLE_DOCUMENT_VERSION = 1

PolicyKind: TypeAlias = Literal["all", "any"]
NodeKind: TypeAlias = Literal["source", "memory", "topic"]
LifecycleReference: TypeAlias = SourceRef | MemoryCitation | ArtifactRef
LifecyclePurpose: TypeAlias = Literal["generation", "audit"]

_PURPOSES: frozenset[str] = frozenset({"generation", "audit"})
_POLICIES: frozenset[str] = frozenset({"all", "any"})


class SourceLifecycleError(ValueError):
    """Reject one invalid lifecycle registration, event, or query.

    ``code`` is a stable, non-sensitive discriminator so callers can branch without parsing the
    message. Every rejection is a contract violation the caller caused, never an internal fault.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One caller-declared Source availability transition.

    The event is a closed record: ``revision`` must equal the registered lifecycle revision and
    ``sequence`` must advance monotonically, so a replayed event is recognizable instead of
    silently re-applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str
    revision: int
    sequence: int
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def _require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source lifecycle text must contain non-whitespace content")  # noqa: TRY003
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def _require_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("source lifecycle counters must be positive")  # noqa: TRY003
        return value


@dataclass
class _LifecycleNode:
    """One registered node: its binding, and (for Sources) its committed state."""

    node_id: str
    kind: NodeKind
    parents: tuple[str, ...] = ()
    policy: PolicyKind = "all"
    revision: int | None = None
    sequence: int = 0
    available: bool = True
    reference: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "parents": list(self.parents),
            "policy": self.policy,
            "revision": self.revision,
            "sequence": self.sequence,
            "available": self.available,
            "reference": self.reference,
        }

    @classmethod
    def from_document(cls, node_id: str, value: Mapping[str, Any]) -> _LifecycleNode:
        kind = value["kind"]
        policy = value["policy"]
        if kind not in {"source", "memory", "topic"} or policy not in _POLICIES:
            raise SourceLifecycleError("document-invalid")
        parents = tuple(str(parent) for parent in value["parents"])
        revision = value["revision"]
        return cls(
            node_id=node_id,
            kind=kind,
            parents=parents,
            policy=policy,
            revision=None if revision is None else int(revision),
            sequence=int(value["sequence"]),
            available=bool(value["available"]),
            reference=dict(value["reference"]),
        )


def lifecycle_reference(node: _LifecycleNode) -> LifecycleReference:
    """Rebuild the exact registered reference of one node."""

    if node.kind == "source":
        return SourceRef.model_validate(node.reference)
    if node.kind == "memory":
        return MemoryCitation.model_validate(node.reference)
    return ArtifactRef.model_validate(node.reference)


class SourceLifecycleService:
    """Hold one Scope's evidence DAG and its durable document.

    Pure graph work: no I/O besides loading and committing the document, so every availability
    answer used by the Runtime comes from the same committed state.
    """

    def __init__(self, scope_id: str, *, store: SourceLifecycleStore | None = None) -> None:
        self.scope_id = scope_id
        self._store = store
        self._nodes: dict[str, _LifecycleNode] = {}
        self._loaded = False

    @property
    def nodes(self) -> Mapping[str, _LifecycleNode]:
        return self._nodes

    def get(self, node_id: str) -> _LifecycleNode | None:
        return self._nodes.get(node_id)

    async def load(self) -> None:
        """Read the committed document once; a reload after restart reproduces the graph."""

        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        document = await self._store.load(self.scope_id)
        if document is None:
            return
        if document.get("version") != SOURCE_LIFECYCLE_DOCUMENT_VERSION:
            raise SourceLifecycleError("document-version")
        for node_id, value in dict(document.get("nodes", {})).items():
            self._nodes[str(node_id)] = _LifecycleNode.from_document(str(node_id), value)

    async def save(self) -> None:
        if self._store is None:
            return
        await self._store.save(
            self.scope_id,
            {
                "version": SOURCE_LIFECYCLE_DOCUMENT_VERSION,
                "nodes": {node_id: node.document() for node_id, node in self._nodes.items()},
            },
        )

    def availability(self) -> dict[str, bool]:
        """Resolve every node's support, Sources first and dependencies bottom-up."""

        resolved: dict[str, bool] = {
            node_id: node.available for node_id, node in self._nodes.items() if node.kind == "source"
        }
        pending = [node_id for node_id, node in self._nodes.items() if node.kind != "source"]
        while pending:
            remaining: list[str] = []
            progressed = False
            for node_id in pending:
                node = self._nodes[node_id]
                if not all(parent in resolved for parent in node.parents):
                    remaining.append(node_id)
                    continue
                support = [resolved[parent] for parent in node.parents]
                resolved[node_id] = all(support) if node.policy == "all" else any(support)
                progressed = True
            if not progressed:
                raise SourceLifecycleError("dependency-cycle")
            pending = remaining
        return resolved

    def add(self, node: _LifecycleNode) -> None:
        """Register one node whose identity and dependency contract were already validated."""

        self._nodes[node.node_id] = node

    def require_node(self, node_id: str) -> _LifecycleNode:
        node = self._nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("node-unknown")
        return node

    def set_source_state(self, node_id: str, *, available: bool, sequence: int) -> None:
        node = self.require_node(node_id)
        node.available = available
        node.sequence = sequence


def validate_registration(
    *,
    node_id: str,
    kind: NodeKind,
    parents: Sequence[str],
    policy: str,
    nodes: Mapping[str, _LifecycleNode],
) -> tuple[str, ...]:
    """Validate one derived registration against the already-registered graph."""

    _require_identifier(node_id)
    if not isinstance(policy, str) or policy not in _POLICIES:
        raise SourceLifecycleError("policy")
    normalized = tuple(parents)
    for parent in normalized:
        if not isinstance(parent, str):
            raise SourceLifecycleError("parent")
        _require_identifier(parent)
    if kind != "source":
        if not normalized:
            raise SourceLifecycleError("parent-missing")
        if len(set(normalized)) != len(normalized):
            raise SourceLifecycleError("parent-duplicate")
        if node_id in normalized:
            raise SourceLifecycleError("parent-self")
        for parent in normalized:
            if parent not in nodes:
                raise SourceLifecycleError("parent-unknown")
    return normalized


def _require_identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("identifier")


def supported_by_parents(parents: Sequence[str], policy: str, available: Mapping[str, bool]) -> bool:
    """Evaluate one node's policy over the current support of its parents."""

    support = [available[parent] for parent in parents]
    if not support:
        return False
    return all(support) if policy == "all" else any(support)


class SourceLifecycleApplication:
    """Scope selector for the Runtime's lifecycle operations."""

    def __init__(self, runtime: BuiltinRuntime, store: SourceLifecycleStore | None = None) -> None:
        self._runtime = runtime
        self._store = store
        self._services: dict[str, SourceLifecycleService] = {}
        self._guard = asyncio.Lock()

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, self, validate_scope_id(scope_id))

    async def service(self, scope_id: str) -> SourceLifecycleService:
        """Return the loaded per-Scope service, sharing one instance across callers."""

        async with self._guard:
            service = self._services.get(scope_id)
            if service is None:
                service = SourceLifecycleService(scope_id, store=self._store)
                self._services[scope_id] = service
            await service.load()
            return service

    async def unsupported_topics(self, scope_id: str) -> frozenset[tuple[str, int]]:
        """Report Topic Memory revisions whose support was withdrawn in one Scope.

        Derived purely from the committed graph, so a restarted Runtime reports the same answer
        without re-running the Original events.
        """

        service = await self.service(scope_id)
        availability = service.availability()
        unsupported: set[tuple[str, int]] = set()
        for node_id, node in service.nodes.items():
            if node.kind != "topic" or availability.get(node_id, False):
                continue
            reference = lifecycle_reference(node)
            if isinstance(reference, ArtifactRef):
                unsupported.add((reference.artifact_id, reference.revision))
        return frozenset(unsupported)


class ScopedSourceLifecycleApplication:
    """Register evidence and apply Source events inside one Scope."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        application: SourceLifecycleApplication,
        scope_id: str,
    ) -> None:
        self._runtime = runtime
        self._application = application
        self.scope_id = scope_id

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Bind one captured Source to a lifecycle revision."""

        _require_identifier(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-reference")
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("source-revision")
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            service = await self._application.service(self.scope_id)
            existing = service.get(node_id)
            reference = source_ref.model_dump(mode="json")
            if existing is not None:
                if existing.kind == "source" and existing.revision == revision and existing.reference == reference:
                    return
                raise SourceLifecycleError("registration-conflict")
            await self._require_readable_source(context, source_ref)
            service.add(
                _LifecycleNode(
                    node_id=node_id,
                    kind="source",
                    revision=revision,
                    available=True,
                    reference=reference,
                )
            )
            await service.save()

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one exact Memory entry version to the Sources or entries that support it."""

        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-reference")
        await self._register_derived(
            node_id=node_id,
            kind="memory",
            reference=citation.model_dump(mode="json"),
            parents=parents,
            policy=policy,
            readability=self._require_readable_memory,
            value=citation,
        )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        """Bind one Topic Memory Artifact revision to the entries that support its body."""

        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("topic-reference")
        await self._register_derived(
            node_id=node_id,
            kind="topic",
            reference=reference.model_dump(mode="json"),
            parents=parents,
            policy=policy,
            readability=self._require_readable_topic,
            value=reference,
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> tuple[str, ...]:
        """Apply one Source event and return every currently supported node, sorted."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event")
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            service = await self._application.service(self.scope_id)
            node = service.require_node(event.node_id)
            if node.kind != "source":
                raise SourceLifecycleError("not-a-source")
            if event.revision != node.revision:
                raise SourceLifecycleError("revision-mismatch")
            if event.sequence < node.sequence:
                raise SourceLifecycleError("sequence-regressed")
            if event.sequence == node.sequence:
                if event.available != node.available:
                    raise SourceLifecycleError("sequence-conflict")
                return _supported_nodes(service.availability())
            previous_state = (node.available, node.sequence)
            before = service.availability()
            service.set_source_state(node.node_id, available=event.available, sequence=event.sequence)
            after = service.availability()
            changes = tuple(
                (service.get(node_id), after[node_id])
                for node_id in sorted(after)
                if before[node_id] != after[node_id] and service.get(node_id).kind == "memory"
            )
            try:
                await self._apply_memory_states(context, changes, reason=event.reason)
            except BaseException:
                # A failed native write leaves the event uncommitted, so the retry replays it.
                service.set_source_state(
                    node.node_id,
                    available=previous_state[0],
                    sequence=previous_state[1],
                )
                raise
            await service.save()
            return _supported_nodes(after)

    async def resolve(self, node_id: str, /, *, purpose: str = "") -> LifecycleReference:
        """Return the registered reference for generation or historical audit."""

        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose")
        service = await self._application.service(self.scope_id)
        node = service.get(_require_resolvable(node_id))
        availability = service.availability()
        if purpose == "generation" and not availability.get(node.node_id, False):
            raise SourceLifecycleError("unsupported")
        return lifecycle_reference(node)

    async def available(self) -> tuple[str, ...]:
        """Return every supported node in this Scope, sorted by node identity."""

        service = await self._application.service(self.scope_id)
        return tuple(_supported_nodes(service.availability()))

    async def _register_derived(
        self,
        *,
        node_id: str,
        kind: NodeKind,
        reference: dict[str, Any],
        parents: Sequence[str],
        policy: str,
        readability: Any,
        value: LifecycleReference,
    ) -> None:
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            service = await self._application.service(self.scope_id)
            normalized = validate_registration(
                node_id=node_id,
                kind=kind,
                parents=tuple(parents),
                policy=policy,
                nodes=service.nodes,
            )
            existing = service.get(node_id)
            if existing is not None:
                if (
                    existing.kind == kind
                    and existing.parents == normalized
                    and existing.policy == policy
                    and existing.reference == reference
                ):
                    return
                raise SourceLifecycleError("registration-conflict")
            await readability(context, value)
            availability = service.availability()
            supported = supported_by_parents(normalized, policy, availability)
            node = _LifecycleNode(
                node_id=node_id,
                kind=kind,
                parents=normalized,
                policy=policy,  # type: ignore[arg-type]
                available=supported,
                reference=reference,
            )
            service.add(node)
            try:
                if kind == "memory" and not supported:
                    await self._apply_memory_states(context, ((node, False),), reason="unsupported at registration")
            except BaseException:
                del service.nodes[node_id]  # type: ignore[attr-defined]
                raise
            await service.save()

    async def _require_readable_source(self, context: Any, source_ref: SourceRef) -> None:
        try:
            record = await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type, source_ref.source_id
            )
        except (SourceNotFoundError, BaseValueNotFoundError) as error:
            raise SourceLifecycleError("source-unreadable") from error
        if record.source_id != source_ref.source_id:
            raise SourceLifecycleError("source-unreadable")

    async def _require_readable_memory(self, context: Any, citation: MemoryCitation) -> None:
        await self._memory_entry(context, citation)

    async def _require_readable_topic(self, context: Any, reference: ArtifactRef) -> None:
        if reference.family != "topic-memory":
            raise SourceLifecycleError("topic-family")
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                reference.family, reference.artifact_id, reference.revision
            )
        except (ArtifactNotFoundError, BaseValueNotFoundError) as error:
            raise SourceLifecycleError("topic-unreadable") from error

    async def _memory_entry(self, context: Any, citation: MemoryCitation) -> Any:
        service = context.artifacts.memory
        head = await _memory_head(service, context.artifacts.memory_artifact_id)
        if head is None:
            raise SourceLifecycleError("memory-missing")
        return await _current_entry(service, head, citation)

    async def _apply_memory_states(
        self,
        context: Any,
        changes: Sequence[tuple[_LifecycleNode, bool]],
        *,
        reason: str,
    ) -> None:
        if not changes:
            return
        service = context.artifacts.memory
        for node, supported in changes:
            citation = lifecycle_reference(node)
            if not isinstance(citation, MemoryCitation):
                raise SourceLifecycleError("memory-reference")
            head = await _memory_head(service, context.artifacts.memory_artifact_id)
            if head is None:
                raise SourceLifecycleError("memory-missing")
            entry = await _current_entry(service, head, citation)
            if supported:
                await service.reactivate(head, entries=(entry,), reason=reason)
            else:
                await service.forget(head, entries=(entry,), reason=reason)


def _supported_nodes(availability: Mapping[str, bool]) -> list[str]:
    """Return the sorted identities that are still supported."""

    return sorted(node_id for node_id, supported in availability.items() if supported)


def _require_resolvable(node_id: str) -> str:
    _require_identifier(node_id)
    return node_id


async def _memory_head(service: MemoryService, artifact_id: str) -> Memory | None:
    try:
        return await service.head(artifact_id)
    except ArtifactNotFoundError:
        return None


async def _current_entry(service: MemoryService, head: Memory, citation: MemoryCitation) -> Any:
    entries = {entry.entry_id: entry for entry in await service.entries(head)}
    entry = entries.get(citation.entry_id)
    if entry is None or entry.entry_version_id != citation.entry_version_id:
        raise SourceLifecycleError("memory-binding")
    return entry
