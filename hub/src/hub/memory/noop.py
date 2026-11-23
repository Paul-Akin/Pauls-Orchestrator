"""Stage-1 memory: remembers nothing, so stage 2 can drop in without refactoring."""

from __future__ import annotations

from hub.memory.protocol import MemoryItem, NewMemoryItem, Sensitivity


class NoOpMemoryProvider:
    async def search(
        self, query: str, *, k: int = 5, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> list[MemoryItem]:
        return []

    async def get(
        self, item_id: str, *, max_sensitivity: Sensitivity = Sensitivity.PERSONAL
    ) -> MemoryItem | None:
        return None

    async def put(self, item: NewMemoryItem) -> str:
        raise NotImplementedError("memory is not implemented until stage 2")
