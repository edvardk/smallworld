# The smallworld channels (client mod <-> server)

The Smallworld client mod (`client/mod/`, Fabric, client side only, no mixins) talks to the server over three
custom payload channels in the play phase. The byte layout lives in one class with no Minecraft or Bukkit types,
`src/main/java/io/github/edvardk/smallworld/client/Wire.java`, so the server plugin can reuse it on the raw
plugin-message bytes (Bukkit's Messenger hands the payload over as a `byte[]`). `tests/test_client_mod.py`
decodes the fixtures the Java side prints (`src/test/wire-fixtures.json`, written by
`scripts/build_client_mod.py build`) with an independent Python implementation.

## Channels

| Channel | Direction | When | Fields |
|---|---|---|---|
| `smallworld:hello` | client -> server | once per join, as soon as the server has registered the channel | VarInt `protocol`, UTF `mod_version`, UTF `tier`, UTF `pack` |
| `smallworld:fps` | client -> server | every interval (default 5 s) | VarInt `fps` (clamped to 0..10 000) |
| `smallworld:config` | server -> client | any time | VarInt `protocol`, VarInt `interval_seconds`, VarInt `flags` |

- **VarInt**: Minecraft's: 7 bits per byte, least significant group first, high bit = more follows, at most 5
  bytes. **UTF**: a VarInt byte length (0..64), then that many UTF-8 bytes; writers cut longer strings at a
  character boundary.
- Decoders ignore trailing bytes: a newer peer may append fields; `protocol` (now 1) changes only on an
  incompatible change, and a client keeps its defaults when the server's `config` names another protocol.
- `flags` bit 0 (`1`): draw the FPS next to the top HUD bar. Other bits are reserved (ignored).
- `interval_seconds` is clamped by the client to 2..60.
- The mod sends nothing to a server that has not registered the channel (Fabric's `canSend`, from the server's
  `minecraft:register` list): other servers learn nothing.

Worked example (the first fixture): a Medium-tier pack `26.3-0123456789ab` with mod 1.0.0 sends

```
01 05 312e302e30 06 6d656469756d 11 32362e332d303132333435363738396162
```

(`protocol` 1, then "1.0.0", "medium" and "26.3-0123456789ab", each with its length.)

## Local files

Both in the instance's `config/` directory, read once at start-up:

- `smallworld-pack.json`, owned by the pack (the `.mrpack` and the official-launcher zip write it; an update
  replaces it): `{"tier": "medium", "pack": "26.3-0123456789ab"}`. Missing or unreadable: tier `none`, pack
  empty (the mod was installed on its own).
- `smallworld.json`, owned by the player (created with the defaults if missing): `{"hud": true}`; `false` hides
  the FPS whatever the server says.

The JVM properties `-Dsmallworld.tier`, `-Dsmallworld.pack` and `-Dsmallworld.hud` override both. Only short
tokens (`[A-Za-z0-9._+-]{1,64}`) leave the machine; anything else becomes the fallback.

## Server side

- **mc-brand** (`plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand/Smallworld.java`, with a verbatim copy
  of `Wire.java`, `tests/test_social.py`) registers `smallworld:hello` (in) and `smallworld:config` (out). A
  hello is the exact "has the pack" test: the join prompt (`/modpack`) goes to every player without one (v1.1
  guessed from the client brand). A pack version other than the server's current one for that tier (rendered
  from the config repo's client lock into mc-brand's `client.packs`) gets one "your pack is out of date" line.
  The answer is `smallworld:config` with server.yml `client.fps` (`interval_seconds`, `hud`); Paper sends only on
  a channel the client registered, so it goes again on the client's `minecraft:register` if that came later.
- **mc-brand** (v1.4.1) also listens to `smallworld:fps`: the latest report is shown inside its HUD (the boss
  bar's line ends with "· N fps", the sidebar has a "Your FPS" line), and for a player whose HUD shows (/hud top
  or side) the config's `flags` bit 0 is cleared whatever `client.fps.hud` says, so the mod's own element doesn't
  sit on the boss bar's title (vanilla draws it at y = 3, the same row). `/hud` resends the config.
- **mc-telemetry** registers `smallworld:fps` itself (the reports are telemetry): the latest value per player is
  in each 10 s snapshot (`fps`, only for players with the mod), mc-agent's minute row has the average and the
  lowest (`fps`, `fps_min`), and the Worker's verdict says "Server fine, your PC: N FPS" (below 30 fair, below
  20 poor) instead of the generic F3 hint.
