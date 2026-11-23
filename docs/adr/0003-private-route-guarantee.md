# ADR 0003: The private-route guarantee

* Status: accepted
* Date: 2026-11-23

## Guarantee

A request on a `local_only` route (the shipped `private` route is one) is
**never sent by the hub to a model outside this machine or private network**,
whatever fails along the way.

## Threats considered

| # | Threat | Mitigation |
|---|---|---|
| 1 | A cloud model listed as a fallback | Config validation rejects it at startup |
| 2 | A config edit labels a cloud provider "local" | Locality cannot be declared in YAML; it is computed in `hub/locality.py` from the provider type |
| 3 | **Ollama cloud models** (`gpt-oss:120b-cloud`, `glm-4.6:cloud`) are proxied by the local daemon to ollama.com | Any model name with a delimited `cloud` token is CLOUD |
| 4 | `OLLAMA_API_BASE` points to a public host | The endpoint must be loopback or RFC 1918/4193, or a hostname resolving *only* to such; checked at startup and before every call |
| 5 | Validation bypassed (bug, `model_construct`, future code path) | The router re-checks the adapter's runtime locality before **every** call |
| 6 | `auto` misclassifies a sensitive prompt as cloud-bound | Rules targeting `local_only` routes always run first; `auto` can escalate privacy, never relax it |
| 7 | Stage-2 memory leaks private notes into a cloud prompt | `Sensitivity` and `max_sensitivity_for(ceiling)` (ADR 0002) |

Every uncertainty fails closed: unresolvable DNS, CGNAT/Tailscale (`100.64/10`)
or a missing base URL all count as CLOUD.

## Proof (tests)

* `tests/unit/test_private_route.py`: every cloud adapter is a *tripwire* that
  fails the test if `availability()` or `stream()` is called. The local model is
  made to fail in every way (unavailable, timeout, crash, mid-stream drop, each
  error kind); the result is always an error and the tripwires are untouched. One
  test builds a private route with cloud fallbacks via `model_construct`
  (skipping validation) to prove the runtime guard alone holds. Another iterates
  every `local_only` route in the shipped config.
* `tests/unit/test_config.py`: cloud primaries/fallbacks, Ollama cloud tags,
  public Ollama endpoints and `private` without `local_only` are all rejected.
* `tests/integration/test_local_e2e.py`: full HTTP request through the real
  LiteLLM adapter to a loopback fake Ollama with cloud tripwires in place.

## Limits (stated honestly)

* The hub controls what **it** sends. It cannot vouch for the Ollama binary,
  your OS, or other software on the machine.
* DNS is resolved by the check and again by the HTTP client, so a rebinding
  attack on your LAN DNS could in principle differ between the two. That is
  acceptable for a home network; pin an IP in `OLLAMA_API_BASE` to remove it.
* Logs and the usage database stay local but contain metadata (route, model,
  token counts). Prompt text is never logged or stored.
