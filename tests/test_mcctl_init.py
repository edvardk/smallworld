"""P5 `mcctl init` against the mock providers (mcctl/mock.py): nothing real is created, contacted or applied."""

import hashlib
import io
import json
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest
import yaml
from conftest import REPO, TEST_SHA

from mcctl import cli, configrepo, envfile, ghapp, init, mcping, mock, sigv4, state, tf, ui, yamledit
from mcctl.providers import cloudflare, discord, mojang, tailscale

ANSWERS = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())
SECRET_KEYS = (
    "cloudflare_api_token",
    "github_token",
    "product_read_token",
    "tailscale_api_key",
    "tailscale_oauth_client_id",
    "tailscale_oauth_client_secret",
    "discord_bot_token",  # P14
)


def run_wizard(root: Path, answers: dict | None = None, *, from_step=None, deps=None):
    out = io.StringIO()
    talk = ui.UI(answers=ANSWERS if answers is None else answers, mock=True, env={}, out=out)
    deps = deps or mock.deps(root)
    rc = init.run(root, talk, deps, init.Options(mock=True, product_sha=TEST_SHA, from_step=from_step))
    return rc, out.getvalue(), deps


def secret_values(deps) -> list[str]:
    values = [ui.MOCK_SECRETS.get(k, f"mock-{k}") for k in SECRET_KEYS] + [ui.MOCK_SECRETS["discord_webhook"]]
    for token_id, value in deps.cloud.tokens.items():
        values += [value, cloudflare.r2_credentials(token_id, value)[1]]
    # P10 review: the GitHub Apps' private keys (as GitHub returns them, and as .env stores them)
    values += ["bW9jay1hcHAta2V5LW5vdC1hLWtleQ==", ghapp.encode_key(mock.MOCK_PEM)]
    # setup automation: the Tailscale OAuth client secret mcctl created, gh's token, the restic password
    values += [*deps.cloud.ts_clients.values(), "mock-gh-oauth-token", "mock-restic-password-not-a-secret"]
    return values


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def test_full_mock_run_then_idempotent_rerun(tmp_path) -> None:
    root = tmp_path / "mc-config"
    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    repo = configrepo.load(root)  # a valid config repo
    cfg = repo.server
    assert (cfg.name, cfg.dns.zone, cfg.minecraft.version, cfg.host.ipv4) == (
        "e2e",
        "tests.mcctl.internal",
        "26.3",
        "9.9.9.9",
    )
    assert cfg.deploy.enabled and cfg.world.expected and cfg.alerts.discord and cfg.jvm.heap_mb == 1024
    assert repo.pin.sha == TEST_SHA and (cfg.github.owner, cfg.github.config_repo) == (
        "mcctl-e2e",
        "e2e-mc-config",
    )
    assert [p.name for p in repo.players.whitelist] == ["E2EAdmin", "E2EFriend"]
    assert [(o.name, o.level) for o in repo.players.ops] == [("E2EAdmin", 4)]
    assert "Written by `mcctl init`" in (root / "server.yml").read_text()

    env_file = root / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    env = envfile.parse(env_file.read_text())
    assert set(env) == {
        "CLOUDFLARE_API_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "GITHUB_TOKEN",
        "TF_VAR_product_read_token", "TF_VAR_discord_webhook_url", "TAILSCALE_OAUTH_CLIENT_ID",
        "TAILSCALE_OAUTH_CLIENT_SECRET", "TF_VAR_panel_github_app_id", "TF_VAR_panel_github_app_private_key",
        "TF_VAR_updater_github_app_id", "TF_VAR_updater_github_app_private_key", "DISCORD_BOT_TOKEN",
    }  # fmt: skip
    assert ghapp.decode_key(env["TF_VAR_panel_github_app_private_key"]) == mock.MOCK_PEM
    gh_out = state.load(root).get("github")["outputs"]
    assert (gh_out["panel_app"], gh_out["updater_app"]) == ("e2e-panel", "e2e-updater")
    assert "TAILSCALE_API_KEY" not in env  # the one-off key is never stored
    # Two Cloudflare tokens: the hand-made account-owned admin token (in .env) and the state token it created
    assert len(deps.cloud.tokens) == 2 and env["CLOUDFLARE_API_TOKEN"] == mock.ADMIN_TOKEN
    token_id = env["AWS_ACCESS_KEY_ID"]
    value = deps.cloud.tokens[token_id]
    assert env["AWS_SECRET_ACCESS_KEY"] == hashlib.sha256(value.encode()).hexdigest()
    (post,) = [r for r in deps.cloud.requests if r.method == "POST" and r.url.endswith("/tokens")]
    assert post.url.endswith(f"/accounts/{mock.ACCOUNT}/tokens")  # only the state token, account-owned
    assert post.headers["Authorization"] == f"Bearer {mock.ADMIN_TOKEN}"
    assert configrepo.load(root).server.cloudflare.token_owner == "account"
    assert "exactly the permissions mcctl needs" in out and "Manage Account > Account API Tokens" in out
    assert "bootstrap token" not in out  # a fresh setup has no leftover to warn about
    assert not deps.cloud.auto_renew and "switch auto-renew on in the dashboard" in out
    # Tailscale: the OAuth client was created through the API (exact scopes and tags), not pasted
    ((cid, csecret),) = deps.cloud.ts_clients.items()
    assert (env["TAILSCALE_OAUTH_CLIENT_ID"], env["TAILSCALE_OAUTH_CLIENT_SECRET"]) == (cid, csecret)
    # The product repo's settings (product-setup) were applied
    prod = deps.cloud.gh_settings[configrepo.PRODUCT_REPOSITORY]
    # main, v* tags (move/delete), v1.5: v* tag creation
    assert prod["allow_auto_merge"] and prod["vulnerability-alerts"] and len(prod["rulesets"]) == 3
    assert "Checklist:" in out and "[x] 17 First backup" in out  # numbered 1..17 like the headers
    assert "[1/17] Prerequisites" in out and "[0/" not in out and "[x]  0 " not in out
    # P14: Discord slash commands: ids + public key in server.yml, commands registered, endpoint = the Worker
    d = cfg.discord
    assert (d.commands, d.application_id, d.public_key) == (True, mock.DISCORD_APP, mock.DISCORD_PUBLIC_KEY)
    assert (d.guild_id, d.channel_ids, d.user_ids) == (
        "1300000000000000002",
        ["1300000000000000003"],
        ["1300000000000000004"],
    )
    assert deps.cloud.discord_commands["1300000000000000002"] == [c["name"] for c in discord.COMMANDS]
    assert deps.cloud.discord_endpoint == "https://hb.mc.tests.mcctl.internal/discord/interactions"
    assert deps.cloud.buckets == {"e2e-tfstate"}

    # Secrets: never in the output, git history, tracked files or state.json.
    history = git(root, "log", "-p", "--all")
    tracked = git(root, "ls-files")
    state_text = (root / state.STATE_FILE).read_text()
    for s in secret_values(deps):
        assert s not in out and s not in history and s not in state_text, s
    assert ".env\n" not in tracked and ".mcctl" not in tracked and "expiry.yml" in tracked

    # Credentials only go to their own API.
    for req in deps.cloud.requests:
        auth = req.headers.get("Authorization", "")
        if env["CLOUDFLARE_API_TOKEN"] in auth:
            assert req.url.startswith(cloudflare.API)
        if "mock-github_token" in auth or "mock-product_read_token" in auth:
            assert req.url.startswith("https://api.github.com/")
        if "mock-discord_bot_token" in auth:  # P14: the bot token goes to Discord's API only
            assert req.url.startswith(discord.API + "/applications/")

    assert deps.tf_log == [
        "plan -target=tailscale_acl.this", "apply", "plan ", "apply", "plan -replace=tailscale_tailnet_key.server", "apply",
    ]  # fmt: skip
    st = state.load(root)
    assert all(st.is_done(s.id) for s in init.STEPS)
    assert "created          GitHub repository mcctl-e2e/e2e-mc-config (private)" in out

    # Re-run: nothing new (no commits, no Terraform, no repo creation), credentials re-validated quietly.
    head = git(root, "rev-parse", "HEAD")
    rc, out2, deps2 = run_wizard(root)
    assert rc == 0, out2
    assert git(root, "rev-parse", "HEAD") == head and deps2.tf_log == []
    assert out2.count("done earlier (skipped)") == 12
    # e2e-mock.yml checks the same count on its second run
    assert 'init2.log")" -eq 12' in (REPO / ".github/workflows/e2e-mock.yml").read_text()
    assert not any(c[:3] == ["gh", "repo", "create"] for c in deps2.tools.calls)
    assert "Cloudflare token: account-owned, active" in out2 and "Paste" not in out2


