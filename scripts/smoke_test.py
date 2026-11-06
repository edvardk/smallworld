#!/usr/bin/env python3
"""Post-deploy smoke test: wait until the server answers a status ping from the public internet.

    smoke_test.py mc.example.com --timeout 180          # IPv4 (A record) only: CI, whose runners lack IPv6
    smoke_test.py mc.example.com --ipv6                 # also the AAAA record (e.g. from the admin's laptop)

The A and AAAA records are resolved separately and every address is pinged directly, so a broken
family can't hide behind a working one. Exit 0 only if every requested family answers in time.
Prints one `UP <family> <address>: ...` line per address, or `DOWN ...` lines and exits 1.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from collections.abc import Callable

from mcstatus import JavaServer

DEFAULT_PORT = 25565
FAMILIES = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}


def split_address(address: str) -> tuple[str, int]:
    """host[:port] -> (host, port). IPv6 literals need brackets: [2001:db8::1]:25565."""
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else DEFAULT_PORT
    if address.count(":") == 1:
        host, port = address.split(":")
        return host, int(port)
    return address, DEFAULT_PORT


def resolve(host: str, port: int, family: socket.AddressFamily) -> list[str]:
    """Addresses of one family (A or AAAA), de-duplicated in resolver order; [] if there are none."""
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def ping(ip: str, port: int) -> str:
    status = JavaServer(ip, port, timeout=5).status()
    return (
        f"{status.version.name} | {status.players.online}/{status.players.max} players | "
        f"{status.latency:.0f} ms"
    )


def wait_up(
    targets: list[tuple[str, str]],
    port: int,
    timeout: float,
    pinger: Callable[[str, int], str] = ping,
    interval: float = 5,
) -> dict[tuple[str, str], str]:
    """Ping (family, ip) targets until each answers or the deadline passes. Returns failures."""
    deadline = time.monotonic() + timeout
    pending = {t: "not tried" for t in targets}
    while pending:
        for fam, ip in list(pending):
            try:
                print(f"UP {fam} {ip}: {pinger(ip, port)}")
                del pending[(fam, ip)]
            except Exception as e:
                pending[(fam, ip)] = str(e) or type(e).__name__
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    return pending


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("address", help="host[:port]")
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--ipv6", action="store_true", help="also require the AAAA record's addresses to answer")
    args = p.parse_args(argv)

    host, port = split_address(args.address)
    families = ["ipv4", "ipv6"] if args.ipv6 else ["ipv4"]
    targets: list[tuple[str, str]] = []
    for fam in families:
        ips = resolve(host, port, FAMILIES[fam])
        if not ips:
            print(f"DOWN {fam}: {host} has no {'A' if fam == 'ipv4' else 'AAAA'} record", file=sys.stderr)
            return 1
        targets += [(fam, ip) for ip in ips]

    failed = wait_up(targets, port, args.timeout)
    for (fam, ip), err in failed.items():
        print(f"DOWN {fam} {ip}: not up after {args.timeout:.0f}s: {err}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
