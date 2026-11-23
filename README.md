# Pauls-Orchestrator: a personal AI hub

A self-hosted orchestrator that runs on a home PC (Windows or Linux; Raspberry Pi 5
later). It routes each request to the right LLM (Claude, Gemini, Meta Muse, or a
local Ollama model) with automatic fallback. It logs which model answered, the
tokens used and the cost, and it keeps a **private** route that provably never
leaves your machine. It is also the hub for future devices: an M5Stack CoreS3
voice satellite, phone events and cameras, all speaking one versioned protocol
over MQTT and HTTP.

```
                 ┌────────────── hub (this repo: hub/) ──────────────┐
 web chat ─HTTP─▶│ API ─▶ classifier ─▶ ContextBuilder ─▶ Router ─────┼─▶ Anthropic
 CLI ──────HTTP─▶│  (SSE)   (auto)      (stage 2: memory)  │ fallback ├─▶ Gemini
 Tasker/HA ─HTTP▶│ /events ─┐                              │ locality ├─▶ Meta Model API
 CoreS3 ───MQTT─▶│ Mosquitto┴▶ validate ▶ EventBus          │  guard   └─▶ Ollama (local)
         ──WS───▶│ /ws/audio (stub)       usage.sqlite3 ◀───┘                 │
                 └──────────────────────────────────────────────────────────┘
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself.

```bash
git clone <this repo> && cd Pauls-Orchestrator
cp .env.example .env        # add whichever API keys you have (any subset, even none)
uv sync
uv run hub serve            # http://127.0.0.1:8765
```

Open <http://127.0.0.1:8765>, pick a route (or leave it on **auto**) and chat.
From a terminal:

```bash
uv run hub providers
uv run hub chat "Euler buckling load for a pinned-pinned steel column?" --route engineering_reasoning
echo "my SSN is ..." | uv run hub chat - --route private
uv run hub chat "hello" --json          # full metadata
```

For the `private` route, install [Ollama](https://ollama.com) and pull the model
named in `hub/config/routes.yaml` (`ollama pull llama3.2`), or change the model id.

### With MQTT (devices)

```bash
# in .env: HUB_MQTT_PASSWORD, HUB_DEVICE_TOKENS, (HUB_API_TOKEN if running the hub in Docker)
uv run python deploy/mosquitto/make_passwd.py   # hashed broker password file
docker compose up -d mosquitto                  # broker only; hub runs natively
# set mqtt.enabled: true in hub/config/hub.yaml, then
uv run hub serve
```

Or run both in Docker with `docker compose up -d`. The hub container needs
`HUB_API_TOKEN` because it binds `0.0.0.0` inside the container; the port is still
only published on `127.0.0.1`. Ollama on the host listens on `127.0.0.1` by default,
which a container can't reach, so for the private route either run the hub natively
(simplest) or make Ollama listen on the Docker bridge address.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/chat` | API token* | `{messages, route?, stream?, max_tokens?, temperature?}`. `messages[].images[]` = `{media_type, data_b64}`. Returns JSON, or SSE if `stream: true` |
| `GET` | `/providers` | API token* | Provider availability, models, routes, config warnings |
| `GET` | `/health` | none | Liveness |
| `GET` | `/usage/recent?limit=20` | API token* | Last N rows of the usage log |
| `GET` | `/devices` | API token* | Declared devices and MQTT presence |
| `POST` | `/events/{device_id}` | device token | Same envelope as MQTT, for Tasker / Home Assistant |
| `WS` | `/ws/audio/{device_id}` | device token | Stub (closes with 1013) |

\* Only when `HUB_API_TOKEN` is set (mandatory for non-loopback binds). OpenAPI docs: `/docs`.

Every successful answer carries metadata like this:

