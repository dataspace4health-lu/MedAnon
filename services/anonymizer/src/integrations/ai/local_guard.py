"""Local-endpoint enforcement for LLM calls (C4  PHI must not leave the host).

Single source of truth for deciding whether an LLM endpoint is provably
local/self-hosted. Used by ``LLMProvider.complete()`` / ``complete_streaming()``
as the unconditional chokepoint and by ``agents/pii_detector.py`` for its
early, caller-side check (nicer error message before any work is done).

This module must stay free of imports from ``integrations.ai.provider`` or
``integrations.ai.agents.*``  the provider imports *us*.

Enforcement matrix (see :func:`require_local`):

==============================  =========================  ==================
call type                       MEDANON_AI_PII_REQUIRE_    MEDANON_AI_
                                LOCAL (default true)       REQUIRE_LOCAL
                                                           (default false)
==============================  =========================  ==================
``phi_payload=True`` (default)  enforced                   enforced
``phi_payload=False``           not enforced               enforced
==============================  =========================  ==================
"""

import ipaddress
import logging
import os
import socket
import urllib.parse

_log = logging.getLogger("medanon.ai.local_guard")


class PiiModelNotLocalError(Exception):
    """Raised when the configured LLM endpoint is not local/self-hosted."""


# litellm provider prefixes whose default routing targets a self-hosted host.
_DEFAULT_LOCAL_PREFIXES: tuple[str, ...] = (
    "ollama/",
    "ollama_chat/",
    "vllm/",
    "lm_studio/",
    "local/",
)

# Hostnames treated as local without DNS resolution. "ollama" is the
# docker-compose service name (resolves to a private bridge IP in the stack,
# unresolvable from the host/test environment  both must pass).
_DEFAULT_LOCAL_HOSTS: tuple[str, ...] = (
    "ollama",
    "localhost",
    "host.docker.internal",
)

# Loopback + RFC1918 + link-local  addresses treated as "self-hosted".
_LOCAL_NETS = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fe80::/10",
        "fc00::/7",
    )
]


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def _pii_require_local() -> bool:
    """Whether local-only enforcement is active for PHI payloads (default: on)."""
    return _truthy(os.environ.get("MEDANON_AI_PII_REQUIRE_LOCAL", "true"))


def _require_local_all() -> bool:
    """Site-wide hard lock: every LLM call must resolve local (default: off)."""
    return _truthy(os.environ.get("MEDANON_AI_REQUIRE_LOCAL", "false"))


def require_local(phi_payload: bool) -> bool:
    """Whether the (model, api_base) pair must be verified local for this call."""
    if _require_local_all():
        return True
    return phi_payload and _pii_require_local()


def _local_model_prefixes() -> tuple[str, ...]:
    """Local provider prefixes, optionally extended via env."""
    extra = os.environ.get("MEDANON_AI_PII_LOCAL_PREFIXES", "").strip()
    if not extra:
        return _DEFAULT_LOCAL_PREFIXES
    return _DEFAULT_LOCAL_PREFIXES + tuple(
        p.strip().lower() for p in extra.split(",") if p.strip()
    )


def _local_hosts() -> tuple[str, ...]:
    """Hostnames trusted as local without DNS, optionally extended via env."""
    extra = os.environ.get("MEDANON_AI_LOCAL_HOSTS", "").strip()
    if not extra:
        return _DEFAULT_LOCAL_HOSTS
    return _DEFAULT_LOCAL_HOSTS + tuple(
        h.strip().lower() for h in extra.split(",") if h.strip()
    )


def _host_is_local(hostname: str) -> bool:
    """True only when *every* resolved address is loopback/private."""
    if not hostname:
        return False
    if hostname.strip().lower() in _local_hosts():
        return True
    try:
        addr = ipaddress.ip_address(hostname)
        return any(addr in net for net in _LOCAL_NETS)
    except ValueError:
        pass
    try:
        results = socket.getaddrinfo(
            hostname,
            None,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return False
    if not results:
        return False
    for _, _, _, _, sockaddr in results:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not any(addr in net for net in _LOCAL_NETS):
            return False
    return True


def assert_endpoint_local(model: str, api_base: str | None) -> None:
    """Raise unless the effective (model, api_base) is provably local.

    Unconditional  callers decide *whether* to enforce via
    :func:`require_local`. The caller MUST treat a raise as fail-closed
    (do not send the payload).
    """
    # An explicit local api_base satisfies the requirement regardless of the
    # model name (e.g. openai/gpt-oss served by a local vLLM gateway).
    if api_base:
        host = urllib.parse.urlparse(api_base).hostname or ""
        if _host_is_local(host):
            return
        raise PiiModelNotLocalError(
            f"LLM api_base {api_base!r} does not resolve to a local "
            "(loopback/private) endpoint",
        )
    # No explicit api_base → rely on the provider prefix routing to localhost.
    if model.strip().lower().startswith(_local_model_prefixes()):
        return
    raise PiiModelNotLocalError(
        f"LLM model {model!r} is not a recognised local provider. Set a "
        "self-hosted api_base or use a local model (e.g. ollama/...). "
        "Refusing to send text to a non-local LLM.",
    )
