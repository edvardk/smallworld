"""v1.3: the "newer Smallworld release" notice (mcctl/notice.py) on interactive commands and the home screen:
minor/major only, GitHub's releases/latest through recorded responses (never the network), a day's cache, silent
on failure, never in CI / non-terminal / --plain paths."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from conftest import Replay

from mcctl import cli, configrepo, home, http, notice, ui

REPO_NAME = "edvardk/smallworld"
URL = f"https://api.github.com/repos/{REPO_NAME}/releases/latest"


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def pin(version: str) -> configrepo.Pin:
    return configrepo.Pin(REPO_NAME, "a" * 40, version)


@pytest.mark.parametrize(
    ("latest", "pinned", "want"),
    [("v1.4.0", "v1.3.2", True), ("v2.0.0", "v1.9.9", True), ("v1.3.5", "v1.3.2", False),
     ("v1.3.0", "v1.3.0", False), ("v1.2.9", "v1.3.0", False), ("v1.4.0", "v1.3.2-4-gabcdef1", True),
     ("v1.4.0", None, False), (None, "v1.3.0", False), ("v1.4.0", "main", False)],
)  # fmt: skip
def test_only_a_newer_minor_or_major_counts(latest: str | None, pinned: str | None, want: bool) -> None:
    assert notice.minor_or_major_newer(latest, pinned) is want


def test_lookup_is_anonymous_short_and_cached_for_a_day(tmp_path: Path) -> None:
    replay, clock = Replay("github_release_latest.json"), [1000.0]
    path = tmp_path / "cache.json"

    def ask() -> str | None:
        return notice.cached_latest(REPO_NAME, transport=replay, path=path, clock=lambda: clock[0])

    assert ask() == "v9.0.0"
    req = replay.requests[0]
    assert req.url == URL and req.timeout == notice.TIMEOUT and "Authorization" not in req.headers
    clock[0] += notice.TTL_S - 1
    assert ask() == "v9.0.0" and len(replay.requests) == 1  # from the cache
    clock[0] += 2
    assert ask() == "v9.0.0" and len(replay.requests) == 2  # a day later: asked again


def test_failures_are_silent_and_cached(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    replay = Replay("github_release_latest_404.json")
    assert notice.cached_latest(REPO_NAME, transport=replay, path=path, clock=lambda: 5.0) is None
    assert notice.cached_latest(REPO_NAME, transport=replay, path=path, clock=lambda: 6.0) is None
    assert len(replay.requests) == 1  # a private product isn't asked again before the TTL

    def down(req: http.Request) -> http.Response:
        raise http.HttpError("api.github.com/...: timed out")

    assert notice.cached_latest("o/other", transport=down, path=path, clock=lambda: 7.0) is None
    path.write_text("not json")
    assert notice.cached_latest("o/other", transport=down, path=path, clock=lambda: 8.0) is None
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:  # an unwritable cache only means asking again
        assert notice.cached_latest(REPO_NAME, transport=Replay("github_release_latest.json"),
                                    path=ro / "x" / "c.json") == "v9.0.0"  # fmt: skip
    finally:
        ro.chmod(0o700)


def test_the_message_names_the_fix() -> None:
    text = notice.for_pin(pin("v1.3.2"), lambda repo: "v1.4.0")
    assert text and "v1.4.0" in text and "v1.3.2" in text and "`mcctl upgrade`" in text
    assert notice.for_pin(pin("v1.3.2"), lambda repo: "v1.3.9") is None  # a patch: the weekly update PR
    assert notice.for_pin(pin(""), lambda repo: pytest.fail("asked")) is None
    assert notice.for_pin(None) is None


def test_wanted_only_for_a_person_at_a_terminal() -> None:
    env = {"TERM": "xterm-256color"}
    assert notice.wanted("status", env, FakeTTY(), FakeTTY())
    assert not notice.wanted("status", env, io.StringIO(), FakeTTY())
    assert not notice.wanted("status", env, FakeTTY(), io.StringIO())
    for extra in ({"CI": "true"}, {"NO_COLOR": "1"}, {ui.PLAIN_ENV: "1"}, {"TERM": "dumb"},
                  {notice.OPT_OUT_ENV: "1"}):  # fmt: skip
        assert not notice.wanted("status", env | extra, FakeTTY(), FakeTTY()), extra
    for quiet in ("update-prs", "pr-gate", "upgrade", "render"):
        assert not notice.wanted(quiet, env, FakeTTY(), FakeTTY())


def test_a_command_shows_it_after_its_output_and_children_stay_quiet(
    monkeypatch: pytest.MonkeyPatch, config_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(notice.OPT_OUT_ENV, "")  # on again (conftest turns it off for every test)
    monkeypatch.setattr(notice, "wanted", lambda command: not os.environ.get(notice.OPT_OUT_ENV))
    asked: list[str] = []
    monkeypatch.setattr(notice, "cached_latest", lambda repo: asked.append(repo) or "v1.0.0")
    assert cli.main(["validate", "-C", str(config_repo)]) == 0
    out = capsys.readouterr()
    assert "valid (product" in out.out and asked == [REPO_NAME]  # the pin is v0.0.0
    assert "Smallworld v1.0.0 is out" in out.err and "`mcctl upgrade`" in out.err
    assert os.environ[notice.OPT_OUT_ENV] == "1"  # a re-executed pinned mcctl stays quiet
    assert cli.main(["validate", "-C", str(config_repo)]) == 0
    assert "is out" not in capsys.readouterr().err and asked == [REPO_NAME]


def test_home_header_has_an_update_row(config_repo: Path) -> None:
    out = ui.Out(io.StringIO(), rich=False)
    probe = home.StatusProbe(lambda cfg: home.remote.Status(reachable=False, error="offline"))
    rows = dict(
        home.header_rows(
            config_repo, "-C", probe, out, update=lambda p: notice.for_pin(p, lambda r: "v2.0.0")
        )
    )
    assert "Smallworld v2.0.0 is out" in rows["Update"]
    rows = dict(home.header_rows(config_repo, "-C", probe, out, update=lambda p: None))
    assert "Update" not in rows
    assert "Update" not in dict(home.header_rows(config_repo, "-C", probe, out))  # tests: no lookup at all
