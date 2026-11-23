from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from hub.memory.embedder import EmbedderError, OllamaEmbedder
from hub.memory.index import MemoryIndex, keyword_query
from hub.memory.protocol import Sensitivity
from hub.types import Locality
from tests.fakes import FakeEmbedder

P, PE, PR = Sensitivity.PUBLIC, Sensitivity.PERSONAL, Sensitivity.PRIVATE


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    root = tmp_path / "mem"
    write(root, "public/beams.md", "# Beams\nSimply supported beam deflection is 5wL^4/384EI.")
    write(root, "personal/bolts.md", "# Bolts\nPreload M10 grade 8.8 bolts to 75% of proof load.")
    write(root, "private/bank.md", "# Bank\nAccount number for the bolt supplier is 1234.")
    return root


@pytest.fixture
def index(tmp_path: Path) -> MemoryIndex:
    return MemoryIndex(tmp_path / "idx.sqlite3")


async def embed_all(index: MemoryIndex, emb: FakeEmbedder) -> None:
    pending = index.pending_chunks(1000)
    vecs = await emb.embed([t for _, t in pending])
    index.store_vectors(emb.model, [(cid, v) for (cid, _), v in zip(pending, vecs, strict=True)])


# ------------------------------------------------------------------ scanning


def test_scan_is_incremental(index: MemoryIndex, notes: Path) -> None:
    r1 = index.scan(notes, 1600)
    assert (r1.added, r1.updated, r1.removed, r1.unchanged) == (3, 0, 0, 0)
    r2 = index.scan(notes, 1600)
    assert (r2.added, r2.updated, r2.unchanged) == (0, 0, 3)
    assert not r2.changed

    p = notes / "public/beams.md"
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10_000))  # touch only
    r3 = index.scan(notes, 1600)
    assert (r3.updated, r3.unchanged) == (0, 3)

    write(notes, "public/beams.md", "# Beams\nCantilever tip deflection PL^3/3EI.")
    (notes / "private/bank.md").unlink()
    r4 = index.scan(notes, 1600)
    assert (r4.updated, r4.removed) == (1, 1)
    assert index.keyword_search("cantilever", k=5, max_sensitivity=PR)[0].note_id == "public/beams"
    assert index.keyword_search("simply supported", k=5, max_sensitivity=PR) == []
    assert index.note_meta("private/bank") is None


def test_unreadable_front_matter_is_private_even_in_public(
    index: MemoryIndex, tmp_path: Path
) -> None:
    """Broken YAML might have been a mistyped 'sensitivity: private': fail closed."""
    root = tmp_path / "m"
    write(root, "public/x.md", "---\nsensitivity: [oops\n---\nbody text here")
    report = index.scan(root, 1600)
    assert any("unreadable" in w for w in report.warnings)
    meta = index.note_meta("public/x")
    assert meta is not None
    assert meta[1] is PR


# ------------------------------------------------------------------ keyword search


def test_keyword_search_filters_sensitivity_in_sql(index: MemoryIndex, notes: Path) -> None:
    index.scan(notes, 1600)
    ids = lambda cap: {h.note_id for h in index.keyword_search("bolt", k=10, max_sensitivity=cap)}  # noqa: E731
    assert ids(PR) == {"personal/bolts", "private/bank"}
    assert ids(PE) == {"personal/bolts"}
    assert ids(P) == set()


def test_keyword_search_stems(index: MemoryIndex, notes: Path) -> None:
    index.scan(notes, 1600)
    hits = index.keyword_search("deflections of beams", k=5, max_sensitivity=P)
    assert hits[0].note_id == "public/beams"
    assert hits[0].title == "Beams"


@pytest.mark.parametrize(
    "q", ['bolt" OR text:*', "NEAR(bolt preload)", "bolt AND (", "*", '"""', "-bolt", "^bolt"]
)
def test_keyword_query_is_injection_safe(index: MemoryIndex, notes: Path, q: str) -> None:
    index.scan(notes, 1600)
    index.keyword_search(q, k=5, max_sensitivity=PR)  # must not raise


def test_keyword_query_builder() -> None:
    assert keyword_query("What is the bolt preload?") == '"bolt" OR "preload"'
    assert keyword_query("the a of") is None
    assert keyword_query('x" OR 1') is None  # 'x' too short, 'or' stopword, '1' too short


# ------------------------------------------------------------------ vectors


