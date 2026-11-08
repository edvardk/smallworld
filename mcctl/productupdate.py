"""`mcctl product-update` (v1.5): the config repo's "Run workflow" button that pins a product release now.

The maintainer runs the config repo's workflow by hand (Actions > server > Run workflow, task
`product-update`, product_version `latest` or `vX.Y.Z`). The reusable workflow then does what the weekly
update would do for the product, plus what `mcctl upgrade` does, without waiting for the schedule and without
Terraform or any Cloudflare credential (the Worker stays a local step):

- `resolve` (job `product-update`; no secret but the read-only product token): the release (`latest` =
  releases/latest; a tag is resolved to its commit), the pin it moves from. Already pinned there: the run's
  summary says so and nothing else happens. Older than the pin: refused (no downgrades). Else the pin is
  moved in the job's checkout of the config repo.
- the job then checks the **target** release out and runs its `mcctl schema-migrate --from <old>` and
  `mcctl validate` on that checkout (as `mcctl upgrade` does from a laptop; the target is a tag of the same
  product repository, the code the pin is about to run anyway).
- `prepared` (same job): which files changed (the workflow, server.yml) and whether the Worker's inputs
  changed between the two releases (`worker_diff`: the files Terraform reads for the Worker and the panel,
  compared by content); both go to the next job as outputs.
- `pr` (job `product-update-pr`, a fresh runner; the updater GitHub App's key in that one step's env): the
  pin pull request on `update/product-vX.Y.Z`, the branch the weekly updater uses, so the two never collide.
  An open pull request for the release is **reused**: the updater's `update/product-vX.Y.Z` (brought up to
  date with the target's migration when it lacks it) or the admin's `upgrade/product-vX.Y.Z` from
  `mcctl upgrade` (left as it is: the App can't write there). A leftover branch without an open pull
  request is reset to main first. Who merges is unchanged: auto-merge only for a pure patch bump (only the
  `uses:` line, `automation.auto_merge: builds+patch`, `github.protect_main`), which `server / gate` checks
  again; anything else waits for the admin. The summary (`$GITHUB_STEP_SUMMARY`) names the old and new
  version, the pull request and its state, and the exact local command when the Worker changed.
- `previous-pin` (validate) and `deploy-summary` (deploy, always): a push that moved the pin gets the same
  summary on its deploy run, with the deploy's result.

Nothing here prints a secret: the App key and tokens come from the environment and only ever reach
`mcctl.http` headers. Every request goes through `mcctl.http` (tests use a fake GitHub API).
"""

from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcctl import configrepo, gate, ghapp, http, updates
from mcctl.providers import ProviderError

WORKFLOW = configrepo.WORKFLOW
SHA = re.compile(r"[0-9a-f]{40}")
# What `mcctl render` + `mcctl apply` upload for the Worker and the panel, and all else Terraform reads:
# watchdog/ (worker.js, panel/; not its tests), terraform/, and render.py (the tfvars and staged assets).
WORKER_INPUTS = ("watchdog", "terraform", "mcctl/render.py")
WORKER_SKIP_DIRS = ("watchdog/test",)
SKIP_PARTS = {".terraform", "__pycache__", "node_modules", ".pytest_cache"}
SHOW_PATHS = 6
PLACEHOLDER = "<your clone of {repository}>"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class UpdateError(ValueError):
    pass


# --- did the Worker change? ----------------------------------------------------------------------------


def worker_inputs(root: Path) -> dict[str, str] | None:
    """{relative path: sha256} of the Worker's inputs in a product checkout (None: not a product checkout)."""
    if not (root / "watchdog" / "worker.js").is_file() or not (root / "terraform").is_dir():
        return None
    out: dict[str, str] = {}
    for name in WORKER_INPUTS:
        base = root / name
        paths = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in paths:
            rel = path.relative_to(root).as_posix()
            if SKIP_PARTS & set(rel.split("/")) or any(rel.startswith(d + "/") for d in WORKER_SKIP_DIRS):
                continue
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def worker_diff(old_root: Path, new_root: Path) -> list[str] | None:
    """The Worker inputs that differ between two product checkouts (sorted), or None when either isn't one."""
    old, new = worker_inputs(old_root), worker_inputs(new_root)
    if old is None or new is None:
        return None
    return sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))


