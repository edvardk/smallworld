from pathlib import Path

import mc_rcon
import pytest


def test_command_roundtrip_strips_formatting(fake_server) -> None:
    srv = fake_server(lambda c: "§6There are §a3§6 of a max of 20 players online")
    with mc_rcon.connect() as r:
        assert r.command("list") == "There are 3 of a max of 20 players online"
        assert mc_rcon.online_players(r) == 3
    assert srv.commands == ["list", "list"]


def test_multi_packet_reply(fake_server) -> None:
    long = "".join(f"line {i}\n" for i in range(2000))  # > 4096 bytes: several packets
    srv = fake_server(lambda c: long if c == "big" else "small")
    with mc_rcon.connect() as r:
        assert r.command("big") == long
        assert r.command("next") == "small"
    assert srv.commands == ["big", "next"]


def test_bad_password(fake_server, tmp_path: Path) -> None:
    fake_server(lambda c: "", password="right")
    props = tmp_path / "server.properties"
    props.write_text(props.read_text().replace("rcon.password=right", "rcon.password=wrong"))
    with pytest.raises(mc_rcon.RconError, match="authentication"), mc_rcon.connect():
        pass


def test_countdown_skipped_when_empty(fake_server, monkeypatch) -> None:
    srv = fake_server(lambda c: "There are 0 of a max of 20 players online")
    monkeypatch.setattr(mc_rcon.time, "sleep", lambda s: None)
    mc_rcon.countdown(60, notify=False)
    assert srv.commands == ["list"]


def test_countdown_marks(fake_server, monkeypatch) -> None:
    srv = fake_server(lambda c: "There are 2 of a max of 20 players online" if c == "list" else "")
    sleeps: list[float] = []
    monkeypatch.setattr(mc_rcon.time, "sleep", sleeps.append)
    mc_rcon.countdown(60, notify=False)
    says = [c for c in srv.commands if c.startswith("say")]
    assert [s.split()[4] for s in says[:3]] == ["60", "30", "10"]
    assert sum(sleeps) == 60


def test_worldborder_idempotent(fake_server) -> None:
    state = {"size": 59999968.0}

    def handler(cmd: str) -> str:
        if cmd.endswith("worldborder get"):
            return f"The world border is currently {state['size']:.0f} block(s) wide"
        if " worldborder set " in cmd:
            state["size"] = float(cmd.rsplit(" ", 1)[1])
        return ""

    fake_server(handler)
    assert mc_rcon.worldborder("minecraft:overworld", 5000) is True
    assert state["size"] == 10000
    assert mc_rcon.worldborder("minecraft:overworld", 5000) is False


def test_stop_when_server_down(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "server.properties").write_text("enable-rcon=true\nrcon.port=1\nrcon.password=x\n")
    monkeypatch.setattr(mc_rcon, "SERVER_DIR", tmp_path)
    assert mc_rcon.stop(None) == 1


def test_read_properties_escapes(tmp_path: Path) -> None:
    p = tmp_path / "server.properties"
    p.write_text("#c\nmotd=A\\u00e9 \\: b\\=c\nrcon.password=abc\nempty=\n")
    props = mc_rcon.read_properties(p)
    assert props == {"motd": "Aé : b=c", "rcon.password": "abc", "empty": ""}


def test_stop_flushes_then_stops(fake_server) -> None:
    srv = fake_server(lambda c: "Stopping the server" if c == "stop" else "Saved the game")
    assert mc_rcon.stop(None) == 0
    assert srv.commands == ["save-all flush", "stop"]


# --- PersistentRcon (v1.0.1: one kept connection instead of one per poll) ----------------------------------


def test_persistent_rcon_keeps_one_connection(fake_server) -> None:
    srv = fake_server(lambda c: f"re {c}")
    client = mc_rcon.PersistentRcon(lambda: mc_rcon.connect(timeout=5.0))
    for _ in range(5):
        assert client.commands(["mspt", "tps"]) == ["re mspt", "re tps"]
    assert client.command("list") == "re list"
    assert srv.connections == 1 and client.connects == 1
    client.close()


def test_persistent_rcon_reconnects_after_a_server_restart(fake_server) -> None:
    srv = fake_server(lambda c: "ok")
    client = mc_rcon.PersistentRcon(lambda: mc_rcon.connect(timeout=5.0))
    assert client.command("list") == "ok"
    srv.drop_clients()  # what a restart does to the kept connection
    assert client.command("list") == "ok"  # the dead connection costs one fresh try, not an error
    assert srv.connections == 2
    client.close()


def test_persistent_rcon_backs_off_while_the_server_is_down(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:  # a free port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    now = [100.0]
    client = mc_rcon.PersistentRcon(
        lambda: mc_rcon.Rcon("127.0.0.1", port, "pw", 1.0), max_backoff=8, clock=lambda: now[0]
    )
    delays = []
    for _ in range(6):
        with pytest.raises(OSError):
            client.command("list")
        delays.append(client.retry_at - now[0])
        with pytest.raises(mc_rcon.RconError, match="backing off"):  # no connect inside the window
            client.command("list")
        now[0] = client.retry_at
    assert delays == [1, 2, 4, 8, 8, 8] and client.connects == 6


def test_persistent_rcon_reports_settings_auth_and_reply_errors(fake_server, tmp_path: Path) -> None:
    missing = mc_rcon.PersistentRcon(lambda: mc_rcon.connect(tmp_path / "nowhere"))
    with pytest.raises(mc_rcon.RconSettingsError):
        missing.command("list")
    fake_server(lambda c: "", password="right")
    wrong = mc_rcon.PersistentRcon(lambda: mc_rcon.Rcon("127.0.0.1", _port(tmp_path), "wrong", 5.0))
    with pytest.raises(mc_rcon.RconAuthError):
        wrong.command("list")

    def broken(c: str) -> str:
        raise OSError("handler died")  # the fake server drops the connection mid-command

    fake_server(broken)
    fresh = mc_rcon.PersistentRcon(lambda: mc_rcon.connect(timeout=2.0))
    with pytest.raises(mc_rcon.RconReplyError):  # logged in, then no reply: alive
        fresh.command("list")
    assert fresh.conn is None


def test_persistent_rcon_is_thread_safe(fake_server) -> None:
    import threading

    srv = fake_server(lambda c: c.upper())
    client = mc_rcon.PersistentRcon(lambda: mc_rcon.connect(timeout=5.0))
    results: list[list[str]] = []

    def work(i: int) -> None:
        for _ in range(20):
            results.append(client.commands([f"a{i}", f"b{i}"]))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 80 and all(r[0][1:] == r[1][1:] and r[0][0] == "A" for r in results)
    assert srv.connections == 1
    client.close()


def _port(tmp_path: Path) -> int:
    return int(mc_rcon.read_properties(tmp_path / "server.properties")["rcon.port"])
