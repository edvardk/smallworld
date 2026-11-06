"""P9 panel: the UI's static files, their wiring into the Worker and Terraform, and the Playwright smoke test of
the real UI against a local mock API (watchdog/test/ui.smoke.mjs; skipped where Playwright isn't installed, e.g.
CI: the Worker-side API and Access JWT checks run everywhere through watchdog/test/panel.test.mjs)."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess

import pytest
from conftest import REPO

PANEL = REPO / "watchdog" / "panel"
TF = (REPO / "terraform" / "panel.tf").read_text()
WATCHDOG_TF = (REPO / "terraform" / "watchdog.tf").read_text()
JS = (REPO / "watchdog" / "worker.js").read_text()
# OFL 1.1 (watchdog/panel/fonts/OFL.txt). Google Fonts' latin subsets, further subset to the glyphs the panel uses
# (panel perf): Chakra Petch and Pixelify Sans to Latin-1 + typographic punctuation, Pixelify instanced at its one
# used weight (700), JetBrains Mono's weight axis cut to 400-700 (the console keeps the full latin range). The OFL
# allows modified versions; none of these three has a Reserved Font Name. IBM Plex Sans has one ("Plex"), so it
# stays exactly as Google serves it. Reproduce with fonttools 4.55.3 (+ brotli):
#   fonttools varLib.instancer <font> wght=700|wght=400:700 -o <ttf>
#   pyftsubset <font|ttf> --unicodes=U+0020-007E,U+00A0-00FF,U+0131,U+0152-0153,U+02C6,U+02DA,U+02DC,U+2013-2014,
#     U+2018-201E,U+2022,U+2026,U+2039-203A,U+20AC,U+2122,U+2212 --layout-features='*' --no-hinting
#     --desubroutinize --flavor=woff2   (JetBrains Mono: Google's latin range, U+0000-00FF,U+2000-206F,...)
FONTS = {
    "chakra-petch-500.woff2": "b63ea272a3ba9fea609fc85f97e52be2d35a2aeda234e503c94cfac991ba68d2",
    "chakra-petch-600.woff2": "d77b5c842ae75d2e15dd7f2770017f450407fa8e99770ac01b1d90d3e8ccee39",
    "chakra-petch-700.woff2": "4cf012abd0a41ee1ae29ca71f7abb0c0121d8fe760f7cfd9cc581149196c6d3c",
    "ibm-plex-sans.woff2": "e2291e842cf5af167122a22881a740c7f2dda7716f1e8cd76680264f4a859470",
    "jetbrains-mono.woff2": "eadba59dd9c8785a776040f745eacf6c3380befad0dbbd5bb302f55fc40c7d5c",
    "pixelify-sans.woff2": "bf6f19c2997fb214e015b40328ced6b346f84e799794390c35b2727ada569b59",
}


def test_fonts_are_pinned_and_licensed() -> None:
    got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (PANEL / "fonts").glob("*.woff2")}
    assert got == FONTS
    licence = (PANEL / "fonts" / "OFL.txt").read_text()
    assert licence.count("SIL Open Font License, Version 1.1") >= 4


def test_ui_has_no_inline_code_styles_or_third_party_urls() -> None:
    """The CSP is script-src/style-src 'self' with no 'unsafe-inline': nothing inline may be needed."""
    html = (PANEL / "index.html").read_text()
    confirm_html = (PANEL / "confirm.html").read_text()
    assert '<script type="module" src="/panel.js"></script>' in html
    assert '<script type="module" src="/confirm.js"></script>' in confirm_html
    client_html = (PANEL / "client" / "index.html").read_text()  # v1.1: the setup page, same rules
    assert '<script type="module" src="/client.js"></script>' in client_html
    for page in (html, confirm_html, client_html):
        assert not re.search(r"<script(?![^>]*\bsrc=)", page) and "<style" not in page
    js = (PANEL / "panel.js").read_text()
    confirm_js = (PANEL / "confirm.js").read_text()
    css = (PANEL / "panel.css").read_text()
    assert "eval(" not in confirm_js and not re.search(r"^import ", confirm_js, re.M)
    client_js = (PANEL / "client" / "client.js").read_text()
    client_css = (PANEL / "client" / "client.css").read_text()
    svgs = [p.read_text() for p in (PANEL / "client" / "img").glob("*.svg")]
    assert not re.search(r"^import ", client_js, re.M) and "eval(" not in client_js
    for text in (html, confirm_html, js, confirm_js, css, client_html, client_js, client_css, *svgs):
        assert not re.search(r"""style=["'$]""", text), "inline style attribute"
        assert not re.search(r"\bon[a-z]+=", text), "inline event handler"
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), "third-party URL"
    assert "eval(" not in js and "new Function" not in js
    assert not re.search(r"^import ", js, re.M)  # one module, no dependencies


def test_ui_escapes_server_strings() -> None:
    js = (PANEL / "panel.js").read_text()
    # every template literal that interpolates into innerHTML goes through esc() for strings from the API;
    # spot-check the fields that carry text written by players or the server
    for field in ("r.msg", "a.msg", "w.text", "b.reason", "a.login", "l.text", "p.file", "w.name"):
        assert f"esc({field}" in js or f"esc({field})" in js, field