def worker_state(changed: list[str] | None) -> str:
    """true | false | unknown (a job output)."""
    return "unknown" if changed is None else ("true" if changed else "false")


def _paths_note(paths: Sequence[str]) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:SHOW_PATHS])
    more = len(paths) - SHOW_PATHS
    return shown + (f" and {more} more" if more > 0 else "")


# --- the target release --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    repository: str
    old: str  # the pin's version comment, else its short sha
    old_sha: str
    new: str  # vX.Y.Z
    sha: str

    @property
    def current(self) -> bool:
        return self.old_sha == self.sha

    @property
    def patch(self) -> bool:
        return updates.is_patch(self.old, self.new)

    @property
    def migrate_from(self) -> str:
        """`schema-migrate --from`: the pinned release, or 0.0.0 (every migration) for an unknown pin."""
        m = updates.RELEASE.match(self.old)
        return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else "0.0.0"


def resolve(pin: configrepo.Pin, version: str, token: str, transport: http.Transport) -> Target:
    """`latest` (or empty): the product's latest release; `vX.Y.Z`: that tag. Refuses downgrades."""
    version = version.strip() or "latest"
    old = pin.version or pin.sha[:12]
    if version == "latest":
        release = updates.latest_release(pin.repository, token, transport)
        if release is None:
            raise UpdateError(f"{pin.repository} has no release (or its tag can't be read)")
        tag, sha = release
    else:
        if not updates.RELEASE.fullmatch(version):
            raise UpdateError(f"product_version must be `latest` or a release tag vX.Y.Z, not {version!r}")
        found = gate.tag_sha(pin.repository, version, token, transport)
        if not found or not SHA.fullmatch(found):
            raise UpdateError(f"{pin.repository} has no tag {version}")
        tag, sha = version, found
    target = Target(pin.repository, old, pin.sha, tag, sha)
    if not target.current and not updates.newer(tag, pin.version):
        raise UpdateError(f"{tag} isn't newer than the pinned {old}: no downgrades")
    return target


# --- the summary -----------------------------------------------------------------------------------------


def run_locally(repository: str, target: str, worker: list[str] | None, *, merged: bool) -> list[str]:
    """The summary's "Run locally" section (Markdown lines)."""
    commands = [
        "```sh",
        f"cd {PLACEHOLDER.format(repository=repository)}",
        "git pull --ff-only",
        f"mcctl rollout --to {target}",
        "```",
    ]
    rollout_does = (
        f"`mcctl rollout` sees the pin is already {target} and goes straight to `mcctl render`, `mcctl plan` "
        "and, after a yes, `mcctl apply` (it shows the plan again and asks you to type the deployment name)."
        if merged
        else f"`mcctl rollout` finds the open pull request, follows its checks, merges it after you type "
        f"{target} (or reports the gate's auto-merge), watches the deploy, then runs `mcctl render`, "
        "`mcctl plan` and, after a yes, `mcctl apply` (typed confirmation)."
    )
    lines = ["### Run locally", ""]
    if worker is None:
        return [
            *lines,
            "Couldn't compare the Worker's inputs between the two releases. **Run this if the panel or "
            "Worker changed** (the release notes say so; a plan without changes needs no apply):",
            "",
            *commands,
            "",
            rollout_does,
        ]
    if worker:
        return [
            *lines,
            f"The Worker or Terraform inputs changed ({_paths_note(worker)}): "
            + ("after the deploy, run this on your computer:" if merged else "run this on your computer:"),
            "",
            *commands,
            "",
            rollout_does,
        ]
    return [
        *lines,
        "**No local step needed:** nothing the Worker or Terraform reads changed between the two releases."
        + (
            ""
            if merged
            else f" Merge the pull request (on GitHub, or `mcctl rollout --to {target}` in your clone, which "
            "merges after you type the tag); the merge deploys."
        ),
        "",
        "(The panel's Health > Versions row shows the Worker one version behind until your next "
        "`mcctl render` + `mcctl apply`; that is only the version stamp.)",
    ]


