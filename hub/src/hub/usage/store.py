"""Append-only SQLite log of every chat request: who answered, tokens, cost.

No prompt or completion text is stored, only metadata. One short-lived
connection per write keeps this thread-safe without a connection pool; at
personal-use volumes (well under 1 write/s) that cost is negligible.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    request_id       TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    route            TEXT    NOT NULL,
    route_reason     TEXT    NOT NULL,
    outcome          TEXT    NOT NULL,   -- ok | failed
    model_alias      TEXT,
    provider         TEXT,
    model            TEXT,
    locality         TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    usage_estimated  INTEGER,
    cost_usd         REAL,
    cost_status      TEXT,
    fell_back        INTEGER NOT NULL,
    latency_ms       INTEGER NOT NULL,
    attempts_json    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests (ts);
"""


@dataclass(frozen=True)
class UsageRecord:
    request_id: str
    source: str
    route: str
    route_reason: str
    outcome: str
    model_alias: str | None
    provider: str | None
    model: str | None
    locality: str | None
    input_tokens: int | None
    output_tokens: int | None
    usage_estimated: bool | None
    cost_usd: float | None
    cost_status: str | None
    fell_back: bool
    latency_ms: int
    attempts: list[dict[str, Any]]


class UsageStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0)

    def _insert(self, r: UsageRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO requests (ts, request_id, source, route, route_reason, outcome,
                   model_alias, provider, model, locality, input_tokens, output_tokens,
                   usage_estimated, cost_usd, cost_status, fell_back, latency_ms, attempts_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    r.request_id,
                    r.source,
                    r.route,
                    r.route_reason,
                    r.outcome,
                    r.model_alias,
                    r.provider,
                    r.model,
                    r.locality,
                    r.input_tokens,
                    r.output_tokens,
                    None if r.usage_estimated is None else int(r.usage_estimated),
                    r.cost_usd,
                    r.cost_status,
                    int(r.fell_back),
                    r.latency_ms,
                    json.dumps(r.attempts),
                ),
            )

    async def record(self, r: UsageRecord) -> None:
        await asyncio.to_thread(self._insert, r)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
