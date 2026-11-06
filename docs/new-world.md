# New world (Phase 8)

A new deployment starts with a brand-new world, so there is nothing to migrate. On a box with no world, the deploy's
`restore.py --if-missing` finds no snapshot (`NO_SNAPSHOT`) and the server generates a fresh world.

## 1. First start

Bootstrap and deploy ([setup.md](setup.md), or [by hand](reference.md#setup-by-hand)). The deploy sets the world borders from
`mc_world_border_radius` (Overworld 5000, Nether 1250, End 3000; per dimension, `execute in minecraft:<dim>`). Then check:

```bash
ssh deploy@mc-server
mc list                      # server answers over RCON
mc worldborder get           # Overworld border matches server.yml world.border_radius
```

Take a first backup once the world exists: `sudo systemctl start mc-backup`. Then set
`world.expected: true` in the config repo's `server.yml` and push: from then on a deploy onto
a box with no world fails if it finds no snapshot, instead of silently generating a new world.

## 2. Pre-generation (automatic, off-peak)

Nothing to type. With Chunky locked, `mc-health` (every 5 minutes) pre-generates each dimension that has a border,
one at a time: `world`, then `world_nether`, then `world_the_end` (Bukkit names from `level-name`), each up to
its world border (`chunky world W`, `chunky worldborder`, `chunky start`). It runs only while nobody is online:
when a player joins the next check pauses it (`chunky pause`), when the server is empty again it continues.
Chunky's `continue-on-restart: true` resumes a task after a restart. Watch it:

```bash
journalctl -u mc-health | grep pregen   # started / running / paused / continued / done, per world
mc chunky progress
```

Progress is in `/var/lib/mc-health/state.json` (`pregen_done`, `pregen_current`). To redo a world, remove it
from `pregen_done` there. Note the result in your config repo's README.

## 3. Whitelist players

```bash
mc whitelist add <name>
mc whitelist list
```

## 4. Performance baseline

Once players have built farms (stand near them, or have players there):

```bash
mc spark profiler start --timeout 600     # 10 minutes
journalctl -u minecraft -f | grep -i spark   # the report URL is printed to the console
mc spark health
```

Put the report link and numbers in [`perf-baseline.md`](perf-baseline.md).
