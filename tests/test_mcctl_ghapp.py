"""init's GitHub App flow in the browser (p11-setup, a live run): one loopback server for both phases, the
API poll and the paste fallback when no install callback arrives, WSL browser opening, the App's own view of
itself (JWT, recorded fixtures), and resume without a second App. Nothing real is contacted: the "browser" is a
thread that requests the loopback URLs, GitHub is mcctl.mock or recorded fixtures."""

import html
import io
import json
import re
import threading
import urllib.request

import pytest
import yaml
from conftest import REPO, TEST_SHA, Replay

from mcctl import ghapp, init, mock, state, ui
from mcctl.providers import ProviderError

ANSWERS = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())
APP_REPO = "tests/tests-mc-config"


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - the test's own loopback server
        return resp.read().decode()


def test_one_loopback_server_answers_the_create_and_the_install_redirect() -> None:
    """The manifest's setup_url is fixed at creation: the install redirect must reach a live server (it used to
    go to the closed create-phase port, and init waited 15 minutes for nothing)."""
    lb = ghapp.Loopback()
    seen: dict[str, str] = {}

    def browser_create(url: str) -> None:
        def run() -> None:
            page = _get(url)
            m = json.loads(html.unescape(re.search(r'name="manifest" value="([^"]+)"', page).group(1)))
            state_ = re.search(r"state=([^\"&]+)", page).group(1)
            seen.update(redirect=m["redirect_url"], setup=m["setup_url"])
            _get(f"{m['redirect_url']}?code=abc123&state={state_}")

        threading.Thread(target=run, daemon=True).start()

    try:
        code = ghapp.browser_create(
            ghapp.PANEL, "e2e", APP_REPO, False, lambda s: None, browser_create, timeout=10, server=lb
        )
        assert code == "abc123"
        assert seen["redirect"] == f"{lb.base}/created" and seen["setup"] == f"{lb.base}/installed"

        def browser_install(url: str) -> None:  # GitHub sends the browser to the manifest's setup_url
            threading.Thread(
                target=lambda: (_get(url), _get(f"{seen['setup']}?installation_id=77&setup_action=install")),
                daemon=True,
            ).start()

        inst = ghapp.browser_install(
            "e2e-panel", lambda s: None, lambda: "", None, browser_install, timeout=10, server=lb
        )
        assert inst == "77"
    finally:
        lb.close()


def test_install_without_a_callback_polls_the_api_then_asks() -> None:
    lb = ghapp.Loopback()
    said: list[str] = []
    try:
        polls = iter([None, "88"])
        inst = ghapp.browser_install(
            "x", said.append, lambda: pytest.fail("asked"), lambda: next(polls), lambda u: None,
            quiet=5, timeout=10, server=lb, poll_every=0.05,
        )  # fmt: skip
        assert inst == "88"
        asked: list[int] = []

        def ask() -> str:
            asked.append(1)
            return (
                ""
                if len(asked) == 1
                else "http://127.0.0.1:1/installed?installation_id=99&setup_action=install"
            )

        inst = ghapp.browser_install(
            "x", said.append, ask, None, lambda u: None, quiet=0.1, timeout=10, server=lb, poll_every=0.05
        )
        assert inst == "99" and len(asked) == 2  # an empty answer waits again
        assert any("paste the address bar's URL or the installation id" in s for s in said)
        none = ghapp.browser_install(
            "x",
            said.append,
            lambda: "",
            None,
            lambda u: None,
            quiet=0.05,
            timeout=0.2,
            server=lb,
            poll_every=0.05,
        )
        assert none is None
    finally:
        lb.close()


def test_an_idle_browser_connection_blocks_neither_callbacks_nor_close() -> None:
    """Browsers open preconnect sockets that never send a request (a live run: "Done" shown, setup still hung).
    Callbacks still arrive, and close() returns at once."""
    import socket
    import time

    lb = ghapp.Loopback()
    idle = socket.create_connection(("127.0.0.1", int(lb.base.rsplit(":", 1)[1])))
    try:
        _get(f"{lb.base}/installed?installation_id=5&setup_action=install")
        assert lb.wait("/installed", 2) == {"installation_id": "5", "setup_action": "install"}
        t0 = time.monotonic()
        closer = threading.Thread(target=lb.close, daemon=True)
        closer.start()
        closer.join(3)
        assert not closer.is_alive() and time.monotonic() - t0 < 3
    finally:
        idle.close()


def test_parse_installation() -> None:
    assert ghapp.parse_installation(" 123 ") == "123"
    assert (
        ghapp.parse_installation("http://127.0.0.1:5/installed?installation_id=456&setup_action=install")
        == "456"
    )
    assert ghapp.parse_installation("https://github.com/settings/installations/789") == "789"
    assert ghapp.parse_installation("nope") is None and ghapp.parse_installation("") is None


