"""Single source of truth for *where a model call executes*.

This module is the foundation of the private-route guarantee
(docs/adr/0003-private-route-guarantee.md). Rules:

* Locality is computed from code-known facts (provider type, model tag,
  endpoint address). It can never be *declared* in YAML, so a config edit
  cannot relabel Anthropic as "local".
* Every uncertainty resolves to CLOUD (fail closed).

Known leak paths this guards against:

1. Ollama "cloud" models (tags such as ``gpt-oss:120b-cloud``) are proxied by
   the local daemon to ollama.com. Any model name containing a ``cloud`` token
   is treated as CLOUD.
2. ``OLLAMA_API_BASE`` pointing at a public host. The endpoint host must be a
   loopback or private (RFC 1918 / RFC 4193) address, or a hostname that
   resolves *only* to such addresses.

Limitation (documented in the ADR): the hub can only vouch for the request it
sends. What the Ollama binary itself does is outside this guarantee.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from enum import StrEnum
from urllib.parse import urlsplit

from hub.types import Locality


class ProviderName(StrEnum):
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    META = "meta"
    OLLAMA = "ollama"


#: Providers whose calls always leave the machine.
CLOUD_PROVIDERS: frozenset[ProviderName] = frozenset(
    {ProviderName.ANTHROPIC, ProviderName.GEMINI, ProviderName.META}
)

# A "cloud" token anywhere in the model name, delimited by start/end or : - _ . /
_CLOUD_TAG = re.compile(r"(?:^|[:\-_./])cloud(?:$|[:\-_./])", re.IGNORECASE)

_DNS_TTL_S = 60.0
_dns_cache: dict[str, tuple[float, bool]] = {}


def is_ollama_cloud_model(model: str) -> bool:
    """True if an Ollama model name denotes a cloud-proxied model."""
    return bool(_CLOUD_TAG.search(model))


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    # is_private covers RFC 1918, RFC 4193 ULA and link-local. CGNAT space
    # (100.64.0.0/10, used by Tailscale) is NOT private here: fail closed.
    return ip.is_private and not ip.is_unspecified


def _hostname_resolves_private(host: str) -> bool:
    now = time.monotonic()
    cached = _dns_cache.get(host)
    if cached and now - cached[0] < _DNS_TTL_S:
        return cached[1]
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        result = False  # cannot resolve -> fail closed
    else:
        addrs = {info[4][0] for info in infos}
        result = bool(addrs) and all(
            _ip_is_private(ipaddress.ip_address(str(a).split("%", 1)[0])) for a in addrs
        )
    _dns_cache[host] = (now, result)
    return result


def is_private_endpoint(url: str) -> bool:
    """True if ``url``'s host is loopback/private (or resolves only to such)."""
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        return _ip_is_private(ipaddress.ip_address(host))
    except ValueError:
        return _hostname_resolves_private(host)


def static_locality(provider: ProviderName, model: str) -> Locality:
    """Locality knowable from config alone (no network/DNS).

    Used by config validation. Runtime checks use :func:`model_locality`, which
    additionally inspects the endpoint.
    """
    if provider in CLOUD_PROVIDERS:
        return Locality.CLOUD
    if is_ollama_cloud_model(model):
        return Locality.CLOUD
    return Locality.LOCAL


def model_locality(provider: ProviderName, model: str, api_base: str | None) -> Locality:
    """Full runtime locality of a call: config facts AND endpoint address."""
    if static_locality(provider, model) is Locality.CLOUD:
        return Locality.CLOUD
    if api_base is None or not is_private_endpoint(api_base):
        return Locality.CLOUD
    return Locality.LOCAL