```json
{
  "model_alias": "local", "provider": "ollama", "model": "llama3.2", "locality": "local",
  "route": "default", "route_reason": "auto: no rule matched",
  "usage": {"input_tokens": 12, "output_tokens": 9, "estimated": false},
  "cost": {"usd": null, "status": "price not configured"},
  "fell_back": true,
  "attempts": [
    {"model_alias": "claude", "outcome": "error", "reason": "auth: AuthenticationError: ..."},
    {"model_alias": "gemini", "outcome": "skipped", "reason": "no API key configured"},
    {"model_alias": "local", "outcome": "ok"}
  ]
}
```

SSE events, in order: `route`, then one `attempt` per model tried, many `delta`,
then exactly one terminal event, `done` (with the metadata above) or `error`.

## Configuration reference

Secrets come **only** from `.env` (see `.env.example`). Everything else lives in
`hub/config/`, validated at startup with readable errors. Unknown keys are
rejected, so a typo such as `fallbacks:` can't silently mean "no fallback".

### `hub/config/routes.yaml`

```yaml
models:
  <alias>:
    provider: anthropic | gemini | meta | ollama
    model: <vendor model id>            # no prefix; the adapter adds it
    vision: false                       # images are only sent to vision models
    timeout_s: 60                       # max wait for the first chunk, and between chunks
    price: { input_per_mtok: null, output_per_mtok: null }   # USD per 1M tokens

routes:
  <name>:
    primary: <alias>
    fallback: [<alias>, ...]
    local_only: false                   # true: may never reach a cloud model
    deadline_s: 120                     # budget for the whole fallback search
    description: ...

classifier:                             # heuristics for route: auto (no LLM call)
  default_route: default
  rules:
    - { route: private, any: [password, ssn, ...] }   # keyword, whole word, plural ok
    - { route: long_context, min_chars: 60000 }
    - { route: cheap_bulk, has_images: true }
```

Rules targeting a `local_only` route are always evaluated first. Otherwise the
first matching rule wins.

### `hub/config/hub.yaml`

| Key | Default | Notes |
|---|---|---|
| `server.host` / `server.port` | `127.0.0.1` / `8765` | Non-loopback requires `allow_lan: true` **and** `HUB_API_TOKEN` |
| `server.allowed_hosts` | loopback names (or `*` with `allow_lan`) | Host-header allowlist against DNS rebinding. `HUB_HOST` does **not** widen it (that is what keeps the Docker setup loopback-only); for LAN access set `allow_lan: true` or list your hostnames here |
| `mqtt.enabled` / `host` / `port` / `client_id` | `false` / `127.0.0.1` / `1883` / `hub` | |
| `usage_db` | `data/usage.sqlite3` | Relative to the repo root |
| `log_level` / `log_format` | `INFO` / `console` | `json` for machine-readable logs |
| `devices[]` | | `{id, kind, description}`; tokens in `HUB_DEVICE_TOKENS` |

### Environment (`.env`)