async def test_vector_search_with_sensitivity(index: MemoryIndex, notes: Path) -> None:
    index.scan(notes, 1600)
    emb = FakeEmbedder()
    await embed_all(index, emb)
    assert index.stats()["chunks_pending_embedding"] == 0
    [q] = await emb.embed(["bolt preload proof load"])
    hits = index.vector_search(q, model=emb.model, k=5, max_sensitivity=PE)
    assert hits[0].note_id == "personal/bolts"
    assert hits[0].cosine is not None
    assert hits[0].cosine > 0.5
    assert all(h.sensitivity <= PE for h in hits)
    assert "private/bank" not in {h.note_id for h in hits}


async def test_vector_search_ignores_other_model_or_dim(index: MemoryIndex, notes: Path) -> None:
    index.scan(notes, 1600)
    emb = FakeEmbedder()
    await embed_all(index, emb)
    [q] = await emb.embed(["bolt"])
    assert index.vector_search(q, model="other-model", k=5, max_sensitivity=PR) == []
    assert index.vector_search(q[:10], model=emb.model, k=5, max_sensitivity=PR) == []


async def test_model_change_rebuilds_vectors(index: MemoryIndex, notes: Path) -> None:
    index.scan(notes, 1600)
    await embed_all(index, FakeEmbedder(dim=64, model="a"))
    assert index.stats()["chunks_pending_embedding"] == 0
    with index.connect() as conn:
        cid, text = conn.execute("SELECT id, embed_text FROM chunks LIMIT 1").fetchone()
    new = FakeEmbedder(dim=32, model="b")
    index.store_vectors("b", [(cid, (await new.embed([text]))[0])])
    assert index.embed_model() == "b"
    # Every other chunk must now be re-embedded with the new model.
    stats = index.stats()
    assert stats["chunks_pending_embedding"] == int(stats["chunks"] or 0) - 1


async def test_updating_note_drops_stale_vectors(index: MemoryIndex, notes: Path) -> None:
    index.scan(notes, 1600)
    emb = FakeEmbedder()
    await embed_all(index, emb)
    write(notes, "personal/bolts.md", "# Welds\nFillet weld throat is 0.707 times leg.")
    index.scan(notes, 1600)
    [q] = await emb.embed(["bolt preload proof load"])
    hits = index.vector_search(q, model=emb.model, k=5, max_sensitivity=PE)
    assert all("Preload" not in h.text for h in hits)  # old chunk's vector is gone


def test_schema_version_mismatch_rebuilds(tmp_path: Path, notes: Path) -> None:
    idx = MemoryIndex(tmp_path / "i.sqlite3")
    idx.scan(notes, 1600)
    with idx.connect() as conn:
        conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    idx2 = MemoryIndex(tmp_path / "i.sqlite3")
    assert idx2.stats()["notes"] == 0  # dropped; next scan rebuilds
    assert idx2.scan(notes, 1600).added == 3


# ------------------------------------------------------------------ Ollama embedder


def _transport(
    seen: list[dict[str, object]], *, dim: int = 3, status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), **body})
        n = len(body["input"])
        return httpx.Response(status, json={"embeddings": [[0.1] * dim for _ in range(n)]})

    return httpx.MockTransport(handler)


async def test_ollama_embedder_batches_and_posts() -> None:
    seen: list[dict[str, object]] = []
    e = OllamaEmbedder(
        "http://127.0.0.1:11434/", "nomic-embed-text", batch_size=2, transport=_transport(seen)
    )
    vecs = await e.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert [s["input"] for s in seen] == [["a", "b"], ["c"]]
    assert seen[0]["url"] == "http://127.0.0.1:11434/api/embed"
    assert seen[0]["model"] == "nomic-embed-text"


async def test_ollama_embedder_errors() -> None:
    e = OllamaEmbedder("http://127.0.0.1:11434", "m", transport=_transport([], status=500))
    with pytest.raises(EmbedderError):
        await e.embed(["a"])


@pytest.mark.parametrize(
    ("base", "model"),
    [("https://ollama.com", "nomic-embed-text"), ("http://127.0.0.1:11434", "x:cloud")],
)
async def test_ollama_embedder_refuses_non_local(base: str, model: str) -> None:
    seen: list[dict[str, object]] = []
    e = OllamaEmbedder(base, model, transport=_transport(seen))
    assert e.locality() is Locality.CLOUD
    with pytest.raises(EmbedderError, match="not local"):
        await e.embed(["secret note"])
    assert seen == []  # nothing was sent
