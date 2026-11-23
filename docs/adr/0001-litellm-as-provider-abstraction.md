# ADR 0001: LiteLLM as the provider abstraction

* Status: accepted
* Date: 2026-11-23

## Context

The hub talks to four providers (Anthropic, Google Gemini, Meta Model API, local
Ollama) and will add more. Each vendor SDK has its own message format, streaming
protocol, image encoding, error types and usage reporting. Writing and
maintaining four adapters by hand is real work that adds no value to the hub.

## Decision

Use [LiteLLM](https://github.com/BerriAI/litellm) (pinned **exactly**, currently
`1.110.2`) behind our own `ProviderAdapter` protocol (`hub/providers/base.py`).
One class, `LiteLLMAdapter`, serves every provider.

Provider strings, verified in the installed LiteLLM source rather than from docs:

| provider | LiteLLM prefix | evidence |
|---|---|---|
| anthropic | `anthropic/` | model table entries `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` |
| gemini | `gemini/` | model table, Google AI Studio route |
| meta | `meta/` | `llms/openai_like/providers.json`: `base_url https://api.meta.ai/v1`, `api_key_env META_API_KEY`, OpenAI-compatible base class; Muse models `meta/muse-spark-1.3`, `meta/muse-wonder-1.0` in the table |
| ollama | `ollama_chat/` | uses `/api/chat` (roles, images) rather than `/api/generate` (`ollama/`) |

**Meta Model API:** LiteLLM 1.110.2 ships a native `meta/` provider, so no custom
adapter was needed. Meta's own docs name the key `MODEL_API_KEY`, while LiteLLM
uses `META_API_KEY`; the hub accepts either and always passes it explicitly.

## Guard rails

* **Exact pin plus `uv.lock` hashes.** LiteLLM is large and fast-moving and has
  had PyPI supply-chain incidents. Upgrades are deliberate: bump, run the
  integration tests, read the changelog.
* **No surprise network at import.** By default `import litellm` downloads a
  price map from `raw.githubusercontent.com`. `hub/__init__.py` sets
  `LITELLM_LOCAL_MODEL_COST_MAP=True`. A test spies on `socket.connect` and
  `getaddrinfo` during import to prove zero calls.
* **Explicit keys.** We pass `api_key=` on every call; LiteLLM never discovers
  credentials from the environment for us.
* **We own retries and fallback** (`num_retries=0`), so the router's fallback
  behaviour and logging are the only ones in play.
* **We use only `acompletion`** and `token_counter`. No LiteLLM proxy, router,
  callbacks or logging integrations.
* **Prices are ours**, from `routes.yaml`; LiteLLM's bundled price table is
  deliberately ignored (see README "Costs").

## Consequences

* Adding a provider LiteLLM supports is a config change plus one enum entry.
* If LiteLLM becomes a liability, replacing it means writing classes that
  satisfy `ProviderAdapter` (about 150 lines per vendor SDK). Nothing outside
  `hub/providers/` imports LiteLLM.
* Import cost (about 1 to 2 s) is paid once at server start (warmed in a worker
  thread); the CLI never imports it.
