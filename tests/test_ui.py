"""mcctl/ui.py, the presentation layer: snapshots of both renderings, mode detection, prompts, links, spinners,
Terraform's log/error summary, and resuming a setup started by the code before the CLI UX change."""

import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import REPO, TEST_SHA

from mcctl import cli, configrepo, desktop, doctor, init, mock, state, tf, ui
from mcctl import render as render_mod

SNAPSHOTS = REPO / "tests/fixtures/ui"
UPDATE = bool(os.environ.get("MCCTL_UPDATE_SNAPSHOTS"))
ANSWERS = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())
PAT = "https://github.com/settings/personal-access-tokens/new?name=mcctl+lh+admin&secrets=write"


def screens(out: ui.Out, root: Path) -> None:
    """Representative screens: a step with its panel and permission table, results, a failure, the plan
    table, doctor, the checklist and a summary."""
    out.header(7, 17, "GitHub tokens", done=6, name="lh")
    out.why("Terraform writes the config repo's Actions secrets and rulesets.")
    out.todo(
        [
            f"Open the pre-filled form: {PAT}",
            "Repository access: Only select repositories > octocat/mc-config.",
            "Paste the token below (input hidden).",
        ],
        intro="GitHub token for Terraform.",
    )
    out.table(
        ["Scope", "Permission", "Level", "Resources"],
        [
            ["Repository", "Secrets", "Read and write", "octocat/mc-config"],
            ["Repository", "Administration", "Read and write", "octocat/mc-config"],
        ],
        title="Token permissions",
        indent=2,
    )
    out.ok("GitHub admin token: valid, expires 2027-11-01")
    out.warn("couldn't allow auto-merge: Settings > General > Allow auto-merge")
    out.bad("the token can't read the Actions secrets")
    out.note("366 days; `mcctl doctor` warns 30 days before it expires.")
    out.info("Domain auto-renew: switch it on in the dashboard.\nSee https://dash.cloudflare.com/ for it.")
    out.code(["export TF_DATA_DIR=/x/.mcctl/terraform"])
    out.fail(
        "terraform apply failed:\nError: creating R2 bucket: 403 Forbidden",
        "give the admin token Workers R2 Storage > Edit",
        "re-run: mcctl init -C /x (it resumes here)",
    )
    tf.show_plan(tf.Plan(create=["cloudflare_r2_bucket.backups"], replace=["tailscale_tailnet_key.server"]))
    report = doctor.Report()
    report.add("tool git", "ok", "/usr/bin/git")
    report.add("github admin token", "warn", "expires in 20 days")
    report.add("dns", "fail", "mc.example.org has no AAAA record")
    report.add("panel", "skip", "panel.mode: off")
    doctor.show(report, out)
    st = state.load(root)
    st.record("preflight", "done")
    st.record("repo", "done")
    st.record("questions", "failed")
    init.show_checklist(init.Ctx(root, out, init.Deps(), init.Options()))  # type: ignore[arg-type]
    out.kv([("config repo", "https://github.com/octocat/mc-config"), ("server", "mc.lh.example:25565")])


def render(rich: bool, root: Path) -> str:
    buf = io.StringIO()
    out = ui.UI(out=buf, rich=rich, width=100, record=rich)
    with ui.using(out):
        screens(out, root)
    text = out.export_text() if rich else buf.getvalue()
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


@pytest.mark.parametrize("mode", ["plain", "rich"])
def test_snapshots_of_both_renderings(tmp_path, mode) -> None:
    got = render(mode == "rich", tmp_path)
    snap = SNAPSHOTS / f"screens.{mode}.txt"
    if UPDATE:
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(got)
    assert got == snap.read_text(), f"{snap} differs (MCCTL_UPDATE_SNAPSHOTS=1 pytest tests/test_ui.py)"