def test_failure_resumes_at_the_failed_step(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, ANSWERS | {"restic_password_stored": False})
    assert rc == 1 and "store the restic password first" in out and "then re-run: mcctl init -C" in out
    st = state.load(root)
    assert st.get("apply")["status"] == "failed" and st.is_done("tailscale") and not st.is_done("host")

    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    assert deps.tf_log[:2] == ["plan ", "apply"]  # the tailscale step's targeted apply wasn't repeated
    assert out.count("done earlier (skipped)") >= 4


def test_pauses_waiting_for_the_host_then_continues(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, ANSWERS | {"host_ipv4": ""})
    assert rc == 0 and "Paused at host (waiting_for_host)" in out
    assert state.load(root).get("host")["status"] == "waiting"
    assert configrepo.load(root).server.host.ipv4 is None
    rc, out, _ = run_wizard(root)
    assert rc == 0 and configrepo.load(root).server.host.ipv4 == "9.9.9.9"


def test_from_step_forces_a_rerun(tmp_path) -> None:
    root = tmp_path / "c"
    assert run_wizard(root)[0] == 0
    rc, out, _ = run_wizard(root, ANSWERS | {"set": {"jvm.heap_mb": 2048}}, from_step="questions")
    assert rc == 0 and configrepo.load(root).server.jvm.heap_mb == 2048
    assert "done earlier" not in out.split("[3/17]")[1]
    out_ = io.StringIO()
    bad = init.run(root, ui.UI(answers=ANSWERS, out=out_), mock.deps(root), init.Options(from_step="nope"))
    assert bad == 2 and "--from" in out_.getvalue()
    # --from N is the number shown (1-based); step names and state.json ids are unchanged
    assert init._from_index("3") == init.STEP_IDS.index("questions") == 2
    assert init._from_index("17") == len(init.STEPS) - 1 and init._from_index("bootstrap") == 13
    for bad_n in ("0", "18"):
        with pytest.raises(ValueError, match="1-17"):
            init._from_index(bad_n)


