"""LocalMemoryProvider: markdown notes + SQLite index + local embedder.

Write path:  put() -> markdown file (atomic) -> incremental reindex -> embed.
Read path:   search() -> keyword (FTS5) + vector (sqlite-vec) -> RRF fusion.
             get()    -> reads the FILE (the source of truth), not the index.

Degradation: if the embedder is missing or fails, search is keyword-only and
unembedded chunks are retried on the next reindex. Memory never breaks a chat.

Logging: query text is user prompt content, so it is never logged; only counts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from hub.memory.embedder import Embedder, EmbedderError
from hub.memory.index import MemoryIndex, ScanReport
from hub.memory.notes import (
    atomic_write,
    new_note_path,
    path_for_id,
    read_note,
    render_note,
)
from hub.memory.protocol import MemoryItem, NewMemoryItem, Sensitivity
from hub.memory.retrieval import RetrievalParams, Scored, fuse

log = structlog.get_logger(__name__)

EMBED_BATCH = 64


@dataclass
class ReindexResult:
    scan: ScanReport
    embedded: int = 0
    embed_error: str | None = None
    pending: int = 0
    warnings: list[str] = field(default_factory=list)


class LocalMemoryProvider:
    def __init__(
        self,
        root: Path,
        index: MemoryIndex,
        embedder: Embedder | None,
        *,
        params: RetrievalParams | None = None,
        candidates: int = 20,
        chunk_max_chars: int = 1600,
    ) -> None:
        self.root = root.expanduser()
        self.index = index
        self.embedder = embedder
        self.params = params or RetrievalParams()
        self.candidates = candidates
        self.chunk_max_chars = chunk_max_chars
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ indexing

    async def reindex(self) -> ReindexResult:
        async with self._write_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            scan = await asyncio.to_thread(self.index.scan, self.root, self.chunk_max_chars)
            result = ReindexResult(scan=scan, warnings=list(scan.warnings))
            result.embedded, result.embed_error = await self._embed_pending()
            stats = await asyncio.to_thread(self.index.stats)
            result.pending = int(stats["chunks_pending_embedding"] or 0)
        log.info(
            "memory_reindexed",
            added=scan.added,
            updated=scan.updated,
            removed=scan.removed,
            embedded=result.embedded,
            pending=result.pending,
            embed_error=result.embed_error,
            warnings=len(result.warnings),
        )
        return result

    async def _embed_pending(self) -> tuple[int, str | None]:
        if self.embedder is None:
            return 0, "no embedder configured"
        done = 0
        while True:
            pending = await asyncio.to_thread(self.index.pending_chunks, EMBED_BATCH)
            if not pending:
                return done, None
            try:
                vectors = await self.embedder.embed([text for _, text in pending])
            except EmbedderError as exc:
                log.warning("memory_embed_failed", error=str(exc), pending=len(pending))
                return done, str(exc)
            items = [(cid, vec) for (cid, _), vec in zip(pending, vectors, strict=True)]
            await asyncio.to_thread(self.index.store_vectors, self.embedder.model, items)
            done += len(items)

    # ------------------------------------------------------------------ search

    async def search_scored(
        self, query: str, *, k: int, max_sensitivity: Sensitivity
    ) -> list[Scored]:
        keyword = await asyncio.to_thread(
            self.index.keyword_search, query, k=self.candidates, max_sensitivity=max_sensitivity
        )
        vector = []
        if self.embedder is not None:
            try:
                [qvec] = await self.embedder.embed([query])
            except EmbedderError as exc:
                log.warning("memory_query_embed_failed", error=str(exc))
            else:
                vector = await asyncio.to_thread(
                    self.index.vector_search,
                    qvec,
                    model=self.embedder.model,
                    k=self.candidates,
                    max_sensitivity=max_sensitivity,
                )
        fused = fuse(keyword, vector, self.params, datetime.now(UTC))
        # Defence in depth: SQL already filtered; never let a violation through.
        safe = [s for s in fused if s.hit.sensitivity <= max_sensitivity]
        if len(safe) != len(fused):
            log.error("memory_sensitivity_violation_blocked", dropped=len(fused) - len(safe))
        return safe[:k]

    async def search(
        self, query: str, *, k: int = 5, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> list[MemoryItem]:
        return [
            _item_from_scored(s)
            for s in await self.search_scored(query, k=k, max_sensitivity=max_sensitivity)
        ]

    # ------------------------------------------------------------------ notes

    async def get(
        self, item_id: str, *, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> MemoryItem | None:
        """Read a whole note from disk. Above the cap it is reported as not found."""
        path = path_for_id(self.root, item_id)
        if path is None or not path.is_file():
            return None
        try:
            note = await asyncio.to_thread(read_note, self.root, path)
        except (OSError, ValueError):
            return None
        if note.sensitivity > max_sensitivity:
            return None
        return MemoryItem(
            id=note.id,
            text=note.body,
            source=note.source,
            created_at=note.created,
            sensitivity=note.sensitivity,
            tags=note.tags,
            title=note.title,
        )

    async def put(self, item: NewMemoryItem) -> str:
        created = datetime.now(UTC)
        title = (item.title or _title_from_text(item.text)).strip()
        content = render_note(
            title=title,
            body=item.text,
            sensitivity=item.sensitivity,
            tags=item.tags,
            created=created,
            source=item.source,
        )
        async with self._write_lock:
            path = new_note_path(self.root, title, created, item.sensitivity)
            await asyncio.to_thread(atomic_write, path, content)
        await self.reindex()
        note_id = path.relative_to(self.root).with_suffix("").as_posix()
        log.info("memory_note_created", note_id=note_id, sensitivity=item.sensitivity.label)
        return note_id

    async def recent(
        self, *, limit: int = 10, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> list[tuple[str, str, str]]:
        return await asyncio.to_thread(
            self.index.recent, limit=limit, max_sensitivity=max_sensitivity
        )

    async def stats(self) -> dict[str, int | str | None]:
        stats = await asyncio.to_thread(self.index.stats)
        return {**stats, "dir": str(self.root)}


def _title_from_text(text: str, max_len: int = 60) -> str:
    first = next((ln.strip(" #\t") for ln in text.splitlines() if ln.strip()), "note")
    return first if len(first) <= max_len else first[: max_len - 1].rstrip() + "…"


def _item_from_scored(s: Scored) -> MemoryItem:
    h = s.hit
    return MemoryItem(
        id=h.note_id,
        text=h.text,
        source=h.source,
        created_at=h.created,
        sensitivity=h.sensitivity,
        tags=h.tags,
        score=s.score,
        title=h.title,
        heading=h.heading,
    )
