from __future__ import annotations

import base64
from typing import Any

import pytest

from hub.config import parse_config
from hub.locality import ProviderName
from hub.providers.base import ProviderErrorKind
from hub.routing.router import (
    Router,
    RouterAttempt,
    RouterCompleted,
    RouterDelta,
    RouterEvent,
    RouterFailed,
    UnknownRouteError,
)
from hub.types import AttemptOutcome, ImageInput, Message, ModelRequest
from tests.fakes import FakeAdapter, registry

A, G, M, O = ProviderName.ANTHROPIC, ProviderName.GEMINI, ProviderName.META, ProviderName.OLLAMA


def req(images: bool = False) -> ModelRequest:
    imgs = [ImageInput(media_type="image/png", data_b64=base64.b64encode(b"x").decode())]
    return ModelRequest(messages=[Message(role="user", content="q", images=imgs if images else [])])


async def run(router: Router, route: str, request: ModelRequest | None = None) -> list[RouterEvent]:
    return [e async for e in router.stream(route, request or req())]


def text_of(events: list[RouterEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, RouterDelta))


def final(events: list[RouterEvent]) -> RouterCompleted | RouterFailed:
    last = events[-1]
    assert isinstance(last, RouterCompleted | RouterFailed)
    return last


def make(routes_dict: dict[str, Any], **adapters: FakeAdapter) -> Router:
    cfg = parse_config({}, routes_dict).routes
    mapping = {ProviderName(k): v for k, v in adapters.items()}
    return Router(cfg, registry(mapping))


async def test_primary_answers(routes_dict: dict[str, Any]) -> None:
    events = await run(make(routes_dict), "default")
    done = final(events)
    assert isinstance(done, RouterCompleted)
    assert done.spec.alias == "claude"
    assert text_of(events) == "answer from claude-opus-5-5"
    assert [a.outcome for a in done.attempts] == [AttemptOutcome.OK]
    assert done.usage is not None and done.usage.input_tokens == 10


@pytest.mark.parametrize("kind", list(ProviderErrorKind))
async def test_falls_back_on_any_provider_error(
    routes_dict: dict[str, Any], kind: ProviderErrorKind
) -> None:
    bad = FakeAdapter(A, behavior="error", error_kind=kind)
    events = await run(make(routes_dict, anthropic=bad), "default")
    done = final(events)
    assert isinstance(done, RouterCompleted)
    assert done.spec.alias == "gemini"
    assert done.attempts[0].outcome in {AttemptOutcome.ERROR, AttemptOutcome.TIMEOUT}
    assert done.attempts[0].reason is not None and kind.value in done.attempts[0].reason
    assert [e.spec.alias for e in events if isinstance(e, RouterAttempt)] == ["claude", "gemini"]


async def test_falls_back_on_timeout(routes_dict: dict[str, Any]) -> None:
    routes_dict["models"]["claude"]["timeout_s"] = 0.05
    done = final(await run(make(routes_dict, anthropic=FakeAdapter(A, behavior="hang")), "default"))
    assert isinstance(done, RouterCompleted)
    assert done.spec.alias == "gemini"
    assert done.attempts[0].outcome is AttemptOutcome.TIMEOUT


async def test_falls_back_on_adapter_bug(routes_dict: dict[str, Any]) -> None:
    done = final(
        await run(make(routes_dict, anthropic=FakeAdapter(A, behavior="crash")), "default")
    )
    assert isinstance(done, RouterCompleted) and done.spec.alias == "gemini"
    assert done.attempts[0].reason == "unexpected RuntimeError"


async def test_unavailable_is_skipped_without_call(routes_dict: dict[str, Any]) -> None:
    claude = FakeAdapter(A, available=False)
    done = final(await run(make(routes_dict, anthropic=claude), "default"))
    assert isinstance(done, RouterCompleted) and done.spec.alias == "gemini"
    assert claude.calls == []
    assert done.attempts[0].outcome is AttemptOutcome.SKIPPED


async def test_chain_walks_to_last(routes_dict: dict[str, Any]) -> None:
    router = make(
        routes_dict,
        anthropic=FakeAdapter(A, behavior="error"),
        gemini=FakeAdapter(G, behavior="error"),
    )
    done = final(await run(router, "default"))
    assert isinstance(done, RouterCompleted) and done.spec.alias == "local"
    assert len(done.attempts) == 3


async def test_all_fail(routes_dict: dict[str, Any]) -> None:
    router = make(
        routes_dict,
        anthropic=FakeAdapter(A, behavior="error"),
        gemini=FakeAdapter(G, behavior="error"),
        ollama=FakeAdapter(O, behavior="error"),
    )
    done = final(await run(router, "default"))
    assert isinstance(done, RouterFailed) and not done.partial
    assert len(done.attempts) == 3


async def test_no_fallback_after_text_streamed(routes_dict: dict[str, Any]) -> None:
    gemini = FakeAdapter(G)
    events = await run(
        make(routes_dict, anthropic=FakeAdapter(A, behavior="fail_mid"), gemini=gemini), "default"
    )
    done = final(events)
    assert isinstance(done, RouterFailed) and done.partial
    assert text_of(events) == "answer"
    assert gemini.calls == []


async def test_empty_answer_is_not_a_failure(routes_dict: dict[str, Any]) -> None:
    done = final(
        await run(make(routes_dict, anthropic=FakeAdapter(A, behavior="silent")), "default")
    )
    assert isinstance(done, RouterCompleted) and done.spec.alias == "claude"


async def test_images_skip_non_vision_models(routes_dict: dict[str, Any]) -> None:
    router = make(
        routes_dict,
        anthropic=FakeAdapter(A, behavior="error"),
        gemini=FakeAdapter(G, behavior="error"),
    )
    done = final(await run(router, "default", req(images=True)))
    assert isinstance(done, RouterFailed)
    assert done.attempts[-1].reason == "model lacks vision"


async def test_deadline_bounds_fallback_search(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["default"]["deadline_s"] = 0.1
    for m in ("claude", "gemini"):
        routes_dict["models"][m]["timeout_s"] = 5
    router = make(
        routes_dict,
        anthropic=FakeAdapter(A, behavior="hang"),
        gemini=FakeAdapter(G, behavior="hang"),
    )
    done = final(await run(router, "default"))
    assert isinstance(done, RouterFailed)
    reasons = [a.reason for a in done.attempts]
    assert reasons[0] == "timed out"
    assert "route deadline exceeded" in reasons


async def test_unknown_route(routes_dict: dict[str, Any]) -> None:
    with pytest.raises(UnknownRouteError):
        await run(make(routes_dict), "nope")
