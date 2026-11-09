import email.message
import email.utils
import hashlib
import io
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import fetch_plugins
import fetch_server_jar
import lockfile
import pytest

PURPUR = fetch_server_jar.PURPUR_API


def fake_purpur(monkeypatch, results: dict[str, str]) -> None:
    def get_json(url: str):
        if url == f"{PURPUR}/26.2":
            return {"builds": {"latest": max(results, key=int), "all": sorted(results, key=int)}}
        build = url.rsplit("/", 1)[1]
        return {"result": results[build], "md5": "m" + build}

    monkeypatch.setattr(fetch_server_jar, "get_json", get_json)
    monkeypatch.setattr(fetch_server_jar, "download_verified", lambda url, expected: b"jar")


def test_purpur_walks_back_to_last_successful_build(monkeypatch) -> None:
    fake_purpur(monkeypatch, {"9": "SUCCESS", "10": "SUCCESS", "11": "FAILURE"})
    assert fetch_server_jar.resolve_purpur("26.2")["build"] == 10


def test_no_paper_fallback_without_flag(monkeypatch) -> None:
    fake_purpur(monkeypatch, {"1": "FAILURE"})
    monkeypatch.setattr(fetch_server_jar, "resolve_paper", lambda v: pytest.fail("Paper used"))
    with pytest.raises(LookupError, match="--allow-paper"):
        fetch_server_jar.resolve("26.2")


def test_paper_fallback_with_flag(monkeypatch) -> None:
    fake_purpur(monkeypatch, {"1": "FAILURE"})
    monkeypatch.setattr(fetch_server_jar, "resolve_paper", lambda v: {"project": "paper"})
    monkeypatch.setattr(fetch_server_jar, "java_major_for", lambda v: 25)
    assert fetch_server_jar.resolve("26.2", allow_paper=True)["server"]["project"] == "paper"


def test_modrinth_without_primary_file_uses_first(monkeypatch) -> None:
    files = [
        {"primary": False, "url": "https://x/a.jar", "filename": "a.jar", "hashes": {"sha512": "1"}},
        {"primary": False, "url": "https://x/b.jar", "filename": "b.jar", "hashes": {"sha512": "2"}},
    ]
    version = {"version_type": "release", "date_published": "1", "version_number": "1.0", "id": "v"}
    monkeypatch.setattr(fetch_plugins, "get_json", lambda url: [version | {"files": files}])
    monkeypatch.setattr(fetch_plugins, "download_verified", lambda url, expected: b"x")
    assert fetch_plugins.resolve_modrinth("p", "26.2")["filename"] == "a.jar"


# --- lockfile.http_get: transient failures are retried (v1.5.1) ------------------------------------------------


class FakeOpener:
    """urlopen's shape: each call pops the next outcome (an exception to raise, or bytes to return)."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, req, timeout):
        self.calls.append((req.full_url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(outcome)


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "x", headers, io.BytesIO(b""))


URL = "https://github.com/example/jdk/releases/download/v1/jdk.tar.gz"


def test_http_get_retries_5xx_429_and_network_errors_with_backoff() -> None:
    opener = FakeOpener(
        http_error(504), http_error(429), urllib.error.URLError("reset"), TimeoutError(), b"payload"
    )
    slept: list[float] = []
    assert lockfile.http_get(URL, timeout=7, opener=opener, sleep=slept.append) == b"payload"
    assert slept == [2, 4, 8, 16] and len(opener.calls) == 5
    assert all(c == (URL, 7) for c in opener.calls)


def test_http_get_gives_up_after_five_attempts() -> None:
    opener = FakeOpener(*[http_error(502)] * 5)
    slept: list[float] = []
    with pytest.raises(urllib.error.HTTPError) as err:
        lockfile.http_get(URL, opener=opener, sleep=slept.append)
    assert err.value.code == 502 and len(opener.calls) == 5 and slept == [2, 4, 8, 16]


@pytest.mark.parametrize("code", [404, 403, 400, 410])
def test_http_get_never_retries_4xx(code) -> None:
    opener = FakeOpener(http_error(code), b"never")
    with pytest.raises(urllib.error.HTTPError):
        lockfile.http_get(URL, opener=opener, sleep=lambda s: pytest.fail("slept"))
    assert len(opener.calls) == 1


def test_http_get_honours_retry_after() -> None:
    soon = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    opener = FakeOpener(http_error(503, "11"), http_error(429, soon), http_error(503, "9999"), b"ok")
    slept: list[float] = []
    assert lockfile.http_get(URL, opener=opener, sleep=slept.append) == b"ok"
    assert slept[0] == 11 and 25 <= slept[1] <= 30 and slept[2] == lockfile.MAX_RETRY_AFTER


def test_http_get_still_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="non-https"):
        lockfile.http_get("http://example.com/x", opener=FakeOpener(b"x"))


def test_download_verified_retries_then_checks_the_hash(monkeypatch) -> None:
    data = b"jdk bytes"
    good = {"sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(urllib.request, "urlopen", FakeOpener(http_error(504), data))
    assert lockfile.download_verified(URL, good) == data
    # a retried download that comes back different still fails the hash check
    monkeypatch.setattr(urllib.request, "urlopen", FakeOpener(http_error(500), b"tampered"))
    with pytest.raises(ValueError, match="sha256 mismatch"):
        lockfile.download_verified(URL, good)
