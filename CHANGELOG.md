# Changelog

All notable changes to the product. Versions are semver tags `vX.Y.Z` (design §1): a config repo pins a
release by its commit SHA. A breaking `server.yml` schema change needs a major version.
`scripts/release.py check vX.Y.Z` (run by `release.yml`) requires a `## [X.Y.Z] - YYYY-MM-DD` section.

## [Unreleased]

### Added

- **Pre-generation switch:** `server.yml` `world.pregenerate.enabled` (default `true`, today's behaviour) turns
  Chunky pre-generation off for the whole deployment; `mcctl render` maps it onto the monitoring role's existing
  `monitoring_pregen_enabled` (one path, health.env's `PREGEN_*`). A library world's `pregen` stays the per-world
  override. Turning it off cancels the task `mc-health` started at its next check.
- **Frontier expansion:** once the bordered passes are done, and for dimensions without a border, `mc-health`
  keeps pre-generating ahead of where players have been, still only while nobody is online: a square around 0,0 of
  the farthest generated chunk (from the region files' names and 4 KiB headers) + `world.pregenerate.margin`
  (default 1024 blocks), capped at the border and at `world.pregenerate.max_radius` (default 10000); a new Chunky
  task only when the target grew by 256 blocks, never while the disk is at its alert threshold.
  `world.pregenerate.frontier: false` turns it off alone.
- **Pre-generation progress:** `mcctl status` shows a `pregen` line (`world 63% · world_nether queued · ...`,
  `done`, `(paused)`, `(held: backup)`), from `/var/lib/mc-health/pregen.json` (health.py records each task's
  percentage from `chunky progress`), and Discord gets one note when every bordered pass is done.
- docs/operations.md "Pre-generation": what runs when, disk use (estimates), backup growth, the
  terrain-version trade-off, watching, stopping and skipping.

### Changed

- **Backups pause pre-generation:** `mc-backup` pauses a running Chunky task before `save-off` and continues it
  after `save-on` (only if it was running, also when the backup fails); a pre-generation hold
  (`/var/lib/mc-snapshots/pregen-hold.json`, expires after 4 h) keeps `mc-health` from continuing it mid-backup.

### Fixed

- A world switch now really cancels the old world's Chunky task: `chunky cancel` asks for `chunky confirm`
  first, which was never sent, so the new world's pass could continue the old world's saved task.
- A task Chunky resumed by itself after a restart (`continue-on-restart`) no longer stays marked as paused in
  health's state (alerts named pre-generation as the likely cause again).

## [1.3.1] - 2026-11-07

### Added

- **Update notice:** interactive mcctl commands (and the home screen's new "Update" row) say when a newer
  Smallworld **minor or major** release than the config repo's pin is out, with the fix (`mcctl upgrade`).
  Patch releases stay quiet (the weekly update pull request brings them). GitHub's releases/latest, asked
  anonymously with a 3 s timeout at most once a day (cached in `~/.cache/mcctl/update-notice.json`), silent on
  any failure, never in CI, without a terminal, with `--plain`/`NO_COLOR`, in a re-executed pinned mcctl or for
  the CI commands; `MCCTL_NO_UPDATE_NOTICE=1` turns it off (docs/reference.md "The update notice").
- **Maintenance window:** `server.yml` `maintenance: {window: "HH:MM-HH:MM", timezone: ...}` (optional; the time
  zone defaults to `host.timezone`; a window may cross midnight; unset = restarts any time, as before). A deploy
  that needs a Minecraft restart outside the window, with the server running, no longer restarts it: it applies
  the files (jar, plugins, unit, JRE), stages the merged config overlays in `/var/lib/minecraft/restart-pending/`
  and Discord says a restart is pending. `mc-restart-window.timer` (as `mcops`, sandboxed; the existing polkit
  rule) restarts it inside the window once the server is empty (RCON `list`; checked at the window's start and
  every 15 minutes), or, with players still online or RCON unreachable, in the window's last
  `maintenance.force_after_minutes_before_end` minutes (default 30, the timer fires then too; `0` = never forced,
  wait for the next window) with the 60-second in-game countdown; any earlier start (the panel, `mcctl restart`, self-heal, a
  reboot) applies the staged change through `minecraft.service`'s new `ExecStartPre` (`restart_window.py apply`,
  as `minecraft`), and a deploy inside the window does the pending restart by the same rule. Forced: `mcctl deploy
  --force-restart`, the reusable workflow's new optional input `force_restart`, or the config repo template's new
  dispatch input `force_restart`. A second deploy outside the window changes nothing. `mcctl publish`'s preview
  says "restart deferred to the maintenance window HH:MM-HH:MM <tz>" and then doesn't ask about players online
  (docs/operations.md "Maintenance window"). The first deploy with this release restarts Minecraft once (the
  unit changed).

## [1.3.0] - 2026-11-07

### Added

- `scripts/release.py bump X.Y.Z [--date YYYY-MM-DD]` sets the version in `pyproject.toml`, `mcctl/__init__.py`
  and README and dates the CHANGELOG's Unreleased section (refuses a version that isn't greater, an empty
  Unreleased section, a dirty tree); `scripts/release.py tag [vX.Y.Z] [--remote R] [--push]` creates the annotated
  release tag only on the fetched tip of `origin/main` with a clean tree, a passing `check` and a new tag name,
  and prints the push command (`--push` pushes after a typed confirmation). v1.2.1's tag had landed on a branch
  commit instead of main's merge commit (docs/reference.md "Releasing").
- Whitelist guard: a deploy no longer silently removes players who are on the live whitelist but were never in
  `players.yml` (console or Discord adds, panel adds whose pull request expired). They are kept, the deploy's
  output says `KEPT: <names>`, Discord gets one message per newly kept player (`mcctl players add NAME` to make
  them permanent) and the panel tags them *not in Git*. Removals still apply: the panel's pending removes, and
  players a commit took out of `players.yml` since the last deploy (remembered in
  `/opt/minecraft/server/whitelist-managed.json`; the first deploy with this release keeps everyone not in
  `players.yml`). docs/operations.md "Players not in Git".
- `server.yml` `minecraft.strict_whitelist` (default `false`): `true` makes `players.yml` the whole whitelist, as
  before this release (a deploy removes every live player it doesn't list, apart from pending panel adds).
- **The home screen:** `mcctl` with no command, on an interactive terminal, shows a status header (deployment,
  config repo, pinned product vs this mcctl, server online/players/TPS/MSPT, last deploy, open panel PRs; slow
  or failed parts read "unknown (why)" and never block) and an arrow-key menu grouped Server, Players, Worlds,
  Config, Publish, Health, Updates, Secrets, Metrics. Its items run the existing commands (status, logs,
  backup list, players list, validate, config get, doctor, update dry runs, secrets dashlane, metrics); the rest
  of v1.3's items are shown as coming. `--plain`, CI, `NO_COLOR` and no terminal keep the usage error (exit 2).
- **Server actions from the CLI** (v1.3 phase 2), over Tailscale SSH like `rcon`/`stop`/`start`:
  `mcctl restart [--countdown 0|10|60|300] [--backup] [--wait]` (the panel's restart: in-game countdown, skipped
  when nobody is online), `mcctl console [COMMAND...]` (an RCON prompt, or one command), `mcctl worlds
  list|create|switch|delete` (the world library, typed `create NAME` / `switch to NAME` / `delete NAME`), `mcctl
  plugins list|enable|disable` (the plugin switch and a countdown restart, typed `enable NAME` / `disable NAME`)
  and `mcctl players kick|ban|pardon NAME [--reason T]` (live only, never `players.yml`). They run the panel's own
  code on the server (`mc_agent.py cli`: the agent's checks and job runner as the ops user, through the existing
  polkit-allowed units) and write the panel's audit entry with the login `mcctl:<local user>`. Confirmations come
  from a terminal prompt or `--confirm TEXT`; without either the command refuses. Panel and CLI jobs never
  overlap: one job at a time across both (`/var/lib/minecraft/job.lock`).
- The home screen's Server, Players, Worlds and Config groups run these (console, restart, stop, start, back up
  now, restore, kick/ban/pardon, worlds and plugins), asking for names and countdowns and for the typed
  confirmation of destructive items.
- The docs write `mcctl ...`: `pip install --no-deps -e .` in the product venv installs the command (the
  `[project.scripts]` entry); `python3 -m mcctl` keeps working. Printed hints no longer say a bare `python`.
- **Remembered config repo:** `~/.config/mcctl/config.toml` (XDG_CONFIG_HOME; Windows `%APPDATA%\mcctl`)
  remembers the config repos used with `-C` (paths only, no secrets). Without `-C`, outside a config repo, mcctl
  uses the remembered one; the home screen's "Switch config repo" picks between them.
- Dependencies: questionary 2.1.1 (with prompt-toolkit 3.0.53 and wcwidth 0.9.4) in all three requirement sets
  and `pyproject.toml`.
- **Friend compass** (mc-brand, vanilla clients): a boss bar that turns with the player, with the cardinal points
  and every online player's name at their bearing. The player faced is centred with ▼, the distance (rounded) and
  ↑/↓ for large height differences. Players in other dimensions are listed after a bar as "name (Nether)", without
  a distance. It is redrawn only when someone turns or moves. `/compass on|off|hide|show|auto`, remembered per
  player (`hide` keeps you off other players' compasses and maps). server.yml `branding.compass`: `enabled`,
  `default_on`, `max_range`, `distance_rounding`, `height_threshold`.
- **Pocket map**: `/map` gives a personal map that re-centres on the player about once a second while held, with
  every other player as a named cursor (an edge dot when off the map). Terrain comes from vanilla's map rendering.
  In the off hand it's a minimap; `/map zoom` cycles the scale. One map per player: free to re-get, and it can't
  be dropped, copied, framed or stored. server.yml `branding.pocket_map.enabled`.
- **Client mods take precedence**: mc-brand reads each client's registered plugin channels (`smallworld:*`,
  Xaero's `xaerominimap:main` / `xaeroworldmap:main`) and checks again when one registers late. The Smallworld
  mod together with Xaero's turns the compass off by default; clients with no mods get the compass and a one-line
  `/map` tip at join. `/compass on|off` and `/map` always override.
- **`/help`** (alias `/commands`): a clickable list of the commands this player can use, built from the enabled
  features and permissions. Vanilla's list stays at `/minecraft:help`.
- **Help hologram**: `branding.holograms[].help: true` appends the same list, and `at: spawn` places a hologram
  above the world spawn instead of explicit `x`/`y`/`z`.
- **Versions everywhere:** each deploy writes `/opt/minecraft/product-version.json` (product version and commit),
  which mc-agent reports with the version its own process runs; `mcctl render` stamps the product version and
  the config repo's pin into the Worker's static assets (`version.json`, uploaded by `mcctl apply`, never served).
  The panel header reads "Smallworld vX.Y.Z · Purpur 26.3"; the Health tab's Versions row shows server, Worker and
  pin, with the fix for a mismatch ("run `mcctl render` + `mcctl apply`", "deploy", "restart mc-agent"). `mcctl
  doctor` compares them with the config repo's current pin (rows `versions`, read from the watchdog's D1), and
  `mcctl status` and the home screen show the deployed version against the pin. (v1.2.2 reached the server and
  the Worker in two separate steps and nothing showed it.)
- **Panel → Git self-test:** mc-agent proves daily (and 5 minutes after each start) that the panel can open pull
  requests, without opening one: the Worker mints a token, a fresh token cache accepts it, its permissions are
  Contents + Pull requests write, and GitHub answers for the config repo (repository, main and `players.yml`,
  pull requests). The result and each step are in the Health tab (with "Run the self-test now": new agent action
  `selftest`, at most once a minute) and in `mcctl doctor` (row `panel → git`); two failures in a row post one
  Discord alert, and recovery one more. Errors name the step and never contain the token. (v1.2.2's 390-character
  tokens were refused for days unnoticed.)
- **Mojang lookup from the server:** the panel's whitelist name check asks mc-agent (new action `lookup {name}`:
  api.mojang.com, then api.minecraftservices.com, from the box; 3 s timeout each, 20 per minute, cached 10
  minutes) and falls back to the Worker's own lookup when the agent is offline or doesn't answer within 4 s; "add
  unverified" stays the last resort. SECURITY.md "Panel actions" lists the two new read-only actions.
- `scripts/build_plugin.py` runs a plugin's fixture class (mc-brand: `WayfindingFixtures`, pure logic on the
  pinned JDK, never in the jar) and compares its output, which `tests/test_wayfinding.py` checks independently.
