import socket

import pytest
import smoke_test


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("mc.example.com", ("mc.example.com", 25565)),
        ("mc.example.com:25566", ("mc.example.com", 25566)),
        ("[2001:db8::1]:25570", ("2001:db8::1", 25570)),
        ("[2001:db8::1]", ("2001:db8::1", 25565)),
        ("2001:db8::1", ("2001:db8::1", 25565)),
    ],
)
def test_split_address(address, expected) -> None:
    assert smoke_test.split_address(address) == expected


def test_resolve_keeps_families_apart(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, family, type_):
        if family == socket.AF_INET6:
            raise socket.gaierror("no AAAA")
        return [(family, type_, 6, "", ("192.0.2.1", port)), (family, type_, 6, "", ("192.0.2.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert smoke_test.resolve("mc.example.com", 25565, socket.AF_INET) == ["192.0.2.1"]
    assert smoke_test.resolve("mc.example.com", 25565, socket.AF_INET6) == []


def test_wait_up_reports_only_failed_targets() -> None:
    def pinger(ip: str, port: int) -> str:
        if ip.startswith("2001"):
            raise OSError("network unreachable")
        return "26.3 | 0/20 players | 5 ms"

    failed = smoke_test.wait_up([("ipv4", "192.0.2.1"), ("ipv6", "2001:db8::1")], 25565, 0, pinger)
    assert failed == {("ipv6", "2001:db8::1"): "network unreachable"}


def test_ipv6_is_opt_in(monkeypatch) -> None:
    seen: list[socket.AddressFamily] = []

    def fake_resolve(host, port, family):
        seen.append(family)
        return ["192.0.2.1"] if family == socket.AF_INET else []

    monkeypatch.setattr(smoke_test, "resolve", fake_resolve)
    monkeypatch.setattr(smoke_test, "ping", lambda ip, port: "up")
    monkeypatch.setattr(smoke_test, "wait_up", lambda targets, port, timeout: {})
    assert smoke_test.main(["mc.example.com"]) == 0
    assert seen == [socket.AF_INET]
    assert smoke_test.main(["mc.example.com", "--ipv6"]) == 1  # no AAAA record
