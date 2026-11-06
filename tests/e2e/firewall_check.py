"""P7 acceptance: the product's nftables ruleset, loaded into a throwaway network namespace, really limits
new Minecraft connections per source (run as root inside the e2e-mock container; stdlib only).

The e2e container shares the runner's network, so the firewall role itself isn't run there (it would load
the ruleset into the runner). Instead the rendered /etc/nftables.conf of the role goes into a namespace
"server" behind a veth pair, a listener holds connections on 25565, and a namespace "client" checks:
  - IPv6: 6 + 6 concurrent connections from two addresses of one /64 -> exactly 10 (the /64 is one source);
  - IPv4: 12 concurrent connections -> exactly 10 (ct count);
  - IPv4: new connections beyond the burst are dropped (the rate limit), and the drop counters moved;
  - fail closed (P7 review): with the IPv4 rate set shrunk to 2 entries, 2 new sources get in, a 3rd is dropped
    (drop_full counted) instead of bypassing the limits, and a source already in the set still gets in.

    python3 tests/e2e/firewall_check.py RULESET
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

SRV, CLI = "mcfw-srv", "mcfw-cli"
V4_SRV, V4_CLI = "10.231.0.1", "10.231.0.2"
V4_MORE = ("10.231.0.3", "10.231.0.4", "10.231.0.5")
# Local runs on a kernel without IPv6 only (never set in CI).
NO_IPV6 = os.environ.get("MCFW_NO_IPV6") == "1"
V6_SRV, V6_CLI_A, V6_CLI_B = "fd00:231::1", "fd00:231::2", "fd00:231::3"

# Holds each connection until the client closes it, then closes its side too (so conntrack sees it closed and
# the per-source count drops again).
LISTENER = r"""
import socket, threading, time
def hold(c):
    try:
        while c.recv(1024):
            pass
    finally:
        c.close()
def serve(family, addr):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind((addr, 25565)); s.listen(256)
    while True:
        c, _ = s.accept()
        threading.Thread(target=hold, args=(c,), daemon=True).start()
threading.Thread(target=serve, args=(socket.AF_INET, "0.0.0.0"), daemon=True).start()
threading.Thread(target=serve, args=(socket.AF_INET6, "::"), daemon=True).start()
time.sleep(600)
"""

CLIENT = r"""
import json, socket, sys
mode, dst = sys.argv[1], sys.argv[2]
family = socket.AF_INET6 if ":" in dst else socket.AF_INET
def conn(src):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(1.0)
    if src:
        s.bind((src, 0))
    try:
        s.connect((dst, 25565))
        return s
    except OSError:
        s.close()
        return None
if mode == "concurrent":  # argv: srcs... (each "addr*count")
    held, ok = [], 0
    for spec in sys.argv[3:]:
        src, n = spec.split("*")
        for _ in range(int(n)):
            c = conn(src or None)
            if c:
                ok += 1; held.append(c)
    for c in held:
        c.close()
    print(json.dumps({"ok": ok}))
else:  # sequential: connect + close until 3 failures or 45 attempts
    ok = fail = 0
    for _ in range(45):
        c = conn(None)
        if c:
            ok += 1; c.close()
        else:
            fail += 1
            if fail >= 3:
                break
    print(json.dumps({"ok": ok, "failed": fail}))