def test_plain_is_the_classic_format(tmp_path) -> None:
    text = render(False, tmp_path)
    assert "\n[7/17] GitHub tokens\n" in text
    assert "  [ok] GitHub admin token" in text and "  [!!] couldn't" in text and "  [xx] the token" in text
    assert "       fix: give the admin token" in text and "       then re-run: mcctl init -C /x" in text
    assert "Terraform will: 1 to add, 0 to change, 1 to replace, 0 to destroy" in text
    assert "[  ok] tool git" in text and "Checklist:" in text and "[!]  3 Your server" in text
    assert PAT in text and "\x1b" not in text  # the whole URL, no escape codes


def test_rich_has_links_icons_and_panels(tmp_path) -> None:
    buf = io.StringIO()
    out = ui.Out(buf, rich=True, width=100)
    out.todo([f"Open {PAT}"])
    out.ok("done")
    out.link("https://example.org/a?b=c", "docs:")
    raw = buf.getvalue()
    assert "\x1b]8;id=" in raw and PAT in raw  # OSC 8 hyperlink + the raw URL on its own line
    text = render(True, tmp_path)
    assert "Step 7/17  ·  GitHub tokens" in text and "6/17 steps done  ·  lh" in text
    assert "╭─ Your turn" in text and "github.com/settings/personal-access-tokens/new?… [1]" in text
    assert f"  [1] {PAT}" in text  # one line, never wrapped
    assert "✓ GitHub admin token" in text and "✗ the token" in text and "Fix  give the admin token" in text
    assert "Scope" in text and "Permission" in text and "Resources" in text


@pytest.mark.parametrize(
    ("env", "tty", "want"),
    [
        ({}, True, True),
        ({}, False, False),
        ({"NO_COLOR": "1"}, True, False),
        ({"CI": "true"}, True, False),
        ({"MCCTL_PLAIN": "1"}, True, False),
        ({"TERM": "dumb"}, True, False),
    ],
)
def test_mode_detection(env, tty, want) -> None:
    class Stream(io.StringIO):
        def isatty(self) -> bool:
            return tty

    assert ui.rich_wanted(Stream(), env) is want


def test_plain_flag_anywhere_and_inherited(monkeypatch) -> None:
    monkeypatch.delenv(ui.PLAIN_ENV, raising=False)
    assert cli.strip_plain(["--plain", "status", "-C", "x"]) == (["status", "-C", "x"], True)
    assert cli.strip_plain(["setup", "-C", "x", "--plain"]) == (["setup", "-C", "x"], True)
    assert cli.strip_plain(["rcon", "-C", "x", "say", "--plain"]) == (
        ["rcon", "-C", "x", "say", "--plain"],
        False,
    )
    assert cli.strip_plain(["status", "--", "--plain"]) == (["status", "--", "--plain"], False)
    ui.set_plain()
    assert os.environ[ui.PLAIN_ENV] == "1"  # a re-executed (pinned) mcctl inherits it
    ui.set_plain(False)
    assert ui.PLAIN_ENV not in os.environ


def test_rich_prompts_label_hidden_input_and_never_echo_secrets() -> None:
    buf = io.StringIO()
    asked: list[str] = []
    talk = ui.UI(
        out=buf,
        rich=True,
        width=100,
        read=lambda p: asked.append(p) or "",
        read_secret=lambda p: asked.append(p) or "s3cr3t-value",
        env={},
    )
    assert talk.ask("k", "Short name", default="lh") == "lh"
    assert talk.secret("t", "Paste the token", env_name="X") == "s3cr3t-value"
    assert talk.choose("v", "Minecraft version", ["26.2", "26.3"], default="26.3") == "26.3"
    text = buf.getvalue()
    assert "Short name" in text and "[lh]" in text and "(input hidden)" in text and "(recommended)" in text
    assert "s3cr3t-value" not in text and asked == ["", "", ""]  # the prompt is printed, not passed


def test_spinner_is_silent_in_plain_and_pauses_for_prompts() -> None:
    buf = io.StringIO()
    out = ui.UI(out=buf, rich=False, read=lambda p: "y")
    with out.spinner("waiting"):
        assert out.confirm("k", "Go?") is True
    assert buf.getvalue() == "  ? Go? (y/N): " or "waiting" not in buf.getvalue()
    rich_out = ui.UI(out=io.StringIO(), rich=True, width=80, read=lambda p: "y")
    with rich_out.spinner("waiting") as sp:
        assert sp.running
        with rich_out.spinner("nested") as inner:
            assert not inner.running  # one live display at a time
        assert rich_out.confirm("k", "Go?") is True
        assert sp.running  # resumed after the prompt
    assert not sp.running


