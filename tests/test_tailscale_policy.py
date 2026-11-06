"""P7: the tailnet policy (terraform/tailscale-policy.json.tftpl) and its ACL tests.

Tailscale evaluates `tests` and `sshTests` on every apply and refuses a policy that fails them. This file runs
the same tests here against a deliberately small model of the grant and SSH semantics the policy uses (user
or tag sources, `*` or tag destinations, `*` / `tcp:PORT` capabilities, accept-only SSH rules), so a policy
change that breaks them fails in CI instead of at `terraform apply`. Anything outside that subset fails the
test instead of being guessed at.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from conftest import REPO

TEMPLATE = REPO / "terraform" / "tailscale-policy.json.tftpl"
ADMIN = "alex@github"


def policy(admin: str = ADMIN) -> dict[str, Any]:
    text = TEMPLATE.read_text()
    # The only template expression is ${admin}, already JSON-encoded by Terraform (jsonencode).
    assert set(re.findall(r"\$\{([^}]*)\}", text)) == {"admin"}
    return json.loads(text.replace("${admin}", json.dumps(admin)))


def _src_matches(selector: str, src: str) -> bool:
    if selector == "*":
        return True
    assert selector.startswith("tag:") or "@" in selector, f"selector outside the modelled subset: {selector}"
    return selector == src


def _dst_matches(selector: str, dst: str) -> bool:
    if selector == "*":
        return True
    assert selector.startswith("tag:"), f"destination outside the modelled subset: {selector}"
    return selector == dst


def _ip_matches(cap: str, port: int) -> bool:
    if cap == "*":
        return True
    m = re.fullmatch(r"tcp:(\d+)", cap)
    assert m, f"ip capability outside the modelled subset: {cap}"
    return int(m.group(1)) == port


def allowed(pol: dict[str, Any], src: str, dst: str, port: int) -> bool:
    return any(
        any(_src_matches(s, src) for s in g["src"])
        and any(_dst_matches(d, dst) for d in g["dst"])
        and any(_ip_matches(c, port) for c in g["ip"])
        for g in pol["grants"]
    )


def ssh_action(pol: dict[str, Any], src: str, dst: str, user: str) -> str:
    for rule in pol["ssh"]:
        assert rule["action"] in ("accept", "check"), rule
        if (
            any(_src_matches(s, src) for s in rule["src"])
            and any(_dst_matches(d, dst) for d in rule["dst"])
            and user in rule["users"]
        ):
            return rule["action"]
    return "deny"


def _split(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host, int(port)


def test_policy_is_json_and_terraform_uses_it() -> None:
    pol = policy()
    assert set(pol) == {"tagOwners", "grants", "ssh", "tests", "sshTests"}
    tf = (REPO / "terraform/tailscale.tf").read_text()
    assert 'templatefile("${path.module}/tailscale-policy.json.tftpl", {' in tf
    assert "admin = jsonencode(var.tailscale_admin_login)" in tf
    # A login with JSON metacharacters stays one string (jsonencode on the Terraform side, dumps here).
    assert policy('a"b@github')["tagOwners"]["tag:ci"] == ['a"b@github', "tag:mc-terraform"]


@pytest.mark.parametrize("case", policy()["tests"], ids=lambda c: c["src"])
def test_acl_tests_hold(case: dict[str, Any]) -> None:
    pol = policy()
    assert set(case) <= {"src", "accept", "deny"}
    for target in case.get("accept", []):
        assert allowed(pol, case["src"], *_split(target)), f"{case['src']} -> {target} should be accepted"
    for target in case.get("deny", []):
        assert not allowed(pol, case["src"], *_split(target)), f"{case['src']} -> {target} should be denied"


@pytest.mark.parametrize("case", policy()["sshTests"], ids=lambda c: c["src"])
def test_ssh_tests_hold(case: dict[str, Any]) -> None:
    pol = policy()
    for dst in case["dst"]:
        for user in case.get("accept", []):
            assert ssh_action(pol, case["src"], dst, user) == "accept"
        for user in case.get("check", []):
            assert ssh_action(pol, case["src"], dst, user) == "check"
        for user in case.get("deny", []):
            assert ssh_action(pol, case["src"], dst, user) == "deny"


def test_design_requirements_are_tested() -> None:
    """§5/§7: tag:ci reaches only SSH; it is denied the game port, RCON and the onserver panel/Grafana."""
    tests = {t["src"]: t for t in policy()["tests"]}
    assert tests["tag:ci"]["accept"] == ["tag:mc-server:22"]
    assert {"tag:mc-server:25565", "tag:mc-server:25575", "tag:mc-server:443", "tag:mc-server:8443"} <= set(
        tests["tag:ci"]["deny"]
    )
    ssh = {t["src"]: t for t in policy()["sshTests"]}
    assert ssh["tag:ci"]["deny"] == ["root"] and ssh[ADMIN]["deny"] == ["root"]
    # Security review (LOW): the admin's SSH is `check` (a fresh tailnet login at most every 12 h), so a stolen
    # unlocked laptop or a hijacked node key alone isn't a shell on the server; CI (no browser) stays accept.
    assert ssh[ADMIN]["check"] == ["deploy"] and "accept" not in ssh[ADMIN]
    assert ssh["tag:ci"]["accept"] == ["deploy"]


def test_admin_ssh_is_check_with_a_period() -> None:
    pol = policy()
    (admin_rule,) = [r for r in pol["ssh"] if ADMIN in r["src"]]
    assert admin_rule["action"] == "check" and admin_rule["checkPeriod"] == "12h"
    assert all(r["action"] == "accept" for r in pol["ssh"] if r["src"] == ["tag:ci"])


def test_model_catches_a_broken_policy() -> None:
    """The evaluator isn't vacuous: widening tag:ci's grant breaks its deny tests."""
    pol = policy()
    pol["grants"][1]["ip"] = ["*"]
    assert allowed(pol, "tag:ci", "tag:mc-server", 25575)


def test_tags_agree_with_the_keys_and_ci() -> None:
    owners = set(policy()["tagOwners"])
    tf = (REPO / "terraform/tailscale.tf").read_text()
    for tags in re.findall(r"tags\s*=\s*\[([^\]]*)\]", tf):
        assert set(re.findall(r'"(tag:[^"]+)"', tags)) <= owners
    wf = (REPO / ".github/workflows/server.yml").read_text()
    assert set(re.findall(r"tags: (tag:[\w-]+)", wf)) <= owners


def test_terraforms_oauth_client_owns_every_tag_terraform_requests() -> None:
    """First live apply: Tailscale lets a trust credential request only ALL of its tags or tags owned by its
    tags. The OAuth client (mcctl/providers/tailscale.py OAUTH_TAGS) has one owner tag; every tag Terraform asks
    for (server key, CI identity) must be owned by it, and no grant or SSH rule mentions the owner tag."""
    from mcctl.providers import tailscale

    pol = policy()
    owners = pol["tagOwners"]
    assert len(tailscale.OAUTH_TAGS) == 1
    (client_tag,) = tailscale.OAUTH_TAGS
    assert owners[client_tag] == [ADMIN]
    tf = (REPO / "terraform/tailscale.tf").read_text()
    requested = {
        t for tags in re.findall(r"tags\s*=\s*\[([^\]]*)\]", tf) for t in re.findall(r'"(tag:[^"]+)"', tags)
    }
    assert requested == {"tag:mc-server", "tag:ci"}
    for tag in requested:
        assert client_tag in owners[tag], tag
    rules = json.dumps([pol["grants"], pol["ssh"]])
    assert client_tag not in rules
