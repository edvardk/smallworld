import fetch_plugins
import fetch_server_jar
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