def notes_url(repository: str, tag: str) -> str:
    return f"https://github.com/{repository}/releases/tag/{tag}"


def summary(
    *,
    title: str,
    repository: str,
    target: Target,
    pr: str,
    deploy: str,
    worker: list[str] | None,
    merged: bool,
    show_local: bool = True,
) -> str:
    worker_row = (
        "couldn't compare"
        if worker is None
        else (f"changed: {_paths_note(worker)}" if worker else "unchanged (nothing Terraform reads)")
    )
    rows = [
        f"## {title}",
        "",
        "| | |",
        "|---|---|",
        f"| Product | `{target.old}` -> **`{target.new}`** "
        f"([release notes]({notes_url(target.repository, target.new)})) |",
        f"| Pull request | {pr} |",
        f"| Deploy | {deploy} |",
        f"| Worker / Terraform | {worker_row} |",
        "",
    ]
    if show_local:
        rows += run_locally(repository, target.new, worker, merged=merged)
    return "\n".join(rows) + "\n"


def nothing_to_do(target: Target) -> str:
    return (
        f"## Product update: nothing to do\n\nThe config repo is already pinned to **`{target.new}`** "
        f"({target.repository}@{target.sha[:12]}): no pull request, no deploy, no local step.\n"
    )


def failed(title: str, why: str) -> str:
    return f"## {title}\n\n**Stopped:** {why}\n"


# --- the pull request (job product-update-pr) -----------------------------------------------------------


@dataclass
class Outcome:
    url: str
    number: int
    branch: str
    auto: bool  # auto-merge on: `server / gate` merges it
    why: str  # why it waits for the admin ("" when auto)
    reused: bool
    log: list[str] = field(default_factory=list)

    def state(self) -> str:
        link = f"[#{self.number}]({self.url})" + (" (reused)" if self.reused else "")
        if self.auto:
            return f"{link}: **will merge itself** once `server / gate` passes (auto-merge on)"
        return f"{link}: **waiting for your merge** ({self.why})"


def _pr_url(repo: updates.Repo, pr: Mapping[str, Any]) -> str:
    return str(pr.get("html_url") or f"https://github.com/{repo.repository}/pull/{pr['number']}")


def pr_body(target: Target, changed: Sequence[str], auto: bool, why: str, worker: list[str] | None) -> str:
    notes = notes_url(target.repository, target.new)
    kind = (
        "A patch release (fixes only)."
        if target.patch
        else "A minor/major release (new features, possibly new server.yml fields)."
    )
    migrated = [c for c in changed if c != WORKFLOW]
    todo = (
        "Nothing: it merges by itself once `server / gate` passes (automation.auto_merge builds+patch), and "
        "the merge deploys."
        if auto
        else f"Read the release notes ({notes}), then merge it (as admin: {why}), or run "
        f"`mcctl rollout --to {target.new}` in your clone; the merge deploys."
    )
    local = (
        f"after the deploy, `mcctl rollout --to {target.new}` in your clone (render, plan, apply)."
        if worker
        else "none (nothing the Worker or Terraform reads changed)."
        if worker == []
        else f"couldn't compare: run `mcctl rollout --to {target.new}` after the deploy if the panel or "
        "Worker changed."
    )
    return "\n".join([
        f"**What changed:** the product {target.old} -> {target.new} "
        f"({target.repository}@{target.sha[:12]}). {kind}", "",
        f"{target.new}'s `mcctl schema-migrate --from {target.migrate_from}` and `mcctl validate` ran on "
        "this change (Actions: the manual product-update run)"
        + (f"; the migration also changed {', '.join(migrated)}." if migrated
           else "; only the pin moved."), "",
        "**What it means for players:** usually nothing they notice; if the deploy changes a setting that "
        "needs a restart, the server restarts once with an in-game countdown.", "",
        f"**What to do:** {todo}", "",
        f"**Local step:** {local}",
    ])  # fmt: skip


