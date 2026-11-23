"""Background tasks: initial reindex at startup, then re-index on file changes.

Uses ``watchfiles`` (Rust notify backend: inotify / FSEvents / ReadDirectoryChangesW),
already installed with uvicorn[standard]. Changes are debounced so saving a
note in an editor (often several writes) triggers one incremental reindex.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from watchfiles import Change, awatch

from hub.memory.store import LocalMemoryProvider

log = structlog.get_logger(__name__)


def _is_note_change(change: Change, path: str) -> bool:
    name = Path(path).name
    return name.endswith(".md") and not name.startswith(".")


async def initial_reindex(memory: LocalMemoryProvider) -> None:
    try:
        await memory.reindex()
    except Exception as exc:  # never take the hub down over memory
        log.error("memory_initial_reindex_failed", error=type(exc).__name__)


async def watch_memory(memory: LocalMemoryProvider, *, debounce_ms: int = 1500) -> None:
    memory.root.mkdir(parents=True, exist_ok=True)
    log.info("memory_watch_started", dir=str(memory.root))
    async for _changes in awatch(memory.root, watch_filter=_is_note_change, debounce=debounce_ms):
        try:
            await memory.reindex()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("memory_watch_reindex_failed", error=type(exc).__name__)
