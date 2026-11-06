# Publishing the public repository

The product is developed in a private repository and published as a **fresh public repository with one
commit per publication**: `edvardk/smallworld`. The private repository is then archived and kept private;
its history (planning notes, design documents, the full changelog) never becomes public. This page is the
maintainer's checklist, in order. Nothing here is automated beyond the dry run and the local build:
`scripts/publish_fresh.py` never pushes, never creates a GitHub repository and never changes a setting. It
prints the commands, and you run them.

The anonymity rule: the public repository identifies nobody except through its owner account. The owner's
name appears only in the allowed forms (the `edvardk/smallworld` repository, the `github.com/edvardk`
account URL, the `io.github.edvardk` Java package, GitHub noreply addresses). Two checks enforce it:

| Check | Where | What |
|---|---|---|
| Generic | `tests/test_publishable.py` (every pytest run), `publish_fresh.generic_problems` | session links, emails outside reserved domains (`example.*`, `.test`, `.invalid`, `.internal`, noreply), public IP literals outside the documentation/test ranges, the owner's name outside the allowed forms, a personal timezone default in `mcctl/schema.py` or `mcctl/init.py`, references to paths that aren't published |
| Denylist | `scripts/publish_fresh.py`; after publishing also `scripts/guard.py` (hooks, CI) | the identifying values themselves, from a file in the private config repo, never in this one |

`tests/fixtures/publishable-known.txt` lists, as paths only, the files that still fail the generic check
during the cleanup. A clean file still on the list fails the test, so the list only shrinks. It must be empty
before you publish, and `publish_fresh.py` refuses anyway while any problem is left.

## 1. Draft the denylist

Create `publish-denylist.txt` at the root of the private config repo (`../mc-config/` next to the product
checkout is the default path; elsewhere, pass `--denylist PATH`, set `SMALLWORLD_DENYLIST=PATH` or
`git config smallworld.denylist PATH`; `publish_fresh.find_denylist` resolves it, for `guard.py` too). Never commit it to the product:
`publish_fresh.py` refuses a denylist that would be published.

Format: one case-insensitive Python regex per line, `#` comments. A `## <category>` line names the category
of the patterns below it. The dry run reports hits per category and never prints the matched text. Write a
case-sensitive part as `(?-i:...)`. The allowed forms above are blanked before the patterns run, so a pattern
for the bare owner name doesn't hit them.

The categories to fill in (the values are yours to list; keep them out of this repository):

- **names**: your real name, its short forms and possessives (case-sensitive where a short form is also a
  common word);
- **handles**: the owner name outside the allowed forms, and the old repository and config repo names;
- **emails**: personal addresses and their domains;
- **domains**: personal domains and the server's DNS zone;
- **IP addresses**: the server's IPv4, its IPv6 /64 prefix and interface id, and the hosting provider's
  prefixes that appeared in examples;
- **provider ids**: server names and customer or contract numbers from the hosting provider, and account,
  zone and tailnet identifiers;
- **hosting**: the original deployment's plan, size and prices, where they are more than a neutral
  recommendation;
- **branding**: the original server's name, slogans, MOTD and join texts, and its drink;
- **app names**: the GitHub Apps' names;
- **buckets**: Terraform state and backup bucket names;
- **Discord**: application, guild, channel, user and role ids, the public key, the invite code;
- **personal defaults**: your timezone and anything else that was a default only because it was yours.

## 2. Dry run

Run it on the commit you intend to tag, before tagging:

```sh
python scripts/publish_fresh.py              # HEAD, the version from pyproject.toml
python scripts/publish_fresh.py --commit main --quiet   # only the counts by category
```

It exports `git ls-files` of that commit (`git archive`, modes kept) minus the entries of `publish-exclude`,
rewrites `CHANGELOG.md` (the preamble, `## [Unreleased]`, the release being published, and one line saying
that earlier releases stay in the archived repository), and scans every path and every file. Binaries are
read as Latin-1, zip archives (the plugin jars) member by member, names included, and PNGs without their
compressed pixel data (random bytes that match short patterns by chance): look at the images yourself. It exits 0 when clean,
1 on hits (`path:line: category (publish-denylist.txt:N)`), 2 on errors. It also notes when
`PRODUCT_REPOSITORY` (`mcctl/configrepo.py`) isn't the public name yet, because `--create` refuses that.

Fix hits in this repository with a normal pull request and run it again. Don't extend the denylist's allowed
forms (`ALLOWED` in the script) to make a hit go away.

## 3. Review

The scan only finds what the denylist names. Read the export once yourself before it goes public:

