# ADR 0006: Exposing memory over MCP

* Status: accepted for local transports; **public exposure deferred** (decision pending)
* Date: 2026-11-23

## Context

The roadmap asks for an MCP server so that claude.ai can use the hub's memory as a
custom connector. A claude.ai custom connector is called **from Anthropic's
cloud**, so it needs a publicly reachable HTTPS endpoint with OAuth. That
conflicts with the stage-1 rule that nothing is exposed beyond the LAN
(ADR 0004).

## Decision (stage 2)

Build the MCP server now, **read-only**, on two local transports only:

| Transport | For | Reachability |
|---|---|---|
| stdio (`hub mcp`) | Claude Desktop, Claude Code on the same PC | none (a child process) |
| Streamable HTTP at `/mcp/` on the hub | local MCP clients; Tailscale devices if `allow_lan` | same as the API: loopback by default; `HUB_API_TOKEN` required otherwise |

Built on the MCP Python SDK **2.3.0** (in 2.x `FastMCP` became `MCPServer`).

Rules:

1. **Read-only tools:** `search_memory`, `get_memory`, `recent_memories`, each
   annotated `read_only_hint=True`. There are no write tools.
2. **Never private:** capped at `memory.mcp.max_sensitivity`, which config only
   allows to be `public` or `personal`; the server constructor refuses
   `private` too. A capped note and a missing note produce byte-identical
   errors, so existence isn't revealed.
3. **Same perimeter as the API:** the hub's Host-header allowlist and bearer
   token apply to `/mcp/`, and the SDK's own DNS-rebinding check (Host and
   Origin) stays enabled.

## Deferred: public access for claude.ai

Not built. If you decide to do it, the minimum bar is:

* **Ingress:** Tailscale Funnel (or Cloudflare Tunnel) to the `/mcp/` path only;
  the rest of the hub stays private. No router port forwarding.
* **Auth:** OAuth 2.1 as the MCP authorization spec requires (the SDK supports
  this via `AuthSettings` and `token_verifier`), with a single allowed user;
  not a static bearer token.
* **Scope:** probably cap at `public`, or introduce an explicit `shareable` tag,
  rather than all `personal` notes.
* **Operations:** rate limiting, an audit log of tool calls (ids only), and a
  kill switch (`memory.mcp.enabled: false`).
* **Threat model:** anything returned becomes part of a claude.ai conversation
  under your account and is subject to that service's retention.

This needs its own decision because it changes the system's exposure
boundary, not just its features.

## Consequences

* Claude Desktop and Claude Code can use the memory today, locally.
* claude.ai (web) cannot until the deferred decision is made.
* The stdio server only *reads* the index; the running hub keeps it fresh. If
  the hub has never run, the stdio server sees an empty index.
