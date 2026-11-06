"""A config repo pinned to an older product commit than the running checkout (P15; seen often).

`mcctl setup`/`init` re-execute into the config repo's pin (mcctl/product.py), so a fix pulled into the
product checkout is invisible until the pin moves. This module finds how the pin relates to the running
checkout and, only on a typed `yes`, moves it forward:

    current     the pin is the running checkout's HEAD
    stale       the pin is an ancestor of HEAD: offered (forward along the product's history only)
    newer       HEAD is an ancestor of the pin: nothing to do (mcctl re-executes into the newer pin)
    unrelated   neither is an ancestor of the other: never offered (a fork, a rewritten branch, a typo)
    unknown     the running checkout is dirty / not git, or the pinned commit isn't in it: never offered

Moving rewrites the single `uses:` line (`updates.bump_pin`: SHA + version comment), adds the secrets the new
product's template passes and the workflow doesn't yet (v1.2, `mcctl/wfsecrets.py`), commits only the
workflow file, and pushes that one commit to main when the config repo exists on GitHub (the admin bypasses
the main ruleset). Guards (P15 review):

- **released line only**: the new SHA must be on `<remote>/main` of the product checkout's remote whose URL
  is the pin's repository (https or ssh form; fetched first, best effort), and the old pin an ancestor of
  it. A fork, a feature branch or a stale PR ref never counts: the config repo's CI checks the product out
  from that repository by SHA.
- **honest version comment** (`version_of`): the exact release tag (`vX.Y.Z`) when the commit is tagged,
  else `git describe --tags` (`vX.Y.Z-N-g<sha>`: N commits after vX.Y.Z), else, before the first tag,
  `v0.0.0-N-g<sha>`. Both untagged forms sort before the next release (`updates.newer` compares
  X.Y.Z then N), never auto-merge (`updates.is_patch` and the gate want plain vX.Y.Z on both sides), and
  run every migration after X.Y.Z (`migrations.pending`; migrations are idempotent).
- **one commit on an up-to-date main**: on GitHub the config repo must be on `main` with HEAD equal to the
  freshly fetched origin/main and the workflow file unmodified; otherwise nothing is changed (`git pull
  --rebase` first). A rejected push (not the admin, ruleset) explains how to open a pull request instead.

Every git/gh call goes through an injectable runner.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mcctl import configrepo, product, schema, updates, wfsecrets
from mcctl.providers import ProviderError
from mcctl.ui import UI

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Relation = Literal["current", "stale", "newer", "unrelated", "unknown"]
# The typed confirmation's answers key (tests/e2e/answers.yml; init.HUMAN_ONLY_ANSWERS outside --mock).
ANSWER_KEY = "move_product_pin"
SHOW_COMMITS = 8


@dataclass(frozen=True)
class Status:
    relation: Relation
    pin: configrepo.Pin
    running: str | None  # the running checkout's HEAD (None: dirty or not a checkout)
    behind: int = 0  # commits between the pin and HEAD (stale only)
    commits: tuple[str, ...] = ()  # `git log --oneline pin..HEAD`, newest first, at most SHOW_COMMITS
    reason: str = ""  # why unknown / unrelated

    @property
    def label(self) -> str:
        return self.pin.sha[:12] + (f" ({self.pin.version})" if self.pin.version else "")


def _git(run: Runner, product_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(product_root), *args])


def status(
    pin: configrepo.Pin, run: Runner, product_root: Path | None = None, running: str | None = None
) -> Status:
    """How `pin` relates to the product checkout at product_root (default: the running one; its HEAD unless
    `running` is given)."""
    product_root = product_root or schema.PRODUCT_ROOT
    running = running or product.checkout_sha(product_root)
    if running is None:
        return Status("unknown", pin, None, reason=f"{product_root} isn't a clean git checkout")
    if running == pin.sha:
        return Status("current", pin, running)
    if _git(run, product_root, "cat-file", "-e", f"{pin.sha}^{{commit}}").returncode != 0:
        return Status(
            "unknown", pin, running, reason=f"the pinned commit isn't in {product_root} (`git fetch` there?)"
        )
    if _git(run, product_root, "merge-base", "--is-ancestor", pin.sha, running).returncode == 0:
        count = _git(run, product_root, "rev-list", "--count", f"{pin.sha}..{running}").stdout.strip()
        span = f"{pin.sha}..{running}"
        log = _git(
            run, product_root, "log", "--oneline", "--no-decorate", f"-{SHOW_COMMITS}", span
        ).stdout.splitlines()
        return Status("stale", pin, running, int(count) if count.isdigit() else 0, tuple(log))
    if _git(run, product_root, "merge-base", "--is-ancestor", running, pin.sha).returncode == 0:
        return Status("newer", pin, running)
    return Status("unrelated", pin, running, reason="neither commit is an ancestor of the other")


def product_remote(run: Runner, product_root: Path, repository: str) -> str | None:
    """The product checkout's remote whose URL is github.com/<repository> (https or ssh form), if any."""
    names = _git(run, product_root, "remote").stdout.split()
    for name in names:
        url = _git(run, product_root, "remote", "get-url", name).stdout.strip()
        parsed = schema.parse_github_remote(url)
        if parsed and "/".join(parsed).lower() == repository.lower():
            return name
    return None


