"""LiteLLM-backed adapter used for every provider in stage 1.

Provider strings (verified against LiteLLM 1.110.2 source):

=========  ===============  =======================================================
provider   LiteLLM prefix   notes
=========  ===============  =======================================================
anthropic  ``anthropic/``
gemini     ``gemini/``      Google AI Studio (not Vertex)
meta       ``meta/``        Meta Model API (Muse); OpenAI-compatible, api.meta.ai/v1
ollama     ``ollama_chat/`` /api/chat endpoint (roles + images), not /api/generate
=========  ===============  =======================================================

Keys are always passed explicitly as ``api_key=``; LiteLLM never reads them
from the environment for us.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr

from hub.config import ModelSpec
from hub.locality import ProviderName, model_locality
from hub.providers.base import Availability, ModelChunk, ProviderError, ProviderErrorKind
from hub.types import Locality, Message, ModelRequest, Usage

_PREFIX: dict[ProviderName, str] = {
    ProviderName.ANTHROPIC: "anthropic/",
    ProviderName.GEMINI: "gemini/",
    ProviderName.META: "meta/",
    ProviderName.OLLAMA: "ollama_chat/",
}

CompletionFn = Callable[..., Awaitable[Any]]


def _load_litellm() -> Any:
    """Import LiteLLM lazily and make it quiet. Import takes ~1-2 s."""
    import litellm

    litellm.suppress_debug_info = True
    litellm.drop_params = True  # drop params a given model does not accept
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return litellm


def to_litellm_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert to OpenAI-style messages; LiteLLM translates per provider."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if not m.images:
            out.append({"role": m.role, "content": m.content})
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
        parts += [{"type": "image_url", "image_url": {"url": img.data_url()}} for img in m.images]
        out.append({"role": m.role, "content": parts})
    return out


def classify_exception(exc: BaseException) -> ProviderErrorKind:
    """Map LiteLLM's exception hierarchy (OpenAI-style class names) to our kinds."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & {"AuthenticationError", "PermissionDeniedError"}:
        return ProviderErrorKind.AUTH
    if "RateLimitError" in names:
        return ProviderErrorKind.RATE_LIMIT
    if names & {"Timeout", "APITimeoutError", "TimeoutError", "TimeoutException"}:
        return ProviderErrorKind.TIMEOUT
    if names & {
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
        "ConnectError",
        "NotFoundError",
    }:
        return ProviderErrorKind.UNAVAILABLE
    if names & {"BadRequestError", "UnprocessableEntityError", "ContextWindowExceededError"}:
        return ProviderErrorKind.BAD_REQUEST
    return ProviderErrorKind.OTHER


def _redact(text: str, secret: SecretStr | None) -> str:
    if secret is not None:
        value = secret.get_secret_value()
        if value:
            text = text.replace(value, "***")
    return text[:300]


def _usage_from(chunk: Any) -> Usage | None:
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt is None or completion is None:
        return None
    return Usage(input_tokens=int(prompt), output_tokens=int(completion))


def _delta_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    content = getattr(delta, "content", None) if delta is not None else None
    return content if isinstance(content, str) else ""


class LiteLLMAdapter:
    def __init__(
        self,
        name: ProviderName,
        *,
        api_key: SecretStr | None,
        api_base: str | None = None,
        completion_fn: CompletionFn | None = None,
        token_counter: Callable[..., int] | None = None,
    ) -> None:
        self._name = name
        self._api_key = api_key
        self._api_base = api_base
        self._completion_fn = completion_fn
        self._token_counter = token_counter

    @property
    def name(self) -> ProviderName:
        return self._name

    def litellm_model(self, spec: ModelSpec) -> str:
        return _PREFIX[self._name] + spec.model

    def locality_for(self, spec: ModelSpec) -> Locality:
        api_base = self._api_base if self._name is ProviderName.OLLAMA else None
        return model_locality(self._name, spec.model, api_base)

    async def availability(self) -> Availability:
        if self._name is ProviderName.OLLAMA:
            return await self._probe_ollama()
        if self._api_key is None or not self._api_key.get_secret_value():
            return Availability(False, "no API key configured")
        # Deliberately no network probe: it would cost money and leak a request.
        return Availability(True, "API key present (not verified)")

    async def _probe_ollama(self) -> Availability:
        assert self._api_base is not None
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{self._api_base.rstrip('/')}/api/tags")
                r.raise_for_status()
        except httpx.HTTPError as exc:
            return Availability(
                False, f"Ollama not reachable at {self._api_base}: {type(exc).__name__}"
            )
        models = [m.get("name", "") for m in r.json().get("models", [])]
        return Availability(True, f"Ollama reachable; {len(models)} model(s) pulled")

    def _fns(self) -> tuple[CompletionFn, Callable[..., int]]:
        if self._completion_fn is not None and self._token_counter is not None:
            return self._completion_fn, self._token_counter
        litellm = _load_litellm()
        return (
            self._completion_fn or litellm.acompletion,
            self._token_counter or litellm.token_counter,
        )

    async def stream(self, spec: ModelSpec, req: ModelRequest) -> AsyncIterator[ModelChunk]:
        if spec.provider is not self._name:
            raise ValueError(f"spec for {spec.provider} sent to {self._name} adapter")
        acompletion, token_counter = self._fns()
        model = self.litellm_model(spec)
        messages = to_litellm_messages(req.messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": spec.timeout_s,
            "num_retries": 0,  # the router owns retries/fallback
        }
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key.get_secret_value()
        if self._api_base is not None:
            kwargs["api_base"] = self._api_base
        if req.max_tokens is not None:
            kwargs["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature

        text_parts: list[str] = []
        usage: Usage | None = None
        try:
            response = await acompletion(**kwargs)
            async for chunk in response:
                text = _delta_text(chunk)
                usage = _usage_from(chunk) or usage
                if text:
                    text_parts.append(text)
                    yield ModelChunk(text=text)
        except ProviderError:
            raise
        except Exception as exc:
            kind = classify_exception(exc)
            raise ProviderError(
                kind, _redact(f"{type(exc).__name__}: {exc}", self._api_key)
            ) from exc

        if usage is None:
            usage = self._estimate_usage(token_counter, model, messages, "".join(text_parts))
        yield ModelChunk(usage=usage)

    @staticmethod
    def _estimate_usage(
        token_counter: Callable[..., int], model: str, messages: list[dict[str, Any]], output: str
    ) -> Usage | None:
        try:
            return Usage(
                input_tokens=int(token_counter(model=model, messages=messages)),
                output_tokens=int(token_counter(model=model, text=output)),
                estimated=True,
            )
        except Exception:
            return None