`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `META_API_KEY` (or `MODEL_API_KEY`),
`OLLAMA_API_BASE`, `HUB_API_TOKEN`, `HUB_DEVICE_TOKENS` (JSON object),
`HUB_MQTT_USERNAME`, `HUB_MQTT_PASSWORD`. Overrides: `HUB_HOST`, `HUB_PORT`,
`HUB_CONFIG_DIR`, `HUB_MQTT_HOST`, `HUB_MQTT_ENABLED`, `HUB_ROOT`.

## Routing and fallback

For each model in `primary, *fallback`:

1. **Locality guard.** If the route is `local_only` and the model is not local, it
   is blocked (logged at ERROR) and never contacted.
2. **Skip** the model if it is unavailable (no key, or Ollama down) or lacks vision
   when the request carries images.
3. **Call** it. Any error or timeout *before the first token* moves on to the next
   model (logged as `provider_failed_falling_back`). A failure *after* text has
   streamed is reported as an error, never spliced with another model's answer.

`timeout_s` bounds time-to-first-token and idle gaps; `deadline_s` bounds the
whole search. Without a deadline the worst case is T_max = Σ timeout_i (for
example 3 × 60 s); with one it is min(Σ timeout_i, deadline_s).

## Costs

cost_USD = (N_in · p_in + N_out · p_out) / 10⁶, with p in USD per million tokens
taken **only** from `routes.yaml`. Shipped cloud prices are `null`, so responses say
`"price not configured"` until you fill them in from each vendor's pricing page.
LiteLLM's built-in price table is deliberately ignored. When a provider doesn't
report usage, tokens are counted locally and flagged `estimated: true`.

Every request (metadata only, no text) is logged to `data/usage.sqlite3`:

```bash
sqlite3 data/usage.sqlite3 "select route, model_alias, sum(input_tokens), sum(output_tokens), sum(cost_usd) from requests group by 1,2"
```

## Verified model IDs (2026-11-23)

Checked against the installed LiteLLM 1.110.2 model table and provider registry
(`docs.litellm.ai` and `dev.meta.ai` were unreachable from the build sandbox):

| alias | provider/model | note |
|---|---|---|
| `claude` | `anthropic/claude-opus-5-5` | also `claude-sonnet-5-5`, `claude-haiku-5-5` |
| `gemini` | `gemini/gemini-3.1-pro-preview` | flash: `gemini-3.8-flash` |
| `muse` | `meta/muse-spark-1.3` | also `muse-wonder-1.0`; native LiteLLM `meta/` provider |
| `local` | `ollama_chat/llama3.2` | whatever you have pulled |

`claude-fable-5-2` is not in LiteLLM 1.110.2's table. It would still route via
`anthropic/`, but LiteLLM has no metadata for it.

## Security model

* Binds to `127.0.0.1`. LAN needs `allow_lan: true` plus `HUB_API_TOKEN`, or the hub refuses to start.
* Host-header allowlist (DNS rebinding) and JSON-only `/chat` (blocks cross-site `text/plain` posts).
* API keys are only ever read from `.env`, passed explicitly to calls, and redacted from errors and logs.
* Prompt and answer text is never logged or stored. Logs carry route, model, tokens, cost and latency.
* No telemetry. LiteLLM's import-time price-map download is disabled (tested).
* MQTT: no anonymous access, per-device accounts, and an ACL confining each device to its own topics.

### Remote access

Use [Tailscale](https://tailscale.com); never port-forward. Install it on the hub
host and your phone, then either set `server.host` to the host's tailnet IP with
`allow_lan: true` and `HUB_API_TOKEN`, or keep loopback and run
`tailscale serve 8765`. Not automated in stage 1.

## Development

```bash
uv run pytest                      # unit + contract + loopback e2e (no network)
uv run pytest -m integration       # live: one call per provider whose key is set
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run datamodel-codegen           # after editing protocol/schemas
```

## Repository layout

| Path | What |
|---|---|
| `hub/` | Python orchestrator (stage 1) |
| `protocol/` | Inter-device contract: JSON Schemas, MQTT topic tree, changelog, fixtures |
| `deploy/mosquitto/` | Broker config, ACL, password-file generator |
| `docs/adr/` | Architecture decision records |
| `firmware/cores3/`, `agents/windows/`, `android/` | Future spokes (placeholders) |

## Roadmap

1. **Orchestrator core** (done): router, adapters, config, web chat, CLI, MQTT/HTTP hub side
2. **Memory**: markdown + vector index, a `ContextBuilder` that injects relevant memory, MCP server for claude.ai
3. **Verify mode**: fan one prompt out to N models; a judge model highlights disagreements
4. **Inputs**: screenpipe (MCP), phone events (webhook), camera frames (go2rtc snapshots)
5. **CoreS3 voice satellite**: on-device wake word; faster-whisper STT and Piper TTS on the hub

## Known limitations (stage 1)

* Conversation history lives in the browser tab; the hub stores no transcripts (memory is stage 2).
* `auto` is keyword-based: transparent and deterministic, but blunt.
* Cloud availability means "key present". The first real call is what verifies the key.
* Mid-stream failures are not retried on another model, by design.
* MQTT runs over plain TCP on the LAN; the TLS listener is documented but off.
