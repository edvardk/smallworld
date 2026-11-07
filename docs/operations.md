# Operating the server

Day 2 and after: what you can do from the panel, from Discord and with `mcctl`, what happens by itself every week,
how backups and restores work, and how to read the player-experience verdicts. Setup is in [setup.md](setup.md);
the full command list and the design details are in [reference.md](reference.md).

In the examples `C=~/code/mc-config` is your config repo, and `mcctl` is the command the product checkout's
venv provides (`. .venv/bin/activate` first; `pip install --no-deps -e .` installed it, see
[setup](setup.md#the-product-checkout)). Without that step `python3 -m mcctl` does the same.

Contents:
[The panel](#the-panel) ·
[Discord](#discord) ·
[mcctl commands](#mcctl-commands) ·
[What runs by itself](#what-runs-by-itself) ·
[Backups and restore drills](#backups-and-restore-drills) ·
[The Experience tab](#the-experience-tab) ·
[What telemetry costs](#what-telemetry-costs) ·
[Performance profile](#performance-profile) ·
[Performance tuning](#performance-tuning) ·
[The client modpack and /view](#the-client-modpack-and-view) ·
[Day-to-day tasks](#day-to-day) ·
[Alerts](#alerts) ·
[Monitoring in detail](#monitoring)

## Changing the server

The config repo is the source of truth. Edit `server.yml` (settings) or `players.yml` (whitelist and operators),
then `mcctl publish -C $C` (v1.3): it shows what the change does (which roles, whether Minecraft restarts,
whether Terraform changes, who is online), validates, opens a pull request, merges it when the checks pass and
follows the deploy; `--dry-run` only shows ([reference](reference.md#publishing-a-config-change-v13)). Committing
and pushing to main yourself still works: CI validates and deploys within minutes. A restart happens only if something changed that needs
one (jar, plugin, Java, a systemd unit or a managed config value), with a 60/30/10-second countdown in game,
skipped when nobody is online. `mcctl config set PATH VALUE -C $C` changes one value (schema-checked, comments
kept, committed), and `mcctl players -C $C add NAME` edits the whitelist. The product's own behaviour changes
only when the pin in `.github/workflows/server.yml` moves (patch releases do that weekly by themselves; see
[What runs by itself](#what-runs-by-itself)).

### Maintenance window (v1.3)

Restarts can be kept to quiet hours: `maintenance: {window: "04:00-07:00", timezone: Europe/Dublin}` in
`server.yml` (`HH:MM-HH:MM`, start included, end excluded, may cross midnight like `23:00-01:00`; `timezone`
defaults to `host.timezone`; no `maintenance` = a deploy restarts whenever it needs to, as before).

- A deploy that needs a restart **outside** the window, with the server running, restarts nothing. It installs the
  new jar, plugins, unit or Java (they take effect at the next start), stages the merged config files in
  `/var/lib/minecraft/restart-pending/` and says `RESTART DEFERRED to the maintenance window ...` in its output;
  Discord gets one note ("A config change waits for the maintenance window"). Deploying again outside the window
  changes nothing more (the second run reports no changes).
- **The restart then happens** at the window's start: `mc-restart-window.timer` (the start, then every 15 minutes
  inside the window while a backup, a restore or a maintenance window is in the way) runs the deploy's restart:
  in-game countdown if anyone is online, restart, Discord "the pending config change is live". **Any earlier
  start applies it too** (a panel or `mcctl restart`, self-heal, a reboot): `minecraft.service` copies the staged
  files into place before the JVM starts (`restart_window.py apply`), and the next deploy inside the window does
  the pending restart itself. An admin-stopped server stays stopped; its next start applies the change.
- **Restart now anyway:** `mcctl deploy -C $C --force-restart`, or run the config repo's workflow by hand
  (Actions > server > Run workflow, task `deploy`, tick `force_restart`).
- `mcctl publish` says `restart deferred to the maintenance window HH:MM-HH:MM <tz>` in its preview when the
  change would deploy outside it, and then doesn't ask about players online (nobody is disconnected now).
- On the box: `sudo -u mcops /opt/minecraft/bin/restart_window.py --status` (with the unit's environment:
  `systemctl show mc-restart-window.service -p Environment`) or `journalctl -u mc-restart-window` show what it
  does; `ls /var/lib/minecraft/restart-pending` shows what is staged.

### Players not in Git

A deploy sets the whitelist to `players.yml` plus the panel's pending adds, minus its pending removes. Since
v1.3 it never silently drops someone who is on the live whitelist but was never in `players.yml` (added on the
console, through Discord, or a panel add whose pull request never merged and expired after 24 h). The rule, per
live player that Git + pending adds don't have:

- **removed** if the panel has a pending remove for them, or if `players.yml` listed them at the last deploy and
  doesn't any more (a commit took them out on purpose);
- **kept** otherwise ("unmanaged"): the deploy's output says `KEPT: <names>`, the panel tags them *not in Git*,
  and Discord gets one ⚠️ message the first time each one is kept (not on every deploy). Make them permanent
  with `mcctl players add NAME`, or remove them in the panel.

What `players.yml` held at the last deploy is remembered in `/opt/minecraft/server/whitelist-managed.json`
(written by the deploy, with the players it kept). On the first deploy with v1.3 there is no such file, so
everyone not in `players.yml` counts as unmanaged and is kept; a player taken out of `players.yml` in that same
deploy stays too (remove them in the panel). Ops always come from Git only.

**Strict mode:** `minecraft.strict_whitelist: true` in `server.yml` makes `players.yml` the whole whitelist: a
deploy removes every live player it doesn't list (pending panel adds still count until they merge or expire).

## The panel

`https://panel.<your domain>` (Terraform output `panel_url`). Cloudflare Access lets in only the admin email(s)
in `server.yml` (`panel.access.emails`) with a one-time code by email (or GitHub, `panel.access.idp: github`),
and the Worker checks Access's signed token itself on every request, so a broken Access setting fails closed.
Light and dark theme, works on a phone.

| Tab | Shows | You can |
|---|---|---|
| Overview | TPS and MSPT gauges, players online, CPU per core and steal, memory and heap, disk, network, 2-hour trends, health checks, recent actions, the experience verdict | Restart / Stop / Start (header), Back up now |
| Experience | per-player and server verdicts with reasons and suggested fixes, history (24 h to 2 years) with deploys marked, lag spikes, the product's own overhead | read [below](#the-experience-tab) |
| Console | the stored log plus a live stream | run console commands from an allowlist (say, kick, ban, time, weather, spark, ...; never op, stop or `execute`) |
| Players | online and whitelisted players, rejected logins (last 50 "not whitelisted" attempts) | whitelist add/remove, one-click whitelist or dismiss a rejected login, kick, ban |
| Bans | banned players and addresses | pardon |
| Plugins | the locked plugins and their state | switch a plugin off/on (restart with countdown) |
| Backups | restic's snapshots | back up now, **restore** a snapshot |
| Worlds (v1.2) | the active world, the archived worlds (size, version), what Git still has to follow | **create** a world, **switch** to an archived one, **delete** one ([below](#world-library)) |
| Logs | journal lines in D1 (search by text, unit, level), the R2 archives | |
| Health | 2 h to 90 d history, watchdog state, **Versions** (v1.3: server, Worker, the pin) and the **Panel → Git self-test** | run the self-test now |
| Audit log | every action with the Access identity | |

How actions work: every action opens a confirmation dialog, goes to the Worker, is audit-logged, signed for the
agent on the server, checked again there and run through a fixed systemd unit (never as root). **Stop, plugin
switches, restore and the world library** open a separate confirmation window on `panel-confirm.<your domain>` with a fresh one-time
code (at most the Zero Trust global session, 15 minutes, plus `panel.access.reauth_minutes` old). Restart, stop
and start count down in game (10 s / 60 s / 5 min) and can be cancelled from the header banner. A restore always
takes a **safety snapshot** of the current world first (tag `pre-restore`).

Whitelist changes from the panel apply at once and are also written to `players.yml` through a pull request from
the panel's GitHub App, which merges by itself when only the whitelist changed. Until it merges, deploys keep the
change; if you close the pull request without merging, the change is reverted on the server. The name check in
the whitelist form (v1.3) asks Mojang **from the server** (mc-agent's `lookup`: api.mojang.com refuses
Cloudflare's addresses); when the agent is offline or slow (4 s) the Worker asks itself, and if nobody gets an
answer the form offers to add the name unverified (the server refuses a name with no Java account).

### Versions (v1.3)

The header reads "Smallworld vX.Y.Z · Purpur 26.3". The Health tab's **Versions** row shows three things that
used to drift apart silently (v1.2.2 reached the server and the Worker in separate steps):

- **Server (deployed):** each deploy writes `/opt/minecraft/product-version.json` (product version and commit,
  root-owned, world-readable); mc-agent reports it, and the version its own process runs.
- **Worker (applied):** `mcctl render` stamps the product version and the config repo's pin into the Worker's
  static assets (`version.json`, not served), which `mcctl apply` uploads with `worker.js`.
- **Config repo pin:** as of the last apply.

| Warning | Fix |
|---|---|
| The Worker is older than the server | `mcctl render` + `mcctl apply` |
| The server is older than the Worker | deploy (push to main, or `mcctl deploy`) |
| mc-agent runs older code than installed | restart it (a deploy does) |

`mcctl doctor` checks the same against the config repo's pin as it is now (rows `versions` and `panel → git`,
read from the watchdog's D1 with the Cloudflare token), and the home screen and `mcctl status` show the deployed
version over SSH.

### The Panel → Git self-test (v1.3)

Proves the panel can still open pull requests without opening one (v1.2.2: GitHub's longer tokens were refused
for days unnoticed). mc-agent asks the Worker for a token, checks it with a fresh token cache (shape, expiry) and
its permissions (Contents + Pull requests write), then reads the config repo with it: the repository, main's head
and `players.yml`, the open pull requests. Nothing is created. It runs 5 minutes after the agent starts (so after
every deploy) and then daily, and from the Health tab ("Run the self-test now", at most once a minute). The result
and each step are in the Health tab and in `mcctl doctor`; two failures in a row post one Discord alert, and one
more when it passes again. Errors name the step (`token`, `permissions`, `repository`, `main`, `pulls`) and never
contain the token.

## World library

The Worlds tab keeps several worlds per server (v1.2). Only the **active** world is on the server; every other
world is an **archive**: a restic snapshot of the server dir in the same R2 repository as the backups, tagged
`world-archive`, `world:<name>` and `dv:<DataVersion>`. The backup retention (`forget --tag auto`) never selects
them, so archives stay until you delete them.

- **New world…**: a name (lowercase letters, digits, `_`, `-`), a seed (empty = random), a **world type** (default,
  large biomes, amplified, single biome: plains on a dedicated server, superflat classic / the void / redstone
  ready) and a **server preset** (survival SMP, creative build, hardcore, farm test, peaceful: difficulty,
  gamemode, force-gamemode, hardcore in `server.properties`), and optionally this world's border and whether
  Chunky pre-generates it.
- **Start from** (in the New world dialog): the product's **seed catalogue** (`worlds/seeds.yml`), researched
  seeds with what makes each one worth playing (a village, a cherry grove, an ancient city near spawn...). Each
  claim was measured with `/locate` from the world spawn on the pinned server build, and only seeds whose every
  claim held are offered, with a **verified on <version>** badge and the measured distances; if your server runs
  another Minecraft version, the dialog says the world may generate differently. Picking one only fills the seed
  and the default world type (the claims hold for that type); the rest of the dialog is the same.
- **Switch…** to an archived world. A world saved by a **newer** Minecraft can't be loaded and is refused; an
  **older** one is upgraded when it loads, one way (its archive keeps the old copy). The dialog says which.
- **Delete…** an archived world (never the active one). Its snapshots go once the bucket lock allows (younger ones
  with a later backup); until then the name can't be reused.

What a switch (or create) does, step by step in the header banner: in-game countdown (skipped when nobody is
online) → stop → **archive the active world** (the final backup and the safety snapshot in one: nothing else
happens unless it is in restic's list) → fetch the target archive (or nothing, for a new world) → swap the world
folders (`world`, `world_nether`, `world_the_end`) as `minecraft` and write the world's `server.properties` keys →
start → check that it answers (and has the seed you asked for) → set its border → back it up. If anything fails
before the swap, the old world starts again untouched; if the swap itself fails, the server stays stopped and the
admin-stopped marker names the archive to restore.

**Per world:** inventories, ender chests, XP, advancements and statistics (each world folder has its own
`playerdata/`, `advancements/`, `stats/`), the border and pre-generation. **Shared:** the whitelist, operators,
bans, plugins and their data, and every other `server.properties` key. Game rules live in each world's
`level.dat`.

**Git follows:** each change goes to `server.yml` (`world.active`, `world.library`, one canonical line per world)
in a pull request from the panel's GitHub App; it merges by itself when only those two keys change. Deploys
honour the live world meanwhile and **never switch worlds**: if `server.yml` names another world, the deploy
warns and keeps the running one. Don't edit `world.active` by hand to switch; use the panel. With a library set,
`minecraft.overrides` may not set the world's `server.properties` keys (validation says so).

**The seed catalogue's verification** (`scripts/seed_check.py`, product CI only): `seed_check.py verify` boots the
locked server jar once per seed (a throwaway world, `level-seed` set, RCON on 127.0.0.1 with a random password,
no plugins), asks every claim (`locate structure <id>`, `locate biome <id>`, `execute in <dimension> run ...` for
the Nether and the End) and writes `watchdog/panel/seeds.json`: the catalogue plus each measured distance, the
Minecraft version and the server build. ci.yml's `seeds` job re-verifies whenever the catalogue, the checker or
the server lock change (and nightly) and fails when a claim no longer holds or the committed file differs;
update-lock.yml regenerates the file with every lock bump. To add a seed: an entry in `worlds/seeds.yml` (name,
quoted seed, one-line description, the `https://` source, claims with `within` blocks), then
`python scripts/seed_check.py verify --seed <id>` (needs Java and downloads the pinned jar) and commit both;
`tests/test_seeds.py` refuses an unverified claim.

**Cost:** R2 includes 10 GB of storage; the tab shows a note when the archives pass that. restic deduplicates, so
an archive costs what changed since the snapshots it shares data with, not its full size.

## Discord

Alerts (optional, `alerts.discord`): the server down (and which of IPv4/IPv6), heartbeats late, a backup
failing, self-heal giving up, disk >= 85 %, sustained CPU steal, a pending reboot after 3 days, credential or
domain expiry, a resize hint when the host is too small, and every restart announcement.

Slash commands (optional, `discord.commands`): only the guild, channel(s) and user(s) in `server.yml` may use
them; they are hidden from non-administrators in the guild. The admins (`discord.user_ids`) may run all of them;
helpers (below) all but `/backup now`.

| Command | Answer |
|---|---|
| `/status` | up or stopped, players online, TPS/MSPT, the watchdog's probes and checks, the experience headline |
| `/players` | who is online |
| `/lag [player]` | the current experience verdict and its reasons (for one player, or the server) |
| `/perf [24h\|7d\|30d]` | tick-time percentiles, TPS and verdict minutes over the range |
| `/backups` | the latest snapshots |
| `/whitelist list` | the whitelist |
| `/rejected` | recent "not whitelisted" login attempts |
| `/backup now` | starts a backup (an action: at most 3 actions per 10 minutes) |
| `/whitelist add <name>` | whitelists a player (live, then the usual pull request) |

Nothing destructive is possible from Discord: no stop, restart, restore, plugins or console. `mcctl discord sync
-C $C` re-registers the commands and the endpoint (for example after changing the allowlist).

### Discord helpers (v1.0.1)

Helpers may run the read commands (`/status`, `/players`, `/lag`, `/perf`, `/backups`, `/rejected`),
`/whitelist list` and `/whitelist add`, in the same channel(s) as you; `/backup now` stays yours. Each helper
action is audited with their Discord id (`discord:<id> (helper)` in the panel's audit), and the rate limits are
per user, like yours (10 commands a minute; Discord actions share the 3 per 10 minutes).

1. In Discord: Server Settings → Roles → Create Role (for example "Whitelist helper"); give it to your helpers.
2. Copy its id: User Settings → Advanced → Developer Mode on, then Server Settings → Roles → right-click the
   role → Copy Role ID. (For single people instead: right-click the user → Copy User ID.)
3. Let the role see the commands (they're hidden from non-administrators): Server Settings → Integrations → your
   application → Manage → add the role (to every command but `/backup` if you like; the Worker refuses
   `/backup now` from helpers in any case).
4. Set it and deploy the Worker binding (it changes, so a push alone isn't enough):

   ```bash
   mcctl config set -C $C discord.helper_role_id '"<role id>"'     # and/or discord.helper_user_ids '["<id>"]'
   git -C $C commit -am "discord: helpers" && git -C $C push
   mcctl apply -C $C
   ```

To revoke a helper, take the role away in Discord (it counts from the next command: the role comes with each
signed interaction), or remove their id from `discord.helper_user_ids` and `mcctl apply` again.

## mcctl commands

The ones you will use (all take `-C $C`; the complete list is in [reference.md](reference.md#mcctl-commands)):

| Command | What it does |
|---|---|
| `mcctl` | v1.3: on a terminal, the home screen: a status header and a menu of these commands ([reference](reference.md#the-home-screen-and-the-remembered-config-repo-v13)); remembers the config repo, so `-C` is optional |
| `mcctl doctor` | checks tools, `.env`, every credential live (writes `expiry.yml`: commit it), the product pin, DNS, the tailnet, the watchdog; `--fix` repairs what it can (file mode, a stale product pin after a typed `yes`) |
| `mcctl status [--json]` | server state, players, TPS/MSPT, last backup, last deploy run, drift (disabled plugins, open panel PRs) |
| `mcctl deploy [--check] [--force-restart]` | deploy from your computer (the same playbook and secrets as CI; `--check` is a dry run with a diff; `--force-restart`: restart now even outside `maintenance.window`) |
| `mcctl publish [--route pr\|main\|deploy] [--dry-run] [--revert] [--wait-empty]` | v1.3: preview what your config change does (roles, restart, Terraform, who is online), validate, then a pull request, its checks, your merge and the deploy ([reference](reference.md#publishing-a-config-change-v13)) |
| `mcctl players list\|add\|remove\|op\|deop NAME` | edit `players.yml` (UUID from Mojang); v1.3: as a pull request you merge when green (`--push`: straight to main) |
| `mcctl rcon CMD`, `mcctl ssh`, `mcctl logs [unit] [-f]` | a console command, a shell as `deploy`, the journal (over Tailscale SSH) |
| `mcctl backup now\|list` | a backup now / the snapshots |
| `mcctl restore --list \| --to /var/tmp/DIR \| --live ID` | list snapshots (the last pre-upgrade one marked), restore into a scratch directory and verify, or restore the live world (typed confirmation, safety snapshot first) |
| `mcctl stop [--reason T]`, `mcctl start` | stop on purpose (self-heal, the watchdog and deploys leave it stopped; the reason is recorded) / start again |
| `mcctl upgrade [--to vX.Y.Z] \| --minecraft-version V [--dry-run]` | a pull request moving the product pin, or upgrading Minecraft (boot-tested; you merge) |
| `mcctl migrate --to IP [--ipv6 IP] [--dry-run]`, `mcctl retire-old` | move to a new machine, resumable, with rollback ([reference.md](reference.md#move-to-a-new-vps)) |
| `mcctl secrets rotate NAME [--dry-run]` | replace a generated secret (`rcon`, `heartbeat`, `agent-token`, `command-hmac`, `r2`, `restic-key`): typed apply, then a deploy |
| `mcctl secrets show-restic` | print the restic password once, to a terminal only |
| `mcctl secrets dashlane [--convert]` | the Dashlane items for `.env`; `--convert` swaps values for `dl://` references |
| `mcctl metrics [--days N]`, `mcctl metrics export --since YYYY-MM-DD` | the experience summary (worst hours, verdict minutes, overhead, tick p99 before/after each deploy), or CSV straight from D1 |
| `mcctl plan`, `mcctl apply` | Terraform with the rendered inputs; apply always shows the plan and asks you to type the name |
| `mcctl setup` | the setup checklist again: resumes, re-checks, offers to move a stale product pin |

`plan`, `apply`, `deploy`, `bootstrap`, `migrate`, `upgrade --minecraft-version` and `setup` run the product
commit your config repo pins: if you started another version, mcctl re-runs itself from a cached checkout of the
pinned one (`~/.cache/mcctl/<sha>`). A product `upgrade` (`--to`) runs the checkout you start it from, and runs
the *target* release's schema migration and validation itself. When the checkout you run is *newer* than the pin, `mcctl setup` and `mcctl doctor
--fix` offer to move the pin forward to it (typed `yes`; forward only, pushed commits only).

## What runs by itself

| What | When | Notes |
|---|---|---|
| Hourly backup | every hour (+ up to 5 min random delay) | restic to R2; the lock keeps every snapshot at least 7 days |
| Prune | Sundays 03:30 | between backups |
| Health check + self-heal | every 5 minutes | restarts a crashed server, dumps and restarts a frozen one; at most 3 restarts a day, then Discord |
| Watchdog | every 5 minutes, outside the box | status pings over IPv4 and IPv6 separately; heartbeat lateness; alerts on Discord |
| Server build and plugin updates | weekly, `automation.update_day` (Tuesday) 04:17 UTC | the newest builds of the same Minecraft version are boot-tested in CI, then the updater App opens a pull request that merges itself once `server / gate` re-verified every hash |
| Weekly deploy | the same slot | deploys `main` as it is; restarts only if something changed |
| Product updates | the same slot | a newer **patch** release moves the pin by itself; minor and major releases open a pull request for you |
| A restart a deploy left for the maintenance window | `maintenance.window`'s start (then every 15 minutes inside it) | only with a window and a pending restart ([Maintenance window](#maintenance-window-v13)); any earlier start of the server applies it too |
| Reboots for OS updates | checked every 30 minutes | only inside `automation.reboot_window` (04:30-06:00 local), with nobody online, no backup or restore running and the server not stopped on purpose; **never forced**: still pending after 3 days, Discord reminds you daily |
| Backup check + expiry check | Mondays | `restic check` on 5 % of the data and a restore of the newest snapshot (must be < 2 h old); credential expiry (`expiry.yml`), domain expiry (RDAP) and the watchdog's freshness. Problems go to Discord and the run turns red |
| Security updates on the box | daily (Debian unattended upgrades) | Tailscale is held at its pinned version (bump the pin for Tailscale security fixes: [reference.md](reference.md#bumping-versions)) |

Never automatic: a new Minecraft version (`mcctl upgrade --minecraft-version`), minor/major product releases,
and anything in `server.yml`.

## Backups and restore drills

- **What is backed up:** the world folders and server data, hourly, with restic (encrypted with the restic
  password, which exists only in Terraform state and your password manager) into an R2 bucket with a **bucket
  lock**: snapshots and data younger than 7 days, and the repository's keys, can't be deleted or overwritten, not
  even with the server's own credentials ([reference.md](reference.md#design-notes) explains the lock and its
  limits).
- **Checked weekly** by CI (Mondays). Do your own drill once after setup and then every few months:

  ```bash
  mcctl backup list -C $C --latest 5
  mcctl restore -C $C --to /var/tmp/drill      # newest snapshot into a scratch dir on the server, verified
  mcctl restore -C $C --list                   # snapshot ids (the last pre-upgrade snapshot is marked)
  ```

  `--to` restores into `/var/tmp` or `/tmp` on the server (restic runs sandboxed) and checks the file count, a
  sample of checksums and that the world loads (level.dat + region files).
- **Restoring the live world** (griefing, a bad plugin, a failed upgrade): the panel's Backups tab (type
  `restore <id>` in the confirmation window), or `mcctl restore -C $C --live <id>` (type `restore <first 8
  characters>`). Both open a maintenance window, take a safety snapshot, stop the server, fetch the snapshot as
  the ops user, copy it in as `minecraft` and start again. `/opt/minecraft/server/.restored-from` then names the
  snapshot. Undo a restore by restoring the safety snapshot.
- **The restic password** is the one thing you can't recover: keep it in your password manager (`mcctl secrets
  show-restic -C $C`).
- **A new machine** restores the newest snapshot automatically before its first start; with `world.expected:
  true` (set by setup after the first backup) the deploy fails rather than generate a new world when it can't.

## The Experience tab

The in-house `mc-telemetry` plugin measures every tick and each player's ping, client view distance and nearby
entities; the agent sends one summary per minute to the Worker, which judges it. Each player and the server get a
verdict, every minute:

| Verdict | Server | Player |
|---|---|---|
| **Good** | the slowest 1 % of ticks under 50 ms and TPS >= 19 | ping under 150 ms, no spikes, and the server is Good |
| **Fair** | slowest 1 % of ticks 50-100 ms, or TPS 17-19, or the heap > 90 % full after garbage collection | ping 150-250 ms, a spike over 300 ms, a very low client render distance, or the server is Fair |
| **Poor** | slowest 1 % of ticks >= 100 ms, or TPS < 17 | ping >= 250 ms, or the server is Poor |

Every verdict comes with plain reasons: "High ping (280 ms): your connection, not the server", "Server lag spikes
(ticks up to 140 ms) while you were near 520 entities at world 1200,-340", "Chunk loading is slow while flying
fast into new land", "Most players have a high ping (the host may be far from them)". A player's ping verdict
changes only when the ping is clearly past the threshold (10 % hysteresis), so the history doesn't flicker.

**Remedies** are suggestions, never applied for you: for server lag, profile with spark (bundled) and lower
`performance.simulation_distance`/`view_distance`; for entity hotspots and crowded chunks, building advice only
(no mechanic changes: spread the pen or farm over more chunks, add floor space, collect drops quickly; vanilla
cramming kills mobs above 24 per block); for chunk loading, the pre-generated world border; for memory,
`jvm.heap_mb` or a bigger host; for a cluster of high pings, a location closer to the players; for players' FPS,
client mods (Sodium, Lithium). Plugins and mods are checked on Modrinth for your Minecraft version first.

**Busiest chunks** lists the loaded chunks with the most entities (and the most tile entities, e.g. hopper
lines) in the latest minute, with block coordinates, villagers and dropped items. The plugin scans every loaded
chunk once per snapshot window, one chunk per step inside its per-tick budget; a chunk at or above
`monitoring.alerts.chunk_entities` (default 300; 0 = off) is flagged here, named in the server verdict and
alerted on Discord once (again after 6 h if it is still crowded).

Players see it too: `/lag` in game explains the current situation privately, and the server can send a short
private note when it was the server's fault, not theirs (rate-limited; `/lag tips off` turns them off). The
history marks every deploy, so you can see whether a change helped (`mcctl metrics` prints tick p99 before and
after each deploy). The **overhead** section shows the product's own services (backup, agent, health) against
Minecraft: they run at low priority and should stay under 1 % CPU and 100 MB each.

## What telemetry costs

Nothing, on the free plans. With the defaults, metrics, logs and the experience telemetry together use about
13 % of the Workers Free daily request limit, under 40 % of D1's daily row writes, a few percent of reads, a
separate telemetry database of 100-180 MB (of 500 MB), and about 0.1-0.3 GB of R2 a year for the raw archive. The
schema refuses settings above 55 % of any free daily limit (`schema.budget`), so a typo can't push you onto a
paid plan. If you ever exceed a free
limit, Cloudflare stops serving that resource for the day; the server itself keeps running, and the agent keeps
the data on the box and backfills it later.

## Performance profile

`performance.profile` decides how the server's game mechanics are tuned. The product default is **`vanilla`**:
every mechanic behaves exactly as in vanilla Minecraft, so mob farms, villager halls, redstone and TNT
contraptions built from vanilla tutorials work as designed. Only optimisations no player can notice are on.
**`balanced`** is the product's earlier tuning (cheaper, but farms and some contraptions behave differently).
Every field marked "Default: performance.profile" in `schema/server.schema.json` overrides its profile value
on its own (`mcctl config set performance.simulation_distance 6`); any other key can still be set through
`minecraft.overrides`.

| Setting | `vanilla` (default) | `balanced` | Player-visible? |
|---|---|---|---|
| simulation / view distance | 8 / 10 chunks | 5 / 8 | yes: 8 = the full 128-block spawning sphere around each player |
| mob caps (`spawn_limits`) | 70 / 10 / 5 / 20 / 15 | 50 / 8 / 3 / 1 / 1 | yes |
| spawn attempts (`bukkit.yml ticks-per`) | monsters, water, ambient every tick; animals every 400 | monsters every 4, others 400 | yes |
| monster despawn soft / hard | 32 / 128 | 30 / 56 | yes |
| entity activation ranges | 0 (= never inactive) | 16-48 blocks | yes: distant mobs, crops, farms |
| villagers | always tick, no lobotomy, search radius 48 | inactive villagers frozen, lobotomy, radius 32 | yes |
| item / XP merge radius | 0.5 / off (vanilla merging) | 3.5 / 4.0 | yes |
| item despawn | 5 min for everything | junk blocks 15-30 s | yes |
| `max-entity-collisions` | 8 (Paper's default; vanilla has no cap, cramming at 24 kills first) | 2 | barely |
| `max-tnt-per-tick` | 0 = no cap | 100 | yes (TNT cannons, world eaters) |
| redstone | `VANILLA` | `ALTERNATE_CURRENT` | yes (update order) |
| armor stands tick, hoppers through occluding blocks, chest cat check, treasure maps, dolphins, zombies vs villagers | vanilla | Paper/Purpur shortcuts | yes |
| `optimize-explosions` | off (Paper's per-tick cache can return a stale exposure) | on | rarely |
| tick rates (grass spread, spawners) | 1 | 4 / 2 | yes |

Kept in **both** profiles because no player can tell: `sync-chunk-writes: false`, chunk GC every 20 s,
autosave spread over ticks (8 chunks per tick), `hopper.disable-move-event` (a plugin event nothing listens to),
`per-player-mob-spawns` (vanilla's own per-player cap since 1.18), network compression, and
`performance.tracking_range`: how far away clients are *sent* entities (animals 48, monsters 64, misc 48 -
item frames and paintings stay visible across a build -, players and display entities 128, other 64). It never
changes whether an entity ticks, spawns or works; it saves the tracker's work and packets around big farms.
ZGC and the CPU split (`jvm.gc`, `performance.cpu_affinity`) are invisible too.

**Cost of vanilla.** More ticking entities and chunks: simulation distance 8 instead of 5 is ~2.5x the ticking
area per player, and no activation range means every mob in it is fully simulated. On a 4-vCPU box with 5-6
players that is the budget the product sizes for (see the bench numbers in the design doc, §8 "Performance
profile"); watch the Experience tab's tick p99 and "Busiest chunks". View distance 10 costs chunk sending and
memory only (chunks outside the simulation distance don't tick).

**Switch**: `mcctl config set performance.profile balanced -C $C`, push (a deploy with an announced
restart). Back: `... vanilla`. Both profiles set the same keys, so switching never leaves an old value behind.

## Performance tuning

Five `server.yml` knobs aim at the slowest ticks (p99/p99.9), not the average. `jvm.gc` defaults to `zgc` (G1
caused 55-65 ms pause ticks in every bench); the others default to the behaviour before they existed. Run a **baseline week** on the defaults first, then
change **one knob at a time** and give it a week of similar play.

| Knob | Values (default first) | What it does |
|---|---|---|
| `jvm.gc` | `zgc`, `g1` | `zgc` = Generational ZGC (Java 25): pauses well under 1 ms instead of G1's occasional 20-60 ms young pauses; costs some CPU (concurrent GC threads) and needs headroom in the heap. Same heap size; the deploy also sets THP `shmem_enabled=advise` for ZGC's heap (back to `never` with `g1`) |
| `performance.cpu_affinity` | `off`, `split` | `split` = the JVM on CPUs 1..n-1, the product's helpers (agent, health, backup, prune, reboot gate) on CPU 0. Refused on fewer than 3 CPUs. Leave `performance.helper_cpus` unset with it |
| `performance.chunk_threads` | `auto`, 1-32 | Paper's chunk worker threads (`auto` = Paper's default: 1 up to 7 cores, 2 on 8-9, cores/4 from 10; without libnuma Paper counts only the CPUs the JVM may use, so `split` can lower it on 8-9 CPUs) |
| `performance.network_compression_threshold` | `256`, -1-65535 | packets of at least this many bytes are compressed (-1 = off); raise it only if CPU, not bandwidth, is short |
| `performance.noatime` | `false`, `true` | adds `noatime` to the fstab entry of the server's filesystem and remounts it live; `false` leaves the mount alone |

**Switch** (each one is a deploy with an announced restart, skipped when nobody is online):

```bash
mcctl config set jvm.gc zgc -C $C                    # schema-checked, comments kept, committed
git -C $C push                                         # CI validates and deploys
```

**Watch** in the panel's Experience tab: the server's tick p99/p99.9 and max lines (the deploy is marked on the
chart), lag spikes, "heap after GC" (with ZGC keep it under ~70 %: above that ZGC may stall allocating threads,
which `mc-health` reports as GC pauses), and the overhead card (minecraft's CPU goes up a little with ZGC). The
Overview's GC pause figure comes from the same log for both collectors.

**Compare** before and after: `mcctl metrics -C $C --days 14` prints tick p99 for the 24 h before and
after every deploy, plus the worst hours; for a full week each side, `mcctl metrics export -C $C --since
YYYY-MM-DD` gives the minute rows (p99, p99.9, max, players) as CSV. Compare hours with similar player counts: a
quiet week always looks better.

**Roll back** the same way: `mcctl config set jvm.gc g1` (or `cpu_affinity off`, `chunk_threads auto`, ...) and
push. `noatime: false` does not remove `noatime` again (it leaves the mount alone): edit `/etc/fstab` by hand if
you really want `relatime` back, then `sudo mount -o remount,relatime /`.

CI measures the collectors on every product change (`bench-gc` in the e2e-mock workflow: G1 vs ZGC, with and
without the CPU split where the runner has 3+ CPUs, same synthetic load); the job's summary has the numbers and the recommendation.

## The client modpack and /view

v1.1: an optional client for players (Fabric with Sodium, Lithium, FerriteCore, Mod Menu, and in the higher tiers
Iris, Distant Horizons and a shader pack) in four tiers, Low, Medium (recommended), High and Maximum, from one
definition (the product's `client/pack.yml`); a setup page at `play.<zone>` that only opens with a one-time code
from `/modpack` in game; and `/view <n>`, a player's own view distance up to 16. Players' side:
[player-guide.md](player-guide.md).

Turning it on (needs `panel.mode: cloudflare` and branding):

1. `mcctl config set client.enabled true -C $C`: if the config repo has no `client.lock.yml` yet, this writes
   it first (the pinned mods for your Minecraft version from Modrinth, their hashes and licences; a mod without
   a build yet is listed as pending) and commits both files. Options: `client.hostname` (default `play`),
   `client.prompt`, `client.tiers`, `client.view.max` / `.check_seconds`.
2. Alternatively, as a pull request: `mcctl lock update --client -C $C` writes the first `client.lock.yml` with
   the other locks; merge it yourself (a new client lock always needs the admin), then step 1.
3. `mcctl apply -C $C` (a deploy alone is not enough: without it `/modpack`'s page answers "the client page
   isn't set up on this server yet"): the setup page's host, the Worker's new bindings, the artifact mirror bucket and the
   `ARTIFACT_PUBLISH_TOKEN` Actions secret. Your config repo's workflow must pass it to the product
   (`ARTIFACT_PUBLISH_TOKEN: ${{ secrets.ARTIFACT_PUBLISH_TOKEN }}` under `secrets:`; new config repos have
   it, and `mcctl doctor --fix`, the pin move and `mcctl upgrade` add it to older ones). With the client on,
   a deploy without it fails.
4. Push: the deploy publishes the packs and fills the mirror (`mcctl client-publish`) before Ansible runs.
5. Check: `mcctl doctor -C $C` reports `client page` (the Worker's `/status` says whether its client bindings are
   in place; a failure means step 3 is missing). On the box, `journalctl -u mc-agent` shows one line per cause
   when the Worker refuses a `/modpack` code request (a 404 is the missing `mcctl apply`).

What players see: a clickable line after the greeting at every join while they play unmodded Minecraft
(`/modpack off` stops it; players on any modded client never see it), `/modpack` for their link, and
`Settling in…` in the HUD for the first seconds after joining.

v1.2: the page's first, recommended path is the official Minecraft Launcher: per tier a zip (Fabric's launcher
profile + a `smallworld-<tier>` game directory with every mod we may share) that the deploy builds and stores in
the mirror; the mods whose licences forbid sharing (Xaero's maps, the Maximum shader) are links to their official
files. The `.mrpack` stays for the Modrinth App and Prism. A client lock from v1.1 has no Fabric profile yet: the
next `mcctl lock update` adds it, until then only the `.mrpack` is offered.

Every week the update job re-resolves `client.lock.yml` with the other locks (newest releases for your Minecraft
version, pending mods picked up as soon as they exist); the gate re-checks each changed entry against Modrinth.
A client-only change skips the server boot test.

**The artifact mirror.** In cloudflare mode the deploy also mirrors every pinned artifact we may redistribute
(the server jar, Modrinth plugins with an allowed licence, restic, the client's mods) into R2 under
`artifacts/<sha256>/<filename>`; the server fetches from there first and from upstream second, hash-checked
either way, so an upstream outage no longer blocks a deploy. The packs' mods come from the mirror through
short-lived signed links, with the official URL as the launcher's fallback. The shader pack is the exception:
its licence allows modpacks only through Modrinth, so the launcher always downloads it from Modrinth.

**/view.** Lowering always works; raising above the server's view distance only while the HUD's health word is
Smooth. Two non-Smooth readings in a row (every `client.view.check_seconds`) put every raised player back to the
server's distance; after six Smooth readings they go back up, one player per check. `/mcbrand status` shows the
guard's counts. The bench (`scripts/bench_view.py`, CI `bench-view`, report-only) measures the tick cost of
walking players at 16 against 12.

## Branding

`server.yml`'s `branding:` section gives the server a name, colours, a server-list look and a friendly welcome.
Everything is server side: players keep vanilla clients and nothing changes gameplay (no rule, limit or
mechanic touches farms). The defaults are generic; set your own and push (`mcctl config set branding.name
"My Server" -C $C`, or edit `server.yml`). Every change is a deploy with an announced restart.

| Field | Default | What players see |
|---|---|---|
| `branding.name` | `minecraft.motd` without codes | titles, the tab list, the sidebar, the panel's heading |
| `branding.colors.text` / `.accent` / `.secondary` / `.subtle` | `#FFFFFF` / `#55AAFF` / `#2E6FB0` / `#C8D0D8` | in-game text colours (`#RRGGBB`): text, the name/players/links, separators, hints and lore |
| `branding.motd` | `[]` = `minecraft.motd` | the server list, 2 lines; `&f` white, `&c`/`&4` reds, `&7`/`&8` greys, `&l` bold, ... (the server list has only Minecraft's 16 colours), 59 visible characters a line |
| `branding.icon` | the product's blue flask on slate | a 64x64 PNG in your config repo (path), shown in the server list |
| `branding.join.*` | title on, generic greetings, a level-up sound | first join: `first_join`; later: `welcome_back` with `{ago}` (from the player's saved data); `{glyph}` (below) in any of them |
| `branding.rules` | 4 short rules | `/rules` (paged) |
| `branding.discord_invite` | none | `/discord` (a clickable link) and the pause menu's "Community" link |
| `branding.rules_url`, `.website` | none | more pause-menu links (https only) |
| `branding.hud.*` | available, `default_mode: top` | the HUD (below), refreshed every 5 s; the tab list's header/footer |
| `branding.drink.*` | off | the signature drink (below): `name`, `command` (default `drink`), `lore`, `color`, `texture` |
| `branding.resource_pack.*` | off | the drink's texture and the `{glyph}` icon (below): `prompt`, `description`, `glyph` |
| `branding.holograms[]` | none | floating texts: `x`/`y`/`z` or `at: spawn` (v1.3: 3 blocks above the world spawn), `lines`, `health`, `help` (v1.3: appends `/help`'s list) |
| `branding.compass.*` | on | v1.3: the friend compass (below): `enabled`, `default_on`, `max_range` (1000 blocks), `distance_rounding` (5), `height_threshold` (8) |
| `branding.pocket_map.enabled` | on | v1.3: `/map` (below) |

**Commands** (everyone): `/help` (v1.3, alias `/commands`: the list below, built from what's on; vanilla's list is
`/minecraft:help`), `/rules [page]`, `/discord`, `/hud [top|side|off]` (remembered per player; `/hud` alone
switches between the top bar and off), `/drink` (and the drink's own `branding.drink.command`), `/compass
[on|off|hide|show|auto]` and `/map [zoom]` (v1.3). A new player command goes into `Help.entries` (the plugin's
one list; `tests/test_wayfinding.py` fails otherwise). Operators and the console: `/mcbrand status` (the plugin's task
time, and the technical numbers: TPS, MSPT, p95) and `/mcbrand selftest` (renders every text with a sample player,
fills a sidebar and a boss bar and checks them; also the e2e test).

**Wayfinding** (v1.3; [features](features.md#finding-friends-without-mods-the-compass-map-and-help) has the
picture). The compass is a boss bar of its own (under or over the HUD's top bar), redrawn only when its player
turns or moves (at most every 4 ticks) or someone else moves (at most once a second); nothing runs while nobody
moves (`/mcbrand status`: `compass showing/renders/updates`). Who gets it by default follows the client: off for
the Smallworld mod together with Xaero's maps, on otherwise (`branding.compass.default_on: false` makes it
opt-in for everyone); players override it with `/compass on|off`. The pocket map is one map id per player (kept
in the player's data), so `/map` never piles up map files; while it is held mc-brand re-centres it and draws the
old picture shifted until vanilla has redrawn the terrain (vanilla only draws while a map is held, 128 blocks
around the player). The client-mod detection reads the channels the client registers (`smallworld:*` = the
Smallworld mod; `xaerominimap:main` / `xaeroworldmap:main`, or any `xaero*` namespace, = Xaero's maps).

**The HUD** (v1.0.1). Players choose where it is; `branding.hud.default_mode` (`top`, `side` or `off`; default
`top`) is what everyone gets until they choose (a config from before v1.0.1 with `default_on: true` means `side`).

- **top**: a bar at the top centre of the screen, one line: `My Server · 3 online · Smooth · 20/20 TPS · 8 of
  50 ms · you 23 ms` (the last number is the player's own connection). The bar fills with the share of the 50 ms
  tick budget the server uses, and its colour is the server's health.
- **side**: the sidebar (the client always puts it at the right): every player with a coloured dot and their ping
  (green under 80 ms, yellow under 200 ms, red above), then `Server Smooth`, `Speed 20.0/20 TPS`, `Tick time 8.3
  of 50 ms` and `FPS: press F3`.

**What the words and numbers mean** (the same wording in the HUD, the tab list, `/lag` and the hover texts in chat):

- **Server health**: *Smooth* (green: the slowest 5 % of recent ticks under 35 ms and at least 19.5 TPS), *A bit
  busy* (yellow: under 50 ms, still full speed), *Laggy* (red: the server can't keep up).
- **TPS (ticks per second)**: how many game updates the server manages each second. 20 is full speed; below 20
  the world slows down. Shown as `20.0/20 TPS`.
- **Tick time (MSPT)**: milliseconds each update takes. Must stay under 50 ms for full speed; lower is better.
  Shown as `8.3 of 50 ms`.
- **Your connection (ping)**: the time for data to reach the server and back. Lower is better; under 80 ms feels
  instant.
- When the server is smooth and the player's connection is fine, `/lag` says so plainly: "The server and your
  connection look fine, so stutter is likely your PC's frame rate. Press F3 to see your FPS (top-left). Lowering
  render distance or installing Sodium usually helps."

**The glyph** (v1.2: `{glyph}`): Minecraft's fonts have no emoji (one would show as a box), so the resource pack
carries a small 8x8 glyph: the product's neutral one, or your own with `branding.resource_pack.glyph:
art/glyph.png` (an 8x8 PNG in the config repo, at most 8 KB, used byte for byte). Write `{glyph}` in
`branding.rules`, `branding.join.*` and the subtitles: players whose client loaded the pack see the glyph there,
in the HUD's and the tab list's title and in the join title; everyone else sees the text without it. Before
v1.2 the placeholder was `{wine}` (or a literal 🍷); `mcctl upgrade` rewrites both to `{glyph}` (the 1.2.0
migration, `mcctl schema-migrate`).

**Your own drink and pack** (v1.2): `branding.drink.command` names the command that hands out the drink
(3-16 lowercase letters, default `drink`, not one of mc-brand's own: `rules`, `discord`, `hud`, `mcbrand`,
`modpack`, `view`). `/drink` always works; another name is registered next to it when the plugin starts (also
as `/mc-brand:<name>`; if another plugin already has the name, only the prefixed form and `/drink` work and the
server log says so). `branding.drink.texture: art/mug.png` replaces the product's neutral item art with a 16x16 PNG
(at most 8 KB); `branding.resource_pack.description` is the pack's description in the client's resource pack
list. `mcctl render` checks the PNGs' sizes; run `mcctl apply -C $C` after changing the pack's art.

**The drink** (`branding.drink.enabled: true`): every player who never had one gets it when they join
(v1.0.1: also players from before the drink existed; a full inventory just means next time). `/drink` (or its own
command) gives a new one if it's lost; one at a time. Drinking never empties it, gives 10 s of Nausea (cosmetic)
and has a 30 s cooldown; it has no real effect and can't be brewed. Each drink is tagged with its owner and a
serial kept in the player's own data: the command makes older copies stale, and stale copies, other players'
drinks and creative copies disappear from the owner's inventory (or when someone tries to drink them). Its
internal ids (the custom_model_data string `mcbrand:wine_glass`, the cooldown group, the pack's paths) are those
of v1.1, when the drink was one fixed item, so drinks already in inventories and chests keep working.

**The resource pack** (`branding.resource_pack.enabled: true`) gives the drink its texture and chat texts their
glyph. Players are asked on join (the prompt is `branding.resource_pack.prompt`) and may say no: without it the
drink is a plain potion in `branding.drink.color`. v1.2: `mcctl render` builds the pack (`mcctl/brandpack.py`)
from your config repo's PNGs, byte for byte (`branding.drink.texture`, `branding.resource_pack.glyph`,
`branding.icon` as its pack.png), or the product's neutral art (`plugin/mc-brand/art/`, the default icon) for
any you don't set; the same art always gives the same file and sha1, so players are only asked again after the
art or the description changes. Your watchdog Worker hosts it at `https://hb.<hostname>.<zone>/pack/<sha1>.zip`
(`mcctl render` stages it with the Worker's files; Terraform uploads them), so run `mcctl apply -C $C`
after turning it on or changing its art; until then the deploy warns and players see the potion.

**Icon and art**: `docs/images/brand-server-icon.png` and `docs/images/brand-drink.png` are enlarged previews of the
product's neutral art (`scripts/brand_art.py`).
To use your own icon, commit a 64x64 PNG to the config repo and set `branding.icon: art/icon.png`
(`mcctl render` checks the size). `branding.enabled: false` removes the plugin and the icon (the MOTD goes back to
`minecraft.motd`).

## Day-to-day

| Task | How |
|---|---|
| Change a setting | Edit the config repo's `server.yml`, push. Live in < 5 min with an announced restart if anything changed. Product behaviour (`server-config/*.j2`, roles) changes with a product release and a new pin |
| Status | `mcctl status -C $C` (`--json`): server state, players, TPS/MSPT, last backup, last deploy run, drift (disabled plugins, open `panel/*` PRs) |
| Deploy from the laptop | `mcctl deploy -C $C` (`--check` = dry run with diff). Runs the pinned product; same playbook and secrets as CI, read from `terraform output -json` |
| Health of the setup | `mcctl doctor -C $C`: tools, `.env`, tokens and their expiry (updates `expiry.yml`: commit it), domain expiry, DNS vs `host.*`, tailnet, watchdog freshness |
| Watchdog | `curl https://hb.<hostname>.<zone>/status`: last cron run, health/backup heartbeat states, IPv4/IPv6 probe results (no addresses) |
| Planned downtime | on the box: `sudo mc-run /etc/minecraft/notify.env /opt/minecraft/bin/notify.py maintenance MINUTES REASON` (0 ends it): self-heal and the watchdog stand down. Deploy restarts do this themselves |
| Console command | `mcctl console -C $C` (v1.3: a prompt, each line sent over RCON; `exit` or Ctrl-D leaves) or `mcctl console -C $C list` / `mcctl rcon -C $C list` (one command), or `mcctl ssh -C $C` then `mc list`, `mc whitelist add <name>`, `mc spark tps` |
| Restart (countdown) | `mcctl restart -C $C [--countdown 0\|10\|60\|300] [--backup] [--wait]` (v1.3): the panel's restart over Tailscale SSH: the in-game countdown (default 60 s; skipped when nobody is online unless `--wait`), an optional backup first, then the restart by the ops user, waiting until RCON answers. Ctrl-C during the countdown cancels it. Refused while admin-stopped (use `mcctl start`) or while another panel/mcctl action runs |
| Logs | `mcctl logs -C $C [unit] [-f] [-n N]` (default unit `minecraft`), or on the box `journalctl -u minecraft -f`, `-u mc-backup`, `-u mc-health` |
| Backup now | `mcctl backup now -C $C`, or `sudo systemctl start mc-backup` on the box (works with the server stopped too) |
| List snapshots | `mcctl backup list -C $C [--latest N]`, or `sudo mc-restic restic snapshots` |
| **Restore test** | `sudo mc-restic /opt/minecraft/bin/restore.py --verify-into /var/tmp/restore-test`: file count, a sample of checksums, and a usable world (level.dat + region files). Weekly on the server (v1.2: `mc-backup-check.timer`, Mondays 09:41 UTC, as `mcops`; `scripts/backup_check.py`): `restic check` on 5% of the data, room for the restore, then the same restore with `--max-age-hours` = two backup periods. Run it now: `sudo systemctl start mc-backup-check`, log: `sudo journalctl -u mc-backup-check`. The workflow's `backup-check` task (manual dispatch) runs the same checks from an Actions runner |
| Stop / start on purpose | `mcctl stop -C $C [--reason TEXT]` / `mcctl start`, or on the box `sudo /opt/minecraft/bin/admin.py stop "reason"` / `start` (`status` shows the marker). Stop writes `/var/lib/minecraft/admin-stopped` first, then stops gracefully: self-heal stands down, the watchdog doesn't alert on the probes (it learns the marker from the health heartbeat; a dead host still alerts), and deploys apply config but leave the server stopped (with a warning). Never `systemctl stop minecraft` alone: self-heal would start it again |
| Players | `mcctl players -C $C list \| add NAME \| remove NAME \| op NAME [--level N] \| deop NAME` (P12): edits `players.yml` in its canonical form (UUID from Mojang, or `--uuid`), validates, and (v1.3) opens a pull request, merges it when green (your merge) and follows the deploy (`--push`: commit and push to main; `--no-push`: only commit). The panel's whitelist button does the same through a PR |
| Kick, ban, pardon | `mcctl players -C $C kick\|ban\|pardon NAME [--reason TEXT]` (v1.3): live only, over RCON through the panel's moderation action (same checks, audited as `mcctl:<you>`); bans live in the server's `banned-players.json`, not in `players.yml` |
| Worlds (CLI) | `mcctl worlds -C $C list` (active world, archives, library, pending change); `create NAME [--type T] [--preset P] [--seed N] [--no-pregen] [--countdown S]`, `switch NAME [--countdown S]`, `delete NAME` (v1.3): the panel's world library over Tailscale SSH, with its typed confirmations (`create NAME`, `switch to NAME`, `delete NAME`; type it when asked, or `--confirm TEXT` in a script). Same rules as the [World library](#world-library): the active world is archived first, no newer-Minecraft archives, never delete the active world. The change goes to `server.yml` through mc-agent's pull request |
| Plugins (CLI) | `mcctl plugins -C $C list`; `enable\|disable NAME [--countdown S]` (v1.3): the panel's plugin switch (only plugins in the deployed lock; typed `enable NAME` / `disable NAME`), then a countdown restart |
| Restore (CLI) | `mcctl restore -C $C --list` (marks the last Minecraft upgrade's pre-upgrade snapshot); `--to /var/tmp/DIR [--snapshot ID]` (`/var/tmp` or `/tmp` only: restic runs sandboxed as mcops) restores into a scratch dir on the server and verifies it; `--live ID` runs the panel's restore (`mc-restore@ID`: maintenance window, **safety snapshot**, stop, fetch as mcops, apply as minecraft, start) after a typed `restore <first 8 hex>` (P12) |
| Rotate a secret | `mcctl secrets rotate -C $C rcon\|heartbeat\|agent-token\|command-hmac\|r2\|restic-key [--dry-run]` (P12): `apply -replace` (typed) then a deploy; restic-key: the deploy itself adds the new password as a key before writing it (whichever deploy comes first after the apply, CI's included; the password on stdin), then mcctl removes the old key, which the bucket lock refuses (`keys/` is locked forever): the old password then stays valid |
| Restore the live world | `sudo /opt/minecraft/bin/admin.py stop "restore"`, move the world folders aside, `sudo mc-restic /opt/minecraft/bin/restore.py --snapshot <id>`, then `sudo /opt/minecraft/bin/admin.py start`. `restore.py --snapshot` refuses unless the server is stopped **and** admin-stopped (or a maintenance window is open), and holds `/var/lib/minecraft/restore.lock` while it runs: self-heal never starts the server mid-restore and backups skip. It fetches the snapshot as `mcops` into `/var/lib/mc-restore/staging`, then `mc-restore-apply.service` copies it in as `minecraft` (needs free space for a second copy of the world). Top-level entries in the snapshot are replaced; paths backups exclude (plugin jars, `server.jar`, `logs/`, caches) are kept. `/opt/minecraft/server/.restored-from` then names the snapshot |
| Where did this world come from? | `cat /opt/minecraft/server/.restored-from`: `{"restored_at", "short_id", "snapshot_id", "snapshot_time", "source_path"}` (absent = generated on this box) |
| Reboot for updates | Automatic (P11): `mc-reboot-gate.timer` reboots inside `automation.reboot_window` (04:30-06:00 local) when `/var/run/reboot-required` exists, nobody is online, no backup/restore runs and the server isn't admin-stopped; Discord before and after, a watchdog maintenance window. **Never forced**: still pending after 3 days, Discord says so daily; then `sudo systemctl reboot` by hand when convenient. `sudo -u mcops /opt/minecraft/bin/reboot_gate.py --status` shows what it would do (or `journalctl -u mc-reboot-gate`) |
| Profile | `mc spark profiler start --timeout 600`, then record in [`docs/perf-baseline.md`](perf-baseline.md) |

## Security upkeep

**Tailscale SSH check.** Your SSH to the server is `check` (12 h): the first session after 12 h prints a
`login.tailscale.com` URL; open it and log in. `mcctl deploy` and `mcctl bootstrap` open such a session visibly
first; before other commands (`status`, `logs`, `migrate`) run `mcctl ssh -C $C` once if they seem to hang.

**Break-glass** (Tailscale broken): root's password is locked and its SSH key removed (security review M1), so
the console login is closed. netcup SCP → your server → Media → **Rescue system** (shows a one-time root
password) → reboot into it → VNC console: `lsblk`, `mount /dev/vda3 /mnt` (the root partition), `chroot /mnt`,
then `passwd root` (sets and unlocks a console password) and/or `systemctl unmask ssh.socket ssh.service`;
`exit`, turn the rescue system off, reboot. Fix Tailscale, deploy: the hardening role locks root and masks sshd again.

**Moving the Actions secrets into environments** (an existing config repo; security review M2, needs GitHub Pro
for a private repo). New config repos get it from `mcctl setup`. Order matters (Terraform would delete the
repository secrets before it creates the environment ones):

1. Re-create (or edit) the GitHub token in `.env` (`GITHUB_TOKEN`) with **Environments: Read and write** and **Actions: Read-only** added (Actions read is what lets Terraform read an environment back).
2. In the config repo set `github.environments: true` in `server.yml` and move the product pin to this release
   (`mcctl upgrade`), but don't push yet.
3. `mcctl apply -C $C`: mcctl sees the repository-level secrets and applies in two steps, each with
   its typed confirmation: (a) `--target github_actions_environment_secret.this`: environments `deploy` and
   `update`, their `main` branch policies and the secrets; (b) the rest: the repository-level secrets are
   destroyed (all but `PRODUCT_READ_TOKEN`), `tailscale_federated_identity.ci` gets the subject
   `...:environment:deploy`, and with `dns.mail_hardening` the null MX, SPF and DMARC records are created.
4. Push right after (b): the deploy job now runs in `deploy`. Between (b) and the push, the old workflow can't
   join the tailnet; a deploy that starts with a secret missing stops at "Check secrets are present" before it
   changes anything.
5. `mcctl doctor -C $C`: `ci oidc subject` and `github environments` are ok.

**Owner checklist** (once, then yearly):

- 2FA (a hardware key or passkey where offered) on GitHub, Cloudflare, Tailscale's identity provider, netcup,
  Discord and the password manager.
- Rotate every secret that ever appeared in a terminal scrollback, a chat or a screenshot (`mcctl secrets
  rotate`; Cloudflare/GitHub/Tailscale admin tokens in their dashboards).
- Delete leftover credentials: unused GitHub PATs (Settings → Developer settings), GitHub App private keys you
  downloaded (`.pem` files; the Apps keep only the one in `.env`), old Cloudflare and Tailscale API keys.
- Discord: the application's Bot → **Public Bot off** (only you can add it to a server).
- From outside (a phone hotspot, not your home network): `nmap -Pn -p- <server IPv4>` and `nmap -6 -Pn -p-
  <server IPv6>` show only 25565/tcp open (41641/udp is silent).
- Cloudflare: the Worker's `workers.dev` route is disabled (Workers → the Worker → Settings → Domains & Routes);
  the R2 buckets have no public access and no `r2.dev` URL.
- GitHub: the config repo is private and forking is off; product-setup's tag ruleset protects `v*`.

## Alerts

Every Discord alert uses one template (`scripts/alerts.py` on the box and in CI, `alertText` in the Worker), and
the update pull requests answer the same three questions as sections:

```
<emoji> **What happened, in player terms**
**Impact:** what it means for players right now (and whether anyone is online)
**Likely cause:** concrete, from the data we have (pre-generation running, the collector, the chunk, ...)
**What to do:** 1-3 concrete actions with exact commands or links, or "No action needed - informational."
**Details:** the raw numbers in one line
```

Severity: ℹ️ information, ⚠️ warning (players may notice, usually passes), 🔴 action needed, ✅ recovered (says
what recovered and how long it lasted). Recurring conditions (GC freezes, lag, memory, disk, CPU steal) alert
once when they start, then at most one reminder per `monitoring.alerts.repeat_hours` (default 6) while they last,
with the number of occurrences in between and whether it got worse, and once when they clear. The state is in
`/var/lib/mc-health/state.json`, so a restart doesn't repeat an alert. One-off conditions (down, crash loop,
watchdog, a failed backup or check) alert once and announce their recovery. No addresses and nothing secret
ever appear in an alert (a CI alert carries the run's link only). Every alert's exact text is snapshotted in
`tests/fixtures/alerts/` (`tests/test_alert_texts.py`; regenerate with `UPDATE_ALERT_SNAPSHOTS=1`).

| Alert | From | Means | What to do |
|---|---|---|---|
| 🔴 The Minecraft server is down / ✅ is back | health.py | RCON doesn't answer (with the server log's last error) / answers again | usually nothing (self-heal); `mcctl doctor`, `journalctl -u minecraft -n 100` |
| 🔴 The server is down and a restart didn't help / restarts stopped / self-heal couldn't restart it | health.py | self-heal restarted it and it is still down; gave up after 3 restarts in 24 h; the restart command failed | `journalctl -u minecraft -n 200`, fix, start from the panel |
| 🔴 The Minecraft server keeps crashing | health.py | a crash loop (systemd's start limit, or restarts between two checks) | `journalctl -u minecraft -n 200`; `sudo systemctl reset-failed minecraft`, then start |
| ⚠️ The Minecraft server had to be restarted again | health.py | more than 2 self-heal restarts in 24 h (they worked) | look for a crash reason in the journal |
| ⚠️ Short freezes: Java memory management paused the server / ✅ stopped | health.py | a G1 pause or ZGC allocation stalls longer than `monitoring.alerts.gc_pause_ms` (50 ms = one tick). ZGC's concurrent cycle times (`Major Collection ... 1.4s`) are never counted | during pre-generation usually nothing; else `jvm.gc zgc` (G1) or more heap (ZGC) |
| ⚠️ The server is lagging / ✅ runs at full speed again | health.py | MSPT above `monitoring.alerts.mspt` for `mspt_checks` checks | Experience tab (Busiest chunks), `spark profiler start --timeout 120` |
| ⚠️ The server's memory is nearly full / ✅ pressure is over | health.py | heap after GC above `heap_pct` for `heap_checks` checks | more `jvm.heap_mb` or a bigger box (`host.resize_hint`) |
| 🔴 The server's disk is filling up / ✅ fine again | health.py | disk at `disk_pct` | `du -sh /opt/minecraft/server/*`, clean logs, grow the disk |
| ⚠️ The hosting provider is short on CPU / ✅ the CPU is ours again | health.py | CPU steal above `steal_pct` for `steal_checks` checks | usually passes; the panel's CPU chart; dedicated cores if it lasts |
| ⚠️ The outside watchdog can't be reached / has stopped checking / ✅ again | health.py | heartbeats fail 3 checks in a row, or the Worker's cron is 15 min stale | cloudflarestatus.com, the Worker's cron trigger and logs |
| ⚠️ Security updates are waiting for a reboot / ℹ️ rebooting / ✅ back | reboot gate | pending 3+ days because the window never found the server empty (daily reminder), or it reboots now | `sudo systemctl reboot` on the box when convenient |
| ℹ️ The server restarts in N s for a config deploy | mc_rcon (deploy) | a deploy restart while players are online (they get an in-game countdown) | - |
| ℹ️ A config change waits for the maintenance window / ✅ the pending config change is live | restart_window (v1.3) | a deploy outside `maintenance.window` left the restart for the window; then the restart at the window's start | - (`mcctl deploy --force-restart` to restart now) |
| ⚠️ A deploy kept whitelisted players that aren't in players.yml | players role (deploy) | live players never in `players.yml` were kept, not removed ([Players not in Git](#players-not-in-git)); once per newly kept player | `mcctl players add NAME`, or remove them in the panel |
| ⚠️ Whitelist changes made in the panel were dropped | mc_panel (agent) | panel whitelist changes didn't reach Git within 24 h; the next deploy undoes them | re-do in the panel or edit `players.yml`; `mcctl doctor` |
| ⚠️ The panel can't open pull requests on the config repo / ✅ can again (v1.3) | mc_panel (agent) | the Panel → Git self-test failed twice in a row (the step and why) / passed again | `mcctl doctor`; the panel's GitHub App (installed, Contents + Pull requests); a stale Worker: `mcctl render` + `mcctl apply`; then the Health tab's self-test |
| ℹ️ A panel whitelist change was turned down | mc_panel (agent) | its pull request was closed unmerged; reverted live (or at the next deploy) | nothing if intended |
| 🔴 Players can't reach the server over IPv4/IPv6 / ✅ again | Worker | 2 failed status pings; says whether the host still reports in | DNS/firewall (`mcctl doctor`) or the host's console |
| 🔴 The backup failed / hasn't run / ⚠️ is taking much longer / ✅ works again | Worker | the backup heartbeat failed, is late (period + 30 min) or runs longer than 60 min | `mcctl backup now`, `journalctl -u mc-backup -n 100` |
| 🔴 The health check hasn't run / ✅ works again | Worker | health.py's heartbeat stopped (10 min late) | `systemctl status mc-health.timer`, `journalctl -u mc-health -n 50` |
| ⚠️ A crowded chunk is slowing the server | Worker | a loaded chunk has `monitoring.alerts.chunk_entities` (300) entities; again after 6 h if still crowded | spread the pen/farm, add floor space, collect items |
| 🔴 The outside watchdog itself is failing / ✅ runs cleanly / ⚠️ housekeeping failed | Worker | its own cron or a maintenance step threw (at most hourly) | usually passes; the Worker's logs (dash.cloudflare.com) |
| 🔴 The weekly backup test failed | `mc-backup-check` on the server (or a dispatched CI `backup-check`) | Monday's restic list/check/restore of the newest snapshot failed, there was no room for the test restore, or the snapshot is older than two backup periods | `sudo journalctl -u mc-backup-check` (or the run's link); `mcctl doctor`, `mcctl backup now`, re-run |
| 🔴 A deploy failed | CI `deploy` | a deploy step failed (mirror, tailnet, Ansible, smoke test) | the run's link; fix, push again or re-run |
| 🔴 The weekly expiry check found N problem(s) | CI `expiry-check` | a credential or the domain expires within 30 days, `expiry.yml` is stale, or the watchdog isn't checking | each problem's action; `mcctl doctor`, commit `expiry.yml` |
| Weekly lock update / Product update to vX.Y.Z (pull requests) | CI `update` | newer builds (same Minecraft version) or a newer product release | nothing with auto-merge; else review and merge, or `mcctl upgrade --to vX.Y.Z` |

Only these reach Discord. Everything else that fails (the `update` jobs, `validate`) shows as a failed Actions
run, which GitHub reports to the repository's admins by email.

## Monitoring

Two independent watchers, so a dead box is noticed even though it can't report itself:

- **On the box, `mc-health.timer`** (every 5 minutes, `scripts/health.py`): MSPT/TPS over RCON, GC pauses and
  post-GC heap from `logs/gc.log`, host stats. It heartbeats the watchdog's `health` check, alerts on Discord
  itself (the single alerting brain for what it sees), and **self-heals**:
  - server down (unit inactive/failed) → `systemctl restart minecraft.service` (a polkit rule lets the
    `mcops` user health.py runs as restart exactly that unit); process up but RCON dead for 2 checks (after a
    15-minute start-up grace) → a thread dump to the journal (`systemctl kill --signal=SIGQUIT`), then the
    restart (proven in e2e: a frozen JVM is dumped, restarted and answers again);
  - silent when the heal works; Discord when it doesn't (still down at the next check), when more than 2
    heals happen in 24 h, and when it gives up (no more than 3 restarts in 24 h);
  - never during a maintenance window (deploy restarts open one, see Planned downtime), while `restore.py`
    holds its lock, while the unit is starting or stopping (or systemd's state can't be read), or when
    `/var/lib/minecraft/admin-stopped` exists (Stop / start on purpose); every heal is logged as
    `AUDIT self-heal ...` (`journalctl -u mc-health`);
  - liveness is "RCON answers" (connect + a reply to the login): an `mspt`/`tps` reply it can't parse, or a
    refused RCON password, is a warning in the journal and never a reason to restart;
  - a crash loop (systemd's `Restart=on-failure` restarting the unit again and again, or its start limit
    hit) is reported on Discord instead of showing as "starting";
  - it watches the watchdog: if the Worker's cron hasn't run for 15 minutes (heartbeat replies carry
    `last_run_age_s`) or the Worker can't be reached for 3 checks, Discord is told directly.
- **Outside, the watchdog Worker** (`hb.<hostname>.<zone>`, Cloudflare, `watchdog/worker.js`, state in D1):
  - every 5 minutes a Minecraft status ping over IPv4 and IPv6 **separately** (3 tries, 20 s apart); after 2
    failed runs Discord says which family is down, and whether the health heartbeats still arrive (host up:
    network/firewall/DNS problem) or not (host down); the recovery is announced too;
  - heartbeats: `health` (every 5 min, alerts 10 min late) and `backup` (every
    `monitoring.alerts.backup_period_minutes`, default 60, alerts 30 min late; a backup failure alerts at once).
    Lateness counts from the last **finished** run (success), so a backup that starts every hour but never
    finishes still alerts; a run going longer than 60 minutes alerts on its own ("running too long");
  - an alert is only marked sent once Discord accepted it (otherwise the next run retries); every
    read-modify-write in D1 is a compare-and-set, so a heartbeat landing mid-cron is never lost; a cron run
    that throws is reported on Discord (at most hourly) and shows in `/status` (`cron_error`);
  - no alerts during maintenance windows, and no probe alerts while the server is admin-stopped;
    `GET /status` shows freshness only (no addresses, no messages), and `mcctl doctor` / `mcctl init` check
    that the cron ran in the last 15 minutes. Worker invocation logs are off (they would record request
    headers, i.e. the bearer token); only the Worker's own error lines are logged.
- **`mc-agent`** (P8, `panel.mode: cloudflare`, the default; `scripts/mc_agent.py`, runs as `mcops`,
  `MemoryMax=64M`): outbound HTTPS only, bearer `AGENT_TOKEN`.
  - every `monitoring.metrics_interval_s` (60 s) one sample (TPS, MSPT, players, heap and GC pauses from the
    GC log, JVM memory, CPU, steal, load, memory, disk, network) → `POST /ingest/metrics` → D1 day tables; samples
    that can't be delivered wait on disk (up to 24 h) and go out oldest first;
  - every 5 minutes the journal since the last cursor: the whole batch gzipped to R2 (`<name>-mc-logs`, deleted
    after `monitoring.logs.r2_days`), the `minecraft` and `mc-*` units' lines to D1 (at most
    `monitoring.logs.max_rows_per_day` a day, kept `monitoring.logs.d1_days`); the cursor only moves once both are
    stored, so the journal is the buffer;
  - holds a WebSocket to the Worker's `Control` Durable Object (hibernating) and falls back to polling every 15 s;
    commands are HMAC-signed by the Worker, checked for age, nonce, an allowlist (status, console, start, stop,
    restart, backup, tail; P10: cancel, whitelist, rejected_dismiss, moderate, plugin, restore) and the console
    allowlist (SECURITY.md), and audit-logged to the local journal (`MC_AUDIT=1`);
  - P9: per-core CPU, system memory, disk used/total, server uptime, TPS 5/15 min and the online players' names
    in every sample, and every 10 minutes the server's whitelist, ops, bans and plugin jars (`/ingest/inventory`);
    a signed `tail` streams `minecraft.service`'s journal over the WebSocket while someone watches the console;
  - the Worker rolls samples older than 7 days into 5-minute averages (kept `monitoring.retention_days`) and drops
    old day tables whole; with the agent's samples, its DOWN alert says host down (no sample for 3 minutes) or
    network/firewall (samples still arrive). `health.py` stays the only on-box alerter.
- **Discord (optional, `alerts.discord`):** state changes, GC pauses > 100 ms, restart announcements, and:
  - disk ≥ 85% on the server filesystem (once per crossing);
  - (P11: pending reboots are the reboot gate's, see [Automation](reference.md#automation-p11));
  - CPU steal > 10% for 3 checks in a row (shared vCores; from `/proc/stat` deltas);
  - the resize hint (post-GC heap > 85% for 30 min, unexplained MSPT > 40, or sustained steal): move to a
    netcup RS (dedicated cores) or larger server.

- **Discord slash commands (optional, P14, `discord.commands`; cloudflare mode):** `/status`, `/players`,
  `/lag [player]`, `/perf [range]`, `/backups`, `/whitelist list`, `/rejected` answer from the Worker's D1 (no
  server round trip), and `/backup now` and `/whitelist add <name>` go to the agent as signed commands like the
  panel's (the reply is edited with the result). Discord POSTs to `https://hb.<host>/discord/interactions`; the
  Worker verifies Discord's Ed25519 signature, allows only server.yml's guild, channel(s) and user(s), answers each
  interaction once and rate-limits (SECURITY.md). Nothing destructive is possible from Discord. `mcctl setup` asks
  for the application's bot token and the ids, registers the commands and sets the endpoint after the apply;
  `mcctl discord sync` repeats that.

- **The panel** (P9, `panel.mode: cloudflare`; `https://<panel.hostname>.<zone>`, Terraform output `panel_url`):
  the console mockup on live data, served by the same Worker (UI in `watchdog/panel/`, uploaded as Workers static
  assets) behind **Cloudflare Access** (one-time PIN by email, or GitHub with `panel.access.idp: github`; only
  `panel.access.emails`). The Worker checks the Access JWT itself on every request (RS256 against the team's
  certs, audience = the Access app, unexpired, email on the list) and answers 403 otherwise, so a missing or
  misconfigured Access app fails closed. Tabs: Overview (TPS/MSPT gauges, players, CPU per core + steal, memory
  + heap, disk, network, 2 h trends, health checks, recent actions), Console (stored log + live stream, commands
  through the agent's allowlist), Players, Bans, Plugins, Backups (restic's snapshot list), Logs (search the D1
  days by text/unit/level, list the R2 archives), Health (2 h to 90 d history, watchdog state), Audit log. Light
  and dark (follows the system; toggle in the header), works on a phone, fonts self-hosted, no third-party
  requests (CSP `default-src 'none'` + `'self'`). One-time setup: enrol the account in Zero Trust (Free) in the
  dashboard (`mcctl init` detects it and links there); the admin token has the two Access permissions.
- **Panel actions** (P10; every one opens a confirmation dialog first, goes to the Worker as `POST /api/action`,
  is audit-logged with the Access identity, signed for the agent, checked again by the agent and run through a
  fixed systemd unit as `mcops` or `minecraft`, never root):
  - **Restart / Stop / Start** (header): 10 s / 60 s / 5 min countdown with chat + action-bar warnings in game
    (skipped when nobody is online), optional backup first, shown in the header banner with *Cancel*. Stop writes
    the admin-stopped marker (self-heal, watchdog and deploys leave it down); Start removes it.
  - **Back up now**: `mc-backup.service` now; the Backups tab lists restic's snapshots (refreshed after every
    backup and restore: `backup.py --list-snapshots` → `/var/lib/mc-snapshots/snapshots.json`).
  - **Whitelist add/remove** (Players tab; names checked against Mojang first): applied live (`whitelist
    add|remove`, the server resolves the UUID), then recorded in `players.yml` through a pull request from a
    branch `panel/<ts>` of the config repo (batched for a minute) that the config repo's workflow merges by
    itself when only the whitelist changed (the required `server / gate` check + `mcctl panel-pr-check`). Until
    it is merged, deploys keep the change (the `players` role applies *Git + pending adds − pending removes*
    and keeps live players that were never in Git: [Players not in Git](#players-not-in-git));
    **closed without merging, it is reverted on the server** and announced on Discord; unmerged after 24 h it is
    dropped with a Discord message. The pull requests come from the panel's **GitHub App** (created by
    `mcctl init` through GitHub's App manifest flow: Contents + Pull requests write on the config repo only).
    Its private key lives only in the Worker, which mints 1 h installation tokens for the agent; there is no
    GitHub credential on the server (SECURITY.md "The panel's GitHub App").
  - **Rejected logins**: the last 50 "not whitelisted" disconnects (name, UUID, count, first/last seen, masked
    address) with one-click *Whitelist* or *Dismiss*.
  - **Kick / ban / pardon** (Players and Bans tabs), through the console allowlist's shapes.
  - **Plugin switch**: moves a locked plugin's jar to `plugins/disabled/` (or back) through
    `mc-plugins-apply.service` and restarts with a countdown; deploys keep it off and the panel shows a drift
    banner with *Re-enable* until it is switched on again or removed from `plugins.lock.yml`.
  - **Restore** (Backups tab): type `restore <id>` to confirm; a **safety snapshot** of the current world
    (tag `pre-restore`) is taken first, then stop → fetch → apply (as `minecraft`) → start through
    `mc-restore@<id>.service`; each step shows in the header banner.
  - **Stop, plugin switches and restore are confirmed in a separate window** on `<panel.hostname>-confirm.<zone>`:
    its own Cloudflare Access application with a session of `panel.access.reauth_minutes` (10) and the one-time
    PIN as the only login. The Worker accepts these three actions only there (that application's AUD, token at
    most `reauth_minutes` old) and never on the panel host. Access re-issues that token silently while the Zero
    Trust global session lasts, so `mcctl setup` keeps the global session short (15 min; up to 30 accepted): a
    confirmation then means a PIN at most global + `reauth_minutes` ago, while the panel itself keeps its 24 h
    login (SECURITY.md "Re-auth"; `mcctl doctor` and a Terraform check warn when it is longer).

The probes connect by name: `v4.<hostname>.<zone>` (A only) and `v6.<hostname>.<zone>` (AAAA only), DNS-only
records Terraform creates for that purpose, so each family is still checked on its own. Cloudflare's
`connect()` refused the IPv4 literal on the first real setup ("proxy request failed, cannot connect to the
specified address"). `TF_VAR_watchdog_probe_by_name=false` in the config repo's `.env` (then apply) probes the
IP literals instead (IPv6 bracketed, `[2001:db8::1]:25565`). If a probe by name fails with that error while
players can connect, check the records are DNS-only (grey cloud) and point at the server.

