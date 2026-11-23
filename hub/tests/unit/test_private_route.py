"""The private-route guarantee (docs/adr/0003-private-route-guarantee.md).

Every cloud adapter here is a :class:`CloudTripwire` that fails the test if the
router asks it for availability or a stream. The scenarios cover every way the
local model can fail, plus attempts to sneak a cloud model in past validation.
"""

from __future__ import annotations

from typing import Any

import pytest

from hub.config import RoutesConfig, RouteSpec, load_config, parse_config
from hub.locality import CLOUD_PROVIDERS, ProviderName
from hub.paths import default_config_dir
from hub.providers.base import ProviderErrorKind
from hub.routing.router import Router, RouterCompleted, RouterEvent, RouterFailed
from hub.types import Locality, Message, ModelRequest
from tests.fakes import CloudTripwire, FakeAdapter, registry

O = ProviderName.OLLAMA
REQ = ModelRequest(messages=[Message(role="user", content="my bank PIN is 1234")])


def tripwires() -> dict[ProviderName, CloudTripwire]:
    return {p: CloudTripwire(p) for p in CLOUD_PROVIDERS}


async def run(router: Router, route: str, ceiling: Locality = Locality.CLOUD) -> list[RouterEvent]:
    return [e async for e in router.stream(route, REQ, ceiling)]


def assert_untouched(wires: dict[ProviderName, CloudTripwire]) -> None:
    for wire in wires.values():
        assert wire.touched == 0, f"{wire.provider} was contacted"


async def test_private_answers_locally(routes_dict: dict[str, Any]) -> None:
    wires = tripwires()
    router = Router(parse_config({}, routes_dict).routes, registry({**wires, O: FakeAdapter(O)}))
    events = await run(router, "private")
    assert isinstance(events[-1], RouterCompleted)
    assert events[-1].locality is Locality.LOCAL
    assert_untouched(wires)


@pytest.mark.parametrize(
    "local",
    [
        FakeAdapter(O, available=False),
        FakeAdapter(O, behavior="hang"),
        FakeAdapter(O, behavior="crash"),
        FakeAdapter(O, behavior="fail_mid"),
        *[FakeAdapter(O, behavior="error", error_kind=k) for k in ProviderErrorKind],
    ],
    ids=lambda a: f"{a.behavior}-{a.error_kind}-avail={a.available}",
)
async def test_private_never_falls_back_to_cloud(
    routes_dict: dict[str, Any], local: FakeAdapter
) -> None:
    routes_dict["models"]["local"]["timeout_s"] = 0.05
    wires = tripwires()
    router = Router(parse_config({}, routes_dict).routes, registry({**wires, O: local}))
    events = await run(router, "private")
    assert isinstance(events[-1], RouterFailed)
    assert_untouched(wires)


async def test_runtime_guard_holds_even_if_validation_is_bypassed(
    routes_dict: dict[str, Any],
) -> None:
    """Simulate a config that skipped validation (model_construct) with cloud fallbacks."""
    valid = parse_config({}, routes_dict).routes
    evil_private = RouteSpec.model_construct(
        primary="local",
        fallback=["claude", "gemini", "muse"],
        local_only=True,
        deadline_s=5.0,
        description="",
    )
    evil = RoutesConfig.model_construct(
        models=valid.models,
        routes={**valid.routes, "private": evil_private},
        classifier=valid.classifier,
    )
    wires = tripwires()
    router = Router(evil, registry({**wires, O: FakeAdapter(O, behavior="error")}))
    events = await run(router, "private")
    final = events[-1]
    assert isinstance(final, RouterFailed)
    assert [a.reason for a in final.attempts[1:]] == ["blocked: not local on local-only route"] * 3
    assert_untouched(wires)


async def test_public_ollama_endpoint_is_treated_as_cloud(routes_dict: dict[str, Any]) -> None:
    wires = tripwires()
    remote = FakeAdapter(O, api_base="http://8.8.8.8:11434")
    router = Router(parse_config({}, routes_dict).routes, registry({**wires, O: remote}))
    events = await run(router, "private")
    assert isinstance(events[-1], RouterFailed)
    assert remote.calls == []
    assert_untouched(wires)


async def test_caller_ceiling_restricts_any_route(routes_dict: dict[str, Any]) -> None:
    """A LOCAL ceiling from the caller (e.g. a future 'privacy mode') clamps cloud routes."""
    wires = tripwires()
    router = Router(parse_config({}, routes_dict).routes, registry({**wires, O: FakeAdapter(O)}))
    events = await run(router, "default", ceiling=Locality.LOCAL)
    assert isinstance(events[-1], RouterCompleted)
    assert events[-1].spec.alias == "local"
    assert_untouched(wires)


async def test_every_local_only_route_in_shipped_config() -> None:
    cfg = load_config(default_config_dir()).routes
    local_routes = [n for n, r in cfg.routes.items() if r.local_only]
    assert "private" in local_routes
    for name in local_routes:
        wires = tripwires()
        router = Router(cfg, registry({**wires, O: FakeAdapter(O, behavior="error")}))
        await run(router, name)
        assert_untouched(wires)