def open_or_reuse(
    repo: updates.Repo,
    target: Target,
    files: Mapping[str, str],
    *,
    auto_merge: str,
    protected: bool,
    worker: list[str] | None,
) -> Outcome:
    """The pin pull request for target (see the module doc); files: the prepared workflow (+ server.yml)."""
    if not files.get(WORKFLOW):
        raise UpdateError(f"nothing prepared: no {WORKFLOW}")
    pin, errors = configrepo.read_pin(files[WORKFLOW])
    if pin is None or (pin.repository, pin.sha, pin.version) != (target.repository, target.sha, target.new):
        raise UpdateError(f"the prepared {WORKFLOW} doesn't pin {target.new} ({'; '.join(errors) or pin})")
    main_text: dict[str, str | None] = {}
    for name in files:
        got = repo.file(name, "main")
        main_text[name] = got[0] if got else None
    changed = sorted(n for n, t in files.items() if main_text[n] != t)
    wf_main = main_text.get(WORKFLOW)
    try:
        pure = wf_main is not None and files[WORKFLOW] == updates.bump_pin(
            wf_main, target.repository, target.sha, target.new
        )
    except ProviderError:
        pure = False
    reasons = []
    if not target.patch:
        reasons.append(f"{target.old} -> {target.new} is a minor/major release")
    if changed != [WORKFLOW] or not pure:
        reasons.append(f"{target.new}'s migration changes more than the `uses:` line ({', '.join(changed)})")
    if auto_merge != "builds+patch":
        reasons.append(f"automation.auto_merge is {auto_merge}")
    if not protected:
        reasons.append("github.protect_main is false (no required gate check)")
    auto = not reasons
    why = "; ".join(reasons)
    proposed = {n: files[n] for n in changed} or {WORKFLOW: files[WORKFLOW]}
    branch = f"update/product-{target.new}"
    title = f"Product update to {target.new}"
    upgrade_branch = f"upgrade/product-{target.new}"
    open_update = repo.open_pulls("update/product-")
    mine = next((p for p in open_update if p["head"]["ref"] == branch), None)
    if mine is not None:
        stale = {n: t for n, t in proposed.items() if (repo.file(n, branch) or ("", ""))[0] != t}
        if stale:
            repo.commit_files(branch, stale, f"{title}: {target.new}'s migration")
            repo.log.append(f"#{mine['number']}: brought {', '.join(sorted(stale))} up to date")
        else:
            repo.log.append(f"#{mine['number']} ({branch}) already proposes this: reused")
        if auto and not mine.get("auto_merge"):
            repo.enable_auto_merge(mine)
        return Outcome(_pr_url(repo, mine), int(mine["number"]), branch, auto, why, True, repo.log)
    theirs = next(
        (p for p in repo.open_pulls("upgrade/product-") if p["head"]["ref"] == upgrade_branch), None
    )
    if theirs is not None:
        repo.log.append(
            f"#{theirs['number']} ({upgrade_branch}, `mcctl upgrade`) proposes {target.new}: reused"
        )
        return Outcome(
            _pr_url(repo, theirs), int(theirs["number"]), upgrade_branch, False,
            "opened by `mcctl upgrade`: the admin merges it", True, repo.log,
        )  # fmt: skip
    for pr in open_update:
        other = str(pr["head"]["ref"]).removeprefix("update/product-")
        if updates.newer(target.new, other):
            repo.close(pr, f"Superseded by {target.new}.")
    body = pr_body(target, changed, auto, why, worker)
    pr = repo.propose(branch, proposed, title, body, auto_merge=auto, reset=repo.ref_sha(branch) is not None)
    return Outcome(_pr_url(repo, pr), int(pr["number"]), branch, auto, why, False, repo.log)


