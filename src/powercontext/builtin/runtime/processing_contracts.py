# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Serializable, domain-independent Scope invocation and Worker contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from powercontext.builtin.persistence.supervision import ArtifactProcessingFence


class ArtifactProcessingWorkerOutcome(StrEnum):
    """Terminal control results; domain conflicts are safe to reschedule."""

    SUCCEEDED = "succeeded"
    CURSOR_CONFLICT = "cursor_conflict"
    HEAD_CONFLICT = "head_conflict"
    LEADERSHIP_LOST = "leadership_lost"


@dataclass(frozen=True, slots=True)
class ArtifactProcessingWorkAssignment:
    """One accepted Scope invocation, independent of its processor's inputs."""

    binding_name: str
    scope_id: str
    artifact_family: str
    claimed_request_generation: int
    fence: ArtifactProcessingFence
    worker_id: str
    # A Family admitted by pending journal identity stops re-admitting itself
    # after one window until the remaining count reaches this threshold again.
    source_count_threshold: int | None = None


@dataclass(frozen=True, slots=True)
class ArtifactProcessingWorkerFailure:
    """Sanitized metadata safe to return across a child process boundary."""

    stage: str
    error_code: str
    exception_type: str
    traceback: str


@dataclass(frozen=True, slots=True)
class ArtifactProcessingWorkerCompletion:
    """Control result; success still requires durable acknowledgement."""

    outcome: ArtifactProcessingWorkerOutcome = ArtifactProcessingWorkerOutcome.SUCCEEDED
    held_count: int = 0
    hold_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactProcessingCountPage:
    """One keyset page of counted admission, independent of domain work."""

    next_after_scope_id: str | None
    admitted: tuple[str, ...] = ()
    pending_input: bool = False


class ArtifactProcessingCountProvider(Protocol):
    """Accept Scopes whose pending journal identities reach a threshold.

    Counting reads persisted journal metadata only; a provider must never run a
    Family processor, build a model, or read a Source body.
    """

    async def admit(
        self,
        after_scope_id: str | None,
        limit: int,
        threshold: int,
        /,
        *,
        fence: ArtifactProcessingFence,
    ) -> ArtifactProcessingCountPage: ...


class ArtifactProcessingWorkerHandle(Protocol):
    """One child whose termination completes only after its exit is observed."""

    async def wait(self) -> ArtifactProcessingWorkerCompletion: ...

    async def terminate(self) -> None: ...


class ArtifactProcessingWorkerLauncher(Protocol):
    """Launch a child, reclaiming partially started resources on cancellation."""

    async def start(self, assignment: ArtifactProcessingWorkAssignment) -> ArtifactProcessingWorkerHandle: ...


WorkerEntrypoint = Callable[[ArtifactProcessingWorkAssignment], ArtifactProcessingWorkerCompletion | None]


__all__ = [
    "ArtifactProcessingCountPage",
    "ArtifactProcessingCountProvider",
    "ArtifactProcessingWorkAssignment",
    "ArtifactProcessingWorkerCompletion",
    "ArtifactProcessingWorkerFailure",
    "ArtifactProcessingWorkerHandle",
    "ArtifactProcessingWorkerLauncher",
    "ArtifactProcessingWorkerOutcome",
    "WorkerEntrypoint",
]
