"""Hybrid retrieval math: Reciprocal Rank Fusion + recency + token budget.

Pure functions, no I/O, so the ranking can be checked by hand (see tests).

**Fusion (RRF).** Cormack, Clarke & Buettcher, "Reciprocal Rank Fusion
outperforms Condorcet and individual rank learning methods", SIGIR 2009::

    RRF(d) = sum over retrievers r of  1 / (k + rank_r(d)),   k = 60, ranks 1-based

RRF combines *ranks*, so BM25 scores (unbounded, corpus-dependent) and cosine
similarities (-1..1) never have to be normalized onto a common scale.

**Recency.** Exponential half-life decay with a floor beta (engineering judgment:
old engineering notes must not vanish)::

    s(d) = RRF(d) * [ beta + (1 - beta) * 2^(-dt / T_half) ]

**Relevance gate.** Vector hits with cosine < tau are dropped *before* fusion,
so "nearest" does not mean "relevant" when nothing relevant exists.

**Budget.** Greedy by s(d), skipping chunks that don't fit, until either
``inject_max`` chunks or ``token_budget`` approximate tokens (chars / 4, a
standard rule of thumb for English with BPE tokenizers) are used.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from hub.memory.index import Hit

CHARS_PER_TOKEN = 4.0


@dataclass(frozen=True)
class RetrievalParams:
    rrf_k: int = 60
    half_life_days: float = 180.0
    recency_floor: float = 0.7
    min_cosine: float = 0.35


@dataclass(frozen=True)
class Scored:
    hit: Hit
    score: float
    rrf: float
    recency: float
    keyword_rank: int | None
    vector_rank: int | None


def recency_factor(created: datetime, now: datetime, half_life_days: float, floor: float) -> float:
    dt_days = max((now - created).total_seconds() / 86_400.0, 0.0)
    return floor + (1.0 - floor) * math.pow(2.0, -dt_days / half_life_days)


def fuse(
    keyword: Sequence[Hit], vector: Sequence[Hit], params: RetrievalParams, now: datetime
) -> list[Scored]:
    """Fuse two ranked lists (best first) into one, best first. Deterministic."""
    relevant_vector = [h for h in vector if h.cosine is not None and h.cosine >= params.min_cosine]
    k_rank = {h.chunk_id: i for i, h in enumerate(keyword, start=1)}
    v_rank = {h.chunk_id: i for i, h in enumerate(relevant_vector, start=1)}
    hits = {h.chunk_id: h for h in [*relevant_vector, *keyword]}

    out: list[Scored] = []
    for cid, hit in hits.items():
        rrf = sum(1.0 / (params.rrf_k + r[cid]) for r in (k_rank, v_rank) if cid in r)
        rec = recency_factor(hit.created, now, params.half_life_days, params.recency_floor)
        out.append(Scored(hit, rrf * rec, rrf, rec, k_rank.get(cid), v_rank.get(cid)))
    out.sort(key=lambda s: (-s.score, s.hit.chunk_id))
    return out


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def select_within_budget(
    scored: Sequence[Scored], *, max_items: int, token_budget: int
) -> list[Scored]:
    chosen: list[Scored] = []
    used = 0
    for s in scored:
        if len(chosen) >= max_items:
            break
        cost = approx_tokens(s.hit.text)
        if used + cost > token_budget:
            continue  # a smaller, lower-ranked chunk may still fit
        chosen.append(s)
        used += cost
    return chosen