def run_pr(
    config_dir: Path,
    env: Mapping[str, str],
    target: Target,
    worker: list[str] | None,
    transport: http.Transport = http.urllib_transport,
    sign: Callable[[str, bytes], bytes] = ghapp.openssl_sign,
) -> tuple[int, str, list[str]]:
    """The CLI half of `pr`: (exit code, summary Markdown, log). The App from UPDATER_APP_ID/UPDATER_APP_KEY,
    the config repo from GITHUB_REPOSITORY, the prepared files from PREPARED_WORKFLOW/PREPARED_SERVER_YML
    (base64)."""
    repository = env.get("GITHUB_REPOSITORY", "")
    title = f"Product update: {target.old} -> {target.new}"
    app_id, key = env.get("UPDATER_APP_ID", ""), env.get("UPDATER_APP_KEY", "")
    if not app_id or not key:
        why = (
            "no updater GitHub App (UPDATER_APP_ID/UPDATER_APP_KEY), so no pull request: in your clone, "
            f"`mcctl rollout --to {target.new}` opens it (`mcctl upgrade`), merges it and deploys"
        )
        return 1, failed(title, why), [why]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository):
        raise ProviderError("GITHUB_REPOSITORY isn't owner/repo")
    files = {}
    for name, var in ((WORKFLOW, "PREPARED_WORKFLOW"), (configrepo.SERVER_YML, "PREPARED_SERVER_YML")):
        if raw := env.get(var, ""):
            files[name] = base64.b64decode(raw).decode()
    cfg = configrepo.load(config_dir).server
    token = ghapp.installation_token(
        app_id, ghapp.decode_key(key), repository, ghapp.UPDATER_PERMISSIONS, transport, sign=sign
    )
    repo = updates.Repo(repository, token["token"], transport)
    out = open_or_reuse(
        repo, target, files, auto_merge=cfg.automation.auto_merge, protected=cfg.github.protect_main,
        worker=worker,
    )  # fmt: skip
    deploy = (
        "runs on the merge: the next `server` run on main (its summary repeats this, with the result)"
        if cfg.deploy.enabled
        else "none: server.yml deploy.enabled is false"
    )
    text = summary(
        title=title, repository=repository, target=target, pr=out.state(), deploy=deploy, worker=worker,
        merged=False,
    )  # fmt: skip
    return 0, text, out.log


# --- the deploy run that follows the merge ---------------------------------------------------------------


def previous_pin(
    config_dir: Path, before: str, current_sha: str, run: Runner = subprocess.run
) -> configrepo.Pin | None:
    """The pin as of `before` (a push's previous head), when it differs from the running product's sha."""
    if not SHA.fullmatch(before or "") or set(before) == {"0"}:
        return None
    proc = run(
        ["git", "-C", str(config_dir), "show", f"{before}:{WORKFLOW}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    pin, _ = configrepo.read_pin(proc.stdout)
    return pin if pin is not None and pin.sha != current_sha else None


PR_NUMBER = re.compile(r"\(#(\d+)\)\s*$")


def deploy_summary(
    *,
    repository: str,
    target: Target,
    result: str,
    run_url: str,
    head_message: str,
    worker: list[str] | None,
) -> str:
    first = (head_message or "").splitlines()[0] if head_message else ""
    m = PR_NUMBER.search(first)
    pr = (
        f"[#{m.group(1)}](https://github.com/{repository}/pull/{m.group(1)}): merged"
        if m
        else "merged (pushed to main)"
    )
    ok = result == "success"
    deploy = f"[{'succeeded' if ok else result or 'unknown'}]({run_url})"
    title = (
        f"Product update deployed: {target.old} -> {target.new}"
        if ok
        else f"Product update: the deploy of {target.new} {result or 'did not finish'}"
    )
    text = summary(
        title=title, repository=repository, target=target, pr=pr, deploy=deploy, worker=worker, merged=True,
        show_local=ok,
    )  # fmt: skip
    if not ok:
        text += (
            "\nFix the deploy first (its log is above; `mcctl deploy` from your computer runs the same "
            f"playbook), then run the local step if the Worker changed: `mcctl rollout --to {target.new}`.\n"
        )
    return text