def test_answers_file_missing_a_key(tmp_path) -> None:
    answers = {k: v for k, v in ANSWERS.items() if k != "zone"}
    rc, out, _ = run_wizard(tmp_path / "c", answers)
    assert rc == 1 and "answers file has no `zone`" in out


def test_invalid_answer_is_refused(tmp_path) -> None:
    rc, out, _ = run_wizard(tmp_path / "c", ANSWERS | {"zone": "not a zone"})
    assert rc == 1 and "answers file: `zone`" in out


def test_interactive_run_uses_getpass_and_reasks(tmp_path, monkeypatch) -> None:
    """A whole interactive run: prompts answered by keyword, secrets only through read_secret."""
    replies = {
        "GitHub repository": ["alex/alex-mc-config"],
        "Short name": [""],  # default derived from the repo name
        "Your name": ["Alex"],
        "Tailscale login": [""],  # default from `tailscale status`
        "log in to the admin panel": ["alex@tests.mcctl.internal"],
        "Your domain": ["not a domain", "tests.mcctl.internal"],  # re-asked after a schema error
        "hostname in that zone": [""],
        "time zone": ["Mars/Base", "Europe/Berlin"],
        "Minecraft version": ["1"],
        "EULA": ["y"],
        "overworld": [""],
        "nether": ["none"],
        "Discord": [""],
        "Minecraft name": ["Alex_1"],
        "Other players": [""],
        "Create the private repository": [""],
        "public IPv4": ["9.9.9.9"],
        "IPv6": [""],
        "match": ["y"],
        "Type the deployment name": ["alex", "alex", "alex"],
        "Stored it": ["y"],
        "Revoked it": ["y"],
        "installed on": ["y"],
        "Apply these": [""],
    }
    asked: list[str] = []

    def read(prompt: str) -> str:
        asked.append(prompt)
        for key, values in replies.items():
            if key in prompt:
                return values.pop(0) if len(values) > 1 else values[0]
        raise AssertionError(f"unexpected prompt {prompt!r}")

    secrets_asked: list[str] = []

    def read_secret(prompt: str) -> str:
        secrets_asked.append(prompt)
        if "admin token" in prompt:  # the hand-made account-owned token (the fake API knows this one)
            return mock.ADMIN_TOKEN
        if "API access token" in prompt:  # Tailscale's format carries the key id (revoked through the API)
            return f"tskey-api-kALEX{len(secrets_asked)}CNTRL-verysecret{len(secrets_asked)}"
        return "very-secret-" + str(len(secrets_asked))

    root = tmp_path / "alex-mc-config"
    out = io.StringIO()
    deps = mock.deps(root, login="alex")
    talk = ui.UI(out=out, read=read, read_secret=read_secret, env={})
    rc = init.run(root, talk, deps, init.Options(mock=True, product_sha=TEST_SHA))
    text = out.getvalue()
    assert rc == 0, text
    cfg = configrepo.load(root).server
    assert (cfg.name, cfg.admin.tailscale_login, cfg.host.timezone) == (
        "alex",
        "mock-user@github",
        "Europe/Berlin",
    )
    assert cfg.world.border_radius.nether is None and cfg.host.ipv6 is None
    assert "doesn't match" in text and "unknown time zone" in text
    assert len(secrets_asked) == 4 and all(
        "input hidden" in p for p in secrets_asked
    )  # Cloudflare admin token, two GitHub PATs, the one-off Tailscale API token (the rest is minted/created)
    assert "very-secret-" not in text and "verysecret" not in text and not any("secret" in p for p in asked)
    assert "[ok] Discord alerts off" in text


def test_state_backend_reuses_a_live_token(tmp_path) -> None:
    root = tmp_path / "c"
    rc, _, deps = run_wizard(root)
    assert rc == 0
    rc, out, _ = run_wizard(root, from_step="state-backend", deps=mock.deps(root, cloud=deps.cloud))
    assert rc == 0 and "belong to a live token" in out and len(deps.cloud.tokens) == 2  # admin + state


# --- pieces -------------------------------------------------------------------------------------------

PROBE_OK = 'ID=debian\nVERSION_ID="13"\n@@\nx86_64\n@@\nMemTotal: 8131452 kB\n@@\n2: eth0 inet6 2620:fe::fe/64 scope global\n'


def test_check_host() -> None:
    assert init.check_host(PROBE_OK, "2620:fe:0::fe") == []
    assert init.check_host(PROBE_OK, None) == []
    bad = PROBE_OK.replace('ID=debian\nVERSION_ID="13"', 'ID=centos\nVERSION_ID="9"').replace(
        "8131452", "2000000"
    )
    problems = init.check_host(bad.replace("x86_64", "riscv64"), "2620:fe::1")
    assert (
        len(problems) == 4
        and "Debian 13" in problems[0]
        and "4 GB" in problems[2]
        and "2620:fe::1" in problems[3]
    )
    assert init.check_host("garbage", None) == ["unexpected output from the host probe"]