```sh
python scripts/publish_fresh.py --commit main --create /tmp/smallworld-review --noreply-id 1
git -C /tmp/smallworld-review ls-files | less        # what is published
git -C /tmp/smallworld-review grep -n -i -e '<a word you worry about>'
```

Look in particular at the docs, the test fixtures (recorded API answers, UI screens) and the images. Then
delete the review directory.

**gitleaks.** CI scans the whole history (`ci.yml` job `gitleaks`). The entries in `.gitleaksignore` of the form
`commit:file:rule:line` name commits of the archived repository, so they don't match the new commit. Run the
pinned gitleaks on the review directory (`gitleaks git --redact --no-banner /tmp/smallworld-review`) before
publishing: it must find nothing. For each test-fixture finding, add a global entry `file:rule:line` with the
file's current line in this repository (v1.2.0's are at the end of `.gitleaksignore`), and keep them current
when a fixture moves. gitleaks 8.30.1 checks a finding's global fingerprint before its commit fingerprint (checked in its
`detect/detect.go`, `AddFinding`).

## 4. Tag and build the repository

Cut the release as usual ([reference: Releasing](reference.md#releasing)): bump the version and the
changelog, merge, tag `vX.Y.Z` on main (the private repository's tag; don't push it if the old repository's
release workflow shouldn't run). Then:

```sh
python scripts/publish_fresh.py --commit vX.Y.Z --create ../smallworld-publish
```

`--create` runs only after a clean scan. It builds a new repository in an empty directory outside the
checkout with **one** commit. The author and the committer are `<owner> <ID+<owner>@users.noreply.github.com>`.
The ID is read with `gh api users/<owner>`; offline, pass `--noreply-id ID`, the number in your noreply
address (GitHub **Settings > Emails**). The message is `Smallworld vX.Y.Z`, with no trailers, and the commit
is tagged `vX.Y.Z`. `git commit-tree` is used, so no hooks or commit templates run, and `GIT_AUTHOR_*`/
`GIT_COMMITTER_*` variables are ignored. The script then reads the result back: no parent, the identity, the
message, the files, the tag.

## 5. The printed commands

It prints the commands and runs none of them. They are, in this order:

```sh
gh repo create edvardk/smallworld --public --description '...'   # empty: no README, licence or .gitignore
git -C ../smallworld-publish remote add origin https://github.com/edvardk/smallworld.git
git -C ../smallworld-publish push -u origin main
git -C ../smallworld-publish push origin vX.Y.Z                   # runs release.yml: CI + the GitHub release
python -m mcctl product-setup --repo edvardk/smallworld
gh repo archive <old owner/name> --yes                            # stays private
```

## 6. After publishing

1. **`mcctl product-setup --repo edvardk/smallworld`**: auto-merge, Dependabot alerts and security updates,
   secret scanning and push protection, the `main` ruleset (the required CI checks) and the tag ruleset. The private-only settings (Actions access
   level, forking) don't apply to a public repository. Run `--check` afterwards.
2. **Private vulnerability reporting**: **Settings > Security > Private vulnerability reporting > Enable**
   (`SECURITY.md` sends reporters there).
3. **Fork pull requests**: **Settings > Actions > General > Approval for running fork pull request workflows
   from contributors > Require approval for all external contributors**. The e2e runner jobs only run for
   same-repository pull requests ([SECURITY.md](../SECURITY.md)). This approval keeps every outside
   contributor's workflow run waiting for you.
4. **Archive the old repository** (the printed `gh repo archive` command), and keep it private. Never reuse its
   name for something else.
5. **Re-pin the config repo** in a pull request there. Change the `uses:` line by hand to
   `edvardk/smallworld/.github/workflows/server.yml@<the public tag's sha> # vX.Y.Z`. `mcctl upgrade` can't do
   this: it keeps the pinned repository and looks for releases there (`mcctl/upgrade.py`). Then run the
   steps that upgrade would run, from a checkout of the new tag: `mcctl schema-migrate -C <config> --from <the
   previous release>` and `mcctl doctor -C <config> --fix` (it adds workflow secrets the template now
   passes). The `server / gate` check rejects a change of repository (`mcctl/gate.py`), so you merge that
   pull request yourself as admin.
6. **`mcctl render -C <config>` and `mcctl apply -C <config>`** (it asks for a typed confirmation): the
   Tailscale federated identity's `job_workflow_ref` rule (`terraform/tailscale.tf`) names the product
   repository. Until this apply, deploys can't join the tailnet.
7. **Deploy** (push to the config repo's main, or `mcctl deploy -C <config>`), and check the server.
8. The `PRODUCT_READ_TOKEN` secret and its PAT were only needed while the product was private. Revoke the PAT
   once the deploy works.

## After publishing: safeguards on every commit

`publish_fresh.py` gates one export. From then on development happens in the public repository, so the same
checks run on every commit. `scripts/guard.py` imports publish_fresh's functions (stdlib only: the hooks work
with any `python3` >= 3.11, no venv):

| Where | What runs | What it checks |
|---|---|---|
| `pre-commit` hook | `guard.py staged` | every staged added or modified file, read from the index: its path and content against the denylist (zips and PNG metadata as in the dry run) and the per-file generic checks (session links, emails, public IPs, the owner's name outside its allowed forms, timezone defaults); paths listed in `publish-exclude` are refused (they never exist in the public repository); the pinned gitleaks on the staged changes when it is on `PATH` (one warning line when it isn't) |
| `commit-msg` hook | `guard.py commit-msg FILE` | the message (denylist, session links, emails) and the author and committer identity: the email must be a GitHub noreply address (or a reserved test domain) |
| `pre-push` hook | `guard.py range BASE HEAD` | every commit being pushed: message, author and committer names (denylist) and emails, the files it changed. A new branch is checked from where it left `origin/main` |
| CI, required check `publication` (`ci.yml`) | `guard.py range` on a pull request (every commit, `fetch-depth: 0`); `guard.py tree HEAD` nightly, on releases and dispatches (the whole tree, exactly like the dry run, including references to excluded paths) | the same checks. The denylist is the Actions secret `PUBLISH_DENYLIST` (the file's content, in `SMALLWORLD_DENYLIST_TEXT`). A fork's pull request gets no secrets and runs the generic checks only; in `edvardk/smallworld` every other run fails without the secret |
| GitHub | secret scanning and push protection (`mcctl product-setup`) | known credential formats in every push; push protection refuses the push |

CI logs are public, so the output never contains a denylist pattern, the text it matched or an email address:
a hit reads `path:line: denylist rule N` (N: the rule's position among the file's patterns, comments not
counted); a path that itself matches the denylist is shown as `<file K of M scanned ...>`. Generic hits keep
their category names. Exit codes: 0 clean, 1 hits, 2 a usage or configuration error.

The denylist is found in this order: `--denylist PATH`, `$SMALLWORLD_DENYLIST_TEXT` (the content), the path
in `$SMALLWORLD_DENYLIST`, `git config smallworld.denylist`, `../mc-config/publish-denylist.txt`. Without one
the generic checks still run with a warning; `git config smallworld.requireDenylist true` (or
`--require-denylist`) turns that into a failure. A configured path that doesn't exist is always an error.

**The maintainer's one-time setup**, in each clone of the public repository:

```sh
python3 scripts/guard.py install        # core.hooksPath = .githooks; reports the identity, denylist, gitleaks
git config smallworld.denylist /path/to/private-config/publish-denylist.txt
git config smallworld.requireDenylist true
git config user.email <ID>+<login>@users.noreply.github.com   # your noreply address; also --global
gh secret set PUBLISH_DENYLIST -R edvardk/smallworld < /path/to/private-config/publish-denylist.txt
```

- GitHub **Settings > Emails**: turn on **Keep my email addresses private** and **Block command line pushes
  that expose my email**.
- Install gitleaks at the version and sha256 pinned in `ci.yml` (job `gitleaks`) somewhere on `PATH`.
- Update the `PUBLISH_DENYLIST` secret whenever the denylist file changes (the same `gh secret set`).
- Never bypass a hook (`git commit --no-verify`, `git push --no-verify`). A hit is fixed in the change; if it
  slipped into a local commit, amend or rebase it before pushing.

## Does anything depend on the repository's numeric id?

No. The only numeric repository ids in the product are the **config** repo's. `terraform/tailscale.tf` builds
the immutable OIDC subject (`repo:OWNER@OWNER_ID/REPO@REPO_ID:...`) from `data.github_user.config_owner.id`
and `data.github_repository.config.repo_id`. `mcctl/providers/github.py` (`oidc_subject_info`) reads the same
ids to check the format. The config repo isn't recreated, so those ids and the Tailscale `subject` don't change.

The product repository is referenced by name only. Each of these changes with the new name, not with an id:

- the `job_workflow_ref` claim rule (step 6);
- the config repo's `uses:` pin (step 5);
- `PRODUCT_REPOSITORY`;
- the `github.repository` guard in `.github/workflows/dependabot-automerge.yml`;
- the rulesets, which `product-setup` creates on the new repository (step 1).

The GitHub Apps are installed on the config repo, so they are unaffected.
