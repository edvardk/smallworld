"""scripts/publish_fresh.py: export minus publish-exclude, the denylist and generic scans, the one-commit
repository (docs/publishing.md). Real git in a temp repository; `gh` is faked; nothing is pushed.
Identifying-looking samples are built at run time so this file passes tests/test_publishable.py itself."""

from __future__ import annotations

import io
import subprocess
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path

import publish_fresh as pf
import pytest

OWNER = pf.OWNER
CHANGELOG = """# Changelog

Preamble.

## [Unreleased]

### Fixed

- after the release

## [1.2.0] - 2026-11-06

### Added

- the release

## [1.1.0] - 2026-11-03

- older history
"""


class Fake:
    """Runs git for real, answers `gh api users/<owner>` with a fixed id, records every call."""

    def __init__(self, gh_id: str = "4242") -> None:
        self.gh_id, self.calls = gh_id, []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "gh":
            return subprocess.CompletedProcess(list(argv), 0, self.gh_id + "\n", "")
        return pf.default_runner(argv)


def git(repo: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    return subprocess.run(
        ["git", "-C", str(repo), *ident, *args], check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path: Path, files: dict[str, bytes | str], repository: str = pf.PUBLIC_REPOSITORY) -> Path:
    repo = tmp_path / "src"
    base: dict[str, bytes | str] = {
        "publish-exclude": "# history\nnotes/\nDESIGN.md\n",
        "pyproject.toml": '[project]\nname = "x"\nversion = "1.2.0"\n',
        "CHANGELOG.md": CHANGELOG,
        "mcctl/configrepo.py": f'PRODUCT_REPOSITORY = "{repository}"\n',
        "run.sh": "#!/bin/sh\necho hi\n",
    }
    for path, data in {**base, **files}.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
    (repo / "run.sh").chmod(0o755)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(repo)], check=True)
    git(repo, "add", "-A", "-f", ".")
    git(repo, "commit", "-q", "-m", "history\n\nCo-Authored-By: someone <t@example.invalid>")
    return repo


def deny(tmp_path: Path, *patterns: str) -> Path:
    path = tmp_path / "deny.txt"
    path.write_text("# test denylist\n## names\n" + "\n".join(patterns) + "\n")
    return path


def run_main(repo: Path, denylist: Path, *extra: str, runner: Fake | None = None) -> tuple[int, str, Fake]:
    runner = runner or Fake()
    lines: list[str] = []
    code = pf.main(["--source", str(repo), "--denylist", str(denylist), *extra], run=runner, out=lines.append)
    return code, "\n".join(lines), runner


def test_denylist_hit_refuses_with_file_and_line_never_the_value(tmp_path) -> None:
    repo = make_repo(tmp_path, {"docs/a.md": "fine\nThe Zorblax server\n"})
    code, out, _ = run_main(repo, deny(tmp_path, "zorblax"))
    assert code == 1
    assert "docs/a.md:2: names (deny.txt:3)" in out and "REFUSED" in out
    assert "zorblax" not in out.lower()
    assert "hits by category: names 1" in out