def test_worker_serves_only_known_assets_and_terraform_uploads_them() -> None:
    m = re.search(r"const ASSET_PATHS = (/.*/);", JS)
    c = re.search(r"const CONFIRM_ASSETS = (/.*/);", JS)
    assert m and c
    served = {p.relative_to(PANEL).as_posix() for p in PANEL.rglob("*") if p.is_file()}
    pattern = re.compile(m.group(1).strip("/").replace("\\/", "/"))
    confirm = re.compile(c.group(1).strip("/").replace("\\/", "/"))
    k = re.search(r"const PACK_PATH = (/.*/);", JS)
    assert k
    pack = re.compile(k.group(1).strip("/").replace("\\/", "/"))
    panel_files = {s for s in served if pattern.fullmatch("/" + s)}
    confirm_files = {s for s in served if confirm.fullmatch("/" + s)}
    pack_files = {s for s in served if pack.fullmatch("/" + s)}  # P17: public on the watchdog host
    # v1.2: the product commits no pack; mcctl render stages one next to these files (pack/<sha1>.zip)
    assert not pack_files and pack.fullmatch("/pack/" + "0" * 40 + ".zip")
    assert not (panel_files | confirm_files) & {"pack/" + "0" * 40 + ".zip"}
    # v1.1: the setup page's files (client/<path> served as /<path> on the client host; fonts/favicon shared)
    cl = re.search(r"const CLIENT_ASSETS = (/.*/);", JS)
    assert cl
    client_re = re.compile(cl.group(1).strip("/").replace("\\/", "/"))
    client_files = {
        s
        for s in served
        if s == "client/index.html"
        or (s.startswith("client/") and client_re.fullmatch("/" + s.removeprefix("client/")))
    }
    assert {"client/index.html", "client/client.js", "client/client.css"} <= client_files
    assert not client_files & (panel_files | confirm_files | pack_files)
    assert panel_files | confirm_files | pack_files | client_files == served - {"fonts/OFL.txt"}
    # P10 review: the confirmation page only on its host, the panel's app (index/panel.js) never there
    assert not panel_files & {"confirm.html", "confirm.js"}
    assert not confirm_files & {"index.html", "panel.js"}
    assert (
        'directory = var.watchdog_assets_dir != "" ? var.watchdog_assets_dir : "${path.module}/../watchdog/panel"'
        in WATCHDOG_TF
    )
    assert "run_worker_first   = true" in WATCHDOG_TF
    assert '{ type = "assets", name = "ASSETS" }' in WATCHDOG_TF  # every mode (P17's pack)
    assert '{ type = "plain_text", name = "PANEL", text = jsonencode(local.panel_config) }' in TF
    assert "local.panel_bindings" in WATCHDOG_TF


def test_terraform_panel_config_matches_what_the_worker_reads() -> None:
    keys = set(
        re.findall(
            r"^\s{4}(\w+)\s+=",
            TF.split("panel_config = local.cloudflare_mode ? {", 1)[1].split("} : null")[0],
            re.M,
        )
    )
    read = set(re.findall(r"raw\.(\w+)", JS.split("function panelConfig(env)", 1)[1].split("\n}\n", 1)[0]))
    assert keys == read
    # Access: the application's AUD tag and the organization's auth domain reach the Worker
    assert "cloudflare_zero_trust_access_application.panel[0].aud" in TF
    assert "data.cloudflare_zero_trust_organization.this[0].auth_domain" in TF
    # first live apply: Terraform only reads the organization (mcctl sets its global session, step zero-trust);
    # an adopted one leaves the state without being touched
    assert 'resource "cloudflare_zero_trust_organization"' not in TF and "import {" not in TF
    assert "removed {\n  from = cloudflare_zero_trust_organization.this" in TF and "destroy = false" in TF
    assert 'decision   = "allow"' in TF and "{ email = { email = email } }" in TF
    # the one-time PIN provider: an existing one (not ours) is reused, else ours is created; both apps use it
    assert (
        'p.type == "onetimepin"' in TF
        and "count = local.cloudflare_mode && length(local.existing_otp) == 0" in TF
    )
    assert "allowed_idps               = [local.panel_idp]" in TF
    assert "allowed_idps               = [local.confirm_idp]" in TF and "confirm_idp = local.otp_idp" in TF
    assert 'type       = "github"' in TF and "github[0].id : local.otp_idp" in TF


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_ui_smoke_with_playwright(tmp_path) -> None:
    probe = subprocess.run(
        ["node", "-e", "require.resolve('playwright')"], capture_output=True, text=True, check=False, cwd=REPO
    )
    if probe.returncode != 0:
        pytest.skip("Playwright isn't installed (the Worker tests cover the API; run ui.smoke.mjs locally)")
    proc = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--import",
            "./watchdog/test/register.mjs",
            "watchdog/test/ui.smoke.mjs",
            str(tmp_path),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "0 failure(s)" in proc.stdout
    assert (tmp_path / "panel-desktop.png").stat().st_size > 10000
    assert (tmp_path / "panel-phone.png").stat().st_size > 10000
    # P10: the action dialogs (restart with its countdown, restore with its typed confirmation)
    for shot in ("panel-restart-desktop.png", "panel-restart-phone.png", "panel-restore-desktop.png"):
        assert (tmp_path / shot).stat().st_size > 10000, shot


def test_console_suggestions_stay_on_the_agent_allowlist() -> None:
    import mc_agent

    js = (PANEL / "panel.js").read_text()
    block = js.split("const CMDS = [", 1)[1].split("];", 1)[0]
    heads = {c.split()[0] for c in re.findall(r'\["([^"]+)", "', block)}
    assert heads and heads <= set(mc_agent.CONSOLE_ALLOWLIST), heads - set(mc_agent.CONSOLE_ALLOWLIST)


def test_restore_confirmation_is_typed_on_the_confirmation_page() -> None:
    """p11-review: the URL fragment can't pre-fill a restore's typed confirmation (a crafted link could)."""
    js = (PANEL / "confirm.js").read_text()
    assert "delete args.confirm" in js and 'id="cTyped"' in js
    assert "ok.disabled = typed.value !== want" in js and "confirm: typed.value" in js
    assert "args.confirm ||" not in js