def test_install_hints_and_os_detection() -> None:
    assert init.os_family("darwin", {}) == "macos" and init.os_family("win32", {}) == "windows"
    assert init.os_family("linux", {"ID": "fedora"}) == "fedora"
    assert init.os_family("linux", {"ID": "ubuntu", "ID_LIKE": "debian"}) == "debian"
    for family in ("macos", "debian", "fedora"):
        for tool in ("git", "gh", "terraform", "tailscale", "ssh", "venv"):
            assert init.install_hint(tool, family)
        assert tf.TERRAFORM_VERSION in init.install_hint("terraform", family)
    assert "brew install gh" in init.install_hint("gh", "macos")
    # Ubuntu 26.04 / Debian: the unversioned package (python3.X-venv names go stale with every release)
    assert init.install_hint("venv", "debian") == "sudo apt install python3-venv"
    assert init.venv_problem("debian", has_ensurepip=True) is None
    assert "sudo apt install python3-venv" in str(init.venv_problem("debian", has_ensurepip=False))


def test_preflight_reports_every_missing_tool_with_hints(tmp_path) -> None:
    deps = mock.deps(tmp_path)
    deps.which = lambda name: None if name in ("gh", "terraform") else f"/bin/{name}"
    deps.platform, deps.os_release = "darwin", dict
    out = io.StringIO()
    rc = init.run(tmp_path, ui.UI(answers={}, out=out), deps, init.Options(mock=True))
    text = out.getvalue()
    assert rc == 1 and "brew install gh" in text and "hashicorp/tap/terraform" in text
    deps.platform = "win32"
    out = io.StringIO()
    assert init.run(tmp_path, ui.UI(answers={}, out=out), deps, init.Options(mock=True)) == 1
    assert "WSL2" in out.getvalue()


def test_preflight_needs_the_pinned_ansible_collections(tmp_path) -> None:
    from mcctl import galaxy

    deps = mock.deps(tmp_path)

    def unreachable(root):
        raise galaxy.GalaxyError("installing the pinned Ansible collections failed: galaxy unreachable")

    deps.collections = unreachable
    out = io.StringIO()
    assert init.run(tmp_path, ui.UI(answers={}, out=out), deps, init.Options(mock=True)) == 1
    text = out.getvalue()
    assert "galaxy unreachable" in text and galaxy.MANUAL in text


def test_prefilled_links() -> None:
    url = init.pat_url("mcctl lh admin", "d", "octocat", {"secrets": "write"})
    assert url.startswith("https://github.com/settings/personal-access-tokens/new?")
    assert "target_name=octocat" in url and "secrets=write" in url and "expires_in=366" in url
    url = cloudflare.token_url("mcctl lh (admin)")
    assert url.startswith("https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=")
    assert "%22workers_r2%22" in url and "%22account_api_tokens%22" in url and "access" not in url


def test_template_note_is_replaced() -> None:
    assert init.TEMPLATE_NOTE in (REPO / "template/server.yml").read_text()


def test_mc_bin_dir_matches_group_vars() -> None:
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert init.MC_BIN_DIR == gv["mc_bin_dir"].replace("{{ mc_root }}", gv["mc_root"])


