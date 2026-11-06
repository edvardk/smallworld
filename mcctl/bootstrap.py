"""`mcctl bootstrap` (design §4, init step 10 alone): mint a fresh server key, run bootstrap.yml, verify.

0. Already bootstrapped (state `done` for the same host) and Tailscale SSH still works: nothing to do, and no
   new key is minted (`--force` re-runs everything, e.g. after reinstalling the host).
1. Render and `terraform init` (like every command that runs Terraform).
2. Probe public TCP 22 on IPv4/IPv6 from here, before anything changes: an address that doesn't answer now
   (no IPv6 on this network, outbound 22 blocked) can't prove later that the firewall closed it.
3. `apply -replace=tailscale_tailnet_key.server` (DNS records + a fresh single-use 24 h key), after the plan
   summary and the typed confirmation. Skipped when this machine already sees the node online with
   `tag:mc-server` (a re-run after a later failure): no key is wasted and no duplicate node is created.
4. `ansible-playbook bootstrap.yml` over public SSH (`host.ssh_bootstrap_user`@`host.ipv4`) with TS_AUTHKEY in
   its environment only. The playbook checks Tailscale SSH from this machine itself, to the node's 100.x
   tailnet address, before it closes public 22.
5. Verify from here: `ssh deploy@<tailnet address> true` works (the address comes from `tailscale status
   --json`, never from DNS, so a search domain can't send it to the public IP), and public 22 no longer
   answers where step 2 showed it did; record the step in .mcctl/state.json.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from mcctl import deploy, remote, render, state, tf, ui
from mcctl.configrepo import ConfigRepo

KEY_RESOURCE = "tailscale_tailnet_key.server"
KEY_OUTPUT = "server_tailscale_auth_key"
STEP = "bootstrap"
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's IPv4 range
SERVER_TAG = "tag:mc-server"  # = ansible group_vars mc_tailscale_server_tag (tests/test_consistency.py)

Probe = Literal["open", "refused", "no-answer", "no-route"]
Prober = Callable[[str, int], Probe]
_NO_ROUTE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}


def probe(address: str, port: int, timeout: float = 5.0) -> Probe:
    """What a TCP connect from here sees: open, refused (RST: the host answered, port closed), no-answer
    (dropped somewhere: by the host's firewall, or by this network), no-route (e.g. no IPv6 here)."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((address, port))
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "no-answer"
    except OSError as e:
        return "no-route" if e.errno in _NO_ROUTE else "no-answer"
    return "open"


def _peers(name: str, run: remote.Runner) -> list[dict]:
    """This machine's `tailscale status --json` Peer entries whose DNSName is `<name>.<tailnet>.`."""
    proc = run(["tailscale", "status", "--json"])
    try:
        status = json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        status = None
    peers = (status.get("Peer") if isinstance(status, dict) else None) or {}
    return [
        p
        for p in (peers.values() if isinstance(peers, dict) else [])
        if isinstance(p, dict) and str(p.get("DNSName", "")).startswith(f"{name}.")
    ]


def already_joined(name: str, run: remote.Runner) -> bool:
    """The server is on the tailnet already (online, tagged SERVER_TAG): a re-run after a bootstrap that
    failed later needs no fresh key (the tailscale role skips `tailscale up` on a Running node)."""
    return any(p.get("Online") is True and SERVER_TAG in (p.get("Tags") or []) for p in _peers(name, run))


def tailnet_address(name: str, run: remote.Runner) -> str | None:
    """The 100.x address of tailnet node `name`, from this machine's `tailscale status --json` (Peer entries:
    DNSName `<name>.<tailnet>.`, TailscaleIPs), or None if this machine doesn't see such a node."""
    for peer in _peers(name, run):
        for ip in peer.get("TailscaleIPs") or []:
            try:
                if ipaddress.ip_address(ip) in CGNAT:
                    return str(ip)
            except ValueError:
                continue
    return None


def playbook_argv(inventory: Path, ipv4: str, user: str) -> list[str]:
    return [
        deploy.exe("ansible-playbook"),
        "-i",
        str(inventory),
        "bootstrap.yml",
        "-e",
        f"ansible_host={ipv4}",
        "-e",
        f"ansible_user={user}",
    ]


def addresses(repo: ConfigRepo) -> list[str]:
    return [a for a in (repo.server.host.ipv4, repo.server.host.ipv6) if a]


def ssh_works(repo: ConfigRepo, run: remote.Runner) -> tuple[bool, str]:
    """(ok, detail) of `ssh deploy@<100.x of host.tailscale_name> true`."""
    cfg = repo.server
    addr = tailnet_address(cfg.host.tailscale_name, run)
    if addr is None:
        return False, (
            f"this machine's tailnet has no node {cfg.host.tailscale_name} (`tailscale status`; is this "
            "laptop logged in to the same tailnet?)"
        )
    if run(remote.ssh_argv(cfg, "true", host=addr)).returncode != 0:
        return (
            False,
            f"`ssh {render.DEPLOY_USER}@{addr} true` (Tailscale SSH to {cfg.host.tailscale_name}) failed",
        )
    return True, f"Tailscale SSH to {cfg.host.tailscale_name} ({addr}) works"


def verify(
    repo: ConfigRepo, run: remote.Runner, prober: Prober, before: dict[str, Probe] | None = None
) -> tuple[list[str], list[str]]:
    """(problems, notes) after a bootstrap. `before` is the port-22 probe from before the playbook: an
    address that didn't answer then can't be verified closed now."""
    problems, notes = [], []
    ok, detail = ssh_works(repo, run)
    (notes if ok else problems).append(detail)
    for address in addresses(repo):
        now = prober(address, 22)
        if now == "open":
            problems.append(f"public TCP 22 still answers on {address}")
        elif before is not None and before.get(address) != "open":
            notes.append(
                f"public TCP 22 on {address}: unverifiable from here (it didn't answer before bootstrap "
                "either: no IPv6 or outbound port 22 on this network?). Check from elsewhere: "
                f"`nmap -Pn -p 22 {address}`"
            )
        else:
            notes.append(f"public TCP 22 on {address}: closed")
    return problems, notes


def run(
    repo: ConfigRepo,
    product_root: Path,
    *,
    ask: Callable[[str], str] | None = None,
    runner: remote.Runner = remote.capture,
    prober: Prober = probe,
    force: bool = False,
    terraform_factory: Callable[[], tuple[tf.Terraform, Path]] | None = None,
    playbook: Callable[[list[str], Path, dict[str, str]], int] | None = None,
    pinned_collections: Callable[[Path], Path] = deploy.collections,
) -> int:
    cfg = repo.server
    out_ = ui.stdout()

    def say(kind: str, text: str) -> None:
        """Plain: the text as is; rich: with its status icon ("fail": a failure box)."""
        if not out_.rich:
            out_.say(text)
        elif kind == "fail" and not text.startswith("check failed: "):
            out_.fail(text.replace("\n", " "))
        else:
            out_.level(kind, text.removeprefix("check failed: ").replace("\n", " "))

    if cfg.host.ipv4 is None:
        raise deploy.DeployError(
            "set host.ipv4 (and host.ipv6) in server.yml first: there is no host to bootstrap"
        )
    st = state.load(repo.root)
    inputs = {"ipv4": cfg.host.ipv4, "ipv6": cfg.host.ipv6, "tailscale_name": cfg.host.tailscale_name}
    if not force and st.is_done(STEP, inputs):
        ok, detail = ssh_works(repo, runner)
        if ok:
            say(
                "ok",
                f"Already bootstrapped: {detail}. Nothing to do (no new key minted; --force re-runs it).",
            )
            return 0
        say(
            "fail",
            f"Bootstrap was done for this host, but now: {detail}.\n"
            "Public SSH is closed, so bootstrap.yml can't simply run again. If the host was reinstalled, "
            "re-run with `--force`; otherwise fix Tailscale on this laptop/the server (provider console).",
        )
        return 1

    before = {a: prober(a, 22) for a in addresses(repo)}
    if before.get(cfg.host.ipv4) != "open":
        say(
            "warn",
            f"warning: public TCP 22 on {cfg.host.ipv4} doesn't answer from here "
            f"({before.get(cfg.host.ipv4)}); bootstrap.yml needs it "
            "(check the provider firewall, or that the host is up).",
        )
    # Before anything changes: the playbook's galaxy collections (a failure here leaves no key minted).
    collections = pinned_collections(product_root)
    if terraform_factory is not None:
        terraform, out = terraform_factory()
    else:
        terraform, out = deploy.terraform_for(repo, product_root, purpose="bootstrap with")
    joined = not force and already_joined(cfg.host.tailscale_name, runner)
    if joined:  # an earlier run joined the node and failed later: a fresh key would only be wasted
        say(
            "ok",
            f"{cfg.host.tailscale_name} is already on the tailnet ({SERVER_TAG}, online): no new key minted; "
            "bootstrap.yml continues from there (--force mints one)",
        )
        key = ""
    else:
        try:
            applied = tf.apply_flow(
                terraform, repo.root, out / render.TFVARS_FILE, cfg.name, replaces=[KEY_RESOURCE], ask=ask
            )
        except tf.TfError as e:
            raise deploy.DeployError(
                f"{e}\nNothing reached the server; fix it and run `mcctl bootstrap` again."
            ) from None
        if not applied:
            return 1
        key = deploy.outputs(terraform).get(KEY_OUTPUT)
        if not isinstance(key, str) or not key:
            raise deploy.DeployError(f"Terraform output {KEY_OUTPUT} is empty after the apply")
    argv = playbook_argv(out / render.INVENTORY_FILE, cfg.host.ipv4, cfg.host.ssh_bootstrap_user)
    say(
        "info",
        f"bootstrap.yml against {cfg.host.ssh_bootstrap_user}@{cfg.host.ipv4}"
        + ("" if joined else " (TS_AUTHKEY in its environment)"),
    )
    say(
        "info",
        "if 'Check Tailscale SSH' keeps retrying: run `ssh deploy@<the server's 100.x address> true` in "
        "another terminal and open the login URL it prints (the admin's Tailscale SSH rule is `check`, 12 h)",
    )
    env = deploy.playbook_env(os.environ, {"TS_AUTHKEY": key}, collections)
    if playbook is not None:
        rc = playbook(argv, product_root / "ansible", env)
    else:
        rc = subprocess.run(argv, cwd=product_root / "ansible", env=env, check=False).returncode
    if rc != 0:
        st.record(STEP, "failed", inputs=inputs)
        now = prober(cfg.host.ipv4, 22)
        if now != "open" and before.get(cfg.host.ipv4) != "open":
            say(
                "fail",
                "bootstrap.yml failed (see above). Public port 22 can't be reached from here, so whether the "
                "firewall step ran is unknown: try `mcctl ssh` (tailnet); if that fails and the host still "
                "takes public SSH from elsewhere, fix the cause and run `mcctl bootstrap` again.",
            )
        elif now == "open":
            say(
                "fail",
                "bootstrap.yml failed (see above) before the firewall step: public SSH is still open. "
                "Fix the cause and run `mcctl bootstrap` again (a fresh single-use key only if the server "
                "isn't on the tailnet yet).",
            )
        else:
            say(
                "fail",
                "bootstrap.yml failed (see above) after the firewall closed public 22, so the server only "
                "takes SSH over the tailnet now (the play checked that before closing it). "
                "Don't re-run bootstrap: "
                f"check `mcctl ssh -C {repo.root}` and continue with a deploy; for a broken box use the "
                "provider console.",
            )
        return rc
    problems, notes = verify(repo, runner, prober, before)
    for n in notes:
        say("ok", n if out_.rich else f"  {n}")
    if problems:
        st.record(STEP, "failed", inputs=inputs)
        for p in problems:
            say("fail", f"check failed: {p}")
        return 1
    closed = all(n.endswith(": closed") for n in notes if n.startswith("public TCP 22"))
    st.record(STEP, "done", inputs=inputs, outputs={"public_ssh_closed": closed})
    say(
        "ok",
        f"Bootstrap done: Tailscale SSH to {remote.target(cfg)} works and public 22 is closed.\n"
        "Next: set `deploy.enabled: true` in server.yml, commit and push (the push deploys).",
    )
    return 0
