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

"""Coalesce concurrent Embedding calls and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage


@dataclass(frozen=True, slots=True)
class _Vector:
    values: Any
    usage: InferenceUsage | None
    call: int | None


class CoalescingEmbeddingModel:
    """Share in-flight text embeddings and reuse successful vectors.

    Every distinct text owns one in-flight future from the moment a caller asks for it until the
    batch carrying it settles. A caller that arrives while that batch is already running
    subscribes to the same future instead of paying for a second provider request.
    """

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, _Vector] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[_Vector]] = {}
        self._queue: deque[str] = deque()
        self._runner: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._calls = 0

    @property
    def profile(self) -> Any:
        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A wrapper reused from a second event loop must not hand out futures owned by a
            # loop that is no longer running.
            self._loop = loop
            self._inflight.clear()
            self._queue.clear()
            self._runner = None

        resolved: dict[str, _Vector] = {}
        waiters: list[tuple[str, asyncio.Future[_Vector]]] = []
        for text in dict.fromkeys(texts):
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                resolved[text] = cached
                continue
            future = self._inflight.get(text)
            if future is None:
                future = loop.create_future()
                self._inflight[text] = future
                self._queue.append(text)
                self._ensure_runner()
            waiters.append((text, future))

        for text, future in waiters:
            # ``shield`` keeps the shared work alive when this caller is cancelled.
            resolved[text] = await asyncio.shield(future)

        vectors = tuple(resolved[text].values for text in texts)
        used_calls: dict[int, InferenceUsage] = {}
        for text in dict.fromkeys(texts):
            entry = resolved[text]
            if entry.call is None or entry.call in used_calls:
                continue
            used_calls[entry.call] = entry.usage or InferenceUsage(requests=0)
        if not used_calls:
            return EmbeddingResult(vectors=vectors, usage=InferenceUsage(requests=0))
        tokens_in = [u.input_tokens for u in used_calls.values()]
        tokens_out = [u.output_tokens for u in used_calls.values()]
        usage = InferenceUsage(
            requests=len(used_calls),
            input_tokens=None if any(value is None for value in tokens_in) else sum(tokens_in),  # type: ignore[arg-type]
            output_tokens=None if any(value is None for value in tokens_out) else sum(tokens_out),  # type: ignore[arg-type]
        )
        return EmbeddingResult(vectors=vectors, usage=usage)

    def _ensure_runner(self) -> None:
        if self._runner is None or self._runner.done():
            self._runner = asyncio.get_running_loop().create_task(self._drain())

    def _discard(self, text: str, future: asyncio.Future[_Vector] | None = None) -> None:
        held = self._inflight.get(text)
        if held is not None and (future is None or held is future):
            del self._inflight[text]

    def _settle_failure(self, batch: list[tuple[str, asyncio.Future[_Vector]]], error: BaseException) -> None:
        for text, future in batch:
            self._discard(text, future)
            if not future.done():
                future.set_exception(error)

    async def _drain(self) -> None:
        try:
            while self._queue:
                if self._coalesce_seconds > 0:
                    await asyncio.sleep(self._coalesce_seconds)
                texts: list[str] = []
                while self._queue and len(texts) < self._max_batch_size:
                    text = self._queue.popleft()
                    if text in self._inflight:
                        texts.append(text)
                if not texts:
                    continue
                batch = [(text, self._inflight[text]) for text in texts]
                self._calls += 1
                call = self._calls
                try:
                    result = await self._model.embed(tuple(texts))
                except asyncio.CancelledError:
                    for text, future in batch:
                        self._discard(text, future)
                        if not future.done():
                            future.cancel()
                    raise
                except BaseException as error:  # noqa: BLE001 - propagated to every waiter
                    self._settle_failure(batch, error)
                    continue
                if len(result.vectors) != len(texts):
                    self._settle_failure(
                        batch, ValueError("embedding model returned an unexpected vector count")
                    )
                    continue
                for (text, future), vector in zip(batch, result.vectors, strict=True):
                    self._store(text, _Vector(values=vector, usage=None, call=None))
                    self._discard(text, future)
                    if not future.done():
                        future.set_result(_Vector(values=vector, usage=result.usage, call=call))
        finally:
            if self._runner is asyncio.current_task():
                self._runner = None

    def _store(self, text: str, entry: _Vector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = entry
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


__all__ = ["CoalescingEmbeddingModel"]