def test_sigv4_matches_the_aws_test_suite() -> None:
    import datetime

    auth = sigv4.authorization(
        "GET",
        "https://example.amazonaws.com/",
        {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="service",
        now=datetime.datetime(2015, 8, 30, 12, 36, tzinfo=datetime.UTC),
    )
    # botocore tests/unit/auth/aws4_testsuite/get-vanilla/get-vanilla.authz
    assert auth == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_cloudflare_state_token_helpers() -> None:
    cloud = mock.Cloud()
    acc = mock.ACCOUNT
    assert not cloudflare.bucket_exists("t", acc, "x-tfstate", cloud)
    cloudflare.create_bucket("t", acc, "x-tfstate", "weur", cloud)
    assert cloudflare.bucket_exists("t", acc, "x-tfstate", cloud)
    token_id, _value = cloudflare.create_r2_token("t", "user", acc, "x-tfstate", cloud)
    body = json.loads(cloud.requests[-1].body)
    assert body["policies"][0]["resources"] == {f"com.cloudflare.edge.r2.bucket.{acc}_default_x-tfstate": "*"}
    assert body["policies"][0]["permission_groups"] == [{"id": "mock-pg-r2-write"}]
    assert cloudflare.token_exists("t", "user", acc, token_id, cloud)
    assert not cloudflare.token_exists("t", "user", acc, "nope", cloud)
    cloudflare.create_r2_token("t", "account", acc, "x-tfstate", cloud)
    assert cloud.requests[-1].url == f"{cloudflare.API}/accounts/{acc}/tokens"


def test_mojang_and_tailscale_key() -> None:
    cloud = mock.Cloud()
    name, uuid = mojang.uuid_for("Notch", cloud)
    assert name == "Notch" and len(uuid) == 36 and uuid.count("-") == 4
    with pytest.raises(mojang.MojangError):
        mojang.uuid_for("no spaces", cloud)
    assert tailscale.check_api_key("k", cloud).level == "ok"


def test_status_ping_against_a_fake_server() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    payload = json.dumps({"version": {"name": "Purpur 26.3"}, "players": {"online": 1, "max": 20}}).encode()

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.recv(1024)
            body = b"\x00" + mcping._varint(len(payload)) + payload
            conn.sendall(mcping._varint(len(body)) + body)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    st = mcping.status("127.0.0.1", port, host="mc.tests.mcctl.internal", timeout=5)
    srv.close()
    assert st["version"]["name"] == "Purpur 26.3" and st["players"]["online"] == 1


# --- CLI ----------------------------------------------------------------------------------------------


def test_cli_init_mock(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(
        "CLOUDFLARE_API_TOKEN", "real-shell-token"
    )  # --mock never uses the shell's credentials
    root = tmp_path / "cfg"
    rc = cli.main([
        "init", "-C", str(root), "--answers", str(REPO / "tests/e2e/answers.yml"), "--non-interactive", "--mock",
        "--product-sha", TEST_SHA,
    ])  # fmt: skip
    assert rc == 0
    assert "real-shell-token" not in (root / ".env").read_text()
    assert cli.main(["validate", "-C", str(root)]) == 0
    assert cli.main(["init", "--answers", "x.yml"]) == 2
    assert cli.main(["init", "--non-interactive"]) == 2


def test_config_get_and_set(config_repo, capsys) -> None:
    subprocess.run(["git", "-C", str(config_repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(config_repo), *mock.GIT_IDENTITY, "add", "-A"], check=True)
    subprocess.run(["git", *mock.GIT_IDENTITY, "-C", str(config_repo), "commit", "-qm", "seed"], check=True)
    assert cli.main(["config", "get", "performance.view_distance", "-C", str(config_repo)]) == 0
    assert capsys.readouterr().out.strip() == "10"  # from performance.profile (vanilla)
    assert cli.main(["config", "get", "host.tailscale_name", "-C", str(config_repo)]) == 0
    assert capsys.readouterr().out.strip() == "example"  # derived values are shown too
    rc = cli.main(["config", "set", "performance.view_distance", "12", "-C", str(config_repo), "--no-commit"])
    assert rc == 0 and "view_distance = 12" in capsys.readouterr().out
    text = (config_repo / "server.yml").read_text()
    assert "performance:\n  view_distance: 12\n" in text
    # cross-field rule (simulation <= view) is checked on the whole file and the old file restored
    assert cli.main(["config", "set", "performance.simulation_distance", "13", "-C", str(config_repo)]) == 1
    assert (
        "simulation_distance" in capsys.readouterr().err and (config_repo / "server.yml").read_text() == text
    )
    assert cli.main(["config", "set", "dns.ttl", "5", "-C", str(config_repo)]) == 1
    assert ">= 60" in capsys.readouterr().err
    assert cli.main(["config", "set", "nope.x", "1", "-C", str(config_repo)]) == 1
    assert cli.main(["config", "get", "nope", "-C", str(config_repo)]) == 1


def test_config_set_commits(config_repo, capsys, monkeypatch) -> None:
    for k, v in (
        ("GIT_AUTHOR_NAME", "t"),
        ("GIT_AUTHOR_EMAIL", "t@t"),
        ("GIT_COMMITTER_NAME", "t"),
        ("GIT_COMMITTER_EMAIL", "t@t"),
    ):
        monkeypatch.setenv(k, v)
    subprocess.run(["git", "-C", str(config_repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(config_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(config_repo), "commit", "-qm", "seed"], check=True)
    assert cli.main(["config", "set", "minecraft.motd", "Hello # world", "-C", str(config_repo)]) == 0
    assert "committed" in capsys.readouterr().out
    assert git(config_repo, "log", "-1", "--format=%s").strip() == "mcctl config set minecraft.motd"
    assert configrepo.load(config_repo).server.minecraft.motd == "Hello # world"
    assert cli.main(["config", "set", "minecraft.motd", "Hello # world", "-C", str(config_repo)]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_existing_clone_without_gitignore_gets_env_ignored(tmp_path) -> None:
    root = tmp_path / "c"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "https://github.com/mcctl-e2e/e2e-mc-config.git"],
        check=True,
    )
    (root / ".gitignore").write_text("*.log\n")
    deps = mock.deps(root)
    deps.tools.created.add("mcctl-e2e/e2e-mc-config")  # the GitHub repo exists already
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 0, out
    text = (root / ".gitignore").read_text()
    assert ".env\n" in text and ".mcctl/\n" in text
    assert ".env" not in git(root, "ls-files").split()


# --- P5 review fixes ----------------------------------------------------------------------------------


class _BucketCreateFails(mock.Cloud):
    def __call__(self, req):
        if req.method == "POST" and req.url.endswith("/r2/buckets"):
            self.requests.append(req)
            body = {"success": False, "errors": [{"code": 10004, "message": "name taken"}], "result": None}
            return mock.http.Response(409, {}, json.dumps(body).encode())
        return super().__call__(req)


def test_provider_errors_fail_the_step_with_a_fix(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=_BucketCreateFails()))
    assert rc == 1 and "Traceback" not in out
    assert "creating R2 bucket e2e-tfstate failed (10004: name taken)" in out
    assert "fix: the token needs Account > Workers R2 Storage > Edit" in out
    assert state.load(root).get("state-backend")["status"] == "failed"


def test_cloudflare_step_requires_token_permission_groups(tmp_path) -> None:
    class NoTokenAdmin(mock.Cloud):
        def __call__(self, req):
            if req.url.endswith("/tokens/permission_groups"):
                return mock._json({"success": False, "errors": [{"code": 9109, "message": "denied"}]}, 403)
            return super().__call__(req)

    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=NoTokenAdmin()))
    assert rc == 1 and "needs Account > Account API Tokens > Edit" in out  # the hand-made token lacks it
    assert state.load(root).get("cloudflare")["status"] == "failed"
    # A user-owned admin token (an older setup's, from the shell) is checked for the user permission
    root = tmp_path / "d"
    deps = mock.deps(root, cloud=NoTokenAdmin())
    deps.credentials_env = {"PATH": "/usr/bin", "CLOUDFLARE_API_TOKEN": "mock-admin-from-shell"}
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 1 and "needs User > API Tokens > Edit" in out


def test_minting_answers_are_gone_and_a_leftover_bootstrap_token_is_named(tmp_path) -> None:
    """Older versions minted the admin token from a bootstrap token (answers `account-mint`/`mint`); Cloudflare
    refuses that (error 1001). An old answers file still runs (hand-made token), and a run after a failed one
    names the bootstrap token that may be left over."""
    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, ANSWERS | {"cloudflare_token": "account-mint"})
    assert rc == 0, out
    assert "cloudflare_token 'account-mint' is gone" in out and "'mcctl e2e bootstrap'" in out
    # A run that failed in the cloudflare step without a token in .env (as an older mcctl's failed mint did)
    root = tmp_path / "d"
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=_TokenRejected()))
    assert rc == 1 and state.load(root).get("cloudflare")["status"] == "failed"
    envfile.write(root, {"CLOUDFLARE_API_TOKEN": ""})
    rc, out, _ = run_wizard(root)
    assert rc == 0, out
    assert "bootstrap token 'mcctl e2e bootstrap', delete it in the dashboard (Manage Account" in out