- **`mcctl publish`** (v1.3 Part B): previews a config change before it lands: the files that differ from
  `origin/main`, the `server.yml` settings and `players.yml` entries, and their consequences from rendering both
  sides (the Ansible roles and server config overlays it touches, whether Minecraft restarts, whether Terraform
  changes, who is online now), then validates and lands it. By default as a pull request (`publish/<timestamp>`):
  checks shown live, your merge offered when green, the deploy run followed, then the server's status.
  `--route main` pushes to main; `--route deploy` is the labelled emergency local deploy that skips CI.
  Terraform changes run `mcctl apply` (plan, typed confirmation) first; a restart with players online asks
  deploy now / wait until empty / cancel (`--wait-empty`; without a terminal it waits, and a timeout cancels).
  `--revert` opens a pull request reverting the last deploy's commits; `--dry-run` only previews. The home
  screen's Publish items run it.
- Home screen: Players "Whitelist a player", "Remove from the whitelist", "Make operator", "Remove operator".

### Changed

- `mcctl players add|remove|op|deop` opens a pull request (`players/*`), merges it when green (the admin's merge;
  `server / gate` still auto-merges whitelist-only changes only from the panel App) and follows the deploy,
  instead of pushing to main. `--push` keeps the old behaviour, `--no-push` still only commits.