def published(run: Runner, product_root: Path, pin: configrepo.Pin, new: str) -> str | None:
    """None if `new` is on the product repository's main and the pin is its ancestor, else why not.

    Only the remote whose URL is the pin's repository counts (fetched first; a failed fetch falls back to the
    last fetched state and says so); other remotes, forks, feature branches and PR refs never do."""
    remote = product_remote(run, product_root, pin.repository)
    if remote is None:
        return f"no remote of {product_root} points at github.com/{pin.repository}"
    fetch = _git(run, product_root, "fetch", "-q", remote, "main")
    stale = ""
    if fetch.returncode != 0:
        stale = (
            f" (`git fetch {remote} main` failed: {(fetch.stderr or fetch.stdout).strip() or 'no output'};"
            " checked the last fetched state)"
        )
    ref = f"refs/remotes/{remote}/main"
    if _git(run, product_root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode != 0:
        return f"{remote}/main isn't known in {product_root}{stale}"
    if _git(run, product_root, "merge-base", "--is-ancestor", new, ref).returncode != 0:
        return f"{new[:12]} isn't on {pin.repository}'s main ({remote}/main){stale}"
    if _git(run, product_root, "merge-base", "--is-ancestor", pin.sha, new).returncode != 0:
        return f"the pinned {pin.sha[:12]} isn't an ancestor of {new[:12]}"
    return None


# `git describe --tags` output for a release tag, exact (vX.Y.Z) or N commits after it (vX.Y.Z-N-g<sha>)
DESCRIBED = re.compile(r"v\d+\.\d+\.\d+(-\d+-g[0-9a-f]{7,40})?")


def version_of(run: Runner, product_root: Path, sha: str) -> str:
    """The version comment for `sha`: its exact release tag, else `git describe --tags` (vX.Y.Z-N-g<sha>),
    else (no release tag yet) v0.0.0-N-g<sha>, which sorts before every release. Never a bare vX.Y.Z for an
    untagged commit: updates/gate/migrations trust a plain vX.Y.Z to be that release."""
    match = ("--match", "v[0-9]*.[0-9]*.[0-9]*", "--exclude", "v*-*")  # release tags, no pre-releases
    exact = _git(run, product_root, "describe", "--tags", "--exact-match", *match, sha)
    if exact.returncode == 0 and updates.RELEASE.fullmatch(exact.stdout.strip()):
        return exact.stdout.strip()
    described = _git(run, product_root, "describe", "--tags", "--abbrev=12", *match, sha)
    if described.returncode == 0 and DESCRIBED.fullmatch(out := described.stdout.strip()) and "-g" in out:
        return out
    count = _git(run, product_root, "rev-list", "--count", sha).stdout.strip()
    return f"v0.0.0-{count if count.isdigit() else 0}-g{sha[:12]}"


def rewrite(text: str, pin: configrepo.Pin, sha: str, version: str) -> str:
    """The workflow text with its one `uses:` line moved to `sha` (comment `# version`)."""
    try:
        return updates.bump_pin(text, pin.repository, sha, version)
    except ProviderError as e:
        raise PinMoveError(str(e)) from None


class PinMoveError(RuntimeError):
    pass


def describe(st: Status) -> str:
    """One line for doctor."""
    if st.relation == "stale":
        return (
            f"the config repo pins {st.label}, {st.behind} commit(s) behind this checkout "
            f"({st.running[:12] if st.running else '?'}): "
            "`mcctl setup` or `mcctl doctor --fix` offers to move it"
        )
    if st.relation == "unrelated":
        return f"the pinned {st.label} isn't on this checkout's history ({st.reason}): never moved for you"
    if st.relation == "newer":
        return f"the pinned {st.label} is newer than this checkout: commands re-execute into it"
    if st.relation == "unknown":
        return f"can't compare the pinned {st.label} with this checkout: {st.reason}"
    return f"this mcctl is the pinned {st.label}"


def offer(
    root: Path,
    talk: UI,
    run: Runner,
    *,
    product_root: Path | None = None,
    running: str | None = None,
    require_published: bool = True,
    version: str | None = None,
) -> bool:
    """If root's pin is behind the running product, show it and move it on a typed `yes`. True if moved."""
    product_root = product_root or schema.PRODUCT_ROOT
    wf = root / configrepo.WORKFLOW
    try:
        text = wf.read_text()
    except OSError:
        return False
    pin, _errors = configrepo.read_pin(text)
    if pin is None:
        return False
    st = status(pin, run, product_root, running)
    if st.relation == "unrelated":
        talk.warn(f"product pin: {describe(st)}")
        return False
    if st.relation != "stale" or st.running is None:
        return False
    new = st.running
    version = version or version_of(run, product_root, new)
    talk.warn(
        f"This config repo runs the product at {st.label}, {st.behind} commit(s) behind the checkout you are "
        f"running ({new[:12]}, {version}). Until the pin moves, mcctl runs the OLD code."
    )
    for line in st.commits:
        talk.info(f"    {line}")
    if st.behind > len(st.commits):
        talk.info(f"    ... and {st.behind - len(st.commits)} more")
    if require_published and (why := published(run, product_root, pin, new)) is not None:
        talk.warn(
            f"not offering to move it: {why}. Push the product commit to main first: the config repo's CI "
            "checks the product out from GitHub"
        )
        return False
    origin = configrepo.git_origin(root)
    on_github = (
        origin is not None and run(["gh", "repo", "view", "/".join(origin), "--json", "name"]).returncode == 0
    )
    if on_github and (why := _not_up_to_date(run, root)) is not None:
        talk.warn(
            f"not moving the pin: {why}. In {root}: `git switch main && git pull --rebase` (push or drop "
            "local commits), then `mcctl doctor --fix` or `mcctl setup` offers the move again"
        )
        return False
    moved = rewrite(text, pin, new, version)
    extra = _missing_secrets(moved, product_root, talk)  # v1.2: secrets the new product's template passes
    if extra:
        talk.info(f"    the new product also needs these secrets passed: {', '.join(extra)} (same commit)")
    answer = talk.typed(ANSWER_KEY)(
        f"Move the pin to {new[:12]} ({version}): commit {configrepo.WORKFLOW}"
        " and push it to main? Type yes to move it: "
    )
    if answer.strip().lower() != "yes":
        talk.info(f"left the pin at {st.label} (mcctl keeps running that version)")
        return False
    if extra:
        moved = wfsecrets.add(moved, extra)
    wf.write_text(moved)
    message = f"mcctl: move the product pin {pin.version or pin.sha[:12]} -> {version} ({new[:12]})"
    body = f"{pin.repository}@{pin.sha} -> {new}: {st.behind} commit(s) forward on the product's history."
    if extra:
        body += f" Passes the secrets the new product's template does: + {', '.join(extra)}."
    commit = run(
        ["git", "-C", str(root), "commit", "-q", "-m", message, "-m", body, "--", configrepo.WORKFLOW]
    )
    if commit.returncode != 0:
        wf.write_text(text)
        raise PinMoveError(f"`git commit` failed: {(commit.stderr or commit.stdout).strip()}")
    talk.ok(f"committed: {message}")
    if not on_github:
        talk.info(
            "the config repo isn't on GitHub (or gh can't see it): the commit is local; setup's config-repo "
            "step pushes it, or `git push origin HEAD:main`"
        )
        return True
    sha = run(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    push = run(["git", "-C", str(root), "push", "-q", "origin", f"{sha}:refs/heads/main"])
    if push.returncode != 0:
        branch = f"mcctl/product-pin-{new[:12]}"
        talk.warn(
            f"`git push` to main was rejected ({(push.stderr or push.stdout).strip() or 'no output'}): "
            "only the admin may push to main (ruleset). The commit is local; open a pull request instead, in "
            f"{root}: `git push origin HEAD:refs/heads/{branch} && gh pr create --head {branch} --fill && "
            "git reset -q --keep origin/main`"
        )
    else:
        talk.ok("pushed to main")
    return True


def _missing_secrets(text: str, product_root: Path, talk: UI) -> list[str]:
    try:
        return wfsecrets.missing(text, wfsecrets.template_text(product_root))
    except FileNotFoundError:  # not a product checkout with a template (tests' fake products)
        return []
    except (OSError, wfsecrets.WorkflowError) as e:
        talk.warn(f"couldn't compare the workflow's secrets with the product's template: {e}")
        return []


def _not_up_to_date(run: Runner, root: Path) -> str | None:
    """None if the config repo is on main, equal to the freshly fetched origin/main, with the workflow file
    unmodified (so the push carries exactly the one pin commit), else why not."""
    git = ["git", "-C", str(root)]
    fetch = run([*git, "fetch", "-q", "origin", "main"])
    if fetch.returncode != 0:
        return f"`git fetch origin main` failed ({(fetch.stderr or fetch.stdout).strip() or 'no output'})"
    branch = run([*git, "symbolic-ref", "-q", "--short", "HEAD"]).stdout.strip()
    if branch != "main":
        return f"the config repo is on {branch or 'a detached HEAD'}, not main"
    head = run([*git, "rev-parse", "HEAD"]).stdout.strip()
    upstream = run([*git, "rev-parse", "FETCH_HEAD"]).stdout.strip()
    if not head or head != upstream:
        return f"local main ({head[:12] or '?'}) isn't origin/main ({upstream[:12] or '?'})"
    if run([*git, "status", "--porcelain", "--", configrepo.WORKFLOW]).stdout.strip():
        return f"{configrepo.WORKFLOW} has uncommitted changes"
    return None
