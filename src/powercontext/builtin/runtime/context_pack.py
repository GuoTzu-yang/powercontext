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

"""Context Pack wire protocol with prefix-reuse deltas.

A full pack carries UTF-8 safe chunks plus a digest over the unsigned payload.
A delta names how many leading chunks of a known base survive and ships only the
new suffix, so the receiver can rebuild the target pack without sender state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PREFIX = "sha256:"


class ContextPackError(Exception):
    """Raised when a pack or delta violates the wire protocol."""


def _dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: Any) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("items must be a JSON array")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("each chunk must be a Base64 string")
        try:
            decoded.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("chunk is not valid Base64") from error
    return tuple(decoded)


def _digest_of(schema: str, items: tuple[bytes, ...]) -> str:
    canonical = _dumps({"schema": schema, "items": _encode_items(items)})
    return _DIGEST_PREFIX + hashlib.sha256(canonical).hexdigest()


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContextPackError(f"{field} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete context payload as ordered UTF-8 safe chunks."""

    items: tuple[bytes, ...]
    digest: str

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        real_items = tuple(bytes(item) for item in items)
        return cls(items=real_items, digest=_digest_of(PACK_SCHEMA, real_items))

    @classmethod
    def from_content(cls, content: str, /, *, chunk_bytes: int = 512) -> ContextPack:
        if chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        raw = content.encode("utf-8")
        if not raw:
            return cls.build(())
        chunks: list[bytes] = []
        current = bytearray()
        for character in content:
            encoded = character.encode("utf-8")
            if len(encoded) > chunk_bytes:
                raise ContextPackError("a single character exceeds the chunk limit")
            if len(current) + len(encoded) > chunk_bytes:
                chunks.append(bytes(current))
                current = bytearray()
            current.extend(encoded)
        if current:
            chunks.append(bytes(current))
        return cls.build(tuple(chunks))

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        head = {
            "schema": PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
        }
        size = 0
        for _ in range(8):
            candidate = _dumps({**head, "byte_count": size})
            if len(candidate) == size:
                return candidate
            size = len(candidate)
        raise ContextPackError("pack byte_count did not converge")

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise ContextPackError("pack is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ContextPackError("pack must be a JSON object")
        if set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("pack fields do not match the wire contract")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("unsupported pack schema")
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        if not isinstance(digest, str):
            raise ContextPackError("digest must be a string")
        expected = _digest_of(PACK_SCHEMA, items)
        if digest != expected:
            raise ContextPackError("pack digest does not match its payload")
        byte_count = _require_int(payload["byte_count"], "byte_count")
        pack = cls(items=items, digest=digest)
        if byte_count != pack.byte_count:
            raise ContextPackError("byte_count does not match the wire payload")
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A prefix-reuse update from one base pack to a target pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        limit = min(len(base.items), len(target.items))
        prefix = 0
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _dumps(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise ContextPackError("delta is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ContextPackError("delta must be a JSON object")
        if set(payload) != {"schema", "base_digest", "target_digest", "prefix_items", "appended"}:
            raise ContextPackError("delta fields do not match the wire contract")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("unsupported delta schema")
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not isinstance(base_digest, str) or not isinstance(target_digest, str):
            raise ContextPackError("delta digests must be strings")
        prefix = _require_int(payload["prefix_items"], "prefix_items")
        if prefix < 0:
            raise ContextPackError("prefix_items must not be negative")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from a base pack and one delta."""

    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the supplied pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("prefix_items exceeds the base pack length")
    rebuilt = base.items[: delta.prefix_items] + delta.appended
    target = ContextPack.build(rebuilt)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match the rebuilt pack")
    return target


__all__ = [
    "PACK_SCHEMA",
    "DELTA_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
