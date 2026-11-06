# Minecraft server config

This repository holds the **values** of one Minecraft server; all code lives in the product,
[`edvardk/smallworld`](https://github.com/edvardk/smallworld), pinned at
`0000000000000000000000000000000000000000` (v0.0.0) by `.github/workflows/server.yml`.

| File | What | Who edits it |
|---|---|---|
| `server.yml` | The server's settings (schema: the product's `schema/server.schema.json`) | You (`mcctl config set`, or by hand) |
| `players.yml` | Whitelist and ops, canonical form | `mcctl players`, the panel (via PR) |
| `server.lock.yml`, `plugins.lock.yml` | Pinned server build and plugins with SHA-256 | The lock tool / weekly update |
| `client.lock.yml` | v1.1: the pinned client modpack (mods, hashes, licences; needed with `client.enabled`) | The lock tool / weekly update |
| `expiry.yml` | Credential expiry dates (no values) | `mcctl doctor` |
| `.github/workflows/server.yml` | The single workflow: one pinned call of the product | `mcctl upgrade` / the weekly update |
| `.env` (not committed) | Admin credentials, see `.env.example` | You |

Every push and pull request runs `validate`; pushes to `main` deploy once `deploy.enabled: true`.
Product docs for the pinned version: the product's `README.md`, `docs/setup.md` and `docs/operations.md` at
that commit.
