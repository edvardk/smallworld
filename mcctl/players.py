"""`mcctl players list | add NAME | remove NAME | op NAME [--level N] | deop NAME` (design §4, P12).

players.yml is the canonical list (the `players` role reconciles the server with it on every deploy; the
panel's whitelist PRs edit the same file). This edits it in the canonical form (`schema.dump_players`, the
exact text `mcctl validate` requires), checks it with `schema.load_players`, validates the whole config repo
(restoring the old file if anything fails), then lands it (v1.3, `route`):

- `pr` (default): a `players/*` branch and a pull request (`publish.players_pr`): its checks, then the admin's
  merge when green (the gate merges whitelist-only changes by itself only from the panel App), then the
  deploy;
- `push` (`--push`): commit and push to main (the pre-v1.3 behaviour; the push deploys);
- `commit` (`--no-push`): only commit.

The deploy's `players` role applies it live (whitelist reload; a new op takes effect at the next start, P10
deviation).

UUIDs come from Mojang (`providers/mojang.py`, unauthenticated) unless `--uuid` is given; an op must be
whitelisted, so `op` adds the player to the whitelist too, and `remove` also removes them from the ops.
"""

from __future__ import annotations

import re

from mcctl import configrepo, init, publish, schema, yamlio
from mcctl.providers import mojang

ROUTES = ("pr", "push", "commit")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _load(ctx: init.Ctx) -> schema.Players:
    ctx.repo()  # start from a valid repo
    return schema.load_players(yamlio.load((ctx.root / configrepo.PLAYERS_YML).read_text()) or {})


def _resolve(ctx: init.Ctx, name: str, uuid: str | None) -> tuple[str, str]:
    if uuid:
        if not UUID.fullmatch(uuid.lower()):
            raise init.StepError(f"not a dashed UUID: {uuid!r}")
        return name, uuid.lower()
    found = mojang.uuid_for(name, ctx.deps.transport)
    if found is None:
        raise init.StepError(f"Mojang knows no player {name!r}", "check the spelling, or pass --uuid")
    return found


def _find(items: list, name: str) -> object | None:  # type: ignore[type-arg]
    for item in items:
        if item.name.lower() == name.lower():
            return item
    return None


def change(
    ctx: init.Ctx,
    action: str,
    name: str,
    *,
    uuid: str | None = None,
    level: int = 4,
    route: str = "pr",
    follow: bool = True,
    tools: publish.Tools | None = None,
) -> int:
    ui = ctx.ui
    if route not in ROUTES:
        raise init.StepError(f"route must be one of {', '.join(ROUTES)}")
    players = _load(ctx)
    whitelist, ops = list(players.whitelist), list(players.ops)
    if action == "list":
        if ui.rich:
            rows = []
            for p in whitelist:
                op = _find(ops, p.name)
                rows.append([p.name, p.uuid, f"level {op.level}" if op else ""])  # type: ignore[attr-defined]
            ui.table(["Player", "UUID", "Operator"], rows, title=f"Whitelist ({len(rows)})", indent=2)
            return 0
        for p in whitelist:
            op = _find(ops, p.name)
            ui.say(f"{p.name:<16} {p.uuid}" + (f"  op level {op.level}" if op else ""))  # type: ignore[attr-defined]
        return 0
    if action in ("add", "op"):
        known = _find(whitelist, name)
        if known is None:
            pname, puuid = _resolve(ctx, name, uuid)
            if any(p.uuid == puuid for p in whitelist):
                raise init.StepError(f"{puuid} is already whitelisted under another name")
            whitelist.append(schema.Player(name=pname, uuid=puuid))
            known = whitelist[-1]
        if action == "op":
            if not 1 <= level <= 4:
                raise init.StepError("--level must be 1-4")
            ops = [o for o in ops if o.uuid != known.uuid] + [schema.Op(known.name, known.uuid, level)]  # type: ignore[attr-defined]
    elif action == "remove":
        gone = _find(whitelist, name)
        if gone is None:
            ui.ok(f"{name} isn't whitelisted: nothing to do")
            return 0
        whitelist = [p for p in whitelist if p is not gone]
        ops = [o for o in ops if o.uuid != gone.uuid]  # type: ignore[attr-defined]
    elif action == "deop":
        ops = [o for o in ops if o.name.lower() != name.lower()]
    else:
        raise init.StepError(f"unknown action {action!r}")
    new = schema.Players(whitelist=whitelist, ops=ops)
    path = ctx.root / configrepo.PLAYERS_YML
    old_text, new_text = path.read_text(), schema.dump_players(new)
    if new_text == old_text:
        ui.ok(f"players.yml already says that about {name}: nothing to do")
        return 0
    schema.load_players(yamlio.load(new_text))
    path.write_text(new_text)
    try:
        ctx.repo()
    except init.StepError:
        path.write_text(old_text)
        raise
    title = f"mcctl players {action} {name}"
    if route == "pr":
        try:
            return publish.players_pr(ctx, title, f"{action}-{name}", follow=follow, tools=tools)
        except init.StepError:
            path.write_text(old_text)  # nothing landed: players.yml as it was
            raise
    push = route == "push"
    ctx.commit(title, [configrepo.PLAYERS_YML], push=push)
    ui.ok("pushed: the deploy applies it live" if push else "committed; `mcctl publish` lands it")
    return 0
