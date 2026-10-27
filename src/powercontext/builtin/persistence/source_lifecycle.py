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

"""Durable one-document-per-Scope storage for Source lifecycle state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE


class SourceLifecycleStore:
    """Persist one canonical lifecycle document per Scope.

    The Scope owns a single writer, and the document is bounded by the registered evidence
    graph, so one row per Scope is enough: registration, events, and derived availability are
    committed together and a reload reproduces exactly the committed graph.
    """

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def load(self, scope_id: str) -> dict[str, Any] | None:
        """Return the committed document for one Scope, or ``None`` when nothing was committed."""

        async with self._database.transaction() as connection:
            return await self._load(connection, scope_id)

    async def save(self, scope_id: str, document: dict[str, Any]) -> None:
        """Replace the committed document for one Scope atomically."""

        payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        async with self._database.transaction() as connection:
            await connection.execute(delete(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id))
            await connection.execute(
                insert(SOURCE_LIFECYCLE_TABLE).values(
                    scope_id=scope_id,
                    document=payload,
                    updated_at=datetime.now(UTC),
                )
            )

    @staticmethod
    async def _load(connection: AsyncConnection, scope_id: str) -> dict[str, Any] | None:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_TABLE.c.document).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
            )
        ).first()
        if row is None:
            return None
        document = json.loads(str(row[0]))
        if not isinstance(document, dict):
            raise ValueError("source lifecycle document must be an object")  # noqa: TRY003
        return document