- The product's e2e `agent` shard is split in two by its rounds (`agent-a`, `agent-b`: `tests/e2e/agent_check.py
  --rounds a|b`, each on its own box with its own mock Worker and GitHub API), so e2e's slowest shard drops from
  16 to about 11 minutes. Every round prints its wall time; without `--rounds` the script still runs every
  round (docs/ci.md).
- `POST /agent/github-token` also returns the token's granted `permissions` (from GitHub's reply), for the
  self-test.
- The e2e `agent-a` shard gains the round `versions` (version file, self-test against the GitHub mock, lookup
  against a mock Mojang; about 20 s).
- The e2e `agent-a` shard gains the round `cli` (about 40 s): `mc_agent.py cli` run with mcctl's own command line
  (`sudo mc-run ...`, as over SSH): a kick and a refused unconfirmed `world_delete`, both in the audit journal with
  `MC_VIA=mcctl`; then two processes in the CLI's place write pending.json while mc-agent's sync loop merges and
  opens pull requests on it (GitHub mock), and no entry is lost.
- Golden rendering (`tests/fixtures/full-render.json`): new group var `mc_strict_whitelist` (the example sets it
  explicitly to `false`); mc-brand's config gains `compass`, `pocket_map` and each hologram's `help` (the example
  adds a spawn help hologram and explicit compass values).

### Fixed

- Deploys restart mc-agent when its code changes. The server-side scripts were copied into place without restarting
  the one long-running process that imports them, so an upgrade's agent and panel fixes stayed inactive until the
  agent restarted for some other reason (v1.2.2's token fix was installed but the old code kept running). The
  script install now notifies a handler pair that restarts mc-agent only where its unit is installed (a fresh box,
  where handlers can flush before the agent role ran, and boxes without the panel skip it).
  `tests/test_playbooks.py` checks that every script mc-agent imports is covered.
- Panel whitelist and world changes could be lost when mcctl ran an action while mc-agent was running (new in
  this release, never in a tag): `mc_agent.py cli` (mcctl's actions over SSH) writes
  `/var/lib/mc-agent/pending.json` from its own process, and the in-process lock didn't cover it, so the agent's sync loop or a panel command could overwrite a change
  the other had just recorded (whose deploy and pull request then never knew of it). Every read-modify-write of
  pending.json now holds an flock on `pending.json.lock` (0600, next to it) and reads the file again under it;
  writes are atomic and stay 0600. The sync loop no longer holds the lock across GitHub, RCON or Discord calls: it
  works on a snapshot and merges its result back, keeping whatever was recorded meanwhile (a player recorded again
  while in an open pull request gets the next one).
- e2e (test only): the restore-guard round's stand-in restore no longer outlives its kill. Killing `runuser`
  left `flock` and its `sleep 300` running, still holding `restore.lock`, so the round's next deploy correctly
  waited ~285 s for a restore that the test had already "ended" (the round took 319 s). The holder now runs in its
  own session and its whole process group is killed; the round checks the lock is free and that the deploy takes
  < 60 s. The product's restore guard was right throughout (it waits only while the lock is really held).

## [1.2.2] - 2026-11-07

Panel whitelist fixes. A deploy sets the whitelist to `players.yml` plus the panel's pending adds; with both bugs,
panel and Discord adds never reached `players.yml`, so a deploy removed them. Add lasting players with
`mcctl players add` (or the panel, from this release on).

### Fixed

- Panel whitelist changes reach `players.yml` again: GitHub's installation tokens grew past 255 characters (390
  seen live) and `mc_panel`'s format check refused them ("the Worker returned no usable GitHub token"), so the
  pull request was never opened and pending adds expired after 24 h. The check now accepts up to 4096 characters
  of the same character set, and a refusal says why (the reply's keys, the length, unexpected characters; never
  the token).
- Panel whitelist form: Mojang answers the Worker's lookup with HTTP 403 (Cloudflare's shared egress), so no name
  could be added. The Worker falls back to `api.minecraftservices.com`'s name lookup; when neither answers, the form
  offers to add the name unverified (the server resolves it itself, as Discord's `/whitelist add` does).

### Changed

- The product's `e2e` CI runs as four parallel shards (core, agent, telemetry-brand, migrate-p16), each on its own
  freshly deployed box; the required check `e2e` aggregates them (docs/ci.md). Measured: a full run 16 minutes
  instead of 42.5.
- Dependencies: ansible-core 2.21.5 -> 2.21.6 (deploy), setuptools 80.9.0 -> 84.1.0 (build backend only).

## [1.2.1] - 2026-11-06

### Added

- **Anonymity safeguards on every commit** now that the product is developed in the open (docs/publishing.md
  "After publishing: safeguards on every commit"): `scripts/guard.py` runs publish_fresh's denylist and generic
  checks from git hooks (`.githooks/`: staged files and paths, `publish-exclude` paths refused, the commit
  message, a GitHub noreply author/committer identity, every pushed commit; gitleaks on the staged changes when
  installed; `python3 scripts/guard.py install`) and in the new required CI check `publication` (a pull request's
  commits, nightly and on releases the whole tree; the denylist from the product repo's Actions secret
  `PUBLISH_DENYLIST`). Its output never shows a pattern, the matched text or an email. The denylist is found
  through `--denylist`, `$SMALLWORLD_DENYLIST_TEXT`, `$SMALLWORLD_DENYLIST` or `git config smallworld.denylist`
  (publish_fresh.py too). `mcctl product-setup` turns on GitHub secret scanning and push protection on a public
  product repository.

### Changed

- `mcctl product-setup`: the product's `main` ruleset has no bypass actor and requires a pull request (no
  approvals), so the admin's changes pass the same checks as everyone's.
- LICENSE: copyright Edvard Kristiansen; the README names the maintainer.

### Fixed

- CI `gitleaks` on the public repository: `.gitleaksignore` names the known test fixtures by `file:rule:line`
  too, so they match in any commit (the v1.2.0 release run failed on them).

## [1.2.0] - 2026-11-06

Smallworld: the product is generic and anonymous (neutral defaults, MIT, renamed to edvardk/smallworld), every
client mod installs with the official Minecraft Launcher, friends show on the map, and the panel gains a world
library. Also the v1.1 rollout fixes.

### Changed

- **The product is called Smallworld** and lives at `edvardk/smallworld` (Python package `smallworld-mc`, release
  asset `smallworld-X.Y.Z`; the CLI stays `mcctl`). A config repo re-pins its `uses:` line to the new repository
  by hand (an admin merge: the gate rejects a repository change), then `mcctl render` and `mcctl apply` update the
  Tailscale CI identity's workflow rule before the next deploy (docs/publishing.md "After publishing").
- **Docs and tests describe a neutral deployment**: an "example deployment" in docs/reference.md, a neutral
  server provider (netcup as one example, no plans or prices), made-up names, domains and addresses in the
  fixtures. The original deployment's seed `server.yml` and notes moved to its config repo.
- **A config repo's Actions minutes near zero** (docs/ci.md "Keeping a config repo's minutes low"; no change
  needed in a config repo's workflow). A push to main or the weekly slot deploys only when something
  deploy-relevant changed since the last successful deploy (`scripts/deploy_record.py`, a record in the Actions
  cache; a manual `deploy` dispatch or a product pin change always deploys). The weekly backup test moved to the
  server: `mc-backup-check.timer` (Mondays 09:41 UTC, as `mcops`, sandboxed; `scripts/backup_check.py`, alerts on
  Discord); the Monday Actions run is `expiry-check` only and `backup-check` remains a manual dispatch task. pip's
  download cache in every job that installs from hashed requirements (none in the jobs that boot third-party
  jars). A typical week drops from ~53 to ~38 minutes, an idle one from ~22 to ~7.
- **Product CI is thorough** (the repo is public): e2e on every pull request and nightly, ci.yml nightly, and the
  controller on Linux, macOS and Windows x Python 3.12/3.13/3.14 (`-m "controller and not posix_only"` off Linux:
  `tests/conftest.py`). mcctl now imports on Windows (`fcntl` only where a build lock needs it), so its WSL2
  hint is shown instead of an ImportError.

### Added

- **World library** (the panel's new Worlds tab, panel only): create a world from a seed with a world type
  (default, large biomes, amplified, single biome, three superflat presets) and a server preset (survival SMP,
  creative build, hardcore, farm test, peaceful), switch to an archived world, delete one. Only the active world
  is on the server; the others are restic snapshots in the existing R2 repository (tags `world-archive`,
  `world:<name>`, `dv:<DataVersion>`), outside the backup retention. A switch warns the players (countdown),
  stops the server, archives the active world first (the safety snapshot), fetches the target, swaps the world
  folders as `minecraft` (`mc-world-apply.service`), starts and verifies (seed, RCON). A world saved by a newer
  Minecraft is refused; an older one is upgraded on load (the panel warns). Every action is confirmed on the
  confirmation host with a typed phrase, never from Discord or in-game. `server.yml` gets `world.active` and
  `world.library` (per-world seed, type, preset, border, pre-generation), written by the panel through a pull
  request that merges by itself when only those two keys change; deploys honour the live world and never switch.
  Inventories, ender chests and XP are per world; the whitelist, ops and bans are shared. The panel notes R2
  storage above 10 GB of archives.
- **Seed catalogue** for the world library: `worlds/seeds.yml` lists researched seeds for the pinned Minecraft
  version (name, one-line description, source, claims such as "a village within 300 blocks of spawn").
  `scripts/seed_check.py` boots the locked server jar per seed and measures every claim with `/locate` over a
  local RCON; the results (`watchdog/panel/seeds.json`) feed the New world dialog's "Start from" list with a
  "verified on <version>" badge and the measured distances. Picking one only fills the seed: no new panel action.
  ci.yml's `seeds` job re-verifies on catalogue, checker or server-lock changes; update-lock.yml regenerates it.

- **The Smallworld client mod** (`client/mod/`, Fabric, client side; design docs/design/client-v1.2.md): shows
  the player's FPS next to the top HUD bar and, only to a server that registers the `smallworld:*` channels,
  reports the pack's tier and version once per join and the FPS every few seconds (wire format
  `client/mod/WIRE.md`). Built reproducibly with javac against the pinned client jar and Fabric jars
  (`scripts/build_client_mod.py check`, CI job `plugin`).
- **The server side of the client mod** (`client/mod/WIRE.md` "Server side"): mc-brand knows exactly who has
  the pack (the mod's hello; the join prompt no longer guesses from the client brand, so modded clients without
  the pack are told too), tells players with an outdated pack (`client.packs`, rendered from the client lock),
  and sends the mod its settings (server.yml `client.fps`: report interval, HUD on/off). mc-telemetry records the
  reported FPS; the Experience tab and `/lag` now say "Server fine, your PC: N FPS" when the frame rate is the
  problem (below 30 fair, below 20 poor), with a private in-game note instead of the generic F3 hint.
- **Xaero's maps on the server** (server.yml `client.map`, both on by default with `client.enabled`): fair play
  for everyone with Xaero (no cave map, no entity radar) and friends on the map at any distance in the same
  dimension (Xaero's own tracked-player packets, sent by mc-brand; spectators and invisible players never show).
- **Vanilla social game rules**, for every player without mods: the Locator Bar on (`gameplay.locator_bar`,
  default true) and half the players asleep skip the night (`gameplay.sleeping_percentage`, default 50). Each
  deploy sets them over RCON in every dimension, read first and set only when different, together with
  `minecraft.gamerules`, which is applied now too and checked against the pinned jar's game rules
  (`server-config/keys/<version>/gamerules.txt`, written by `scripts/config_keys.py`; 26.x names are
  snake_case, e.g. `keep_inventory`).
- **Graves** (AxGraves 1.32.2, MIT, a default plugin next to Chunky): a player's items and XP wait in a grave
  where they died, 15 minutes, openable only by them (then the items drop as in vanilla); no update checks
  (`server-config/plugins/AxGraves.yml.j2`). A config repo with the default plugins needs `mcctl lock update`
  (or the update PR) after moving its pin to this release, so its plugins lock pins AxGraves too.
- **Spawn holograms** (server.yml `branding.holograms`, up to 10): floating texts any vanilla client sees, e.g.
  at spawn, with `{server}`, `{players}`, `{tps}` and `{glyph}` (a pack-only variant for players with the
  resource pack) and an optional live health line. mc-brand draws them as text display entities that are never
  saved with the world, sweeps its own tagged ones when chunks load and updates the text only when it changes.
- **Social mods in the client pack**, every tier: Xaero's Minimap and World Map (All Rights Reserved: downloaded
  from Modrinth, never mirrored; optional in the `.mrpack`), Chat Heads (MPL-2.0, now an allowed mirror licence),
  and the Smallworld mod (in-house, from the product's jar). Emotecraft and its library are listed but pending
  (only beta builds for 26.3). A pending entry's reason says when only betas exist.
- **Install with the official Minecraft Launcher** (recommended; design docs/design/client-v1.2.md): per tier a
  zip with Fabric's launcher profile and a `smallworld-<tier>` game directory (every mod we may share, the tier's
  settings, `MISSING-MODS.txt` with the official links of the rest), built and stored by `client-publish`
  (reused while unchanged) and served behind the same code and signed link as the `.mrpack`. The client lock pins
  the Fabric profile (`loader.profile`, sha256). The setup page puts this path first with the exact steps per OS,
  links the files we may not share, keeps the `.mrpack` for the Modrinth App or Prism, and adds troubleshooting.
- **A new look for the client setup page**: a four-step stepper (computer, tier, install, play) that shows only
  the chosen OS's steps, with copy buttons, the extras and the `.mrpack` path as collapsible sections, dark by
  default with a remembered light toggle, and a blocky, Minecraft-like style (bevelled buttons, pixel headings).
- **docs/features.md**: what Smallworld gives you, feature by feature, with screenshots from mock and test runs
  only (neutral fixtures). `scripts/screenshots.py` regenerates every image in `docs/images/` (the panel and
  the client setup page through `watchdog/test/ui.smoke.mjs` at scale 2, the branding previews, the mock
  setup screen); the panel's old "before" screenshots (an earlier panel version) are gone.
- Client pack feedback: per-tier memory (4/6/6/8 GB, with where to set it), the cinematic camera's key left
  unbound in every tier (`key_key.smoothCamera:key.keyboard.unknown`), frame-rate defaults reviewed.

### Fixed

- mcctl on Windows: `host.timezone` validation works there too (new pinned dependency `tzdata`, the time-zone
  database the OS doesn't ship; on Linux and macOS the system's is used as before).

- Turning the client on: `mcctl config set client.enabled true` writes and commits the first `client.lock.yml`
  when the config repo has none (it refused before, and `mcctl lock update` never created one while the client
  was off); `mcctl lock update --client` does the same as a pull request. It also says that `mcctl apply` is
  needed for the client page.
- `mcctl lock update` commits a refreshed `client.lock.yml` (it was updated on disk but left out of the PR).
- Secrets the product needs but a config repo's workflow doesn't pass (v1.1's `ARTIFACT_PUBLISH_TOKEN`):
  `mcctl doctor` reports them (a failure with the client on) and `--fix` adds the lines and commits them; the
  pin move and `mcctl upgrade` (the target release's `schema-migrate`) add them in the same change.
- `client-publish` fails the deploy when the client is on and `ARTIFACT_PUBLISH_TOKEN` isn't passed (it only
  warned, and the client packs silently went stale); without the client it stays a warning.

- `client-publish`: artifact uploads get a 120 s timeout (was the 20 s API timeout) and three attempts with backoff; one
  that still fails is a warning, not a failed deploy (the server and the launchers fall back to upstream).
- A bad pin comment (`# v1.1.0+fix`, `# 1.1.0`) in the config repo's workflow: `mcctl validate`, `doctor` and
  the `server / gate` check now say what's wrong and show a valid line (`uses: ...@<40-hex sha> # v1.1.0`, notes
  after a space: `# v1.1.0 (upload fix)`); the gate's errors name the workflow file.