def test_error_blocks_pick_terraforms_errors() -> None:
    text = (
        "cloudflare_r2_bucket.backups: Creating...\n\n"
        "Error: creating R2 bucket\n\n  with cloudflare_r2_bucket.backups,\n  on r2.tf line 1:\n\n"
        "403 Forbidden: the token lacks Workers R2 Storage Edit\n\n\n"
        "╷\n│ Error: Unsupported argument\n│ \n│   on main.tf line 9\n╵\n"
    )
    blocks = tf.error_blocks(text)
    assert len(blocks) == 2 and blocks[0].startswith("Error: creating R2 bucket")
    assert "403 Forbidden" in blocks[0] and "Creating..." not in blocks[0]
    assert blocks[1].startswith("Error: Unsupported argument") and "│" not in blocks[1]


def test_rich_terraform_output_goes_to_a_private_log(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "bin" / "terraform"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\necho 'aws_thing.x: Creating...'\necho\necho 'Error: boom happened'\necho\n"
        "echo '  the detail line'\nexit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    data = tmp_path / "cfg" / ".mcctl" / "terraform"
    terraform = tf.Terraform(tmp_path, {"TF_DATA_DIR": str(data), "PATH": os.environ["PATH"]})
    out = ui.Out(io.StringIO(), rich=True, width=100)
    with ui.using(out), pytest.raises(tf.TfError) as exc:
        terraform.apply(tmp_path / "plan")
    msg = str(exc.value)
    assert "Error: boom happened" in msg and "the detail line" in msg and "Creating..." not in msg
    logs = list((tmp_path / "cfg" / ".mcctl" / "logs").glob("terraform-apply-*.log"))
    assert len(logs) == 1 and f"Full output: {logs[0]}" in msg
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600 and "Creating..." in logs[0].read_text()


def test_failed_apply_outcome_table() -> None:
    plan = tf.Plan(create=["a.x", "b.y"], update=["c.z"])
    assert tf.apply_outcome(plan, ["a.x"]) == [
        ["ok: created", "a.x"],
        ["fail: NOT created", "b.y"],
        ["warn: maybe changed", "c.z"],
    ]
    assert tf.apply_outcome(plan, None) is None


def test_desktop_openers_and_clipboard(monkeypatch) -> None:
    started: list[list[str]] = []

    class Popen:
        def __init__(self, argv, **kw) -> None:
            assert kw["stdout"] == subprocess.DEVNULL and kw["stderr"] == subprocess.DEVNULL  # no gio noise
            started.append(argv)

    monkeypatch.setattr(desktop.subprocess, "Popen", Popen)
    url = "https://example.org/"
    assert desktop.open_browser(url, wsl=False, which=lambda n: n, platform="linux", fallback=lambda u: False)
    assert desktop.open_browser(url, wsl=False, which=lambda n: n, platform="darwin")
    assert started == [["xdg-open", url], ["open", url]]
    have = {"clip.exe", "pbcopy", "xclip"}

    def which(n: str) -> str | None:
        return n if n in have else None

    assert desktop.clipboard_argv(wsl=True, platform="linux", which=which) == ["clip.exe"]
    assert desktop.clipboard_argv(wsl=False, platform="darwin", which=which) == ["pbcopy"]
    assert desktop.clipboard_argv(wsl=False, platform="linux", which=which) == [
        "xclip",
        "-selection",
        "clipboard",
    ]
    got: dict = {}

    def run(argv, **kw):
        got.update(argv=argv, input=kw["input"])
        return subprocess.CompletedProcess(argv, 0)

    assert desktop.copy("ssh-ed25519 AAAA", wsl=True, which=which, run=run)
    assert got == {"argv": ["clip.exe"], "input": "ssh-ed25519 AAAA"}
    assert not ui.Out(io.StringIO(), rich=False).copy("x")  # plain runs never touch the clipboard


def test_rich_mock_setup_end_to_end_no_secrets(tmp_path) -> None:
    root = tmp_path / "c"
    talk = ui.UI(answers=ANSWERS, mock=True, env={}, out=io.StringIO(), rich=True, width=100, record=True)
    deps = mock.deps(root)
    assert init.run(root, talk, deps, init.Options(mock=True, product_sha=TEST_SHA)) == 0
    text = talk.export_text()
    assert (
        "Step 17/17" in text
        and "Step 1/17" in text
        and "Step 0/" not in text
        and "Token permissions" in text
        and "Checklist" in text
    )
    assert "What exists now" in text and "Terraform will" in text and "✓ add" in text
    for secret in (
        ui.MOCK_SECRETS["discord_webhook"],
        ui.MOCK_SECRETS["tailscale_api_key"],
        mock.ADMIN_TOKEN,
    ):
        assert secret not in text
    assert "<svg" in talk.export_svg("mcctl setup --mock")


def test_resumes_a_setup_started_before_the_cli_ux_change(tmp_path) -> None:
    """state.json written by the code before this change (a4b5716, paused at the host step with
    tests/e2e/answers.yml): the same step ids, statuses and input hashes as now, and the current code resumes
    from it: done steps are skipped, the host step continues, the run finishes."""
    old = json.loads((REPO / "tests/fixtures/state/pre-cli-ux.json").read_text())
    root = tmp_path / "c"
    out = io.StringIO()
    talk = ui.UI(answers=ANSWERS | {"host_ipv4": ""}, mock=True, env={}, out=out)
    assert init.run(root, talk, mock.deps(root), init.Options(mock=True, product_sha=TEST_SHA)) == 0
    now = json.loads((root / state.STATE_FILE).read_text())

    def shape(doc: dict) -> dict:
        return {k: (v["status"], v.get("input_hash")) for k, v in doc["steps"].items()}

    p14 = {"discord-endpoint"}  # P14's new step (after apply): not in the old state, runs on resume
    # P14: apply's inputs are {} (the old hash) while the slash commands are off, the Worker's DISCORD
    # binding when they're on (answers.yml turns them on, so this run's apply recorded the binding)
    assert old["steps"]["apply"]["input_hash"] == state.input_hash({})
    # (and since the immutable OIDC subject, github.oidc_subject's format: answers.yml leaves it immutable)
    cfg = configrepo.load(root).server
    binding = {"ci_oidc_subject": cfg.github.oidc_subject, "discord": render_mod.discord_commands(cfg)}
    binding["github_environments"] = True  # security review M2: the template sets github.environments
    assert now["steps"]["apply"]["input_hash"] == state.input_hash(binding)
    assert {k: v for k, v in shape(now).items() if k not in p14 | {"apply"}} == {
        k: v for k, v in shape(old).items() if k != "apply"
    }
    assert set(old["steps"]) <= set(init.STEP_IDS)
    # the old setup: its apply never gave the Worker a DISCORD binding, Discord has no endpoint
    (root / state.STATE_FILE).write_text(json.dumps(old))
    mock.worker_binding_file(root).unlink()
    (root / ".mcctl" / "mock-discord.json").unlink()
    out = io.StringIO()
    talk = ui.UI(answers=ANSWERS, mock=True, env={}, out=out)
    tf_log: list[str] = []
    assert (
        init.run(root, talk, mock.deps(root, tf_log=tf_log), init.Options(mock=True, product_sha=TEST_SHA))
        == 0
    )
    text = out.getvalue()
    assert "Resuming in" in text and "11/17 steps done before" in text
    assert text.count("done earlier (skipped)") == 5  # repo, questions, state-backend, product, tailscale
    assert "apply" in tf_log  # the discord step turned the commands on: apply re-ran (the binding) ...
    assert json.loads(mock.worker_binding_file(root).read_text())["public_key"] == mock.DISCORD_PUBLIC_KEY
    assert "Discord verified the Worker's signed PING" in text  # ... before discord-endpoint's PATCH
    assert all(state.load(root).is_done(s) for s in init.STEP_IDS)
