from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hub.memory.index import Hit, MemoryIndex
from hub.memory.protocol import NewMemoryItem, Sensitivity
from hub.memory.retrieval import (
    RetrievalParams,
    approx_tokens,
    fuse,
    recency_factor,
    select_within_budget,
)
from hub.memory.store import LocalMemoryProvider
from tests.fakes import FakeEmbedder

NOW = datetime(2026, 11, 23, tzinfo=UTC)
P, PE, PR = Sensitivity.PUBLIC, Sensitivity.PERSONAL, Sensitivity.PRIVATE


def hit(cid: int, *, cosine: float | None = None, age_days: float = 0, text: str = "x") -> Hit:
    return Hit(
        chunk_id=cid, note_id=f"n{cid}", title="t", heading="", text=text,
        sensitivity=P, created=NOW - timedelta(days=age_days), tags=(), source="test",
        cosine=cosine,
    )  # fmt: skip


# ------------------------------------------------------------------ math


def test_rrf_hand_computed() -> None:
    a, b, c = hit(1), hit(2, cosine=0.9), hit(3, cosine=0.8)
    out = fuse([a, b], [b, c], RetrievalParams(rrf_k=60, recency_floor=1.0), NOW)
    assert [s.hit.chunk_id for s in out] == [2, 1, 3]
    assert out[0].rrf == pytest.approx(1 / 62 + 1 / 61)  # 0.032522
    assert out[1].rrf == pytest.approx(1 / 61)  # 0.016393
    assert out[2].rrf == pytest.approx(1 / 62)  # 0.016129
    assert (out[0].keyword_rank, out[0].vector_rank) == (2, 1)


def test_low_cosine_hits_are_dropped_before_fusion() -> None:
    out = fuse([], [hit(1, cosine=0.2), hit(2, cosine=0.5)], RetrievalParams(min_cosine=0.35), NOW)
    assert [s.hit.chunk_id for s in out] == [2]
    assert out[0].vector_rank == 1  # rank counts only relevant hits


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [(0, 1.0), (180, 0.7 + 0.3 * 0.5), (360, 0.7 + 0.3 * 0.25), (100_000, 0.7), (-5, 1.0)],
)
def test_recency_factor(age_days: float, expected: float) -> None:
    created = NOW - timedelta(days=age_days)
    assert recency_factor(created, NOW, 180, 0.7) == pytest.approx(expected)


def test_recency_breaks_ties() -> None:
    old, new = hit(1, cosine=0.9, age_days=720), hit(2, age_days=0)
    out = fuse([new], [old], RetrievalParams(), NOW)  # both rank 1 in one list
    assert [s.hit.chunk_id for s in out] == [2, 1]


def test_budget_skips_oversized_but_keeps_going() -> None:
    big, small1, small2 = hit(1, text="x" * 4000), hit(2, text="y" * 40), hit(3, text="z" * 40)
    scored = fuse([big, small1, small2], [], RetrievalParams(), NOW)
    chosen = select_within_budget(scored, max_items=5, token_budget=100)
    assert [s.hit.chunk_id for s in chosen] == [2, 3]
    assert select_within_budget(scored, max_items=1, token_budget=10_000)[0].hit.chunk_id == 1
    assert approx_tokens("abcde") == 2


# ------------------------------------------------------------------ provider


def provider(tmp_path: Path, embedder: FakeEmbedder | None) -> LocalMemoryProvider:
    return LocalMemoryProvider(
        tmp_path / "mem", MemoryIndex(tmp_path / "idx.sqlite3"), embedder, candidates=10
    )


async def seed(mem: LocalMemoryProvider) -> dict[str, str]:
    return {
        "bolts": await mem.put(NewMemoryItem(
            text="Preload M10 8.8 bolts to 75% of proof load.", source="test",
            sensitivity=PE, title="Bolt preload",
        )),
        "beam": await mem.put(NewMemoryItem(
            text="Cantilever tip deflection is P L^3 / 3 E I.", source="test",
            sensitivity=P, title="Cantilever deflection",
        )),
        "bank": await mem.put(NewMemoryItem(
            text="Bolt supplier bank account 1234-5678.", source="test",
            sensitivity=PR, title="Supplier bank",
        )),
    }  # fmt: skip