- Turning the client on without `mcctl apply` (the Worker lacked its client bindings, so `/modpack` answered "the
  client page isn't set up on this server yet") is now visible: `mcctl doctor` reports `client page` as a
  failure with the fix (`mcctl apply -C <config>`), from a new non-secret `client` flag in the watchdog's
  `/status` (an older Worker shows a warning).
- mc-agent logs why the Worker refused a `/modpack` code request (one journal line per distinct cause, with the
  fix); before, the reason only reached the code file the plugin reads.
- `client-publish`: an artifact upload that the Worker refuses or fails reports the Worker's answer (status and
  reason, e.g. `the Worker answered 413 (...)`) instead of "The write operation timed out", and the upload timeout
  is an idle timeout (per 64 KiB block), no longer a cap on the whole upload (restic's 10 MB file timed out). The
  Worker drops the body of every refusal (401, 400, 411, 413, already stored), streams uploads to R2 with a
  fixed length and a sha256 check (a body that isn't the pinned file is never kept: 422), and answers a storage
  failure with a JSON 502.

### Security

Pre-publication audit (`plans/public-audit.md`):
- H1: `e2e-mock`'s optional self-hosted runner (`vars.E2E_RUNNER`) is used only for trusted code (schedule,
  `workflow_dispatch`, pull requests from the product repo itself); a fork's pull request always runs on
  GitHub's `ubuntu-24.04`. On a public repo also require approval for external contributors' workflow runs.
- H2, H3, M5, L7: tests, fixtures and comments use made-up values instead of real ones: a fake EUI-64 IPv6
  (fixture `tests/fixtures/ip/eui64_slaac.json`), `alex@mail.mcctl.internal`, a masked provider host name
  and `tests.mcctl.internal` (`.internal` is never delegated,
  and render refuses `.test` as a placeholder).
- M1: SECURITY.md asks for reports through GitHub's private vulnerability reporting (Security tab > Report a
  vulnerability). The docs treat a private product repo and `PRODUCT_READ_TOKEN` as optional.

