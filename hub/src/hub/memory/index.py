"""SQLite search index over the markdown notes: FTS5 (BM25) + sqlite-vec (cosine).

The index is **derived data**: delete the file and ``reindex`` rebuilds it from
the notes. All methods are synchronous and meant to be called through
``asyncio.to_thread``; each call opens its own connection (WAL mode), so reads
run concurrently with a write.

Sensitivity is filtered **inside the SQL** of both searches (a JOIN for FTS5,
a vec0 metadata column for KNN), so items above the caller's ceiling are never
loaded into Python at all.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sqlite_vec

from hub.memory.notes import chunk_note, embedding_text, iter_note_paths, read_note
from hub.memory.protocol import Sensitivity

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    title       TEXT NOT NULL,
    sensitivity INTEGER NOT NULL,
    tags        TEXT NOT NULL,      -- JSON array
    created     TEXT NOT NULL,      -- ISO-8601
    source      TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    size        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    note_id     TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    heading     TEXT NOT NULL,
    text        TEXT NOT NULL,
    embed_text  TEXT NOT NULL,
    sensitivity INTEGER NOT NULL,
    embedded    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_note ON chunks(note_id);
CREATE INDEX IF NOT EXISTS chunks_pending ON chunks(embedded);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(title, heading, text, tokenize='porter unicode61');
"""

# Too common to help ranking; dropped from keyword queries.
_STOPWORD_TEXT = (
    "a an and are as at be but by can do does for from how i if in is it its me my of on or "
    "so than that the their them then there these they this to was we what when where which "
    "who why will with you your"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    note_id: str
    title: str
    heading: str
    text: str
    sensitivity: Sensitivity
    created: datetime
    tags: tuple[str, ...]
    source: str
    cosine: float | None = None
    """Cosine similarity (vector hits only)."""


@dataclass
class ScanReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def keyword_query(text: str, max_terms: int = 32) -> str | None:
    """User text -> safe FTS5 query: quoted terms OR-ed together (no FTS syntax injection)."""
    seen: list[str] = []
    for tok in re.findall(r"\w+", text.lower()):
        if len(tok) < 2 or tok in _STOPWORDS or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in seen) or None


class MemoryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            version = self._meta(conn, "schema_version")
            if version not in (None, SCHEMA_VERSION):
                self._drop_all(conn)  # derived data: rebuild rather than migrate
            conn.executescript(_SCHEMA)
            self._set_meta(conn, "schema_version", SCHEMA_VERSION)

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10.0)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _drop_all(conn: sqlite3.Connection) -> None:
        for name in ("chunks_vec", "chunks_fts", "chunks", "notes", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {name}")

    @staticmethod
    def _has_vec(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        return row is not None

    @staticmethod
    def _delete_chunks(conn: sqlite3.Connection, note_id: str, has_vec: bool) -> None:
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE note_id=?", (note_id,))]
        for cid in ids:
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
            if has_vec:
                conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (cid,))
        conn.execute("DELETE FROM chunks WHERE note_id=?", (note_id,))

    # ------------------------------------------------------------------ scan

    def scan(self, root: Path, chunk_max_chars: int) -> ScanReport:
        """Bring the index in line with the files under ``root`` (incremental)."""
        report = ScanReport()
        with self.connect() as conn:
            has_vec = self._has_vec(conn)
            known = {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute("SELECT id, mtime_ns, size, sha256 FROM notes")
            }
            seen: set[str] = set()
            for path in iter_note_paths(root):
                try:
                    st = path.stat()
                    rel_id = path.relative_to(root).with_suffix("").as_posix()
                    seen.add(rel_id)
                    prev = known.get(rel_id)
                    if prev and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
                        report.unchanged += 1
                        continue
                    note = read_note(root, path)
                except (OSError, ValueError) as exc:
                    report.warnings.append(f"{path.name}: {type(exc).__name__}: {exc}")
                    continue
                report.warnings += [f"{note.id}: {w}" for w in note.warnings]
                if prev and prev[2] == note.sha256:  # touched, not changed
                    conn.execute(
                        "UPDATE notes SET mtime_ns=?, size=? WHERE id=?",
                        (st.st_mtime_ns, st.st_size, note.id),
                    )
                    report.unchanged += 1
                    continue
                self._delete_chunks(conn, note.id, has_vec)
                conn.execute(
                    """INSERT OR REPLACE INTO notes
                       (id, path, title, sensitivity, tags, created, source, sha256, mtime_ns, size)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        note.id,
                        str(path),
                        note.title,
                        int(note.sensitivity),
                        json.dumps(list(note.tags)),
                        note.created.isoformat(),
                        note.source,
                        note.sha256,
                        st.st_mtime_ns,
                        st.st_size,
                    ),
                )
                for chunk in chunk_note(note, chunk_max_chars):
                    cur = conn.execute(
                        """INSERT INTO chunks (note_id, ordinal, heading, text, embed_text,
                           sensitivity) VALUES (?,?,?,?,?,?)""",
                        (
                            note.id,
                            chunk.ordinal,
                            chunk.heading,
                            chunk.text,
                            embedding_text(note, chunk),
                            int(note.sensitivity),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid, title, heading, text) VALUES (?,?,?,?)",
                        (cur.lastrowid, note.title, chunk.heading, chunk.text),
                    )
                if prev:
                    report.updated += 1
                else:
                    report.added += 1
            for gone in set(known) - seen:
                self._delete_chunks(conn, gone, has_vec)
                conn.execute("DELETE FROM notes WHERE id=?", (gone,))
                report.removed += 1
        return report

    # ------------------------------------------------------------------ vectors

    def pending_chunks(self, limit: int) -> list[tuple[int, str]]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT id, embed_text FROM chunks WHERE embedded=0 ORDER BY id LIMIT ?",
                    (limit,),
                )
            )

    def store_vectors(self, model: str, items: list[tuple[int, list[float]]]) -> None:
        """Write vectors. A change of model or dimension rebuilds the vector table."""
        if not items:
            return
        dim = len(items[0][1])
        with self.connect() as conn:
            if self._meta(conn, "embed_model") != model or self._meta(conn, "embed_dim") != str(
                dim
            ):
                conn.execute("DROP TABLE IF EXISTS chunks_vec")
                conn.execute("UPDATE chunks SET embedded=0")
                conn.execute(
                    f"CREATE VIRTUAL TABLE chunks_vec USING vec0("
                    f"embedding float[{dim}] distance_metric=cosine, sensitivity integer)"
                )
                self._set_meta(conn, "embed_model", model)
                self._set_meta(conn, "embed_dim", str(dim))
            for cid, vec in items:
                if len(vec) != dim:
                    raise ValueError("mixed embedding dimensions in one batch")
                row = conn.execute("SELECT sensitivity FROM chunks WHERE id=?", (cid,)).fetchone()
                if row is None:  # chunk deleted while we were embedding
                    continue
                conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (cid,))
                conn.execute(
                    "INSERT INTO chunks_vec(rowid, embedding, sensitivity) VALUES (?,?,?)",
                    (cid, sqlite_vec.serialize_float32(vec), row[0]),
                )
                conn.execute("UPDATE chunks SET embedded=1 WHERE id=?", (cid,))

    def embed_model(self) -> str | None:
        with self.connect() as conn:
            return self._meta(conn, "embed_model")

    # ------------------------------------------------------------------ search

    @staticmethod
    def _hit(row: tuple[object, ...], cosine: float | None = None) -> Hit:
        return Hit(
            chunk_id=int(row[0]),  # type: ignore[call-overload]
            note_id=str(row[1]),
            title=str(row[2]),
            heading=str(row[3]),
            text=str(row[4]),
            sensitivity=Sensitivity(int(row[5])),  # type: ignore[call-overload]
            created=datetime.fromisoformat(str(row[6])),
            tags=tuple(json.loads(str(row[7]))),
            source=str(row[8]),
            cosine=cosine,
        )

    def keyword_search(self, query: str, *, k: int, max_sensitivity: Sensitivity) -> list[Hit]:
        fts = keyword_query(query)
        if fts is None:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.id, c.note_id, n.title, c.heading, c.text, c.sensitivity,
                           n.created, n.tags, n.source
                    FROM chunks_fts f
                    JOIN chunks c ON c.id = f.rowid
                    JOIN notes n ON n.id = c.note_id
                    WHERE chunks_fts MATCH ? AND c.sensitivity <= ?
                    ORDER BY bm25(chunks_fts)
                    LIMIT ?""",
                (fts, int(max_sensitivity), k),
            ).fetchall()
        return [self._hit(r) for r in rows]

    def vector_search(
        self, vector: list[float], *, model: str, k: int, max_sensitivity: Sensitivity
    ) -> list[Hit]:
        with self.connect() as conn:
            if not self._has_vec(conn) or self._meta(conn, "embed_model") != model:
                return []
            if self._meta(conn, "embed_dim") != str(len(vector)):
                return []
            knn = conn.execute(
                """SELECT rowid, distance FROM chunks_vec
                   WHERE embedding MATCH ? AND k = ? AND sensitivity <= ?
                   ORDER BY distance""",
                (sqlite_vec.serialize_float32(vector), k, int(max_sensitivity)),
            ).fetchall()
            hits: list[Hit] = []
            for rowid, distance in knn:
                row = conn.execute(
                    """SELECT c.id, c.note_id, n.title, c.heading, c.text, c.sensitivity,
                               n.created, n.tags, n.source
                        FROM chunks c JOIN notes n ON n.id=c.note_id
                        WHERE c.id=? AND c.sensitivity <= ?""",
                    (rowid, int(max_sensitivity)),
                ).fetchone()
                if row is not None:
                    hits.append(self._hit(row, cosine=1.0 - float(distance)))
        return hits

    # ------------------------------------------------------------------ notes

    def note_meta(self, note_id: str) -> tuple[str, Sensitivity] | None:
        """(path, sensitivity) of a note, or None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT path, sensitivity FROM notes WHERE id=?", (note_id,)
            ).fetchone()
        return (str(row[0]), Sensitivity(int(row[1]))) if row else None

    def recent(self, *, limit: int, max_sensitivity: Sensitivity) -> list[tuple[str, str, str]]:
        """(id, title, created) of the newest notes within the sensitivity cap."""
        with self.connect() as conn:
            return list(
                conn.execute(
                    """SELECT id, title, created FROM notes WHERE sensitivity <= ?
                       ORDER BY created DESC LIMIT ?""",
                    (int(max_sensitivity), limit),
                )
            )

    def stats(self) -> dict[str, int | str | None]:
        with self.connect() as conn:
            notes = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
            chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            pending = conn.execute("SELECT count(*) FROM chunks WHERE embedded=0").fetchone()[0]
            return {
                "notes": int(notes),
                "chunks": int(chunks),
                "chunks_pending_embedding": int(pending),
                "embed_model": self._meta(conn, "embed_model"),
            }
