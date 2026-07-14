"""SSRF protection primitives: resolve hostnames and reject private/loopback targets.

Extracted from ``api/deps.py`` so adapters (e.g. ``integrations/fhir/bulk.py``)
can reuse the guard without importing the web layer. The HTTP-facing wrapper
(``_validate_server_url``, which raises ``HTTPException``) stays in ``api/deps.py``.
"""

from __future__ import annotations

import ipaddress
import socket

PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 carrier-grade NAT
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6 (belt-and-suspenders)
]


def effective_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the canonical IP for SSRF checks.

    IPv4-mapped IPv6 addresses (``::ffff:x.x.x.x``) are unmapped to their
    underlying IPv4 form so that private-range checks against IPv4 networks
    (e.g. ``127.0.0.0/8``) are not silently bypassed.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def check_hostname_ssrf(hostname: str) -> str | None:
    """Resolve *hostname* via DNS and check all addresses against private ranges.

    Returns an error message if any resolved address is private/loopback,
    or ``None`` when the hostname is safe.
    """
    try:
        results = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}"
    for family, _, _, _, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = effective_ip(ipaddress.ip_address(ip_str))
            if any(addr in net for net in PRIVATE_NETS):
                return f"Hostname {hostname} resolves to private address {ip_str}"
        except ValueError:
            continue
    return None