class _TokenRejected(mock.Cloud):
    """The admin token fails its check (as if it weren't there yet)."""

    def __call__(self, req):
        if req.url.endswith("/tokens/verify"):
            return mock._json(
                {"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}, 401
            )
        return super().__call__(req)


def test_ipv6_change_after_bootstrap_probes_over_the_tailnet(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    text = (root / "server.yml").read_text()
    (root / "server.yml").write_text(yamledit.set_value(text, "host.ipv6", "2620:fe::9"))
    git(root, *mock.GIT_IDENTITY, "commit", "-qam", "new ipv6")
    deps2 = mock.deps(root, cloud=deps.cloud)
    rc, out, _ = run_wizard(root, deps=deps2)
    assert rc == 0, out
    assert "checking it over the tailnet instead" in out
    ssh = [c for c in deps2.tools.calls if c[0] in ("ssh", "ssh-keyscan")]
    assert ssh and not any("root@" in " ".join(c) or c[0] == "ssh-keyscan" for c in ssh)
    assert any(f"deploy@{mock.TAILNET_IP}" in c for c in ssh)
    assert deps2.tf_log == ["plan ", "apply"]  # DNS records only; no new server key, no bootstrap
    # bootstrap and the first backup are unaffected by host.ipv6; host, deploy and smoke re-ran
    assert out.count("done earlier (skipped)") == 12 - 3


def test_head_bucket_forbidden_falls_back_to_list_objects(tmp_path) -> None:
    root = tmp_path / "c"
    cloud = mock.Cloud(head_bucket_status=403)
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 0, out
    methods = [r.method for r in cloud.requests if "r2.cloudflarestorage.com" in r.url]
    assert "HEAD" in methods and "GET" in methods


def test_unusable_state_token_can_be_deleted_not_orphaned(tmp_path) -> None:
    class TokenNeverWorks(mock.Cloud):
        def __call__(self, req):
            if "r2.cloudflarestorage.com" in req.url:
                self.requests.append(req)
                return mock.http.Response(403, {}, b"")
            return super().__call__(req)

    root = tmp_path / "c"
    cloud = TokenNeverWorks()
    rc, out, _ = run_wizard(root, ANSWERS | {"delete_state_token": True}, deps=mock.deps(root, cloud=cloud))
    assert rc == 1 and "HeadBucket and ListObjectsV2 refused" in out and "fix: re-run: a fresh token" in out
    assert list(cloud.tokens.values()) == [
        envfile.load(root)["CLOUDFLARE_API_TOKEN"]
    ]  # the state token is gone
    env = envfile.load(root)
    assert "AWS_ACCESS_KEY_ID" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def test_shell_state_credentials_are_ignored_loudly(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.credentials_env = {"PATH": "/usr/bin", "AWS_ACCESS_KEY_ID": "someone-elses-key"}
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 0, out
    assert "IGNORING AWS_ACCESS_KEY_ID from your shell" in out and "someone-elses-key" not in out
    assert envfile.load(root)["AWS_ACCESS_KEY_ID"] != "someone-elses-key"


def test_tailscale_revoke_is_verified_and_survives_an_interruption(tmp_path) -> None:
    class NoSelfRevoke(mock.Cloud):
        def __call__(self, req):
            if req.method == "DELETE" and "/tailnet/-/keys/" in req.url:
                self.requests.append(req)
                return mock._json({"message": "forbidden"}, 403)
            return super().__call__(req)

    root = tmp_path / "c"
    cloud = NoSelfRevoke()
    rc, out, _ = run_wizard(root, ANSWERS | {"api_key_revoked": False}, deps=mock.deps(root, cloud=cloud))
    assert rc == 1 and "revoke the one-off Tailscale API token" in out
    rec = state.load(root).get("tailscale")
    assert rec["status"] == "failed" and rec["outputs"] == {
        "api_key": "unrevoked",
        "api_key_id": "kMOCK1CNTRL",
        "old_client": None,
        "oauth_tags": ["tag:mc-terraform"],
    }

    # Re-run: the OAuth client in .env works, but the API still knows the key: a "yes" isn't enough.
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 1 and "still works: it isn't revoked yet" in out
    cloud.revoked_ts_keys.add("kMOCK1CNTRL")  # the user revokes it in the console
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 0, out
    assert "revoked (checked through the API)" in out
    assert state.load(root).get("tailscale")["outputs"]["api_key"] == "revoked"


def test_tailscale_self_revoke_through_the_api(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, deps = run_wizard(root, ANSWERS | {"api_key_revoked": False})
    assert rc == 0, out  # never asked: the token revoked itself and stopped working
    assert deps.cloud.revoked_ts_keys == {"kMOCK1CNTRL"}


def test_an_oauth_client_with_the_old_tags_is_replaced(tmp_path) -> None:
    """First live apply: a client tagged [tag:mc-server, tag:ci] can create neither the server key nor the CI
    identity. On an existing setup (step done before the fix: other input hash, no oauth_tags) the re-run
    applies the policy with the owner tag, makes a client tagged tag:mc-terraform, deletes the old one."""
    root = tmp_path / "c"
    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    st = state.load(root)
    old_cid = envfile.load(root)["TAILSCALE_OAUTH_CLIENT_ID"]
    st.record("tailscale", "done", outputs={"acl_applied": True, "oauth": True, "api_key": "revoked"})
    deps.cloud.revoked_ts_keys.clear()  # a new one-off API token
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=deps.cloud))
    assert rc == 0, out
    assert f"the OAuth client {old_cid} in .env was made with tags" in out
    new_cid = envfile.load(root)["TAILSCALE_OAUTH_CLIENT_ID"]
    assert new_cid != old_cid and deps.cloud.ts_client_tags[new_cid] == ["tag:mc-terraform"]
    assert old_cid in deps.cloud.revoked_ts_keys and f"deleted the old OAuth client {old_cid}" in out
    assert state.load(root).get("tailscale")["outputs"]["oauth_tags"] == ["tag:mc-terraform"]
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=deps.cloud))  # converged: no third client
    assert rc == 0 and envfile.load(root)["TAILSCALE_OAUTH_CLIENT_ID"] == new_cid


