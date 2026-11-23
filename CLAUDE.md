# CLAUDE.md

Context for AI coding sessions on this repo. Read this first.

## What this is

A self-hosted personal AI orchestrator ("hub") in a polyglot hub-and-spoke
monorepo. Stage 1 (done): router, LiteLLM adapters, config, FastAPI web chat +
SSE, CLI, MQTT/HTTP hub side, protocol schemas. See README "Roadmap" for stages 2-5.

The owner is a mechanical/structural engineer who writes Python daily. Favor
clear, well-typed, readable code over cleverness; explain non-obvious decisions
in docstrings or ADRs (`docs/adr/`).

## Commands (run from repo root)

```bash
uv sync                                   # install (Python 3.12, locked)
uv run hub serve                          # http://127.0.0.1:8765
uv run hub chat "q" --route private       # CLI is an HTTP client of the RUNNING hub
uv run pytest                             # all offline tests (~6 s)
uv run pytest -m integration              # live provider calls; needs keys; costs money
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run datamodel-codegen                  # regenerate hub/src/hub/events/generated
uv run python hub/tests/integration/fake_ollama.py --port 11434   # manual local testing
```

CI (`.github/workflows/ci.yml`) runs ruff, ruff format, mypy (strict on `hub/src`),
pytest, a stale-generated-models check, and `docker compose config`.

## Architecture map (`hub/src/hub/`)

| Module | Role |
|---|---|
| `settings.py` | Secrets/env only (pydantic-settings, `.env` at repo root) |
| `config.py` | YAML config models + all validation (`parse_config`, `check_runtime`) |
| `locality.py` | **Where a call executes.** Single source of truth for the private guarantee |
| `types.py` | Request/response/stream-event models |
| `providers/` | `ProviderAdapter` protocol, `LiteLLMAdapter`, `ProviderRegistry` |
| `routing/` | `KeywordClassifier` (route=auto), `Router` (fallback + locality guard) |
| `pipeline.py` | `ChatService`: classify, ContextBuilder, Router, cost, usage log. Every interface goes through it |
| `memory/`, `context/` | Stage-2 extension points (protocols + no-op/pass-through) |
| `usage/` | Pricing (config-only) and SQLite usage store (metadata only) |
| `events/` | Generated protocol models, `ProtocolValidator`, `EventBus`, `InputSource` |
| `transports/mqtt.py` | aiomqtt client; `handle()` never raises |
| `api/` | FastAPI app factory (`create_app`), chat/system/devices routers, auth |
| `web/static/` | Plain HTML/JS/CSS chat UI (no build step) |
| `cli.py` | Typer CLI |

## Invariants: do not break these

1. **`local_only` routes never reach a cloud model.** Locality is computed in
   `locality.py`, never declared in YAML. The router re-checks before every call.
   Any change near routing must keep `tests/unit/test_private_route.py` passing;
   add a tripwire test for any new path.
2. **No model IDs or prices in code.** They live in `hub/config/routes.yaml`.
   Never invent prices; `null` means "price not configured".
3. **Secrets only from `.env`**, always passed explicitly (`api_key=`). Never log
   keys, prompts or completions at any level. The usage DB stores metadata only.
4. **Bind to 127.0.0.1 by default.** Non-loopback requires `allow_lan` + `HUB_API_TOKEN`.
5. **`protocol/schemas` is the contract.** Never hand-edit
   `hub/src/hub/events/generated/`. After a schema change: regenerate, add fixtures
   under `protocol/examples/{valid,invalid}/<schema>/`, update `protocol/CHANGELOG.md`
   (breaking change = new `schema_version` and new file).
6. **Inbound device messages never crash the hub**: validate, log the reason
   (not the payload), drop.
7. **No network in tests** except loopback. Providers are faked (`tests/fakes.py`);
   live tests are `@pytest.mark.integration` and excluded by default.
8. **LiteLLM stays behind `ProviderAdapter`.** Nothing outside `providers/` imports
   it. It is pinned exactly; `LITELLM_LOCAL_MODEL_COST_MAP=True` is set in
   `hub/__init__.py` so importing it makes no network call.
9. Don't use em or en dashes in prose you write for the owner.

## Conventions

* Python 3.12, `from __future__ import annotations`, full type hints, mypy strict.
* pydantic v2 models for data crossing boundaries; frozen dataclasses internally.
* Protocols (structural typing) for extension points; constructor injection
  (see `create_app(..., registry=...)`) so tests can swap fakes.
* structlog with event names in snake_case (`chat_completed`,
  `provider_failed_falling_back`) and metadata as key/values.
* Tests: `pytest-asyncio` auto mode; tests import fakes as `from tests.fakes import ...`.
* Small, logical commits with descriptive messages.

## Stage-2 entry points

Implement `MemoryProvider` (`memory/protocol.py`) and a `ContextBuilder` that calls
`memory.search(..., max_sensitivity=max_sensitivity_for(ctx.locality_ceiling))`, then
pass it to `build_state(..., context_builder=..., memory=...)` in `api/app.py`.
New input sources implement `events.bus.InputSource` and publish to the `EventBus`.