def test_open_browser_under_wsl_uses_the_windows_side() -> None:
    url = "http://127.0.0.1:1234/"
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)

    def fallback(u: str) -> bool:
        raise AssertionError("the Linux opener (gio) fails under WSL")

    have = {"explorer.exe"}
    assert ghapp.open_browser(
        url, wsl=True, which=lambda n: n if n in have else None, run=run, fallback=fallback
    )
    assert calls == [["explorer.exe", url]]
    have.add("wslview")
    ghapp.open_browser(url, wsl=True, which=lambda n: n if n in have else None, run=run, fallback=fallback)
    assert calls[-1] == ["wslview", url]
    have = {"cmd.exe"}
    ghapp.open_browser(url, wsl=True, which=lambda n: n if n in have else None, run=run, fallback=fallback)
    assert calls[-1] == ["cmd.exe", "/c", "start", "", url]
    assert not ghapp.open_browser(url, wsl=True, which=lambda n: None, run=run, fallback=fallback)
    opened: list[str] = []
    assert ghapp.open_browser(
        url, wsl=False, which=lambda n: None, run=run, fallback=lambda u: opened.append(u) or True
    )
    assert opened == [url]
    assert ghapp.is_wsl(lambda: "Linux version 6.6.87.2-microsoft-standard-WSL2")
    assert not ghapp.is_wsl(lambda: "Linux version 6.12.0 (debian)")


def _sign(pem: str, data: bytes) -> bytes:
    return b"signature"


def test_the_apps_own_view_through_recorded_responses() -> None:
    t = Replay("github_app_jwt_installed.json")
    assert ghapp.app_slug("123456", "PEM", t, _sign, now=1_800_000_000) == "tests-panel"
    inst = ghapp.repo_installation("123456", "PEM", APP_REPO, t, _sign, now=1_800_000_000)
    assert inst["id"] == 4321 and inst["repository_selection"] == "selected"
    assert all(r.headers["Authorization"].endswith(".c2lnbmF0dXJl") for r in t.requests)  # a JWT, not a PAT
    t = Replay("github_app_jwt_not_installed.json")
    assert ghapp.repo_installation("123456", "PEM", APP_REPO, t, _sign) is None
    t = Replay("github_app_jwt_rejected.json")
    assert ghapp.app_slug("123456", "PEM", t, _sign) is None
    with pytest.raises(ProviderError, match="HTTP 401"):
        ghapp.repo_installation("123456", "PEM", APP_REPO, t, _sign)


def run_wizard(root, answers=None, *, deps=None):
    out = io.StringIO()
    talk = ui.UI(answers=ANSWERS if answers is None else answers, mock=True, env={}, out=out)
    deps = deps or mock.deps(root)
    rc = init.run(root, talk, deps, init.Options(mock=True, product_sha=TEST_SHA))
    return rc, out.getvalue(), deps


def test_resume_finds_the_existing_app_and_its_installation(tmp_path) -> None:
    """A run that stopped while installing (a live run: the redirect never came back) leaves the App in .env; the
    re-run finds it as the App (slug, installation) instead of creating a second one."""
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.app_install = lambda slug, say, ask, poll: None  # the browser never came back, nothing installed
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 1 and "isn't installed on mcctl-e2e/e2e-mc-config (checked as the App)" in out
    assert state.load(root).get("github")["status"] == "failed"
    apps = dict(deps.cloud.gh_apps)
    assert len(apps) == 1  # the panel App exists, not installed
    deps2 = mock.deps(root, cloud=deps.cloud)
    rc, out, _ = run_wizard(root, deps=deps2)
    assert rc == 0, out
    assert "e2e-panel (id 909) exists already: not creating another one" in out
    conversions = [r for r in deps2.cloud.requests if "/conversions" in r.url]
    assert [r.url.split("/")[-2] for r in conversions] == ["mock-code-panel-e2e", "mock-code-updater-e2e"]
    assert len(deps2.cloud.gh_apps) == 2  # panel (from the first run) + updater, no second panel App
    assert "installed on mcctl-e2e/e2e-mc-config only (checked as the App)" in out


def test_a_private_app_is_invisible_by_slug_but_verified_as_the_app() -> None:
    """a live re-run (2026-11-02): `GET /apps/{slug}` with the admin PAT answers 404 for the private App that
    exists; `GET /app` with a JWT signed by the saved key sees it, with its permissions."""
    old = ghapp.check_app("admin-secret", "tests-panel", ghapp.PANEL, Replay("github_app_private_404.json"))
    assert old.level == "fail" and "isn't visible (HTTP 404)" in old.messages[0]
    t = Replay("github_app_jwt_installed.json")
    check = ghapp.verify_app("123456", "PEM", ghapp.PANEL, t, _sign, now=1_800_000_000)
    assert check.level == "ok" and check.facts["slug"] == "tests-panel", check.messages
    assert t.urls() == ["https://api.github.com/app"]  # never /apps/{slug}
    assert t.requests[0].headers["Authorization"].endswith(".c2lnbmF0dXJl")  # a JWT, not the PAT
    lacks = ghapp.verify_app("123456", "PEM", ghapp.UPDATER, Replay("github_app_jwt_installed.json"), _sign)
    assert lacks.level == "fail" and "lacks {'workflows': 'write'}" in lacks.messages[0]
    with pytest.raises(ghapp.KeyRejected, match="HTTP 401") as e:
        ghapp.verify_app("123456", "PEM", ghapp.PANEL, Replay("github_app_jwt_rejected.json"), _sign)
    assert "Developer settings > GitHub Apps" in e.value.fix and "TF_VAR_panel_github_app_id" in e.value.fix
    with pytest.raises(ghapp.KeyRejected, match="belongs to GitHub App tests-panel"):
        ghapp.verify_app("999", "PEM", ghapp.PANEL, Replay("github_app_jwt_installed.json"), _sign)


