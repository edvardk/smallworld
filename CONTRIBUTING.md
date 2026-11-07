# Contributing

The product is maintained by its owner with the help of coding agents. Changes come as pull requests to `main`,
which require the CI checks (`lint`, `gitleaks`, `actionlint`, `config-repo / validate`, `e2e`).

Read first: [`CLAUDE.md`](CLAUDE.md) (the binding rules: no secrets in the repo, pin everything, verify config
keys and provider schemas against the pinned versions, idempotent Ansible, the repo conventions) and [docs/map.md](docs/map.md) (where each feature lives).
[docs/reference.md](docs/reference.md#development) has the layout.

## Set up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes --no-deps -r requirements/dev.txt
ansible-galaxy collection install -r ansible/requirements.yml
```

Terraform 1.17.1, Node >= 22.13 (the Worker's tests use `node:sqlite`) and, optionally, Playwright for the panel's
UI smoke test.

## Before every commit

```bash
ruff check . && ruff format --check . && yamllint --strict . && pytest -q
(cd ansible && ansible-lint)
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate
```

CI also runs actionlint, gitleaks over the history, the example config repo through the reusable workflow, and
`e2e-mock` (a mock `mcctl setup` and the real server in a systemd Debian 13 container).
Which of these run depends on the paths a pull request changes, and the benches only run when dispatched: see
[docs/ci.md](docs/ci.md). Minutes are limited, so run the checks above locally and push when they're green.

## Conventions in short

- No deployment values in `terraform/`, `ansible/` or workflows: add a `server.yml` field (`mcctl/schema.py`, then
  `python3 -m mcctl schema`) and render it.
- After changing `template/`, run `python scripts/sync_example.py`.
- A new secret goes in every place `tests/test_consistency.py` checks (`mcctl/secrets.py` is the list).
- Python helper scripts are type-hinted and ruff-clean; server-side scripts are stdlib-only (Python >= 3.11).
- Tests never call a real provider API: HTTP goes through `mcctl/http.py` transports with recorded fixtures.
- User-visible changes go under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md); docs links are checked by
  `tests/test_docs.py`.
- Security-relevant changes update [SECURITY.md](SECURITY.md).

## Releasing

See [docs/reference.md](docs/reference.md#releasing): versions in `pyproject.toml`, `mcctl/__init__.py` and
`CHANGELOG.md` must agree (`scripts/release.py check vX.Y.Z`); the owner bumps them (`release.py bump`) and,
after the merge, tags main's tip (`release.py tag`) and pushes the tag.