def test_paths_binaries_and_zip_members_are_scanned(tmp_path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/MANIFEST.MF", "Built-By: zorblax\n")
        z.writestr("zorblax/Main.class", b"\xca\xfe")
    repo = make_repo(
        tmp_path,
        {
            "art/zorblax.txt": "nothing\n",
            "art/icon.png": b"\x89PNG\0\0 zorblax \xff\xfe",
            "plugin/x.jar": buf.getvalue(),
        },
    )
    code, out, _ = run_main(repo, deny(tmp_path, "zorblax"))
    assert code == 1
    assert "art/zorblax.txt (path)" in out
    assert "art/icon.png:1: names" in out
    assert "plugin/x.jar!/META-INF/MANIFEST.MF:1: names" in out
    assert "plugin/x.jar!/zorblax/Main.class (path)" in out


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


def test_png_pixels_are_not_scanned_but_its_text_chunks_are(tmp_path) -> None:
    """A real PNG's compressed pixel data is random bytes (short patterns match by chance); its metadata
    chunks, inflated zTXt included, are scanned."""
    ihdr = _png_chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + b"\x08\x02\0\0\0")
    end = _png_chunk(b"IEND", b"")
    pixels = pf.PNG_SIGNATURE + ihdr + _png_chunk(b"IDAT", b"zorblax") + end
    meta = pf.PNG_SIGNATURE + ihdr + _png_chunk(b"zTXt", b"Author\0\0" + zlib.compress(b"zorblax")) + end
    repo = make_repo(tmp_path, {"a.png": pixels, "b.png": meta})
    code, out, _ = run_main(repo, deny(tmp_path, "zorblax"))
    assert code == 1
    assert "a.png" not in out
    assert "b.png:" in out


def test_elf_binaries_are_scanned_by_their_strings(tmp_path) -> None:
    """v1.4: an ELF executable (rust/dist/) is scanned by its printable runs of 8+ characters: a pattern inside
    random machine-code bytes doesn't count, embedded text does."""
    noise = pf.ELF_MAGIC + b"\x02\x01\x01\0\x93\x01zorblax\xff\x00\x12"
    text = pf.ELF_MAGIC + b"\x02\x01\x01\0\x93/home/zorblax/src\0"
    repo = make_repo(tmp_path, {"dist/a": noise, "dist/b": text})
    code, out, _ = run_main(repo, deny(tmp_path, "zorblax"))
    assert code == 1
    assert "dist/a" not in out
    assert "dist/b:" in out


def test_allowed_forms_pass_and_others_hit(tmp_path) -> None:
    ok = (
        f"https://github.com/{pf.PUBLIC_REPOSITORY}\nuses: {pf.PUBLIC_REPOSITORY}/.github/workflows/x.yml@abc\n"
        f"package io.github.{OWNER}.mcbrand;\nsrc/main/java/io/github/{OWNER}/X.java\n"
        f"$SwitchMap$io$github${OWNER}$mcbrand$Hud$Mode\n"
        f"Owner: https://github.com/{OWNER}\n12345+{OWNER}@users.noreply.github.com\n"
    )
    repo = make_repo(tmp_path, {"README.md": ok, f"src/io/github/{OWNER}/A.java": "class A {}\n"})
    code, out, _ = run_main(repo, deny(tmp_path, rf"\b{OWNER}\b"))
    assert code == 0, out
    assert "clean" in out

    bad = make_repo(tmp_path / "b", {"README.md": f"see github.com/{OWNER}/private-config\n"})
    code, out, _ = run_main(bad, deny(tmp_path, rf"\b{OWNER}\b"))
    assert code == 1 and "README.md:1: names" in out and "README.md:1: owner (generic)" in out


def test_excluded_paths_are_neither_scanned_nor_published(tmp_path) -> None:
    repo = make_repo(tmp_path, {"notes/plan.md": "zorblax\n", "DESIGN.md": "zorblax\n", "a.txt": "ok\n"})
    runner = Fake()
    files, excludes = pf.select(pf.export(runner, repo, "HEAD"), "1.2.0")
    assert excludes == ["notes/", "DESIGN.md"]
    assert "a.txt" in files and "notes/plan.md" not in files and "DESIGN.md" not in files
    assert files["run.sh"].mode == 0o100755 and files["a.txt"].mode == 0o100644
    code, out, _ = run_main(repo, deny(tmp_path, "zorblax"))
    assert code == 0, out
    assert "2 excluded" in out