def test_the_installations_repositories_through_recorded_responses() -> None:
    t = Replay("github_app_installation_repos.json")
    assert ghapp.installation_repositories("123456", "PEM", 4321, t, _sign) == (1, [APP_REPO])
    assert t.requests[0].headers["Authorization"].endswith(".c2lnbmF0dXJl")  # the token: as the App
    assert json.loads(t.requests[0].body) == {"permissions": {"metadata": "read"}}  # no repo narrowing
    assert t.requests[1].headers["Authorization"] == "Bearer ghs_fixture_installation_token"
    t = Replay("github_app_installation_repos_extra.json")
    assert ghapp.installation_repositories("123456", "PEM", 4321, t, _sign) == (
        2,
        [APP_REPO, "tests/dotfiles"],
    )


def test_rerun_with_existing_private_apps_checks_them_as_the_app(tmp_path) -> None:
    """The live case: both Apps created and installed earlier (slugs in state.json), step 6 re-run. Nothing
    asks GitHub for /apps/{slug} (404 for a private App); the check is as the App."""
    root = tmp_path / "c"
    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    seen = len(deps.cloud.requests)
    deps2 = mock.deps(root, cloud=deps.cloud)
    talk = ui.UI(answers=ANSWERS, mock=True, env={}, out=(buf := io.StringIO()))
    rc = init.run(root, talk, deps2, init.Options(mock=True, product_sha=TEST_SHA, from_step="github"))
    out = buf.getvalue()
    assert rc == 0, out
    assert "panel App e2e-panel (id 909) exists already: not creating another one" in out
    assert "updater App e2e-updater (id 911) exists already" in out
    assert "isn't visible" not in out and "installed on mcctl-e2e/e2e-mc-config only" in out
    rerun = deps2.cloud.requests[seen:]
    assert any(r.url.endswith("/installation/repositories?per_page=100") for r in rerun)
    assert not [r for r in rerun if "/apps/" in r.url or "/conversions" in r.url]
    assert len(deps2.cloud.gh_apps) == 2


def test_rerun_with_a_key_github_rejects_says_how_to_start_over(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    del deps.cloud.gh_apps[909]  # the panel App was deleted on GitHub (or the saved key isn't its key)
    deps2 = mock.deps(root, cloud=deps.cloud)
    talk = ui.UI(answers=ANSWERS, mock=True, env={}, out=(buf := io.StringIO()))
    rc = init.run(root, talk, deps2, init.Options(mock=True, product_sha=TEST_SHA, from_step="github"))
    out = buf.getvalue()
    assert rc == 1
    assert "doesn't accept the saved private key for the panel App (id 909; HTTP 401" in out
    assert "Developer settings > GitHub Apps" in out and "TF_VAR_panel_github_app_private_key" in out
    assert len(deps2.cloud.gh_apps) == 1  # no second App made behind the admin's back


def test_an_app_installed_on_other_repositories_too_is_refused(tmp_path) -> None:
    """repository_selection "selected" isn't enough: the config repo must be the only one."""
    root = tmp_path / "c"
    deps = mock.deps(root)
    real = deps.cloud._github

    def extra(req, path):
        resp = real(req, path)
        if path.startswith("/installation/repositories"):
            body = json.loads(resp.body)
            body["repositories"].append({"full_name": "mcctl-e2e/dotfiles"})
            body["total_count"] = 2
            resp = type(resp)(200, dict(resp.headers), json.dumps(body).encode())
        return resp

    deps.cloud._github = extra
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 1 and "is installed on 2 repositories (mcctl-e2e/dotfiles besides" in out


def test_an_app_installed_on_all_repositories_is_refused(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.app_install = lambda slug, say, ask, poll: deps.cloud.install_app(slug, "all")
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 1 and "is installed on ALL your repositories" in out and "Only select repositories" in out


def test_without_openssl_init_falls_back_to_asking(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)

    def no_openssl(pem: str, data: bytes) -> bytes:
        raise FileNotFoundError("openssl")

    deps.app_sign = no_openssl
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 0, out
    assert "couldn't ask GitHub as the App" in out and "checked as the App" not in out
