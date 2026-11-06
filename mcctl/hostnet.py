"""The host's public IPv6 addresses, from `ip -j -6 addr show scope global` (init's host probe, doctor).

Usable for host.ipv6 (the AAAA record): global unicast (2000::/3), not Tailscale's (tailscale0,
fd7a:115c:a1e0::/48, a ULA anyway), not link-local, not a temporary (privacy), deprecated, tentative or
DAD-failed address. Stable ones first: a static address, then a stable SLAAC one (EUI-64, e.g.
...:5054:ff:fe12:3456, or the mngtmpaddr base), then any other dynamic one. A SLAAC address is `dynamic`
(it has a lifetime) but stable. `ip -o` text (the probe before this module) is still understood.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any

COMMAND = "ip -j -6 addr show scope global"
TAILSCALE_NET = ipaddress.IPv6Network("fd7a:115c:a1e0::/48")
GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
SKIP_FLAGS = ("temporary", "deprecated", "tentative", "dadfailed")


def _usable(ip: ipaddress.IPv6Address, ifname: str) -> bool:
    return (
        ifname != "tailscale0"
        and ip not in TAILSCALE_NET
        and ip in GLOBAL_UNICAST
        and ip.is_global
        and not ip.is_link_local
    )


def _rank(info: dict[str, Any], ip: ipaddress.IPv6Address) -> int:
    if not info.get("dynamic"):
        return 0  # configured statically
    eui64 = (int(ip) >> 24) & 0xFFFF == 0xFFFE  # ...:xxff:fexx:xxxx
    return 1 if eui64 or info.get("mngtmpaddr") else 2


def _from_json(data: object) -> list[str]:
    found: list[tuple[int, int, str]] = []
    for n, link in enumerate(data if isinstance(data, list) else []):
        if not isinstance(link, dict):
            continue
        ifname = str(link.get("ifname") or "")
        for info in link.get("addr_info") or []:
            if not isinstance(info, dict) or info.get("family") != "inet6" or info.get("scope") != "global":
                continue
            if any(info.get(flag) for flag in SKIP_FLAGS) or info.get("preferred_life_time") == 0:
                continue
            try:
                ip = ipaddress.IPv6Address(str(info.get("local")))
            except ValueError:
                continue
            if _usable(ip, ifname):
                found.append((_rank(info, ip), n, str(ip)))
    return list(dict.fromkeys(addr for _, _, addr in sorted(found)))


def _from_text(text: str) -> list[str]:
    """`ip -6 -o addr show scope global` lines: `2: eth0    inet6 ADDR/64 scope global [flags] \\ ...`."""
    found: list[str] = []
    for line in text.splitlines():
        words = line.split()
        if "inet6" not in words or any(f in words for f in SKIP_FLAGS):
            continue
        i = words.index("inet6")
        ifname = words[i - 1] if i >= 1 else ""
        try:
            ip = ipaddress.IPv6Interface(words[i + 1]).ip
        except (IndexError, ValueError):
            continue
        if _usable(ip, ifname) and str(ip) not in found:
            found.append(str(ip))
    return found


def public_ipv6(output: str) -> list[str]:
    """The usable public IPv6 addresses in `ip -j -6 addr show scope global` output, stable first."""
    text = output.strip()
    if text.startswith("["):
        try:
            return _from_json(json.loads(text))
        except ValueError:
            return []
    return _from_text(text)


def configured(output: str) -> set[ipaddress.IPv6Address]:
    """Every IPv6 address in the output, usable or not (is host.ipv6 configured on the host at all?)."""
    text = output.strip()
    have: set[ipaddress.IPv6Address] = set()
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            return have
        for link in data if isinstance(data, list) else []:
            for info in (link.get("addr_info") or []) if isinstance(link, dict) else []:
                try:
                    have.add(ipaddress.IPv6Address(str(info.get("local"))))
                except (ValueError, AttributeError):
                    continue
        return have
    for word in text.split():
        if "/" in word:
            try:
                have.add(ipaddress.IPv6Interface(word).ip)
            except ValueError:
                continue
    return have