async def test_put_writes_file_in_sensitivity_folder(tmp_path: Path) -> None:
    mem = provider(tmp_path, FakeEmbedder())
    ids = await seed(mem)
    assert ids["bank"].startswith("private/")
    assert ids["beam"].startswith("public/")
    assert (tmp_path / "mem" / f"{ids['bolts']}.md").read_text().startswith("---\ntitle: Bolt")
    stats = await mem.stats()
    assert stats["notes"] == 3
    assert stats["chunks_pending_embedding"] == 0


async def test_hybrid_search_respects_cap(tmp_path: Path) -> None:
    mem = provider(tmp_path, FakeEmbedder())
    ids = await seed(mem)
    personal = await mem.search("bolt preload", k=5, max_sensitivity=PE)
    assert personal[0].id == ids["bolts"]
    assert ids["bank"] not in {i.id for i in personal}
    private = await mem.search("bolt bank account", k=5, max_sensitivity=PR)
    assert ids["bank"] in {i.id for i in private}
    public = await mem.search("bolt", k=5, max_sensitivity=P)
    assert all(i.sensitivity is P for i in public)


async def test_search_degrades_to_keyword_when_embedder_down(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    mem = provider(tmp_path, emb)
    ids = await seed(mem)
    emb.fail = True
    hits = await mem.search("cantilever deflection", k=3, max_sensitivity=PR)
    assert hits[0].id == ids["beam"]


async def test_reindex_reports_embed_failure_and_backfills_later(tmp_path: Path) -> None:
    emb = FakeEmbedder(fail=True)
    mem = provider(tmp_path, emb)
    await seed(mem)
    result = await mem.reindex()
    assert result.embed_error is not None
    assert result.pending > 0
    emb.fail = False
    result = await mem.reindex()
    assert (result.embed_error, result.pending) == (None, 0)


async def test_works_without_any_embedder(tmp_path: Path) -> None:
    mem = provider(tmp_path, None)
    ids = await seed(mem)
    assert (await mem.search("preload", max_sensitivity=PE))[0].id == ids["bolts"]


async def test_get_reads_file_and_enforces_cap(tmp_path: Path) -> None:
    mem = provider(tmp_path, FakeEmbedder())
    ids = await seed(mem)
    note = await mem.get(ids["bolts"], max_sensitivity=PE)
    assert note is not None
    assert note.title == "Bolt preload"
    assert "75% of proof load" in note.text
    assert await mem.get(ids["bank"]) is None  # default cap PERSONAL: looks like "not found"
    assert await mem.get(ids["bank"], max_sensitivity=PR) is not None
    assert await mem.get("../../etc/passwd", max_sensitivity=PR) is None
    assert await mem.get("nope", max_sensitivity=PR) is None


async def test_hand_written_notes_are_picked_up(tmp_path: Path) -> None:
    mem = provider(tmp_path, FakeEmbedder())
    (tmp_path / "mem" / "projects").mkdir(parents=True)
    (tmp_path / "mem" / "projects" / "nx.md").write_text("# NX Open\nJournal tips for NX Open.")
    await mem.reindex()
    # Unknown folder + no front matter => PRIVATE: invisible at PERSONAL.
    assert await mem.search("journal tips", max_sensitivity=PE) == []
    assert (await mem.search("journal tips", max_sensitivity=PR))[0].id == "projects/nx"


async def test_recent(tmp_path: Path) -> None:
    mem = provider(tmp_path, FakeEmbedder())
    ids = await seed(mem)
    recent_ids = [r[0] for r in await mem.recent(limit=10, max_sensitivity=PE)]
    assert set(recent_ids) == {ids["bolts"], ids["beam"]}