def test_a_mention_of_an_excluded_path_is_a_problem(tmp_path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "notes/plan.md": "x\n",
            "docs/guide.md": "See [the plan](../notes/plan.md).\n",
            "b.py": "# DESIGN.md\n",
            "DESIGN.md": "x\n",
        },
    )
    code, out, _ = run_main(repo, deny(tmp_path, "zorblax"))
    assert code == 1
    assert "docs/guide.md:1: excluded-reference" in out and "b.py:1: excluded-reference" in out
    assert "publish-exclude" not in out  # the exclude file itself names them


def test_changelog_starts_at_the_release() -> None:
    text = pf.public_changelog(CHANGELOG, "1.2.0")
    assert text.startswith("# Changelog\n\nPreamble.\n\n## [Unreleased]\n")
    assert "## [1.2.0] - 2026-11-06" in text and "the release" in text and "after the release" in text
    assert "1.1.0" not in text and "older history" not in text
    assert text.endswith(pf.changelog_line("1.2.0") + "\n")
    with pytest.raises(pf.PublishError, match=r"no `## \[1.3.0\]"):
        pf.public_changelog(CHANGELOG, "1.3.0")
    # An empty [Unreleased] is added when the source has none (tests/test_release.py wants one).
    assert "## [Unreleased]" in pf.public_changelog(
        CHANGELOG.replace("## [Unreleased]", "## [9.9.9] - x"), "1.2.0"
    )


def test_create_makes_one_commit_with_the_noreply_identity_and_no_trailers(tmp_path) -> None:
    repo = make_repo(tmp_path, {"a.txt": "ok\n", ".gitignore": "*.log\n", "kept.log": "tracked anyway\n"})
    dest = tmp_path / "public"
    code, out, runner = run_main(repo, deny(tmp_path, "zorblax"), "--create", str(dest))
    assert code == 0, out
    assert ["gh", "api", f"users/{OWNER}", "--jq", ".id"] in runner.calls
    log = git(dest, "log", "--format=%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00%(trailers)", "main")
    sha, parents, an, ae, cn, ce, body, trailers = log.split("\0")
    email = f"4242+{OWNER}@users.noreply.github.com"
    assert (parents, an, ae, cn, ce) == ("", OWNER, email, OWNER, email)
    assert body.strip() == "Smallworld v1.2.0" and trailers.strip() == ""
    assert git(dest, "rev-list", "--count", "--all").strip() == "1"
    assert git(dest, "rev-parse", "v1.2.0^{commit}").strip() == sha
    published = set(git(dest, "ls-tree", "-r", "--name-only", sha).split())
    base = {"publish-exclude", "pyproject.toml", "CHANGELOG.md", "mcctl/configrepo.py", "run.sh"}
    assert published == base | {"a.txt", ".gitignore", "kept.log"}  # ignored but tracked: kept
    assert "100755 blob" in git(dest, "ls-tree", sha, "run.sh")
    assert "older history" not in (dest / "CHANGELOG.md").read_text()
    # It prints the commands and runs none of them.
    for cmd in (
        f"gh repo create {pf.PUBLIC_REPOSITORY} --public",
        "push -u origin main",
        "push origin v1.2.0",
    ):
        assert cmd in out
    assert "mcctl product-setup --repo " + pf.PUBLIC_REPOSITORY in out and "gh repo archive" in out
    assert not [c for c in runner.calls if "push" in c or c[:3] == ["gh", "repo", "create"]]
    assert [c[:2] for c in runner.calls if c[0] == "gh"] == [["gh", "api"]]


def test_noreply_id_flag_needs_no_network(tmp_path) -> None:
    repo = make_repo(tmp_path, {})
    dest = tmp_path / "public"
    code, out, runner = run_main(repo, deny(tmp_path, "zorblax"), "--create", str(dest), "--noreply-id", "7")
    assert code == 0, out
    assert not [c for c in runner.calls if c[0] == "gh"]
    assert git(dest, "log", "-1", "--format=%ae").strip() == f"7+{OWNER}@users.noreply.github.com"


