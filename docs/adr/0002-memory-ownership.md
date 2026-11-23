# ADR 0002: The hub owns memory; vendors never share it

* Status: accepted; implemented in stage 2 (see ADR 0005 and ADR 0006)
* Date: 2026-11-23

## Context

Each vendor now offers its own "memory" (ChatGPT memory, Claude projects,
Gemini personal context). Using them would scatter what the system knows about
you across companies, make answers depend on which vendor happened to be asked,
and make "forget this" impossible to guarantee.

## Decision

1. **Single source of truth.** The hub's own store (stage 2: markdown files plus
   a vector index on your disk) is the only memory. Vendor memory features are
   not used.
2. **Per-request injection only.** Relevant memory is added to a prompt by a
   `ContextBuilder` immediately before the model call. A vendor sees only what
   is in that request, never the store.
3. **No vendor-to-vendor sharing.** Output from one model is never written to
   another vendor's memory; anything worth keeping is written to the hub store.
4. **Sensitivity travels with each item.** `MemoryItem.sensitivity` is one of
   `PUBLIC`, `PERSONAL` or `PRIVATE`. `max_sensitivity_for(ceiling)` returns
   `PRIVATE` only for a LOCAL ceiling, so private notes can only ever be injected
   into local-model prompts.
5. **External access is via MCP.** Stage 2 exposes read/search through an MCP
   server so claude.ai can use the store as a custom connector. It is read-only
   by default and honours sensitivity.

## Stage-1 implementation

* `hub/memory/protocol.py`: `MemoryProvider` protocol (`search`, `get`, `put`),
  `MemoryItem`, `Sensitivity`.
* `hub/memory/noop.py`: `NoOpMemoryProvider` (returns nothing).
* `hub/context/builder.py`: `ContextBuilder` protocol, `PassThroughContextBuilder`,
  `RequestContext` (carries `locality_ceiling`).
* `ChatService` calls the builder before every routed call; a test proves an
  injected system message reaches the model.

## Consequences

* The builder runs once per request with the **route's** ceiling, not the
  eventual model's. That is conservative: on a cloud-capable route, `PRIVATE`
  memory is withheld even if a local fallback ends up answering.
* Backups, export and deletion are file operations on your disk.