"""


def sh(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise SystemExit(f"{' '.join(argv)}: {proc.returncode}\n{proc.stdout}{proc.stderr}")
    return proc


def ns(name: str, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return sh("ip", "netns", "exec", name, *argv, check=check)


def client(mode: str, dst: str, *args: str) -> dict[str, int]:
    return json.loads(ns(CLI, sys.executable, "-c", CLIENT, mode, dst, *args).stdout)


def setup() -> None:
    for name in (SRV, CLI):
        sh("ip", "netns", "del", name, check=False)
        sh("ip", "netns", "add", name)
        ns(name, "ip", "link", "set", "lo", "up")
        if not NO_IPV6:
            ns(name, "sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=0")
    sh("ip", "link", "add", "mcfw0", "netns", SRV, "type", "veth", "peer", "name", "mcfw1", "netns", CLI)
    ns(SRV, "ip", "addr", "add", f"{V4_SRV}/24", "dev", "mcfw0")
    for a in (V4_CLI, *V4_MORE):
        ns(CLI, "ip", "addr", "add", f"{a}/24", "dev", "mcfw1")
    if not NO_IPV6:
        ns(SRV, "ip", "-6", "addr", "add", f"{V6_SRV}/64", "dev", "mcfw0", "nodad")
        for a in (V6_CLI_A, V6_CLI_B):
            ns(CLI, "ip", "-6", "addr", "add", f"{a}/64", "dev", "mcfw1", "nodad")
    ns(SRV, "ip", "link", "set", "mcfw0", "up")
    ns(CLI, "ip", "link", "set", "mcfw1", "up")


def set_full(ruleset: str, expect: Callable[[bool, str], None]) -> None:
    """Fail closed: shrink the IPv4 rate set to 2 entries (a fresh table) and fill it from 2 sources."""
    text = Path(ruleset).read_text()
    small, n = re.subn(r"(set mc_rate_v4 \{ type ipv4_addr; size )\d+", r"\g<1>2", text)
    expect(n == 1, "found the IPv4 rate set to shrink")
    path = Path(tempfile.mkdtemp()) / "mc-nftables-small.conf"
    path.write_text(small)
    ns(SRV, "nft", "-f", str(path))  # replaces the table: empty sets, zero counters
    time.sleep(1)
    a, b, c = V4_MORE
    got = [client("concurrent", V4_SRV, f"{src}*1")["ok"] for src in (a, b, c, a)]
    expect(got == [1, 1, 0, 1], f"set full: 2 sources in, a 3rd dropped, a known one still in ({got})")
    out = ns(SRV, "nft", "list", "chain", "inet", "mc_filter", "drop_full").stdout
    expect("counter packets 0 " not in out, "drop_full counted the untracked source")


def main(ruleset: str) -> int:
    failures: list[str] = []

    def expect(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    sh("nft", "-c", "-f", ruleset)
    setup()
    listener = subprocess.Popen(["ip", "netns", "exec", SRV, sys.executable, "-c", LISTENER])
    try:
        ns(SRV, "nft", "-f", ruleset)
        table = ns(SRV, "nft", "list", "table", "inet", "mc_filter").stdout
        for needle in (
            "set mc_rate_v4",
            "set mc_conn_v6",
            "jump minecraft_new",
            "ct count over",
            "limit rate over",
        ):
            expect(needle in table, f"ruleset loaded: {needle!r}")
        time.sleep(1)
        before = client("concurrent", V4_SRV, "*1")
        expect(before["ok"] == 1, f"a single IPv4 connection is accepted ({before})")
        time.sleep(1)
        if not NO_IPV6:
            v6 = client("concurrent", V6_SRV, f"{V6_CLI_A}*6", f"{V6_CLI_B}*6")
            expect(v6["ok"] == 10, f"IPv6: 12 concurrent from one /64 -> 10 accepted ({v6})")
            time.sleep(1)
        v4 = client("concurrent", V4_SRV, "*12")
        expect(v4["ok"] == 10, f"IPv4: 12 concurrent from one address -> 10 accepted ({v4})")
        time.sleep(1)
        seq = client("sequential", V4_SRV)
        expect(
            seq["failed"] > 0 and 1 <= seq["ok"] <= 30,
            f"IPv4: new connections beyond the burst are dropped ({seq})",
        )
        for chain in ("drop_conn", "drop_rate"):
            out = ns(SRV, "nft", "list", "chain", "inet", "mc_filter", chain).stdout
            expect("counter packets 0 " not in out, f"{chain} counted drops")
        set_full(ruleset, expect)
    finally:
        listener.kill()
        for name in (SRV, CLI):
            sh("ip", "netns", "del", name, check=False)
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