def test_create_refuses_on_hits_a_non_empty_dest_or_the_old_repository_name(tmp_path) -> None:
    repo = make_repo(tmp_path, {"a.txt": "zorblax\n"})
    dest = tmp_path / "public"
    assert run_main(repo, deny(tmp_path, "zorblax"), "--create", str(dest))[0] == 1
    assert not dest.exists()

    old = make_repo(tmp_path / "o", {}, repository="someone/old-name")
    code, out, _ = run_main(old, deny(tmp_path, "zorblax"), "--create", str(dest))
    assert code == 2 and "PRODUCT_REPOSITORY" in out and not dest.exists()
    assert "--create refuses" in run_main(old, deny(tmp_path, "zorblax"))[1]  # the dry run says so

    clean = make_repo(tmp_path / "c", {})
    (dest / "x").mkdir(parents=True)
    code, out, _ = run_main(clean, deny(tmp_path, "zorblax"), "--create", str(dest))
    assert code == 2 and "isn't empty" in out


def test_the_denylist_must_exist_be_valid_and_stay_unpublished(tmp_path) -> None:
    repo = make_repo(tmp_path, {"deny.txt": "## names\nzorblax\n"})
    code, out, _ = run_main(repo, repo / "deny.txt")
    assert code == 2 and "would be published" in out
    code, out, _ = run_main(repo, tmp_path / "missing.txt")
    assert code == 2 and "no denylist" in out
    with pytest.raises(pf.PublishError, match="no patterns"):
        pf.parse_denylist("# only comments\n## names\n")
    with pytest.raises(pf.PublishError, match="d:2: not a valid regex"):
        pf.parse_denylist("ok\n(unclosed\n", "d")
    with pytest.raises(pf.PublishError, match="empty string"):
        pf.parse_denylist("a*\n")
    rules = pf.parse_denylist("one\n## domains\ntwo\n(?-i:\\bAb\\b)\n")
    assert [(r.category, r.source) for r in rules] == [
        ("uncategorised", "denylist:1"),
        ("domains", "denylist:3"),
        ("domains", "denylist:4"),
    ]
    assert rules[2].pattern.search("x Ab y") and not rules[2].pattern.search("x ab y")


def test_generic_checks() -> None:
    session = "https://" + "claude.ai/code/" + "session_0123"
    public_v4 = ".".join(["45", "10", "20", "30"])
    real_mail = "alex@" + "mail" + ".org"
    tz = "Europe" + "/" + "Oslo"
    files = {
        "a.md": f"{session}\n{real_mail}\nhost {public_v4}\nowner {OWNER} here\n",
        "mcctl/schema.py": f'timezone = "{tz}"\n',
        "other.py": f'example = "{tz}"\n',
        "fine.txt": (
            "a@example.com b@corp.example c@x.test d@y.internal git@github.com noreply@anthropic.com\n"
            "mc-restore@abc.service 192.0.2.7 198.51.100.1 2001:db8::1 3ffe:ffff::1 9.9.9.9 2620:fe::fe\n"
            "10.0.0.1 100.64.0.1 224.0.0.1 3fff::1 fd7a:115c:a1e0::1 ::1 ffff:ffff:ffff:ffff:: 2000::/3 12:30:45 v1.2.3.4\n"
            f"::ffff:192.0.2.1 25.0.5.0.0+8 io.github.{OWNER}.x github.com/{OWNER}\n"
        ),
        "icon.png": b"\x89PNG\0" + public_v4.encode() + b" " + real_mail.encode(),
    }
    entries = {p: pf.Entry(0o100644, d if isinstance(d, bytes) else d.encode()) for p, d in files.items()}
    got = sorted(str(h) for h in pf.generic_problems(entries))
    assert got == [
        "a.md:1: session-link (generic)",
        "a.md:2: email (generic)",
        "a.md:3: public-ip (generic)",
        "a.md:4: owner (generic)",
        "mcctl/schema.py:1: timezone-default (generic)",
    ]
