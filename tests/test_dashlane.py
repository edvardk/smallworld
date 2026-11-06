"""P14: Dashlane as a secret source. `.env` values that are `dl://` references are resolved through a fake `dcli`
(never the real one): in memory and child environments only, never on argv, in messages, logs or state.json;
doctor checks each reference; setup's read-back (dcli can't write) decides between a reference and the value; the
restic password's Secure Note; `mcctl secrets dashlane [--convert]`."""

import io
import json
import subprocess

import pytest
from conftest import REPO, make_config_repo

from mcctl import dashlane, doctor, envfile, init, integrations, state, tf, ui

SECRET = "cf-admin-token-value-0123456789"


class FakeDcli:
    """`dcli read REF` from a dict (trailing newline like a CLI), `dcli accounts whoami`, `dcli sync`."""

    def __init__(self, items: dict[str, str] | None = None, login: str | None = "sam@example.com") -> None:
        self.items = dict(items or {})
        self.login = login
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["accounts", "whoami"]:
            return subprocess.CompletedProcess(argv, 0 if self.login else 1, f"{self.login or ''}\n", "")
        if argv[1:2] == ["sync"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:2] == ["read"] and argv[2] in self.items:
            return subprocess.CompletedProcess(argv, 0, self.items[argv[2]] + "\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "no secret found")

    def resolver(self, installed: bool = True) -> dashlane.Resolver:
        return dashlane.Resolver(
            run=self,
            status=self,
            terminal=self,
            interactive=lambda: False,
            which=lambda n: "/usr/bin/dcli" if installed else None,
        )


def argv_text(fake: FakeDcli) -> str:
    return json.dumps(fake.calls)


def test_a_master_password_prompt_is_never_inside_a_captured_read() -> None:
    """L1: dcli may print its master password prompt to stdout, which `dcli read` captures (invisible, hangs).
    On a terminal the first read is preceded by a pass-through `dcli sync` (the prompt shows there); off a
    terminal sync runs quietly; a dcli that isn't logged in stops with the fix before any read."""
    fake, seen = FakeDcli({"dl://a/password": SECRET}), []

    def terminal(argv):
        seen.append(list(argv))
        return fake(argv)

    r = dashlane.Resolver(
        run=fake, status=fake, terminal=terminal, interactive=lambda: True, which=lambda n: "/usr/bin/dcli"
    )
    assert r.read("dl://a/password") == SECRET
    assert seen == [["dcli", "sync"]] and fake.calls[-1] == ["dcli", "read", "dl://a/password"]
    assert fake.calls.index(["dcli", "sync"]) < fake.calls.index(["dcli", "read", "dl://a/password"])
    r._cache.clear()
    r.read("dl://a/password")
    assert seen == [["dcli", "sync"]], "once per process"
    quiet = FakeDcli({"dl://a/password": SECRET})
    r = quiet.resolver()  # not a terminal: sync through the quiet runner (stdin closed)
    r.read("dl://a/password")
    assert ["dcli", "sync"] in quiet.calls
    nobody = FakeDcli({"dl://a/password": SECRET}, login=None)
    with pytest.raises(dashlane.DashlaneError, match=r"isn't logged in: run `dcli sync`"):
        nobody.resolver().read("dl://a/password", "CLOUDFLARE_API_TOKEN")
    assert ["dcli", "read", "dl://a/password"] not in nobody.calls
    # the real runners: reads capture only stdout (stdin closed off a terminal), sync inherits everything
    src = (REPO / "mcctl/dashlane.py").read_text()
    assert "stdin = None if _interactive() else subprocess.DEVNULL" in src
    assert "subprocess.run(list(argv), check=False, timeout=600)" in src


def test_read_resolves_strips_the_cli_newline_caches_and_never_shows_values() -> None:
    fake = FakeDcli({"dl://mcctl-lh-cloudflare/password": SECRET, "dl://multi/password": "a\nb"})
    r = fake.resolver()
    assert r.read("dl://mcctl-lh-cloudflare/password", "CLOUDFLARE_API_TOKEN") == SECRET
    assert r.read("dl://mcctl-lh-cloudflare/password") == SECRET
    assert fake.calls == [
        ["dcli", "accounts", "whoami"],  # set up? (before the first read only)
        ["dcli", "sync"],
        ["dcli", "read", "dl://mcctl-lh-cloudflare/password"],
    ], "read once per process"
    with pytest.raises(dashlane.DashlaneError) as e:
        r.read("dl://missing/password", "GITHUB_TOKEN")
    assert "GITHUB_TOKEN" in str(e.value) and "dl://missing/password" in str(e.value)
    with pytest.raises(dashlane.DashlaneError, match="multi-line"):
        r.read("dl://multi/password", "X")
    with pytest.raises(dashlane.DashlaneError, match="isn't a Dashlane reference"):
        r.read("dl://has space/password", "X")
    with pytest.raises(dashlane.DashlaneError, match="isn't installed"):
        FakeDcli().resolver(installed=False).read("dl://x/password", "X")
    assert isinstance(dashlane.DashlaneError("x"), envfile.EnvFileError)  # every .env error path reports it
    assert SECRET not in argv_text(fake)
    assert fake.resolver().logged_in() == "sam@example.com"
    assert FakeDcli(login=None).resolver().logged_in() is None
    assert FakeDcli().resolver(installed=False).logged_in() is None
    # the reference syntax from Dashlane's docs: id or title, optional field, optional transformer
    for ok in (
        "dl://QD145B53-B987-4CFE-9408-F25803DC47A4",
        "dl://title/password",
        "dl://t/f?otp",
        "dl://t/f?json=$.a",
    ):
        assert dashlane.REF.fullmatch(ok), ok


def test_merged_env_resolves_references_from_env_and_shell(tmp_path) -> None:
    root = tmp_path
    envfile.write(
        root,
        {
            "CLOUDFLARE_API_TOKEN": "dl://cf/password",
            "PLAIN": "x",
            dashlane.RESTIC_REF_KEY: "dl://restic/content",
        },
    )
    fake = FakeDcli(
        {"dl://cf/password": SECRET, "dl://gh/password": "gh-secret", "dl://restic/content": "restic-pw"}
    )
    env = envfile.merged(root, {"GITHUB_TOKEN": "dl://gh/password", "PATH": "/bin"}, fake.resolver().resolve)
    assert (
        env["CLOUDFLARE_API_TOKEN"] == SECRET and env["GITHUB_TOKEN"] == "gh-secret" and env["PLAIN"] == "x"
    )
    # the restic note's reference stays a reference: it never reaches a child process (doctor checks it)
    assert env[dashlane.RESTIC_REF_KEY] == "dl://restic/content"
    assert ["dcli", "read", "dl://restic/content"] not in fake.calls
    # .env itself keeps the references (nothing resolved is written anywhere)
    assert "dl://cf/password" in (root / ".env").read_text() and SECRET not in (root / ".env").read_text()
    with pytest.raises(dashlane.DashlaneError):
        envfile.merged(root, {"GITHUB_TOKEN": "dl://nope/password"}, fake.resolver().resolve)
    lenient = envfile.merged(root, {"GITHUB_TOKEN": "dl://nope/password"}, fake.resolver().resolve_lenient)
    assert "GITHUB_TOKEN" not in lenient and lenient["CLOUDFLARE_API_TOKEN"] == SECRET


def test_terraform_gets_the_resolved_value_in_its_environment(tmp_path, monkeypatch) -> None:
    envfile.write(tmp_path, {"CLOUDFLARE_API_TOKEN": "dl://cf/password"})
    fake = FakeDcli({"dl://cf/password": SECRET})
    monkeypatch.setattr(dashlane, "DEFAULT", fake.resolver())
    env = tf.environment(tmp_path, {"PATH": "/bin"})
    assert env["CLOUDFLARE_API_TOKEN"] == SECRET  # a child process environment, never argv
    monkeypatch.setattr(dashlane, "DEFAULT", FakeDcli().resolver())
    with pytest.raises(tf.TfError) as e:
        tf.environment(tmp_path, {"PATH": "/bin"})
    assert "CLOUDFLARE_API_TOKEN" in str(e.value) and SECRET not in str(e.value)


def test_doctor_checks_every_reference(tmp_path, monkeypatch) -> None:
    from test_mcctl_doctor import SECRETS, deps, levels

    for k in [*SECRETS, "TAILSCALE_API_KEY", "TF_VAR_discord_webhook_url", "GH_TOKEN"]:
        monkeypatch.delenv(k, raising=False)
    root = make_config_repo(tmp_path / "config", renderable=True)
    envfile.write(
        root, SECRETS | {"GITHUB_TOKEN": "dl://gh/password", "CLOUDFLARE_API_TOKEN": "dl://cf/password"}
    )
    fake = FakeDcli({"dl://cf/password": SECRETS["CLOUDFLARE_API_TOKEN"]})
    d = deps()
    d.dashlane = fake.resolver()
    report = doctor.run(root, deps=d)
    got = levels(report)
    assert got["dashlane CLOUDFLARE_API_TOKEN"] == "ok" and got["dashlane GITHUB_TOKEN"] == "fail"
    assert got["cloudflare token"] == "ok", report.format()  # the resolved value was checked live
    assert got["github admin token"] == "fail"  # unresolved: as if unset
    assert SECRETS["CLOUDFLARE_API_TOKEN"] not in report.format()
    d.dashlane = FakeDcli().resolver(installed=False)
    assert "dcli isn't installed" in doctor.run(root, deps=d).format()


def ctx_for(tmp_path, fake: FakeDcli, answers: dict, store: bool | None = True) -> init.Ctx:
    root = make_config_repo(tmp_path / "config", renderable=True)
    talk = ui.UI(answers=answers, mock=True, env={}, out=io.StringIO())
    c = init.Ctx(
        root=root, ui=talk, deps=init.Deps(dashlane=fake.resolver(), credentials_env={}), opts=init.Options()
    )
    c.dashlane_store = store
    return c


def test_setup_writes_a_reference_only_when_it_reads_back_the_same_value(tmp_path) -> None:
    title = "mcctl-example-cloudflare-api-token"
    fake = FakeDcli({f"dl://{title}/password": SECRET, "dl://mcctl-example-github-token/password": "other"})
    c = ctx_for(tmp_path, fake, {integrations.ITEM_SAVED: True})
    c.save_env(CLOUDFLARE_API_TOKEN=SECRET, GITHUB_TOKEN="gh-value", AWS_ENDPOINT_URL_S3="https://x.example")
    env = envfile.parse((c.root / ".env").read_text())
    assert env["CLOUDFLARE_API_TOKEN"] == f"dl://{title}/password"
    assert env["GITHUB_TOKEN"] == "gh-value", "a different value in Dashlane: the value stays"
    assert env["AWS_ENDPOINT_URL_S3"] == "https://x.example", "not a credential: never offered"
    assert ["dcli", "sync"] in fake.calls  # a new item reaches the CLI on its next sync
    out = c.ui.out.getvalue()
    assert (
        f"add a Password item titled {title}" in out and SECRET not in out and SECRET not in argv_text(fake)
    )
    # not chosen: values as before, dcli untouched
    fake2 = FakeDcli()
    c2 = ctx_for(tmp_path / "2", fake2, {}, store=False)
    c2.save_env(CLOUDFLARE_API_TOKEN=SECRET)
    assert (
        envfile.parse((c2.root / ".env").read_text())["CLOUDFLARE_API_TOKEN"] == SECRET and fake2.calls == []
    )


def test_preflight_offers_dashlane_once(tmp_path) -> None:
    fake = FakeDcli()
    c = ctx_for(tmp_path, fake, {"dashlane_store": True}, store=None)
    assert integrations.preflight(c) == {"dashlane_store": True} and c.dashlane_store is True
    state.load(c.root).record("preflight", "done", outputs={"os": "debian", "dashlane_store": False})
    c2 = ctx_for(tmp_path / "2", fake, {}, store=None)  # no answer needed: the recorded choice is kept
    c2.root = c.root
    c2._state = None
    assert integrations.preflight(c2) == {"dashlane_store": False}
    none = ctx_for(tmp_path / "3", FakeDcli(login=None), {}, store=None)
    assert integrations.preflight(none) == {}


def test_restic_password_secure_note(tmp_path) -> None:
    fake = FakeDcli({"dl://mcctl-example-restic/content": "restic-pw-123"})
    c = ctx_for(tmp_path, fake, {"restic_dashlane": True, integrations.ITEM_SAVED: True})
    assert integrations.save_restic(c, "restic-pw-123") is True
    env = envfile.parse((c.root / ".env").read_text())
    assert env[dashlane.RESTIC_REF_KEY] == "dl://mcctl-example-restic/content"
    out = c.ui.out.getvalue()
    assert out.count("restic-pw-123") == 1, "shown exactly once"
    assert "Secure Note titled mcctl-example-restic" in out
    st = c.root / state.STATE_FILE
    assert not st.exists() or "restic-pw-123" not in st.read_text()
    # a note that doesn't read back: not stored (setup then asks to confirm it was stored, as before)
    bad = ctx_for(
        tmp_path / "2",
        FakeDcli(),
        {"restic_dashlane": True, integrations.ITEM_SAVED: True, "restic_dashlane_ref": ""},
    )
    assert integrations.save_restic(bad, "restic-pw-123") is False


def test_secrets_dashlane_lists_and_converts(tmp_path) -> None:
    fake = FakeDcli(
        {
            "dl://mcctl-example-cloudflare-api-token/password": SECRET,
            "dl://mcctl-example-github-token/password": "a-different-value",
        }
    )
    c = ctx_for(tmp_path, fake, {})
    envfile.write(
        c.root,
        {"CLOUDFLARE_API_TOKEN": SECRET, "GITHUB_TOKEN": "gh-value", "TAILSCALE_OAUTH_CLIENT_ID": "k1"},
    )
    assert integrations.secrets_dashlane(c, convert=False) == 0
    out = c.ui.out.getvalue()
    for title in (
        "mcctl-example-cloudflare-api-token",
        "mcctl-example-github-token",
        "mcctl-example-tailscale-oauth-client-id",
        "mcctl-example-restic",
    ):
        assert title in out
    assert SECRET not in out and fake.calls == []
    assert integrations.secrets_dashlane(c, convert=True) == 0
    env = envfile.parse((c.root / ".env").read_text())
    assert env["CLOUDFLARE_API_TOKEN"] == "dl://mcctl-example-cloudflare-api-token/password"
    assert env["GITHUB_TOKEN"] == "gh-value" and env["TAILSCALE_OAUTH_CLIENT_ID"] == "k1"
    assert SECRET not in c.ui.out.getvalue() and SECRET not in argv_text(fake)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_show_restic_prints_once_to_a_terminal_only_with_references_resolved(tmp_path, monkeypatch) -> None:
    """M2: hand-run hints used `(set -a; . .env; terraform output -raw restic_password)`, which exports dl://
    references, not values. `mcctl secrets show-restic` reads it through Terraform with .env resolved by the same
    resolver as every command, and prints it only to a terminal."""
    root = make_config_repo(tmp_path / "config", renderable=True)
    envfile.write(root, {"AWS_SECRET_ACCESS_KEY": "dl://state/password"})
    monkeypatch.setattr(dashlane, "DEFAULT", FakeDcli({"dl://state/password": SECRET}).resolver())
    seen: list[dict[str, str]] = []

    class FakeTf:
        def outputs(self) -> dict[str, str]:
            return {"restic_password": "restic-pw-value"}

    def terraform(repo, product_root, extra):
        seen.append(tf.environment(repo.root, {"PATH": "/bin"}))  # what `_real_terraform` gives Terraform
        return FakeTf(), root

    ctx = init.Ctx(root=root, ui=ui.UI(answers={}), deps=init.Deps(terraform=terraform), opts=init.Options())
    piped = io.StringIO()
    with pytest.raises(init.StepError, match="isn't a terminal"):
        integrations.show_restic(ctx, piped)
    assert piped.getvalue() == "" and not seen, "refused before Terraform is even asked"
    tty = _Tty()
    assert integrations.show_restic(ctx, tty) == 0
    assert tty.getvalue() == "restic-pw-value\n"
    assert seen[0]["AWS_SECRET_ACCESS_KEY"] == SECRET  # the reference resolved for Terraform
    # the CLI refuses a non-terminal stdout before anything is read (pytest's stdout is captured: not a tty)
    from mcctl import cli

    args = cli.build_parser().parse_args(["secrets", "show-restic", "-C", str(root)])
    assert args.func is cli.cmd_secrets and args.action == "show-restic"
    assert cli.cmd_secrets(args) == 2


def test_no_hint_sources_dot_env() -> None:
    """M2: every hand-run hint uses mcctl (which resolves dl:// references), never `. .env` / `source .env`
    for a secret."""
    import re

    for path in [*(REPO / "mcctl").rglob("*.py"), REPO / "README.md", *(REPO / "docs").rglob("*.md")]:
        text = path.read_text()
        assert not re.search(r"\(set -a; \. ", text), path
        assert "output -raw restic_password" not in text, path
    assert integrations.show_restic_command("/c") == "mcctl secrets show-restic -C /c"
    init_src = (REPO / "mcctl/init.py").read_text()
    assert "integrations.show_restic_command(repo.root)" in init_src
