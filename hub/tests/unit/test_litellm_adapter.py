"""Adapter tests. Uses LiteLLM's own ``mock_response`` so the real chunk objects are
parsed, but no request leaves the process."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from hub.config import ModelSpec
from hub.locality import ProviderName
from hub.providers.base import ProviderError, ProviderErrorKind
from hub.providers.litellm_adapter import LiteLLMAdapter, classify_exception, to_litellm_messages
from hub.types import ImageInput, Locality, Message, ModelRequest

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def spec(provider: str = "anthropic", model: str = "claude-opus-5-5") -> ModelSpec:
    return ModelSpec.model_validate({"alias": "m", "provider": provider, "model": model})


def req(text: str = "hi") -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=text)])


async def collect(it: AsyncIterator[Any]) -> list[Any]:
    return [x async for x in it]


def test_cost_map_pinned_to_local() -> None:
    import hub  # noqa: F401

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_message_translation_with_images() -> None:
    msgs = [
        Message(role="system", content="be terse"),
        Message(
            role="user",
            content="what is this",
            images=[ImageInput(media_type="image/png", data_b64=PNG)],
        ),
    ]
    out = to_litellm_messages(msgs)
    assert out[0] == {"role": "system", "content": "be terse"}
    assert out[1]["content"][0] == {"type": "text", "text": "what is this"}
    assert out[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("provider", "prefix"),
    [
        ("anthropic", "anthropic/"),
        ("gemini", "gemini/"),
        ("meta", "meta/"),
        ("ollama", "ollama_chat/"),
    ],
)
def test_litellm_model_prefix(provider: str, prefix: str) -> None:
    a = LiteLLMAdapter(ProviderName(provider), api_key=None, api_base="http://127.0.0.1:11434")
    assert a.litellm_model(spec(provider, "x")) == prefix + "x"


async def test_streams_real_litellm_mock_chunks() -> None:
    import litellm

    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return await litellm.acompletion(**kwargs, mock_response="Hello there, engineer.")

    a = LiteLLMAdapter(
        ProviderName.ANTHROPIC,
        api_key=SecretStr("sk-test"),
        completion_fn=fake,
        token_counter=litellm.token_counter,
    )
    chunks = await collect(a.stream(spec(), req()))
    text = "".join(c.text for c in chunks)
    assert text == "Hello there, engineer."
    assert chunks[-1].usage is not None
    assert captured["model"] == "anthropic/claude-opus-5-5"
    assert captured["api_key"] == "sk-test"
    assert captured["num_retries"] == 0
    assert captured["stream"] is True


async def test_usage_estimated_when_provider_reports_none() -> None:
    class Chunk:
        def __init__(self, text: str) -> None:
            self.choices = [type("C", (), {"delta": type("D", (), {"content": text})()})()]
            self.usage = None

    async def gen() -> AsyncIterator[Chunk]:
        for t in ["a", "b"]:
            yield Chunk(t)

    async def fake(**_: Any) -> Any:
        return gen()

    a = LiteLLMAdapter(
        ProviderName.OLLAMA,
        api_key=None,
        api_base="http://127.0.0.1:11434",
        completion_fn=fake,
        token_counter=lambda **_: 7,
    )
    chunks = await collect(a.stream(spec("ollama", "llama3.2"), req()))
    usage = chunks[-1].usage
    assert usage is not None
    assert usage.estimated
    assert (usage.input_tokens, usage.output_tokens) == (7, 7)


async def test_errors_are_normalized_and_key_redacted() -> None:
    import litellm

    async def fake(**_: Any) -> Any:
        raise litellm.AuthenticationError(
            message="invalid x-api-key sk-secret-123", llm_provider="anthropic", model="m"
        )

    a = LiteLLMAdapter(
        ProviderName.ANTHROPIC,
        api_key=SecretStr("sk-secret-123"),
        completion_fn=fake,
        token_counter=lambda **_: 0,
    )
    with pytest.raises(ProviderError) as ei:
        await collect(a.stream(spec(), req()))
    assert ei.value.kind is ProviderErrorKind.AUTH
    assert "sk-secret-123" not in str(ei.value)


def test_classify_real_litellm_exceptions() -> None:
    import litellm

    kw: dict[str, Any] = {"message": "x", "llm_provider": "p", "model": "m"}
    assert classify_exception(litellm.AuthenticationError(**kw)) is ProviderErrorKind.AUTH
    assert classify_exception(litellm.RateLimitError(**kw)) is ProviderErrorKind.RATE_LIMIT
    assert classify_exception(litellm.Timeout(**kw)) is ProviderErrorKind.TIMEOUT
    assert classify_exception(litellm.APIConnectionError(**kw)) is ProviderErrorKind.UNAVAILABLE
    assert (
        classify_exception(litellm.ServiceUnavailableError(**kw)) is ProviderErrorKind.UNAVAILABLE
    )
    assert classify_exception(RuntimeError("?")) is ProviderErrorKind.OTHER


async def test_cloud_availability_needs_key() -> None:
    no_key = LiteLLMAdapter(ProviderName.GEMINI, api_key=None)
    assert not (await no_key.availability()).available
    empty = LiteLLMAdapter(ProviderName.GEMINI, api_key=SecretStr(""))
    assert not (await empty.availability()).available
    ok = LiteLLMAdapter(ProviderName.GEMINI, api_key=SecretStr("k"))
    assert (await ok.availability()).available


async def test_ollama_unreachable_is_unavailable() -> None:
    a = LiteLLMAdapter(ProviderName.OLLAMA, api_key=None, api_base="http://127.0.0.1:9")
    assert not (await a.availability()).available


def test_ollama_locality_depends_on_endpoint() -> None:
    local = LiteLLMAdapter(ProviderName.OLLAMA, api_key=None, api_base="http://127.0.0.1:11434")
    remote = LiteLLMAdapter(ProviderName.OLLAMA, api_key=None, api_base="http://8.8.8.8:11434")
    assert local.locality_for(spec("ollama", "llama3.2")) is Locality.LOCAL
    assert remote.locality_for(spec("ollama", "llama3.2")) is Locality.CLOUD
    assert local.locality_for(spec("ollama", "gpt-oss:120b-cloud")) is Locality.CLOUD
