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

"""HTTP scoring-service reranker with batch requests and global ordering.

The service is asked only for scores.  Candidate selection still happens here,
using the original coarse ranks so that batching never renumbers candidates and
never changes hit identity.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference.models import InferenceUsage

_DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score Memory candidates with one HTTP cross-encoder endpoint."""

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = _DEFAULT_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be a non-empty string")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return the best ``limit`` original ranks ordered by service score."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        candidate_count = len(candidates)
        if candidate_count == 0:
            raise ValueError("candidates must not be empty")
        if limit < 1 or limit > candidate_count:
            raise ValueError("limit must be within the candidate pool")

        scored: list[tuple[float, int]] = []
        requests = 0
        for start in range(0, candidate_count, self._batch_size):
            batch = tuple(enumerate(candidates[start : start + self._batch_size], start=start + 1))
            payload = {
                "query": query,
                "candidates": [{"rank": rank, "text": hit.text} for rank, hit in batch],
            }
            response = await self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
            requests += 1
            scored.extend(self._parse_scores(response.json(), tuple(rank for rank, _ in batch)))

        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = tuple(rank for _, rank in scored[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=requests),
            discarded_rank_count=candidate_count - len(selected),
        )

    @staticmethod
    def _parse_scores(document: Any, expected_ranks: tuple[int, ...]) -> list[tuple[float, int]]:
        if not isinstance(document, dict):
            raise ValueError("reranker response must be a JSON object")
        if set(document) != {"scores"}:
            raise ValueError("reranker response must contain exactly the scores field")
        scores = document["scores"]
        if not isinstance(scores, list):
            raise ValueError("scores must be a JSON array")
        if len(scores) != len(expected_ranks):
            raise ValueError("scores must cover every candidate exactly once")

        parsed: list[tuple[float, int]] = []
        seen: set[int] = set()
        for item in scores:
            if not isinstance(item, dict):
                raise ValueError("each score record must be a JSON object")
            if set(item) != {"rank", "score"}:
                raise ValueError("each score record must contain exactly rank and score")
            rank = item["rank"]
            if type(rank) is not int:
                raise ValueError("rank must be an integer")
            value = item["score"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("score must be a number")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("score must be finite")
            if rank in seen:
                raise ValueError("rank must not repeat inside one batch")
            seen.add(rank)
            parsed.append((numeric, rank))

        if seen != set(expected_ranks):
            raise ValueError("score ranks must match the requested ranks")
        return parsed


__all__ = ["HTTPMemoryReranker"]