def test_a_failed_apply_reports_what_was_created_and_the_rerun_converges(tmp_path) -> None:
    """First live apply: some resources were created, five failed. mcctl says which, and a re-run resumes."""
    root = tmp_path / "c"
    fake = mock.FakeTerraform([], fail_apply=["github_actions_secret.this"])
    deps = mock.deps(root, fake_tf=fake)
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 1 and "terraform apply failed part-way" in out
    assert "created (1): cloudflare_r2_bucket.backups" in out
    assert "NOT created (1): github_actions_secret.this" in out
    assert "re-running plans only what is still missing" in out and "re-run `mcctl setup`" in out
    assert state.load(root).get("apply")["status"] == "failed"
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=deps.cloud, fake_tf=fake))
    assert rc == 0, out
    assert "github_actions_secret.this" in fake.state_list()


def _org_puts(deps: init.Deps) -> list:
    return [r for r in deps.cloud.requests if r.method == "PUT" and r.url.endswith("/access/organizations")]


def test_zero_trust_global_session_is_set_by_mcctl_on_a_fresh_org(tmp_path) -> None:
    """Live (a freshly created organization): echoing GET back was refused (12062: a partial
    service_token_inactivity, nulls) and 10m is below Cloudflare's 15-minute minimum. mcctl sends a cleaned body
    with the target (15m), keeps the other settings, and doesn't write again."""
    root = tmp_path / "c"
    rc, out, deps = run_wizard(root)
    assert rc == 0, out
    org = deps.cloud.zt_org
    assert org["session_duration"] == "15m" and org["name"] == "mock-team"
    assert "service_token_inactivity" not in org and "custom_pages" not in org and "created_at" not in org
    assert "global session 24h -> 15m" in out and "keeps its own 24h session" in out
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=deps.cloud))
    assert rc == 0 and "global session 24h ->" not in out and "global session 15m (<= 30m" in out
    assert len(_org_puts(deps)) == 1