### Changed

- Licensed under the MIT License (`LICENSE`; `pyproject.toml` `license = "MIT"`, PEP 639).
- `examples/full/`: a neutral full-featured example deployment ("Lighthouse": every optional feature on,
  branding with its own icon and a "Cocoa Mug" drink, the client, the Cloudflare panel, Discord, UTC). The tests
  use it as their reference deployment instead of a real one; a frozen rendering of it
  (`tests/fixtures/full-render.json`) catches unintended changes to what a deployment renders. Example IPv6
  addresses in prompts and docs use the retired `3ffe:ffff::/32` documentation prefix.
- Neutral product defaults: branding colours `#FFFFFF` / `#55AAFF` / `#2E6FB0` / `#C8D0D8` (text, accent,
  secondary, subtle), drink colour `#3A6EA5` and lore `["Never runs dry."]`, resource-pack prompt "Textures for
  this server's items (optional)", and `host.timezone` `UTC` (also `mcctl init`'s fallback and the template). A
  server.yml that relied on the old defaults changes look: set them explicitly to keep it.
- New `server.yml` fields: `branding.drink.command` (the drink's command, default `drink`),
  `branding.drink.texture` (own 16x16 item art), `branding.resource_pack.glyph` (own 8x8 glyph) and
  `branding.resource_pack.description` (the pack's description). The `{glyph}` placeholder replaces `{wine}` and
  the literal wine emoji in branding texts; the 1.2.0 migration (`mcctl upgrade` / `schema-migrate`) rewrites
  them. `mcctl render` checks the art's PNG sizes.
- Every alert in the plain-English template (what happened, what it means for players, what to do, with the
  exact command or link): the deploy-restart note, the panel's dropped or turned-down whitelist changes and the
  weekly expiry check now use it too, the update pull requests answer the same questions as sections, and the
  Worker's alerts are one builder each (`WORKER_ALERTS`; recoveries say what it means for players). New: a failed
  `deploy` or Monday `backup-check` job alerts on Discord with the run's link (`scripts/ci_alert.py`; the
  reusable workflow's `DISCORD_WEBHOOK_URL`, no change for config repos). Resize hints no longer read "a a host"
  with the default `host.resize_hint`. docs/operations.md "Alerts" lists every alert; `tests/test_alert_texts.py`
  snapshots each one's text.
- mc-brand: the v1.1 drink item is now the server's signature drink. Its command is `/drink` (permission
  `mcbrand.drink`; `/wine` and `mcbrand.wine` are gone), and `branding.drink.command` registers its own name next
  to it (e.g. `/wine` again, through the server's command map; also `/mc-brand:<name>`). The plugin reads
  `{glyph}` itself (render no longer translates it; `{wine}` and the wine emoji still work in a hand-written
  config). Messages name the drink (`branding.drink.name`), the plugin's built-in colours are the neutral
  defaults. Drinks already in inventories keep working: their ids (custom_model_data `mcbrand:wine_glass`, the
  cooldown group, the pack's paths) are frozen.
- The resource pack is built by `mcctl render` (`mcctl/brandpack.py`) from the config repo's own PNGs, byte for
  byte (`branding.drink.texture`, `branding.resource_pack.glyph`, `branding.icon` as pack.png; description
  `branding.resource_pack.description`), or the product's new neutral art (a blue flask:
  `plugin/mc-brand/art/`, the default server icon) for any not set. Rendered as
  `rendered/watchdog/pack/<sha1>.zip` with `mc_brand_pack_sha1` in the rendered group_vars; the product no
  longer commits or pins a pack (`watchdog/panel/pack/` and the `mc_brand_pack_sha1` constant are gone). The
  watchdog Worker's static assets are the directory `mcctl render` stages (the panel's files plus the pack;
  Terraform variable `watchdog_assets_dir`). Same art = same file and sha1: players aren't asked again.
- The product's default server icon and pack art are neutral (`mc_brand_icon_sha256` changes). A server that
  used the v1.1 wine-glass art keeps it by committing those PNGs to its config repo (`branding.icon`,
  `branding.drink.texture`, `branding.resource_pack.glyph`).

### Upgrading

- Run `mcctl apply` once after upgrading (the Worker's assets now come from the rendered directory).

Releases before 1.2.0 were developed in a private repository, kept as an archive; their history stays there.
