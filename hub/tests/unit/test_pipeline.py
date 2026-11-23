from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hub.config import Price
from hub.context.builder import RequestContext, max_sensitivity_for
from hub.locality import ProviderName
from hub.memory import NoOpMemoryProvider, Sensitivity
from hub.pipeline import ChatFailedError, ChatService
from hub.routing.router import UnknownRouteError
from hub.types import (
    ChatRequest,
    CostStatus,
    DeltaEvent,
    DoneEvent,
    Locality,
    Message,
    ModelRequest,
    RouteChosenEvent,
    Usage,
)
from hub.usage.pricing import compute_cost
from hub.usage.store import UsageStore
from tests.fakes import FakeAdapter, chat_service


def ask(text: str, route: str = "auto") -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content=text)], route=route)


def svc(routes_dict: dict[str, Any], **kw: Any) -> ChatService:
    s = chat_service(routes_dict, **kw)
    assert isinstance(s, ChatService)
    return s


async def test_auto_route_answer_with_metadata(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite3")
    resp = await svc(routes_dict, usage_store=store).complete(ask("beam buckling check"))
    meta = resp.metadata
    assert resp.content == "answer from claude-opus-5-5"
    assert meta.route == "engineering_reasoning"
    assert meta.route_reason.startswith("auto:")
    assert meta.model_alias == "claude"
    assert meta.usage == Usage(input_tokens=10, output_tokens=3)
    assert meta.cost.status is CostStatus.PRICE_NOT_CONFIGURED
    assert meta.cost.usd is None
    assert not meta.fell_back

    [row] = store.recent()
    assert row["request_id"] == meta.request_id
    assert row["model_alias"] == "claude"
    assert row["cost_status"] == "price not configured"
    assert row["outcome"] == "ok"


async def test_fallback_visible_in_metadata_and_db(
    routes_dict: dict[str, Any], tmp_path: Path
) -> None:
    store = UsageStore(tmp_path / "u.sqlite3")
    bad = FakeAdapter(ProviderName.ANTHROPIC, behavior="error")
    s = svc(routes_dict, adapters={ProviderName.ANTHROPIC: bad}, usage_store=store)
    meta = (await s.complete(ask("hello", route="default"))).metadata
    assert meta.fell_back
    assert meta.model_alias == "gemini"
    assert [a.model_alias for a in meta.attempts] == ["claude", "gemini"]
    assert store.recent()[0]["fell_back"] == 1


async def test_local_model_has_configured_zero_cost(routes_dict: dict[str, Any]) -> None:
    meta = (await svc(routes_dict).complete(ask("x", route="private"))).metadata
    assert meta.cost.status is CostStatus.OK
    assert meta.cost.usd == 0
    assert meta.locality is Locality.LOCAL


async def test_failure_raises_and_is_recorded(routes_dict: dict[str, Any], tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "u.sqlite3")
    bad = FakeAdapter(ProviderName.OLLAMA, behavior="error")
    s = svc(routes_dict, adapters={ProviderName.OLLAMA: bad}, usage_store=store)
    with pytest.raises(ChatFailedError):
        await s.complete(ask("x", route="private"))
    assert store.recent()[0]["outcome"] == "failed"


async def test_unknown_route_rejected_before_stream(routes_dict: dict[str, Any]) -> None:
    with pytest.raises(UnknownRouteError):
        svc(routes_dict).resolve_route(ask("x", route="nope"))


async def test_stream_event_order(routes_dict: dict[str, Any]) -> None:
    events = [e async for e in svc(routes_dict).stream(ask("hi", route="default"))]
    assert isinstance(events[0], RouteChosenEvent)
    assert [e.type for e in events] == ["route", "attempt", "delta", "delta", "delta", "done"]
    assert isinstance(events[-1], DoneEvent)
    assert (
        "".join(e.text for e in events if isinstance(e, DeltaEvent))
        == "answer from claude-opus-5-5"
    )


async def test_context_builder_sees_route_ceiling(routes_dict: dict[str, Any]) -> None:
    seen: list[RequestContext] = []

    class Spy:
        async def build(self, req: ModelRequest, ctx: RequestContext) -> ModelRequest:
            seen.append(ctx)
            return req

    s = svc(routes_dict, context_builder=Spy())
    await s.complete(ask("x", route="private"))
    await s.complete(ask("x", route="default"))
    assert [c.locality_ceiling for c in seen] == [Locality.LOCAL, Locality.CLOUD]
    assert max_sensitivity_for(seen[0].locality_ceiling) is Sensitivity.PRIVATE
    assert max_sensitivity_for(seen[1].locality_ceiling) is Sensitivity.PERSONAL


async def test_context_builder_can_inject_messages(routes_dict: dict[str, Any]) -> None:
    captured: list[ModelRequest] = []

    class Injector:
        async def build(self, req: ModelRequest, ctx: RequestContext) -> ModelRequest:
            out = req.model_copy(
                update={"messages": [Message(role="system", content="memory"), *req.messages]}
            )
            captured.append(out)
            return out

    await svc(routes_dict, context_builder=Injector()).complete(ask("x", route="default"))
    assert captured[0].messages[0].content == "memory"


async def test_usage_store_failure_does_not_break_chat(routes_dict: dict[str, Any]) -> None:
    class Broken:
        async def record(self, _: object) -> None:
            raise OSError("disk full")

    resp = await svc(routes_dict, usage_store=Broken()).complete(ask("x", route="default"))
    assert resp.content


async def test_noop_memory() -> None:
    mem = NoOpMemoryProvider()
    assert await mem.search("x") == []
    assert await mem.get("x") is None


@pytest.mark.parametrize(
    ("price", "usage", "status", "usd"),
    [
        (
            Price(input_per_mtok=3, output_per_mtok=15),
            Usage(input_tokens=1000, output_tokens=500),
            CostStatus.OK,
            0.0105,
        ),
        (Price(), Usage(input_tokens=1, output_tokens=1), CostStatus.PRICE_NOT_CONFIGURED, None),
        (
            Price(input_per_mtok=3),
            Usage(input_tokens=1, output_tokens=1),
            CostStatus.PRICE_NOT_CONFIGURED,
            None,
        ),
        (Price(input_per_mtok=3, output_per_mtok=15), None, CostStatus.USAGE_UNAVAILABLE, None),
    ],
)
def test_compute_cost(
    price: Price, usage: Usage | None, status: CostStatus, usd: float | None
) -> None:
    cost = compute_cost(price, usage)
    assert cost.status is status
    assert cost.usd == (pytest.approx(usd) if usd is not None else None)