@pytest.mark.parametrize("duration", ["15m", "30m", "20m"])
def test_zero_trust_global_session_already_short_enough_is_left_alone(tmp_path, duration) -> None:
    """(a) Set by hand within the limit: the step is done without any write."""
    root = tmp_path / "c"
    cloud = mock.Cloud()
    cloud.zt_org["session_duration"] = duration
    rc, out, deps = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 0, out
    assert f"global session {duration} (<= 30m" in out and not _org_puts(deps)


def test_zero_trust_refused_write_names_the_dashboard_setting_and_rerun_only_rechecks(tmp_path) -> None:
    root = tmp_path / "c"
    cloud = mock.Cloud(zt_put_refused=True)
    rc, out, deps = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 1 and "setting it to 15m through the API failed" in out
    assert "Zero Trust > Access controls > Access settings > Set your global session duration" in out
    assert "choose 15 minutes" in out and "re-running only re-checks it" in out
    assert len(_org_puts(deps)) == 1
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=cloud))  # not fixed yet: re-check, no write
    assert rc == 1 and "still 24h" in out and len(_org_puts(deps)) == 1
    cloud.zt_org["session_duration"] = "15m"  # the admin changes it in the dashboard
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 0, out
    assert "global session 15m (<= 30m" in out and len(_org_puts(deps)) == 1


def test_session_body_keeps_complete_settings_and_drops_what_the_api_refuses() -> None:
    org = {
        "auth_domain": "t.cloudflareaccess.com", "name": "t", "session_duration": "24h",
        "created_at": "x", "updated_at": "y", "custom_pages": None, "login_design": {"logo_path": ""},
        "mfa_config": {"allowed_authenticators": ["totp"], "session_duration": ""},
        "is_ui_read_only": False, "deny_unmatched_requests_exempted_zone_names": [],
        "service_token_inactivity": {"enabled": True, "inactivity_threshold_days": 90, "action": "disable"},
    }  # fmt: skip
    body = cloudflare.session_body(org, "15m")
    assert body == {
        "auth_domain": "t.cloudflareaccess.com", "name": "t", "session_duration": "15m",
        "mfa_config": {"allowed_authenticators": ["totp"]}, "is_ui_read_only": False,
        "deny_unmatched_requests_exempted_zone_names": [],
        "service_token_inactivity": {"enabled": True, "inactivity_threshold_days": 90, "action": "disable"},
    }  # fmt: skip
    for partial in ({"enabled": False, "inactivity_threshold_days": 0, "action": "none"}, {"enabled": True}):
        assert "service_token_inactivity" not in cloudflare.session_body(
            org | {"service_token_inactivity": partial}, "15m"
        )
    assert org["session_duration"] == "24h"  # the caller's object is untouched


def test_global_session_bounds() -> None:
    assert cloudflare.global_session_bounds(10) == (15, 30)
    assert cloudflare.global_session_bounds(1) == (15, 30)
    assert cloudflare.global_session_bounds(60) == (60, 120)
    assert "choose 1 hour(s)" in cloudflare.global_session_fix(60)


def test_product_owned_by_an_org_gets_organization_access(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.tools.access_level, deps.tools.product_owner_type = "none", "Organization"
    rc, out, _ = run_wizard(root, ANSWERS | {"share_workflows": True}, deps=deps)
    assert rc == 0, out
    put = [c for c in deps.tools.calls if c[:4] == ["gh", "api", "-X", "PUT"]]
    assert put and put[0][-1] == "access_level=organization"


def test_answers_cant_pre_approve_outside_mock(tmp_path, capsys) -> None:
    answers = tmp_path / "a.yml"
    answers.write_text("name: x\nconfirm_apply: x\nhost_key_matches: true\n")
    rc = cli.main(["init", "-C", str(tmp_path / "c"), "--answers", str(answers), "--non-interactive"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "can't pre-approve confirm_apply, host_key_matches outside --mock" in err
    assert not (tmp_path / "c").exists()


SUDO_WARNING = "sudo: unable to resolve host mc-server: Name or service not known\n"


def test_first_backup_is_judged_by_the_units_result_not_stderr(tmp_path) -> None:
    """First real setup: step 17 said "backup failed" although mc-backup.service finished with Result=success
    (sudo warned on stderr, `systemctl status` of the finished oneshot exited 3)."""
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.tools.backup_stderr = SUDO_WARNING  # type: ignore[attr-defined]
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 0, out
    assert "backup passed" in out and "the server also said: sudo: unable to resolve host" in out

    root = tmp_path / "d"
    deps = mock.deps(root)
    deps.tools.backup_result = "Result=exit-code\nExecMainStatus=1"  # type: ignore[attr-defined]
    rc, out, _ = run_wizard(root, deps=deps)
    assert rc == 1 and "backup failed: mc-backup.service Result=exit-code ExecMainStatus=1" in out
    assert state.load(root).get("backup")["status"] == "failed"
