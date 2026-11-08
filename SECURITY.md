# Security

How this product protects a Minecraft server, what it assumes, and what it deliberately leaves open
Hardening that runs on the box is
checked on a live systemd in every `e2e-mock` CI run, not only rendered.

**Reporting a problem:** report vulnerabilities privately through GitHub's private vulnerability reporting
(the repository's **Security** tab > **Report a vulnerability**), not in a public issue. Never paste a token,
password or webhook URL into an issue, a PR, an advisory or a chat: say which secret is affected and rotate it
(`terraform apply -replace=<resource>`, then a deploy).

## The model in one page

- **Trust anchors:** the admin's laptop (`.env` with the admin tokens, or Dashlane references), the admin's
  GitHub, Cloudflare and Tailscale accounts, and the product commit the config repo pins. Everything else is
  derived from these and can be rebuilt from them plus the backups.
- **Public surface:** Minecraft on TCP 25565 (IPv4 + IPv6) and Tailscale's silent UDP 41641 on the host; on
  Cloudflare, the Worker's ingest host `hb.<host>` (bearer tokens, Discord signatures) and the panel behind
  Cloudflare Access with the Worker re-checking every token. No public SSH after bootstrap.
- **Credentials stay where they are used:** admin tokens only on the laptop; deploy secrets in Terraform state,
  the config repo's Actions secrets and root-only env files on the box; the panel App's key only in the Worker;
  the updater App's key only in the scheduled update job; the Discord bot token only on the laptop. Nothing secret
  in git, on a command line or in logs.
- **On the box:** the JVM (and every plugin) runs as `minecraft`; everything holding a credential runs as
  `mcops`; every unit is sandboxed and gated by `systemd-analyze security`; root is reachable only through
  Tailscale SSH as `deploy` (or the provider console).
- **What gets deployed** is whatever is on `main` of the config repo, running the pinned product commit. The pin
  moves only by a pull request through `server / gate` (patch releases), by the admin (`mcctl upgrade`), or after
  the admin types `yes` to move it forward to a newer commit of the product checkout they run (P15: `mcctl
  setup`/`doctor --fix`; ancestry checked, never backwards or to an unpushed or unrelated commit).
- **Backups survive a compromised server:** the server's R2 token can't lift the bucket lock (7 days; repository
  keys forever).
- **Deliberately open:** see [Not covered (v1)](#not-covered-v1) (GitHub Free without rulesets, no second backup
  copy, no WebAuthn-signed panel commands).

## Assets

| Asset | Where | Why it matters |
|---|---|---|
| The world | `/opt/minecraft/server`, restic snapshots in R2 | The thing players care about; the only irreplaceable data |
| Backups | R2 bucket with bucket lock | The way back after anything else goes wrong |
| Deploy secrets (RCON, restic password, R2 keys, heartbeat token, agent token, command HMAC key, Discord webhook) | Terraform state (R2), config repo Actions secrets of the environment `deploy` (main only; `github.environments`, [Actions secrets](#actions-secrets-security-review-m2)), root-only `/etc/minecraft/*.env` (read by systemd for `mcops` units only) | Each one opens a piece of the box or its backups |
| GitHub App private keys (P10 review / P11) | panel App: `.env` → Terraform state → the Worker's secret binding only; updater App: `.env` → Terraform state → secret of the environment `update` (main only), read by the scheduled update job only | Each can open pull requests and push branches of the config repo (the updater also workflow files); the rulesets keep both off main |
| Admin credentials (Cloudflare, GitHub, Tailscale OAuth) | The admin's laptop, config repo `.env` (0600, git-ignored), or Dashlane with only `dl://` references in `.env` (P14) | Can rebuild or destroy everything; never in CI or on the server |
| Discord bot token (P14, optional) | `.env` (or a Dashlane reference) on the laptop only; never in Terraform, CI, the Worker or the server | Can change the application's commands and its interactions endpoint (point Discord elsewhere); can't sign interactions or run anything on the server |
| Shell on the box | Tailscale SSH as `deploy` (passwordless sudo); root's password locked and its SSH key removed after bootstrap (security review M1) | Root on the box |
| The config repo | GitHub (private) | Whatever is on `main` gets deployed |
| The product repo | GitHub (public; nothing relies on the code being secret) | Code every config repo runs, pinned by SHA |

## Actors and mitigations

| Actor | Can | Mitigations | Accepted risk |
|---|---|---|---|
| **Internet** (scanners, DDoS, exploit kits) | Reach TCP 25565 and UDP 41641 | nftables default drop; only 25565/tcp, 41641/udp (WireGuard, silent) and ICMP open; **per-source limits on new 25565 connections** (IPv4 /32, IPv6 /64: 20/min, burst 30; at most `firewall.max_connections_per_source` = 10 concurrent; log-limited drops; **fail closed** when a per-source set is full, see Firewall below); SSH, RCON and everything else only on `tailscale0`; `sshd` masked after bootstrap; kernel/network sysctls (SYN cookies, no redirects/source routing, loose `rp_filter`, martians logged); DNS-only records, no other public name except `hb.<host>` (the Worker's ingest) and `panel.<zone>` (the panel, behind Cloudflare Access, P9) plus `<panel>-confirm.<zone>` (destructive-action confirmations, a second Access app, P10 review) | Volumetric floods beyond what one VPS link takes; netcup's own DDoS filtering is the only layer above the host |
| **Griefer with a Minecraft account** | Join if whitelisted; send game traffic | `online-mode`, `white-list` + `enforce-whitelist`, `enable-query=false`, `broadcast-rcon-to-ops=false`, RCON on 127.0.0.1 only with a 32-char random password; backups hourly with 7-day lock | In-game griefing by whitelisted players (roll back from a snapshot) |
| **Malicious or compromised plugin** (runs inside the JVM) | Anything the `minecraft` user can | Plugins are pinned by version and sha256 (`plugins.lock.yml`); the JVM runs as `minecraft` in a systemd sandbox: `ProtectSystem=strict` (writes only to the server dir), `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`, `ProtectProc=invisible`, `RestrictNamespaces`, `RestrictAddressFamilies`, `SystemCallFilter=@system-service`, no capabilities, `NoNewPrivileges`; `kernel.yama.ptrace_scope=1`, `kptr_restrict=2`, `dmesg_restrict=1`, `unprivileged_bpf_disabled=1`; **every credential-holding helper runs as a different user** (`mcops`, [Privilege separation](#privilege-separation)) | The plugin can read the RCON password (`server.properties`) and use RCON, outbound network is open (it needs Mojang/auth), and it can damage the live world and anything else the server dir holds. It can't read the restic password, R2 keys, heartbeat token or Discord webhook (proven in e2e). Restore from a locked snapshot |
| **Compromised server** (root on the VPS) | Everything on the box; the server's R2 key; the heartbeat token | The R2 token is object read/write on one bucket only and can't change the bucket lock: snapshots and packs younger than 7 days and the repository keys can't be deleted ([R2 bucket lock](docs/reference.md#design-notes)); no admin credential is ever on the box; the tailnet ACL lets `tag:mc-server` reach nothing | Older, deduplicated packs can be deleted once their lock expired (no second copy in v1); the attacker can post fake heartbeats and see the Discord webhook |
| **Compromised CI run / GitHub Actions** | What the deploy job holds: the deploy secrets, a `tag:ci` tailnet node | No long-lived Tailscale secret: workload identity federation trusts only the config repo's `main` (with `github.environments`: its environment `deploy`, which admits main only, `sub` `repo:...:environment:deploy`) **and** the product's reusable workflow (the `sub` names owner and repo with their numeric ids, GitHub's immutable subject claims: a deleted and re-registered owner or repo name can't match; no wildcards; `legacy` name-only subjects only for repos older than 2026-07-15); `tag:ci` reaches only `tag:mc-server:22` as `deploy` (ACL tests assert 25565, 25575, 443, 8443 denied and root SSH denied); only the deploy job may request `id-token`; every action pinned by SHA; Python deps hash-pinned; gitleaks scans the product history and every config repo's history | The deploy job has root on the box (Ansible needs `sudo`); a malicious change merged to `main` of either repo is deployed |
| **Workflow on another branch** (security review M2: whoever can push a workflow file to a branch the rulesets leave open, i.e. the updater App on `update/**`, which has Workflows write; or a pull request's workflow) | Run any job in the config repo's Actions, with whatever secrets that job can read | With `github.environments` (default for new config repos; Terraform): every deploy secret (RCON, restic, R2, heartbeat, agent token, command HMAC key, Discord webhook, the CI identity's ids) is a secret of environment `deploy`, the updater App's key of `update`, and both environments' deployment branch policy is `main` only, so a job on `update/*`, `panel/*` or a pull request is refused before it starts; the tailnet's CI identity trusts only the `environment:deploy` subject; `main`'s workflow file only changes through the rulesets' pull request + gate (a workflow change never passes the gate) | `PRODUCT_READ_TOKEN` (contents: read on the product, optional: only for a private product repo) stays repository-level (every job checks out the product with it); **with `github.environments: false` (GitHub Free) every Actions secret is readable by such a workflow** ([Not covered](#not-covered-v1)) |
| **Compromised GitHub account** (the admin's) | Push to both repos, read Actions secrets indirectly through a workflow | Config repo pins the product by SHA, so a product push alone changes nothing until the pin moves; secrets are created by Terraform only | Full compromise of the deployment (the admin is the config repo's ruleset bypass actor and can edit the product's rulesets); protect the account with a hardware key/2FA |
| **Holder of a GitHub App key or token** (P10 review: the panel App's key is in the Worker, its 1 h tokens reach mc-agent; the updater's key is an Actions secret) | Push branches and open pull requests on the config repo (only `panel/**` and `update/**` for the Apps, by ruleset; the gate binds each to its App's bot user); the updater can also change workflow files on its own branches | **Rulesets** (`terraform/github.tf`): main only through a pull request whose `server / validate` and `server / gate` checks passed, checks pinned to the GitHub Actions app (an App can't post them), no force-push or deletion, neither App a bypass actor; the gate passes only whitelist-only `players.yml` changes from `panel/*`, strictly newer lock updates re-verified against the upstream APIs and product patch bumps to a tagged release, both from `update/*` (p11-review: policies bound to the head branch; first live apply: and to the App's bot user as PR author and event sender) ([below](#the-panels-github-app)); the panel App has no Workflows permission, so it can't change the workflow that runs the checks | Whitelist changes (the panel can make those anyway); with the updater key: open a product/lock PR that only merges if the gate verifies it. **On GitHub Free (no rulesets on a private personal repo) none of this holds**: the panel App can push to main and so deploy anything (see [Not covered](#not-covered-v1)) |
| **Compromised Cloudflare account** (cloudflare mode, §5a) | DNS, R2 (backups, log archive), the Worker (watchdog, agent ingest, Control DO with the command key, the panel), D1 (metrics, logs, the panel's audit table), Access | Admin token least privilege (table below); bucket lock can't be lifted by the server's token; **delta vs onserver mode:** see [Panel actions (P10)](#panel-actions-p10-what-the-panel-can-change): it can sign any command on the agent's allowlist, so it **can change the world** (restore an older snapshot, stop/restart, switch locked plugins off, whitelist/kick/ban/pardon, allowlisted console lines), but it can't get a shell, read the restic password or R2 keys, delete or alter a locked snapshot, change ops, or rewrite the server's own audit copy in journald; it can read the shipped logs (scrubbed) and metrics. P10 review: it **holds the panel App's key** (a Worker secret), so it can open pull requests and push `panel/**` branches on the config repo; with the rulesets only a whitelist-only change merges, which it could make through the agent anyway, so this adds nothing (on GitHub Free it would add a push to main) | Loss of DNS/backups/monitoring; a restore to an older snapshot or a stop (undone from the safety snapshot / with Start; every action is in the server's journal with the Access identity it claimed); with Access it controls who reaches the panel, so it can also sign itself in (re-auth doesn't help against that) |
| **Lost or stolen laptop** | `.env` with the admin tokens; a tailnet node of the admin | `.env` mode 0600 (doctor checks), tokens expire (`expiry.yml`), Tailscale device can be removed in the console; the admin's Tailscale SSH rule is `check` (security review): a shell on the server needs a fresh tailnet login (browser, the IdP's 2FA) at most 12 h old | Until revoked, the tokens work; use full-disk encryption and revoke from another device |
| **mc-agent / Worker link** (P8, cloudflare mode) | Talk to `hb.<host>` like the agent; send the agent commands | Outbound HTTPS only (no listening port); bearer `AGENT_TOKEN` (separate from the heartbeat token; constant-time compare); commands are HMAC-SHA256-signed with `COMMAND_HMAC_KEY` (only the Worker secret and the root-only `agent.env`), checked for age (±60 s) and nonce (10 min, persisted; an unreadable nonce store refuses every command for 10 min), then the agent's own allowlist (status, console, start, stop, restart, backup, tail; P10: cancel, whitelist, rejected_dismiss, moderate, plugin, restore, each with its own argument checks) and the **console allowlist** (P8 review; [below](#console-allowlist)): only listed commands with listed argument shapes run, `execute`/`return`/`function`, op/deop, stop/restart/reload, save-off and every namespaced head are refused, the line is NFKC-normalised and control characters are refused; whitelist changes go through the panel's Git flow; every command is audit-logged to the local journal (`MC_AUDIT=1`), which Cloudflare can't alter; the agent runs as `mcops` in a sandbox (`MemoryMax=64M`, exposure ≤ 2.5) and gets `CAP_DAC_READ_SEARCH`/root nowhere | Whoever holds the agent token can push fake metrics/logs (samples capped per day at twice the configured rate, log rows at `max_rows_per_day`) and read queued commands' payloads (not forge them); a Cloudflare-account compromise can sign allowlisted commands (the §5a delta above); allowlisted console commands (say, kick, ban, time, weather, ...) run with RCON's power |
| **Panel visitor** (P9, cloudflare mode: anyone who finds `panel.<zone>`) | Reach Cloudflare Access's login; send any request to the hostname | Cloudflare Access (self-hosted app, one-time PIN or GitHub, only `panel.access.emails`, 24 h sessions, HttpOnly/SameSite=Lax cookie (Strict caused a login redirect loop; CSRF is covered by the Origin/Sec-Fetch-Site/JSON checks)); **the Worker verifies the Access JWT itself on every request** (`Cf-Access-Jwt-Assertion`: RS256 only, signature against `https://<team>/cdn-cgi/access/certs`, issuer, the Access app's AUD tag, `exp`/`nbf`/`iat` with 60 s leeway, email on the list; certs cached 1 h, unknown kids refetched at most once a minute, a failing certs URL asked again at most once a minute and the last good keys kept for 15 min past the hour while it fails (P9 review; never any key = closed); any failure, a missing or misconfigured PANEL binding included, is a 403), so deleting or misconfiguring the Access app fails closed; workers.dev off; assets only through the Worker (`run_worker_first`, an explicit path list); CSP `default-src 'none'` with `'self'` for scripts/styles/fonts/connections, `frame-ancestors 'none'`, no inline code, no third-party requests (fonts self-hosted, avatars drawn locally); POSTs need `Origin: https://panel.<zone>`, `Sec-Fetch-Site: same-origin` (when sent) and a JSON body; every API answer `no-store` | A signed-in admin's browser session can run what the panel can, except stop, plugin switches and restore, which run only on the confirmation host after a one-time PIN login (P10 review, [Re-auth](#panel-actions-p10-what-the-panel-can-change)); the IdP mailbox/GitHub account is part of the trust boundary |
| **Discord** (P14, `discord.commands`: anyone who can reach `hb.<host>/discord/interactions`, and Discord itself) | POST interactions; a guild member could invoke the commands | Every request's Ed25519 signature (X-Signature-Ed25519 over X-Signature-Timestamp + body) is verified with the application's public key (WebCrypto) before anything is parsed; a timestamp more than 5 min off is refused; anything that fails is a 401 and writes nothing; the application id, guild, channel and user must be on server.yml's allowlist (DMs refused; refusals audited, the first 5 per user and minute one by one, the rest as one aggregate row); each interaction id is answered once (D1 `discord_seen`, kept 1 h > the 5 min window); 10 commands/min per user, 3 actions per 10 min; the commands are hidden from non-administrators in the guild (`default_member_permissions: "0"`); replies never mention anyone (`allowed_mentions: {parse: []}`) and escape Markdown. Read commands answer from D1 only. **Only two actions**, both the agent's existing ones, shaped like the panel's and HMAC-signed by the Control object: `backup` and `whitelist add` (a Java name; live now, then the usual pending.json → PR flow); the agent checks them again and logs them with login `discord:<user id>` | Whoever controls the allowlisted Discord account (or the Discord application's signing key, i.e. Discord) can read the server's status and start a backup or whitelist a player; never stop, restart, restore, plugins or console. A compromised bot token could repoint the endpoint (commands stop working; it can't forge signatures the Worker accepts) |
| **Resource-pack fetch** (P17: anyone, `GET hb.<host>/pack/<sha1>.zip`) | download the product's public pack | Only `pack/<40 hex>.zip` of the static assets is served there (GET/HEAD; everything else 404, the panel's files only on the Access-protected panel host); the content is product code, its sha1 is the name and `resource-pack-sha1`; request headers (the client's name/UUID) are never read or logged | Anyone can download a public 19 KB file; each download is one Worker request |
| **Watchdog request forgery** | POST to `hb.<host>` | Bearer token (48 random chars) compared in constant time; `/status` shows freshness only, no addresses (v1.2: plus a boolean `client`, whether the client page is set up); invocation logs off so the `Authorization` header is never logged; workers.dev disabled | Anyone with the token can silence alerts (heartbeats, maintenance windows) |

### Actions secrets (security review M2)

| Secret | Repository-level (`github.environments: false`) | `github.environments: true` (template default) | Read by |
|---|---|---|---|
| RCON, restic password + repository, R2 keys, heartbeat token, agent token, command HMAC key, Discord webhook, `TS_OAUTH_CLIENT_ID`/`TS_AUDIENCE` | every workflow run | environment `deploy`, deployment branches: `main` | the reusable workflow's `deploy`, `backup-check` (manual dispatch only since v1.2), `expiry-check` jobs (`environment: deploy`) |
| `UPDATER_APP_ID`, `UPDATER_APP_KEY` | every workflow run | environment `update`, `main` only | the scheduled `update` job (`environment: update`) |
| `PRODUCT_READ_TOKEN` (optional: only for a private product repo) | every workflow run | repository-level (unchanged) | every job's product checkout (read-only) |

`validate`, `gate`, `panel-merge`, `update-test` and `boot-test` run without an environment and read no deploy
secret. The config repo's workflow still passes every secret explicitly (the reusable workflow's interface is
unchanged): with environments the caller's values are empty and the environment's take their place inside the
jobs that name the environment. The deploy job's OIDC `sub` becomes `repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:deploy`
(`legacy`: `repo:OWNER/REPO:environment:deploy`), which `tailscale_federated_identity.ci` trusts exactly. GitHub's
docs give the environment form only for name-only subjects; the immutable form above follows GitHub's description
that only the `repo:` part gains the ids (unverified live until the first deploy after the switch; a mismatch is
a 403 at "Join the tailnet" that `mcctl setup`/`doctor` explain). The Terraform GitHub token needs **Environments:
Read and write** besides Secrets and Administration.

## Privilege separation

Two system users (P7 review). `minecraft` runs the JVM, so every plugin runs as it. `mcops` runs everything that
holds a credential: `mc-backup`, `mc-prune`, `mc-backup-check` (v1.2, the weekly backup test), `mc-health` (and
its self-heal), `notify.py`, the fetch half of a restore, every `mc-run`/`mc-restic` command, and `mc-agent` (P8).

| | `minecraft` (JVM, plugins) | `mcops` (helpers) |
|---|---|---|
| Groups | `minecraft` | `mcops`, `minecraft` |
| Server dir `/opt/minecraft/server` | owner, read/write | read only (group); RCON password from `server.properties` |
| `/etc/minecraft/*.env` (root 0600) | no | no: systemd reads them as root and hands them to `mcops` units only |
| Their processes | `/proc/<mcops pid>/environ`, signals, ptrace: denied (different uid; `ptrace_scope=1`; `ProtectProc=invisible` hides them from the JVM's view of `/proc`) | the JVM's environment: denied too |
| `/var/lib/minecraft` (markers: admin-stopped, maintenance, restore lock) | read only: a plugin can't silence self-heal or the watchdog | owner (`mc-agent` may write only this directory outside its state: `ReadWritePaths=`, for the panel's stop/start) |
| restic cache `/var/cache/mc-restic`, `/var/lib/mc-health` | no access | owner |
| Reading the world | it owns it | group read; `mc-backup` alone also has `CAP_DAC_READ_SEARCH` (the JVM writes some files 0600, e.g. `level.dat`), read-only in its sandbox with every other secret hidden (`InaccessiblePaths`: password databases, sudoers, `/etc/minecraft` (its own env file is read by systemd first), SSH host keys, Tailscale state, the agent's state, TLS private keys; proven in e2e) |
| The journal | no | `mc-agent` only (`SupplementaryGroups=systemd-journal`): it ships the product's units (minecraft, mc-*, its audit entries, firewall drops), scrubbed of every secret value the agent can read (its env file's tokens, keys and webhook, the RCON password) and of secret shapes (any `*_KEY`/`*_SECRET`/`*_TOKEN`/`*_PASSWORD` name with a plain, quoted or JSON value, `Bearer`/`Basic` credentials, URL userinfo, GitHub/Tailscale/AWS key shapes, Discord webhooks; P9 review), never the rest of the journal |
| systemd actions (polkit, `50-mc-ops.rules`) | none | `minecraft.service` restart + kill (self-heal; SIGQUIT thread dump), start + stop (agent: the panel's start/stop/restart), `mc-backup.service` start (agent), `mc-restore-apply.service` start (restore), P10: `mc-plugins-apply.service` start (the plugin switch) and `mc-restore@<8-64 hex>.service` start (the panel's restore; nothing else matches the pattern), P11: `reboot.target` start (`mc-reboot-gate`: only inside the reboot window with nobody online; a reboot is a graceful stop + start of everything), v1.2: `mc-world.service` start (the world library, fixed unit) and `mc-world-apply.service` start (its folder swap, as `minecraft`); v1.3: nothing new (`mc-restart-window`, the restart a deploy left for `maintenance.window`, uses the existing `minecraft.service` restart) |

**Restore** keeps the world owned by `minecraft` without giving either user the other's power: `restore.py` (as
`mcops`, with the restic credentials) fetches the snapshot into `/var/lib/mc-restore/staging` (setgid, group
`minecraft`, made group-readable) and writes a manifest, then starts `mc-restore-apply.service`, which runs
`restore.py --apply-staged` as `minecraft` with **no** credentials in a sandbox that may write only the server dir.
It refuses unless a restore holds `restore.lock`, the server is stopped and the manifest is valid, and it copies
(never follows) symlinks. Only root and `mcops` can start it.

**Proven live** in every e2e-mock run (`tests/e2e/hardening_check.py`): `mcops` is in exactly `mcops minecraft`;
a process running as `minecraft` gets EACCES on every env file, the restic cache and `/proc/<pid>/environ` of a
running `mcops` helper that holds `RESTIC_PASSWORD`, can't signal it, and can't write the admin-stopped marker; a
live restore runs end to end (apply unit as `minecraft`, `level.dat` back and owned by `minecraft`); a frozen JVM
(SIGSTOP) is dumped and restarted by `mc-health` through polkit.

**Not protected:** the RCON password (the server needs it in `server.properties`; RCON listens on 127.0.0.1 only,
so whoever reads it can run console commands, which a plugin can anyway); the world and plugin data (the plugin
runs inside the server); root (`deploy` has passwordless sudo; root reads everything); the outbound network.

## Panel actions (P10): what the panel can change

The panel now changes the server, not only reads it. Every action follows one path: a confirmation dialog in
the browser → `POST /api/action` (same-origin, JSON, Access JWT verified by the Worker) → the Worker checks the
arguments, re-auth freshness and the typed confirmation, writes an audit row with the Access email → signs the
command (HMAC, nonce, 60 s) → the agent verifies it, checks the action and its arguments again against **its
own** allowlist (the Worker's checks are not trusted), runs it through a fixed unit, and writes its own audit
entry to the journal (`MC_AUDIT=1`, with the login the command carried).

| Action | Runs as / how | What it can do at worst | Limits |
|---|---|---|---|
| Restart / stop / start | `mcops` → polkit → `minecraft.service` | Downtime | Countdown + in-game warnings (skipped with nobody online); stop writes the admin-stopped marker; **stop is confirmed on the confirmation host** |
| Back up now | `mcops` → `mc-backup.service` | An extra snapshot | Same unit as the timer |
| Whitelist add/remove | RCON `whitelist add\|remove` (the server resolves the name), then `pending.json` → a PR with a 1 h token of the panel's GitHub App (minted by the Worker) | Let a player in / keep one out | Names `[A-Za-z0-9_]{3,16}`; ops can't be removed (Git only); the PR is merged automatically **only** if it touches `players.yml`'s whitelist alone (`server / gate` + `mcctl panel-pr-check`: valid, canonical, ops unchanged, exact head commit); **closed unmerged = reverted on the server** (P10 review) |
| Rejected logins | read from the server's log | – | Addresses masked (IPv4 /24, IPv6 /48) before they leave the box |
| Kick / ban / pardon | RCON, through the console allowlist's shapes | Kick or ban a player | No selectors; reasons one printable line ≤ 100 chars |
| Plugin switch | `mcops` writes `/var/lib/minecraft/plugins-disabled.json` → `mc-plugins-apply.service` (as `minecraft`, writes only `plugins/`, no network) moves the jar → restart | Switch a **locked** plugin off | Only names in the deployed lock; jars only moved (never deleted, never followed); **confirmed on the confirmation host**; drift banner until undone |
| World library (v1.2): create, switch, delete | the agent writes `/var/lib/minecraft/world-request.json` (checked: name, seed, type, preset, border; the target archived, not newer than the server) → polkit → `mc-world.service` (fixed unit, as `mcops`, restic credentials, `CAP_DAC_READ_SEARCH`, same hidden paths as mc-restore@; `worlds.py` checks the request again) → `mc-world-apply.service` (as `minecraft`, no credentials, no network, writes only the server dir) | Replace the live world with another one (the active one is archived first), generate a new one, or forget an archived world | **Confirmed on the confirmation host** with a typed confirmation (`create <name>`, `switch to <name>`, `delete <name>`); never from Discord or in-game; before anything changes the active world is **archived** (a restic snapshot tagged `world-archive`, `world:<name>`: the safety snapshot; a failure there leaves the world untouched and starts it again); a world saved by a newer Minecraft is refused; restore.lock held throughout (deploys, backups, self-heal wait), admin-stopped `by=world`; the apply step writes only the three world folders and the world's `server.properties` keys (seed, type, difficulty, gamemode, force-gamemode, hardcore: a fixed list, values one line); archives are outside the backup retention and a delete can't forget a snapshot younger than the bucket lock (forgotten later); the active world can't be deleted; the panel's restore refuses another world's archive |
| Restore | `mcops` → polkit → `mc-restore@<id>.service` (as `mcops`, restic credentials, `CAP_DAC_READ_SEARCH` for reading the world like mc-backup, same hidden paths) → `mc-restore-apply.service` (as `minecraft`) | Roll the world back to any snapshot in the repository | Typed confirmation `restore <id>`; id must be in restic's list; **a safety snapshot (tag `pre-restore`) of the current world is taken first** and a failure there stops the restore before anything changes; snapshots are protected by the R2 bucket lock, so nothing the panel does can delete one; **confirmed on the confirmation host**; while it runs the server is marked admin-stopped (`by=restore`) and deploys wait for `restore.lock`, so nothing starts the server halfway (P10 review) |

**Read-only agent actions (v1.3).** Two more signed commands change nothing on the server:

- `selftest` (the Health tab, panel host, no arguments; also daily by the agent itself): the agent asks the Worker
  for a panel App token (`POST /agent/github-token`, as for a pull request) and reads the config repo with it
  (`GET /repos/{repo}`, main's ref and `players.yml`, the open pull requests). No branch, commit or pull request.
  At most one run a minute from the panel (a sooner request gets the last result); the result names the failing
  step and never contains the token (GitHub errors carry the request line and status only). The Worker now also
  passes the token's granted `permissions` (from GitHub's mint reply, nothing secret) for the check.
- `lookup {name}` (only through the whitelist form's name check, `GET /api/mojang`; `POST /api/action` refuses it):
  the agent sends the name, `[A-Za-z0-9_]{3,16}` checked on both sides, to `https://api.mojang.com` and then
  `https://api.minecraftservices.com` (stdlib HTTPS, 3 s timeout each, unauthenticated, only the name), at most 20
  requests a minute, answers cached 10 minutes. New outbound hosts from the box: those two (the box already reaches
  Mojang's session servers through the JVM). The Worker waits for the answer at most 4 s and only while the agent
  holds its WebSocket, then falls back to its own lookup. Someone holding the Cloudflare account could make the box
  ask Mojang about names (rate-limited); nothing more.

The deploy also writes `/opt/minecraft/product-version.json` (root, 0644: the product version and commit, nothing
secret), which the agent reports in its inventory; `mcctl render` stamps the same into the Worker's static assets
(`version.json`, read through the ASSETS binding only, never served).

**Re-auth** (P10 review). Stop, plugin switches and restore (v1.2: and every world library action) are never accepted on `panel.<zone>`. The panel opens
`<panel.hostname>-confirm.<zone>` in a new window with the action in the URL fragment; that host is a second Access
application with a session of `panel.access.reauth_minutes` (default 10) and the **one-time PIN as its only login
method** (a GitHub login would be re-issued silently while the GitHub session lives, so the confirmation app never
uses GitHub even when the panel does). The Worker accepts the three actions only there, and only with a JWT whose
`aud` is that application's AUD tag (the panel's token is refused) and whose `iat` is at most `reauth_minutes` old;
the confirmation page shows what will happen and posts the action same-origin. Access's account-wide *global
session* (Cloudflare: 15 minutes to one month, default 24 h) is what lets Access re-issue an expired application
token without a new login (we assume whichever login method started it), so it, not the confirmation app's session, bounds how fresh the PIN behind a confirmation
is: **a confirmed action means a one-time PIN at most global session + `reauth_minutes` ago** (25 min with the
target below). A token's `iat` is when Access issued it, not when the user last logged in (no claim records that),
so the Worker can't tell a silent re-issue from a new PIN. The global session doesn't shorten any application's own
session: the panel keeps its 24 h login whatever the global session is; a short one only ends single sign-on from
one Access application into another. `mcctl setup` (step zero-trust) therefore accepts a global session up to
max(30 min, 2 x `reauth_minutes`) as the admin set it and, only when it is longer (a new organization's 24 h), sets
it to max(15 min, `reauth_minutes`) through the API (the organization as read, cleaned of nulls and of a disabled or
incomplete `service_token_inactivity`, which the API refuses to take back; Terraform only reads the organization).
If that write is refused, setup names the dashboard setting (Zero Trust > Access controls > Access settings > Set
your global session duration) and later runs only re-check. `mcctl doctor` and a Terraform `check` warn when it is
longer. Anyone who leaves it at 24 h gets a confirmation host that only checks *who* (a separate PIN-only app, its own
AUD), not *how recently*. Access's independent MFA (`mfa_config` on the confirmation
application with a short `session_duration`, provider 5.28.1 has it; users enrol a TOTP app or security key in the
App Launcher) would force a fresh second factor regardless of the global session; not used in v1. It protects against a forgotten or stolen panel session, not against a compromised Cloudflare account (which
can mint its own sessions) or IdP mailbox.

**A compromised Cloudflare account** can therefore stop the server, restore an older snapshot (after a safety
snapshot), switch to another world or generate a new one (after archiving the active one), delete archived worlds
(their snapshots go only once the bucket lock allows), switch locked plugins off, change the whitelist (the PR it causes is still subject to the
whitelist-only merge check), kick/ban, and run allowlisted console lines. It **can't**: get a shell or run
arbitrary commands (allowlists on both sides, no `execute`/`op`), read or exfiltrate the restic password or R2
keys (none are in Cloudflare), delete or alter snapshots (bucket lock; the server's R2 token can't lift it),
change operators or anything in Git beyond the whitelist (it holds the panel App's key, but the rulesets let only
a whitelist-only PR merge; GitHub Free: see below), or erase the server's own audit trail (journald on the box). Recovery: Start, restore the `pre-restore` snapshot, re-enable the plugin,
revert the whitelist PR; rotate `COMMAND_HMAC_KEY` (`terraform apply -replace=random_password.command_hmac_key`
+ deploy) to cut it off. WebAuthn-signed destructive commands that the agent verifies would close this gap
(post-v1).

### The world library (v1.2)

Only the active world is on the box; the others are restic snapshots in the same R2 repository (encrypted and
deduplicated like every backup). What it adds to the attack surface:

- **Two fixed units, no new verbs:** `mc-world.service` (as `mcops`, the restic credentials, the same read
  capability and hidden paths as `mc-restore@`) and `mc-world-apply.service` (as `minecraft`, no credentials,
  `PrivateNetwork=true`). polkit allows `mcops` to `start` each, nothing else; the request comes from a file only
  `mcops` can write (`/var/lib/minecraft`), and `worlds.py` re-checks it (an hour old at most). The apply step
  refuses unless a switch holds `restore.lock`, the server is stopped and the manifest names a world, its
  level-name and only world keys of `server.properties`.
- **Git:** a world change goes to `server.yml` by a pull request from `panel/world-*` (the panel's GitHub App, the
  same narrowed token as the whitelist). `server / gate` and `mcctl panel-pr-check` let it merge without the admin
  only when `server.yml` alone changes and, parsed, only `world.active` and `world.library` differ, and the head
  validates. A deploy **never switches worlds** (it honours the live world, warns when Git names another one, and
  applies only the running world's border and pre-generation setting), so a merged world PR can't make the server
  do anything it didn't already do. Closing such a PR doesn't undo the switch (that would need a second switch);
  the agent keeps the pending state and says so on Discord.
- **Not covered:** an archive is the whole server dir (like any backup), so switching back restores that world's
  folders only; the rest of the server dir stays the current one. A world from an older Minecraft is upgraded on
  load, one way (the archive keeps the old copy).

### The same actions from mcctl (v1.3)

`mcctl restart|worlds|plugins|players kick|ban|pardon` run the panel's actions over Tailscale SSH as the deploy
user (the admin's `check` rule): `sudo mc-run -/etc/minecraft/notify.env /opt/minecraft/bin/mc_agent.py cli ...`.
No new credential, unit, polkit verb or listening socket: the entry point runs mc-agent's Executor as `mcops` in
mc-run's sandbox, so its systemd actions are the units `50-mc-ops.rules` already allows, and it reads only
`notify.env` (the watchdog's maintenance window), never `agent.env`. It needs no signature because only the
deploy user reaches it, and that user has sudo anyway; the checks are the agent's, plus the typed confirmations
(world library, plugins) checked again on the box. Every action, refused or not, writes the panel's audit
entry (`MC_AUDIT=1`, `MC_VIA=mcctl`) with the login `mcctl:<local user>` as the controller states it (the
Tailscale SSH session itself is in tailscaled's log). A panel job and a CLI job can't overlap:
`mc_panel.Jobs` holds an flock on `/var/lib/minecraft/job.lock` (a directory only `mcops` can write) from the
countdown to the end of the work.

### The panel's GitHub App

P10 review (the P10 PAT could push to `main`, which deploys). The panel's whitelist pull requests use a **GitHub
App** `<name>-panel`, created by `mcctl init` through GitHub's App manifest flow and installed on the config repo
only, with exactly **Contents write, Pull requests write, Metadata read** (`mcctl init`/`doctor` check the App's
permissions with the admin PAT and fail on any extra one), no webhook events, private. During init the laptop
also signs short (9 min) App JWTs with each App's key from `.env` (openssl, key on stdin, never on argv or disk)
to read the App's slug and its installation on the config repo (`GET /app`, `GET /repos/{repo}/installation`:
installed, on selected repositories only); the setup callbacks go to a loopback server on `127.0.0.1` only (no
logging; the manifest code is single-use and the installation id isn't secret).

- **Where the key lives:** `.env` (base64 PEM, 0600) → Terraform state → the Worker's secret binding
  `GITHUB_APP_KEY`. Not on the server, not in an Actions secret, not in the deploy job. The Worker signs an App JWT
  with WebCrypto (RS256) and mints a **1 h installation token narrowed to the config repo and Contents + Pull
  requests write** when mc-agent asks (`POST /agent/github-token`, bearer `AGENT_TOKEN`); the agent keeps it in
  memory only, the log scrubber removes token shapes. Chosen over signing on the server (openssl): the server is
  the most exposed host, and the key there would outlive a compromise; whoever holds the Cloudflare account
  already holds the command key, so the key in the Worker adds no power that the rulesets don't take away again.
- **What it can do:** create and update branches `panel/**` (other branches: ruleset), commit files there, open
  and comment on pull requests, read the repo. **It can't** push to `main` (ruleset: pull request + required
  checks; neither App bypasses), force-push or delete `main`, change a workflow file (no Workflows permission, so
  the checks that judge its PRs are always main's), post the required checks (they are pinned to the GitHub
  Actions app, and it has no Checks/Statuses permission), read or change secrets, settings or rulesets. Whether a
  PR merges is decided by `server / gate`, which binds each policy to the head branch (p11-review) AND to the App's
  bot user: from `panel/*` only a whitelist-only `players.yml` change passes (valid, canonical, ops unchanged) and
  only when the PR was opened by, and the event sent by, `<panel_app>[bot]`; lock and product updates pass only from
  `update/*` with author and sender `<updater_app>[bot]` (slugs from server.yml `github.panel_app` /
  `github.updater_app` as of the base). First live apply: GitHub refuses an App as ruleset bypass actor on a
  personal account's repository (422), so `update/**` (like `panel/**`) is excluded from the branch ruleset and
  both Apps can push there; a push by the panel App onto an update PR makes it the `synchronize` sender and fails
  the gate, and bot logins can't be registered by users or other Apps. The panel App can still delete or
  create `update/*` branches (nuisance only: nothing it opens or pushes there merges). So a panel
  App token (which anyone holding `AGENT_TOKEN` can get from the Worker) can't merge a lock change, e.g. a
  downgrade to an older plugin release; lock updates also must be strictly newer (server builds; plugin versions
  published after the base's, from the APIs). It could merge a passing whitelist PR itself (Contents write),
  which changes nothing. main's ruleset requires the branch to be up to date (`strict`), so a verdict judged
  against an older main can't merge stale.
- **Revoke:** delete the App or its key on GitHub, or remove the Worker binding (`TF_VAR_panel_github_app_*` empty
  + apply); rotate by generating a new key on the App's settings page into `.env` + apply.
- **The updater App** (P11) `<name>-updater` has Contents, Pull requests and **Workflows** write (a product update
  changes the workflow file's pin). It is a separate App because Workflows write must never sit with a key outside
  CI: a workflow change pushed to a branch runs with the repository's secrets. Its key is the Actions secret
  `UPDATER_APP_KEY`, used only by the last step of the scheduled `update` job. p11-review: the boot test runs the
  new (third-party) jar and plugins in a **separate job**, `update-test`, which holds no secret beyond the
  read-only product checkout (finished before anything runs); a process a malicious plugin leaves behind dies with
  that runner and can't read the later step's environment. `update` runs on a fresh runner, re-verifies the
  boot-tested lock texts against the upstream APIs (no jar runs there) and only then gets the key. It is no ruleset
  bypass actor (GitHub doesn't allow Apps there on personal repos); `update/*` is open to it by exclusion: its PRs merge only
  through the gate (verified lock updates, product patch bumps to a tagged release), and without
  `github.protect_main` it doesn't enable auto-merge at all (nothing would gate it).
- **Product bumps judge themselves:** the gate job runs the reusable workflow named by the PR head's `uses:`
  line, so for a product update the *new* product version evaluates its own pull request. What binds it is the
  check that the new SHA is the commit of a patch release tag of the same product repository; a compromised
  product repository (one that can create release tags) is outside what the gate can catch, as it is for any
  pinned dependency.
- **GitHub Free:** rulesets and branch protection on a private repository of a personal account need **GitHub
  Pro** (`mcctl init` and `doctor` probe the plan through the branch-protection endpoint: 403 "Upgrade to GitHub
  Pro"). Without them (`github.protect_main: false`) the panel App's Contents write is a push to `main`: whoever
  holds the Cloudflare account could deploy anything (a lock file pointing at their own jar runs as `minecraft`).
  Then leave the panel's App out (`TF_VAR_panel_github_app_*` empty: whitelist changes stay pending and live,
  deploys keep them for 24 h) or accept that risk.

## Console allowlist

The panel's console (P8 review: the P8 denylist was bypassed by `execute as … run run op`, `return run op`,
`execute run return run op` and namespaced forms) runs a line only if it passes `scripts/mc_agent.py`
`check_console`:

1. One line of at most 256 characters, before and after NFKC normalisation (full-width `ｏｐ` becomes `op`; a
   character that expands, such as U+FDFA, can't push the line past the limit: P9 review); control, format and
   private-use characters and any whitespace other than a space are refused. The words are re-joined with single
   spaces and the head lowercased: **the normalised text is what reaches RCON**, so what was checked is what runs.
2. A head containing `:` (`minecraft:op`, `bukkit:reload`, `paper:…`) is refused.
3. Refused with a reason: `execute`, `return`, `function` (they run other commands), `op`, `deop`, `stop`,
   `restart`, `reload`/`rl`, `save-off`; `whitelist add|remove` goes to the panel's Git flow.
4. Everything else must be on `CONSOLE_ALLOWLIST`, with its arguments matching that command's shape:

| Commands | Allowed arguments |
|---|---|
| `list`, `seed`, `tps`, `mspt`, `plugins`/`pl`, `version`/`ver`, `help`, `banlist` | read only (`list uuids`, `banlist players\|ips`, one plugin/topic name) |
| `say`, `me`, `tell`/`msg`/`w` | free text (`tell` needs a player name or `@a`/`@p`/`@r`); **no selector words in the message** (`say @e`, `me @a[...]`: the server would expand them into every matching entity's name, a chat flood on a busy world; P9 review) |
| `kick`, `ban`, `pardon`, `ban-ip`, `pardon-ip` | a player name `[A-Za-z0-9_]{3,16}` (or an IP for the `-ip` forms) first; **no selectors** (`ban @a` is refused); a reason may follow |
| `whitelist` | `list`, `on`, `reload` (`off` is refused) |
| `save-all`, `save-on` | `save-all [flush]` |
| `time`, `weather`, `difficulty` | `time query\|set\|add …`, `weather clear\|rain\|thunder [duration]`, `difficulty [level]` |
| `gamerule`, `worldborder` | read only: `gamerule <rule>`, `worldborder get` (server.yml/Ansible own the values) |
| `spark`, `chunky` | `spark tps\|gc\|ping\|health [--memory] [--network]` (no profiler, no uploads); `chunky progress\|pause\|continue\|selection\|help` |

Not on the list (so refused): gameplay powers (`give`, `gamemode`, `tp`, `kill`, `summon`, `effect`, …), world
editing (`fill`, `setblock`, `clone`, `data`, `datapack`, `forceload`), `transfer`, `scoreboard`, `tick`, `debug`,
`jfr`, `perf`, `publish`. Players are moderated, not puppeted, from the panel; anything else is a PR or happens
in game. Tests: every review bypass and its variants (`tests/test_agent.py` `BYPASSES`), the refused set, the
shapes, Unicode look-alikes and control characters.

## Hardening on the box (design §7, as built in P7)

| Item | What | Checked by |
|---|---|---|
| systemd sandbox | One shared set (`mc_systemd_sandbox` in `ansible/group_vars/all/main.yml`) on `minecraft`, `mc-backup`, `mc-prune`, `mc-health`, `mc-restore-apply`, `mc-agent`, P10's `mc-plugins-apply` and `mc-restore@`, P11's `mc-reboot-gate`, v1.3's `mc-restart-window` (as the ops user); `MemoryDenyWriteExecute` on all but the JVM (JIT); `mc-run` transient units sandboxed too (`ProtectSystem=full`); THP set by `systemd-tmpfiles` instead of a root helper unit (the old `thp-madvise.service` is stopped, disabled and removed on upgraded hosts) | `systemd-analyze security`: offline in `pytest`, live in e2e (`--threshold`: ≤ 2.5, minecraft ≤ 4.5; e2e also checks the running JVM has `NoNewPrivs`, a seccomp filter and no capabilities, and runs backup + health to success in their sandboxes) |
| Sysctls | `hardening_sysctls` (role `hardening`) | e2e reads every value back |
| Journal | Persistent, `SystemMaxUse=1G` | e2e |
| OpenSSH | Masked after bootstrap (only when Tailscale SSH is up); break-glass below | e2e (`ssh.service` masked and stopped) |
| root (security review M1) | Password locked (`user: password_lock`, i.e. `passwd -l`) and `/root/.ssh/authorized_keys` (the bootstrap key) removed, only in a play that came in as `deploy` over Tailscale SSH (the role asserts it: never a lock-out); break-glass below | `tests/test_hardening.py`; e2e (`passwd -S root` = `L`, the key planted in the image is gone) |
| Admin SSH | Tailnet SSH rule `check` with `checkPeriod` 12 h (security review); CI stays `accept` | `tests/test_tailscale_policy.py` (`sshTests` `check`); `mcctl deploy` and bootstrap run one plain session first so the login URL is visible |
| Mail spoofing (`dns.mail_hardening`, default on) | The zone has no mail: Terraform adds a null MX (`MX 0 .`, RFC 7505), `TXT "v=spf1 -all"` and `_dmarc TXT "v=DMARC1; p=reject"` (covers subdomains); the plan refuses while another MX exists. **No CAA**: which CAs Cloudflare's Universal SSL uses (the panel's and the Worker's certificates) changes, and Cloudflare adds its own CAA entries only once a zone has CAA records; a wrong one would break the certificates, so it is left to the admin | `terraform validate`; the precondition at plan time |
| Firewall | nftables, per-source rate/concurrency limits; fail closed when a per-source set is full | `nft -c` in `pytest` (as root) and in the role (`validate:`); e2e loads the ruleset into a network namespace and proves the limits (IPv4 and an IPv6 /64) and the fail-closed drop with a shrunk set |
| Tailnet ACL | `terraform/tailscale-policy.json.tftpl`: grants, SSH rules, `tests` and `sshTests` | `tests/test_tailscale_policy.py` evaluates them; Tailscale re-runs them on every apply |
| Secrets scan | gitleaks 8.30.1 (sha256-pinned release) on the product's whole history and on every config repo's whole history (`fetch-depth: 0`, `gitleaks git`); `.gitleaksignore` lists, by fingerprint (commit:file:rule:line), only test fixtures that look like secrets (the log scrubber's test inputs) | `ci` / the reusable workflow's validate job |
| Anonymity (the public product) | `scripts/guard.py` with `scripts/publish_fresh.py`'s checks on every change: git hooks (`.githooks/`: the staged files, the message and the author/committer identity, every pushed commit; pinned gitleaks on the staged changes when installed) and the required `publication` CI check (a pull request's commits one by one, nightly and on releases the whole tree). The private denylist reaches CI only as the product repo's Actions secret `PUBLISH_DENYLIST`, in an environment variable; the log shows rule numbers and paths, never a pattern, the matched text or an email. GitHub secret scanning and push protection on the public repository (`mcctl product-setup`) | `tests/test_guard.py`; `ci` / `publication`; docs/publishing.md "After publishing: safeguards on every commit" |
| Supply chain | Actions by SHA, Python deps `--require-hashes`, server jar/plugins/restic/gitleaks sha256, Temurin/Tailscale held at pinned versions, Worker without npm deps, release source archive + `SHA256SUMS` | `tests/test_workflows.py`, `tests/test_consistency.py`, `tests/test_hardening.py` |

**Firewall limits and shared addresses.** A "source" is one IPv4 address or one IPv6 /64. Players behind
carrier-grade NAT, at a LAN party or in a school share one source: at most `firewall.max_connections_per_source`
(default 10) of them can be connected at once, and their reconnects share 20/min (burst 30). Raise the server.yml
value for such groups; the rate is fixed. **Set full:** the per-source sets hold 65535 entries each (rate entries
expire 10 min after a source's last new connection, count entries when it has none open). nftables adds nothing to
a full set, and then the limit statements can't match: before the P7 review that meant *accepted without limits*
(reproduced in a namespace). Now the chain accepts only a source present in both of its family's sets; an
untracked source is dropped (`mc-setfull:` in the kernel log, `drop_full` counter). Fail closed was chosen because
whoever can fill 65535 entries (trivially from an IPv6 /48) can also flood: new players are refused during such an
attack, players already tracked keep playing, and it clears by itself as entries expire.

**Break-glass** (Tailscale down or misconfigured): root's password is locked and `deploy` has none, so the console
login doesn't work by design (security review M1). Use the provider's **rescue system** (netcup SCP → Media →
Rescue system: it boots a live system and shows a one-time root password), open the VNC console, mount the root
filesystem (`mount /dev/vda3 /mnt` or the partition `lsblk` shows), `chroot /mnt`, then either set a console
password (`passwd root`, which also unlocks it) or directly
`systemctl unmask ssh.socket ssh.service && systemctl enable ssh.service`, and reboot out of the rescue system. Public port 22 is still closed by
nftables outside `tailscale0`; for public SSH also run `nft add rule inet mc_filter input tcp dport 22 accept`
(lost on the next deploy or reload, which is the point). Fix Tailscale, then deploy: the hardening role masks
sshd again.

**Sudo:** `deploy` has `NOPASSWD: ALL`. Ansible needs root and runs non-interactively; only `tag:ci` and the
admin may SSH in, and only as `deploy`. Accepted.

## Least-privilege credentials

| Credential | Needs exactly | Where it lives | Checked |
|---|---|---|---|
| Cloudflare admin token, **account-owned** | Zone DNS Edit + Zone Read (that zone only); Account Workers R2 Storage Edit, Workers Scripts Edit, D1 Edit, Account API Tokens Edit (that account only); cloudflare mode: Access: Apps and Policies Edit, Access: Organizations, Identity Providers, and Groups Edit. **Created by the admin by hand** (init's pre-filled link and exact form). It can't be minted: Cloudflare refuses to create a token holding token-management permissions through the API (error 1001 "sub-token is not allowed to have permissions to manage other tokens"), so no bootstrap token ever exists. **Blast radius: that one account** (an account-owned token can't act on any other account the user belongs to, and the tokens it creates are account tokens too); `cloudflare.token_owner: account` makes Terraform's R2 token an account token | `.env` | init and `doctor`: each permission exercised live; init audits the token's own policies (`GET /accounts/{id}/tokens/{id}`): warns on extra groups and on resources beyond the zone/account |
| Cloudflare admin token (user-owned, older setups only) | the same groups, User API Tokens Write instead of Account API Tokens. init no longer creates or asks for one (the custom builder doesn't offer User API Tokens Edit and minting is refused); one already in `.env` keeps working. "API Tokens Write" on the user can create tokens for **every** account the user can reach, so a leak is worse than the account-owned token's: replace it with an account-owned one | `.env` | `doctor`: each permission exercised live |
| GitHub token for `mcctl product-setup` | admin on the product repo (`gh auth token`'s OAuth token, or `GH_TOKEN`) | read from `gh`'s output or the environment, never stored, never on argv | – |
| Terraform state token | Object read/write on the state bucket | `.env` (`AWS_*`), created by `mcctl init` with the admin token (Workers R2 Storage Bucket Item Write on that bucket only: no token-management permission) | HeadBucket / ListObjectsV2 |
| R2 backup token | Object read/write on the backup bucket (Terraform `cloudflare_account_token`, or `cloudflare_api_token` for a user-owned admin token; no token-management permission) | TF state → Actions secret → `/etc/minecraft/restic.env` (0600) | `terraform plan` |
| GitHub admin PAT | Fine-grained, config repo only: Secrets read/write, Administration read/write (the rulesets) | `.env` | `doctor`: repo private, secrets readable, rulesets available on the plan, expiry; **warns on a classic PAT** (`X-OAuth-Scopes`) |
| Product-read PAT (optional: only for a private product repo) | Fine-grained, product repo: Contents read | `.env` → Actions secret | `doctor`: pinned commit readable; classic PAT warned |
| Tailscale OAuth client | `policy_file`, `devices:posture_attributes`, `devices:core:read`, `auth_keys`, `federated_keys`; tags `tag:mc-server`, `tag:ci`; **created by `mcctl init`** through the API with exactly these | `.env` | `doctor`: token exchange + policy read; **warns on any extra scope** in the token's `scope` |
| Restic password | – | TF state; `mcctl init` hands it to `op`/`bw` **on stdin** (never argv or output) if one is signed in | – |
| Heartbeat token | – (random, Worker secret) | TF state → Actions secret → root-only env files | – |
| Agent token, command HMAC key (P8) | – (random, Worker secrets) | TF state → Actions secrets → root-only `agent.env` (`mc-agent` only) | – |
| Discord webhook | – | TF state → Actions secret, Worker secret | `GET` (nothing posted) |
| Discord bot token (P14, slash commands) | the application's own bot (no intents, no guild permissions: the app is installed with the `applications.commands` scope only) | `.env` / Dashlane only; used by `mcctl setup` / `mcctl discord sync` for `GET`/`PATCH /applications/@me` and the guild command bulk overwrite | `doctor`: the token's application = `discord.application_id`, endpoint = the Worker |
| Panel GitHub App (P10 review, cloudflare mode) | Contents write, Pull requests write, Metadata read; installed on the config repo only; tokens narrowed to that repo + those two | `.env` (`TF_VAR_panel_github_app_id`, `_private_key`) → TF state → Worker secret only | `init`/`doctor`: exactly these permissions (`GET /apps/{slug}`), no events |
| Updater GitHub App (P11) | Contents, Pull requests, Workflows write, Metadata read; the config repo only | `.env` (`TF_VAR_updater_github_app_*`) → TF state → Actions secrets `UPDATER_APP_ID`/`UPDATER_APP_KEY` (scheduled update job only) | `init`/`doctor`: exactly these permissions |
| Tailscale API access token (P12 `migrate` / `--rollback` / `retire-old` only) | the user's own access token (admin of the tailnet), 1-day expiry recommended: renames the old node to `<name>-old`, deletes the new or old node | typed into a hidden prompt (or `TAILSCALE_API_KEY` in the shell) for that run only; never stored, never on argv; the user revokes it | `GET /tailnet/-/devices` before use |
| GitHub OAuth app secret (panel login, `panel.access.idp: github` only) | – | `.env` (`TF_VAR_panel_github_client_secret`) → TF state → the Access identity provider | `doctor`: set when needed |

## Lifecycle commands (P12)

What `migrate`, `restore`, `secrets rotate`, `upgrade` and `players` add to the attack surface:

- **No new standing credential and no new privilege on the box.** Everything runs from the admin's laptop over
  Tailscale SSH as the deploy user (who already has sudo). `mcctl restore --live` starts the existing
  `mc-restore@<id>.service` with `sudo systemctl` (the unit still runs as `mcops`, the apply as `minecraft`), so
  `50-mc-ops.rules` is unchanged; snapshot ids are checked against `^[0-9a-f]{8,64}$` before they reach a unit
  name or a shell. `--to DIR` only accepts a new directory under `/var/tmp` or `/tmp` (restic runs as `mcops` in `mc-run`'s
  sandbox: `ProtectHome=yes`, no write access to `/srv`).
- **Tailscale devices:** Terraform's OAuth client keeps `devices:core:read`; renaming/deleting a node needs the
  user's one-off API access token (table above), held in memory for one run.
- **Fencing** (migrate): the old box keeps its env files (credentials) on disk until it is wiped. Its timers and
  `mc-agent` are disabled and the root-written marker `/var/lib/minecraft/fenced` makes it stay that way (P12
  review): `ConditionPathExists=!` on minecraft, mc-backup, mc-prune, mc-agent, mc-health, mc-reboot-gate and (v1.3)
  mc-restart-window;
  `admin.py start` and mc-agent's start/restart refuse; self-heal and the reboot gate treat it as admin-stopped;
  `site.yml` fails before any role. So neither a reboot nor a stray deploy (CI schedule, panel merge) can bring a
  stale copy back to write backups or answer players. It stays on the tailnet as `<name>-old` until `retire-old`,
  which reminds the admin to wipe and cancel it. Only mcctl's rollback removes the marker; a rollback also fences
  the new box (or takes a typed "it is off") before the old one runs again.
- **Secret rotation:** values move from `terraform output` to the server through the deploy as before. For
  `restic-key` the deploy itself (backups role, `scripts/restic_rekey.py` as `mcops`) checks the new password
  opens the repository before writing `restic.env`, and if not but the current `restic.env` does, runs `restic key
  add --new-password-file /dev/stdin` with the new password on the module's stdin (never argv; the task is
  `no_log`; restic's own error text, which may name the repository, isn't printed); otherwise the deploy fails and
  `restic.env` stays. So no deploy, whoever starts it, can leave the server with a password the repository
  doesn't know. `.mcctl/state.json` holds the old key id and a phase only. **Residual:** the R2 bucket lock keeps `keys/` forever, so the old restic key can't
  be removed: rotating `restic-key` does not lock out someone who has the old password *and* R2 access; rotate
  `r2` as well (the old R2 token is deleted by the apply).
- **Upgrades** open pull requests only; the required `server / gate` check still decides what merges unattended
  (a Minecraft version change never does). The staged `boot-test` job (pull requests from a same-repo
  `upgrade/minecraft-*` branch) runs the locked, sha256-verified jar and plugins on the runner with `contents:
  read` and no secrets but the optional product-read token for the checkout.

## Player-experience telemetry (P13)

What the mc-telemetry plugin, mc-agent's telemetry and the Worker's experience storage add:

- **No IP addresses anywhere.** The plugin never calls `Player.getAddress()`; snapshots carry UUID, name, ping,
  client brand (sanitised, 32 chars), client/server view distance, locale, world and *chunk* coordinates, a count of
  nearby entities, and tick-time statistics. The agent re-parses every snapshot against a fixed set of fields
  (`telemetry.parse_snapshot`), the Worker again (`cleanRow`); neither keeps unknown keys. Exports (`mcctl metrics
  export`), D1 tables and the R2 archive therefore hold none. The vanilla server log *does* print IPs on login
  lines: those logs stay on the box (`logs/`, 30-day tmpfiles age rule) and the journal copies the agent ships are
  scrubbed of secrets only (P8), as before.
- **Privilege separation unchanged.** The plugin runs in the JVM as `minecraft` and writes only to
  `/run/mc-telemetry` (tmpfs, `RuntimeDirectory=`, 0750, files 0640) and its own data folder (the tips opt-out
  list). mc-agent (`mcops`) reads that directory (group `minecraft`) and writes verdicts/messages into
  `/run/mc-verdicts` (owner `mcops`, setgid `minecraft`, 2750, files 0640): the plugin may read it, nothing running
  as `minecraft` may write it, and no credential is ever in it. The e2e run proves both (`tests/e2e/
  telemetry_check.py`).
- **A hostile snapshot** (code in the JVM writing to `/run/mc-telemetry`) can only feed the agent bounded,
  type-checked numbers and short strings: it could fake its own verdicts or flood nothing (files are read with a
  1 MB cap, 500 players max, rows go out once a minute).
- **Planted files** (P13 review L4): every file mc-agent and mc-health read from a directory `minecraft` can write
  (snapshots, the GC log, player stats, `whitelist.json`/`ops.json`/ban lists, `server.properties`) goes through
  `mc_rcon.read_untrusted`: opened `O_NONBLOCK|O_NOFOLLOW` (a FIFO can't block the reading thread, a symlink to a
  file only `mcops` can read is refused), accepted only if `fstat` says it's a regular file, read with a size cap
  (the GC log: its last 1 MiB). Only the last path component is checked: a directory under the server dir
  replaced by a symlink (e.g. the world's `stats/`) could still point the stats reader at other JSON `mcops` can
  read, of which only three integer counters would be shipped. v1.4: mc-health's frontier expansion lists the
  region folders and reads each `r.X.Z.mca`'s 4 KiB header the same way, and refuses a region folder with a
  symlink anywhere under the server dir; what it derives is one number (a radius, capped by `max_radius`). The telemetry thread holds no lock while it reads
  files or talks to the Worker, so a signed `player_message` (handled inline on the WebSocket) never waits on it.
- **In-game messages are a signed command** (`player_message`, HMAC like every other): the Worker's verdict engine
  decides, the agent re-checks the shape and the rate limits with the live state (one per player per 30 min, none
  in the first minute, tips off respected, broadcasts one per 30 min, 20/hour), the plugin checks again. Texts come
  from the Worker's catalogue; a Cloudflare-account compromise could send short private chat lines (no commands:
  the plugin only builds a text component whose click runs `/lag`).
- **No network listener, no new outbound target on the box.** The plugin has none; the agent talks only to the
  Worker it already used. The Worker checks plugin/mod availability on Modrinth once a day (`api.modrinth.com`,
  read-only, no credential).
- **Storage**: D1 database `<name>-telemetry` (raw 14 d, 1-minute 180 d, hourly 2 y), R2 `telemetry/` in the logs
  bucket (kept; the lifecycle rule deletes `journal/` only), the box's segments under `/var/lib/mc-telemetry`
  (`mcops`, 0700, 30 days / 200 MB). `mcctl metrics` reads D1 with the admin token from `.env` (never printed).

## Discord slash commands and Dashlane (P14)

- **The Discord endpoint** is the only new public route: `POST /discord/interactions` on the existing ingest
  host (no new hostname, no Access app: Discord can't sign in). Its checks, in order, each failing closed: the
  DISCORD binding is complete (else 404 for every request), body ≤ 32 KB, the Ed25519 signature, the timestamp
  (±300 s), PING → PONG, the application id, guild, channel and user allowlists, the replay guard, the rate limits,
  then the command. Discord's own monitoring sends bad signatures on purpose and removes an endpoint that accepts
  one; `watchdog/test/discord.test.mjs` and the e2e run prove the 401s.
- **Nothing secret in the Worker for Discord.** The DISCORD binding is plain text: ids and the application's
  public key. The bot token stays on the laptop; follow-up edits use each interaction's own 15-minute token, which
  is never logged or stored.
- **What a Discord command can do**: read status/players/lag/perf/backups/whitelist/rejected from D1, start a
  backup, whitelist a player. Nothing destructive: `DISCORD_ACTIONS` is the complete list, and a test fails if it
  grows. Every action is audited in D1 (`audit`, login `discord:<id>`) and in the server's journal.
  **A compromised allowlisted Discord account can whitelist a player permanently**: `/whitelist add` goes live and
  its whitelist-only PR is merged automatically (`server / gate`), so the player stays after the next deploy. To
  revoke: remove that user id from `discord.user_ids` in `server.yml` (or set `discord.commands: false`) and push
  (the apply drops it from the Worker's DISCORD binding), then remove the player (`mcctl players remove NAME`, or
  the panel) and check the audit for other `discord:<id>` actions.
- **Helpers (v1.0.1, `discord.helper_user_ids` / `discord.helper_role_id`)**: a second, smaller tier. A helper
  is a user id on the list or a guild member whose `member.roles` (part of the signed interaction payload, so
  Discord vouches for it) contains the role id; the guild and channel allowlists apply as for admins. Helpers
  may run `DISCORD_HELPER_COMMANDS` (the read commands, `/whitelist list`, `/whitelist add`); `/backup now`
  and anything added later stay admin-only (refused and audited). Their actions are audited as
  `discord:<id> (helper)`; the per-user rate limits apply to each of them, the 3-per-10-minutes action limit is
  shared with the admins. The same risk as above applies: **a helper can whitelist a player permanently**, and so
  can anyone a guild administrator gives the role. Revoke: remove the role from the member (effective with the
  next interaction), or delete the role / clear `discord.helper_role_id` and `discord.helper_user_ids` and
  `mcctl apply`; then remove unwanted players and check the audit for `(helper)` rows.
- **Refusals and the D1 write budget**: anyone in an allowed guild can make Discord deliver validly signed
  interactions that are refused. Each user's first 5 refusals per minute are audited one by one, the rest only
  counted and written as one aggregate row (per Worker isolate), so a flood can't exhaust D1's daily writes. The
  Discord action limit (3 per 10 min) is one atomic `INSERT ... SELECT ... WHERE count < 3`, and the signature
  covers the request's exact bytes (timestamp, then the raw body; no text decoding before the check; a body that
  isn't strict UTF-8 JSON is refused after it).
- **Dashlane references** (`dl://...` in `.env`): resolved by mcctl through `dcli read` when a command runs, into
  the process's memory and its children's environment only (Terraform/Ansible, as before); the reference is on
  dcli's argv (it names an item, it isn't a secret), the value only on dcli's stdout pipe. Errors name the key and
  the reference, never a value. The CLI can only read Dashlane, so mcctl creates no items: setup reads each new
  item back and writes the reference only if it returns the exact value. The restic password is shown **once**
  (for a Secure Note) when Dashlane is the store; its reference (`MCCTL_RESTIC_REF`) is checked by doctor and
  never resolved into a child process. A lost laptop then holds references, not tokens (dcli's own lock and
  master password protect the values).

## Branding (P17)

- **The mc-brand plugin** runs in the JVM as `minecraft` like any plugin: no network, no files written (its
  settings `plugins/mc-brand/config.yml` and `server-icon.png` are `root:minecraft 0640`; per-player state, the
  `/hud` choice and the glass serial, lives in each player's own data via the PersistentDataContainer). Texts from
  `server.yml` are plain text with `{player}`/`{server}`/`{ago}` placeholders, never parsed as markup, so a player
  name or a rule can't inject formatting, clicks or commands. Only `https://` links reach `/discord` and the
  pause-menu server links (schema patterns; the plugin checks again). Built, pinned and verified like mc-telemetry
  (javac-only reproducible build, `mc_brand_jar_sha256` asserted on deploy, CI rebuild). Its compile-only jars
  (JetBrains annotations from Maven Central, guava from Mojang's library CDN, both sha256-pinned in
  `plugin/mc-brand/build.yml`) are on javac's classpath only and never reach the jar or the server.
- **New public route: the resource pack** (only with `branding.resource_pack.enabled`, but the route exists in
  every mode): `GET`/`HEAD /pack/<40 hex>.zip` on the watchdog host serves `pack/<sha1>.zip` from
  the Worker's static assets (v1.2: the directory `mcctl render` stages, `terraform` var `watchdog_assets_dir`), without authentication (Minecraft clients can't sign in). Nothing else of the asset
  directory is reachable there (the panel's files stay behind Access on the panel host;
  `watchdog/test/panel.test.mjs`). v1.2: the file is built by `mcctl render` (`mcctl/brandpack.py`, reproducible)
  from the config repo's PNGs (copied byte for byte after a size check: `mcctl render` refuses anything but a PNG
  of the right size, at most 8 KB for the texture and glyph) or the product's neutral art; its sha1 is rendered
  as `mc_brand_pack_sha1` and checked by the client against `resource-pack-sha1`. It is public by design and
  holds only that art (the deployment's look, which players see anyway). Each download is one Worker request (Free plan: a few per join at most, cached by the client
  by hash). Clients send `X-Minecraft-Username`/`-UUID` headers with the request (26.3 client); the Worker never
  reads or logs them (invocation logs are off), but Cloudflare sees them like any request metadata. A
  Cloudflare-account compromise could swap the pack only together with its sha1 in `server.properties`, i.e. with a
  deploy; a swapped file alone fails the client's hash check. Players may decline the pack
  (`require-resource-pack=false`).

## The client modpack, its setup page and the artifact mirror (v1.1)

- **No credential in the minecraft process.** `/modpack` (mc-brand) only writes a request file (uuid, name, a
  random nonce) into its own tmpfs hand-off directory; mc-agent (`mcops`, the agent token's only holder on the box)
  asks the Worker for a code and writes the answer into the verdict directory (setgid minecraft, `0640`: the
  plugin reads, never writes). A compromised JVM can ask for codes for online players' UUIDs (5 per player per
  hour at the Worker, 20 per hour from the box at the agent): at worst it opens the setup page, which holds no
  secret.
- **New public host `play.<zone>`** (only with `client.enabled`): the setup page's static files (CSP `'self'`,
  no third-party requests), `POST /api/code` (a 6-digit code, stored as an HMAC with `CLIENT_KEY`, single use,
  15 minutes; 10 attempts per IP per 10 minutes, keyed by an HMAC of the address, never the address; 200
  failures per hour overall, then everyone waits), a session cookie (HttpOnly, Secure, SameSite=Lax, 1 h, HMAC),
  same-origin JSON POSTs only, signed download links (10 min) and signed mirror links (6 h). Codes, rate counters
  and the download audit (UUID, name, tier, time) live in the watchdog D1 database; the cron deletes spent codes
  daily and audit rows after 180 days. Brute force: 10 tries per IP per window against at most 5 live codes per
  player in a 10^6 space, plus the global cap. The gate keeps the page and the server address unadvertised; it is
  not a security boundary for the mods (public files, integrity by their pinned sha512, checked by the launcher).
- **The packs** are written per download from plans the deploy publishes; every file in them is pinned by sha1 +
  sha512 from `client.lock.yml`, which only the lock tool writes and the gate re-verifies against Modrinth. A
  swapped mirror copy fails the launcher's hash check and it falls back to the official URL.
- **The mirror** (R2 `<name>-mc-artifacts`): written only through the Worker's `/publish/*` routes with
  `ARTIFACT_PUBLISH_TOKEN` (an Actions secret of the `deploy` environment, never on the server, never Ansible's;
  `mcctl/secrets.py` PUBLISH_SECRETS). Artifacts are keyed by their sha256 and never overwritten; the publisher
  checks every pinned hash before uploading, and every consumer (Ansible's `get_url` checksum, the launcher's
  sha512) checks again. The server reads `/artifacts/*` with the agent token. Plans can be replaced by the publish
  token holder only (the deploy job). Only licences on `client/pack.yml`'s `redistributable` list are mirrored;
  each pack carries `CREDITS.txt` and the licence texts.
- **/view** changes only the player's own chunk distance (Paper's per-player API) and is guarded by the health
  word (see [operations](docs/operations.md#the-client-modpack-and-view)); the bench measures its tick cost.

## CI minutes (v1.2): the deploy record and the caches

- **The weekly backup test runs on the server** (`mc-backup-check.service`, as `mcops`, the shared sandbox, no
  capability, scratch copy in its private `/var/tmp`; within the `systemd-analyze` gate). It uses the restic
  credentials the box already holds for its backups: no new credential on the server, and the Monday Actions job
  no longer reads them. It tests from the backed-up box itself; a dispatched `backup-check` still tests from a
  separate machine.
- **The deploy record** (`scripts/deploy_record.py`) lives in the config repo's Actions cache, which any job on
  `main` can write, including `update-test`, which boots third-party jars (and a process with the runner's root
  can lift its cache token). A forged record can make validate **skip** a push's or the weekly slot's deploy,
  never run anything: the record is a fingerprint compared for equality. It can't skip a manual `deploy`
  dispatch, nor a push that changes the workflow (the product pin): that is decided by the git diff, not the
  cache. Worst case, a config change waits for the next push or a dispatched deploy.
- **Caches and third-party code:** pip's download cache is used only where every install is `--require-hashes`
  (a poisoned wheel fails the install), never in `update-test` / `boot-test`, and the deploy job caches nothing it
  would reuse unverified (no Ansible collections cache there).

## The maintenance window (v1.3)

What `maintenance.window` and the update notice add:

- **A deferred restart** is staged in `/var/lib/minecraft/restart-pending/` (owned by `minecraft`, mode 0750, group
  read for the ops user): the marker and the merged config files, written by the deploy as `minecraft` (the same
  user and contents as the live files, `server.properties` with its RCON password included, mode 0640).
  `minecraft.service`'s `ExecStartPre` (`restart_window.py apply`, as `minecraft`, `ReadWritePaths` = the server
  dir and this directory) copies regular files from there into the server dir and empties it: nothing the JVM
  couldn't write itself, and no symlink is followed. `mc-restart-window.service` runs as `mcops` in the shared
  sandbox (+ `MemoryDenyWriteExecute`, `ReadWritePaths` = the markers directory only) and restarts
  `minecraft.service` through the polkit verb self-heal already has: `50-mc-ops.rules` is unchanged.
- **Force:** `MC_FORCE_RESTART` reaches `ansible-playbook` only from `mcctl deploy --force-restart` (never inherited
  from the shell: not in the deploy's environment allowlist) or the workflow input; it can only make a restart
  happen sooner, as every deploy did before v1.3.
- **The update notice** asks `api.github.com` anonymously (no token, `mcctl.http`, HTTPS only) for the pinned
  repository's latest release, at most once a day; it learns nothing about the deployment but which product
  repository it pins, and its answer only selects a line of text.

## Not covered (v1)

- **GitHub Free:** without GitHub Pro the config repo (private, personal account) can't have rulesets, so
  `github.protect_main: false` and the panel's GitHub App could push to main and deploy (see
  [The panel's GitHub App](#the-panels-github-app)); leave the App out on Free, or upgrade.

- No second, independent backup copy (the [R2 bucket lock](docs/reference.md#design-notes) caveat).
- **GitHub Free and environments:** environments with a deployment branch policy on a private personal repo need
  the same paid plan as rulesets. With `github.environments: false` every Actions secret is repository-level,
  readable by any workflow run of the config repo, including one the updater App pushes to `update/**`.
- Admin SSH is `check` (12 h). mcctl's captured SSH sessions (status, logs, migrate, setup's probes) still hold
  silently while a check is due: `mcctl deploy` and `mcctl bootstrap` print the hint and open one visible
  session first; for the others run `mcctl ssh -C $C` once (it shows the login URL).
- No CAA records (see the hardening table): the admin adds them in the dashboard if wanted.
- Tailnet device approval (`tailscale_tailnet_settings.devices_approval_on`) is not managed: the OAuth scope it
  needs and whether CI's federated nodes are pre-approved are unverified.
- WebAuthn-signed panel commands (would defeat a Cloudflare-account compromise) are post-v1.
- **Optional self-hosted e2e runner** (product repo variable `E2E_RUNNER`, unset by default; [docs/ci.md](docs/ci.md)):
  the `e2e` job then runs the branch's code on that machine with `--privileged` Docker and `sudo`. It holds
  no secret (the e2e values are fixed fakes), but the machine is the branch author's. The workflow uses it only
  for trusted code (schedule, `workflow_dispatch`, pull requests from the product repo itself); a fork's pull
  request always runs on GitHub's runner. Use a throwaway VM, give push access only to people you would let run
  code there, and require approval for external contributors' workflow runs.
