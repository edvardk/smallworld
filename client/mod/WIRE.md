# The smallworld channels (client mod <-> server)

The Smallworld client mod (`client/mod/`, Fabric, client side only, no mixins) talks to the server over three
custom payload channels in the play phase (five since v1.5, mod 1.1.0). The byte layout lives in one class with no Minecraft or Bukkit types,
`src/main/java/io/github/edvardk/smallworld/client/Wire.java`, so the server plugin can reuse it on the raw
plugin-message bytes (Bukkit's Messenger hands the payload over as a `byte[]`). `tests/test_client_mod.py`
decodes the fixtures the Java side prints (`src/test/wire-fixtures.json`, written by
`scripts/build_client_mod.py build`) with an independent Python implementation.

## Channels

| Channel | Direction | When | Fields |
|---|---|---|---|
| `smallworld:hello` | client -> server | once per join, as soon as the server has registered the channel | VarInt `protocol`, UTF `mod_version`, UTF `tier`, UTF `pack`, VarInt `capabilities` (v1.5) |
| `smallworld:fps` | client -> server | every interval (default 5 s) | VarInt `fps` (clamped to 0..10 000) |
| `smallworld:config` | server -> client | any time | VarInt `protocol`, VarInt `interval_seconds`, VarInt `flags` |
| `smallworld:compass` (v1.5) | server -> client | when the compass is turned on or off for this player | VarInt `flags`, VarInt `max_range`, VarInt `rounding`, VarInt `height_threshold`, VarInt `text`, `accent`, `secondary`, `subtle` (RGB) |
| `smallworld:friends` (v1.5) | server -> client | while drawing: at once, then at most every 4 ticks while someone moves, only when changed | UTF `dimension`, VarInt `count` (0..100), then per friend: UTF `name`, UTF `dimension`, VarInt `x`, `y`, `z` |

- **VarInt**: Minecraft's: 7 bits per byte, least significant group first, high bit = more follows, at most 5
  bytes. **UTF**: a VarInt byte length (0..64), then that many UTF-8 bytes; writers cut longer strings at a
  character boundary.
- Decoders ignore trailing bytes: a newer peer may append fields; `protocol` (now 1) changes only on an
  incompatible change, and a client keeps its defaults when the server's `config` names another protocol.
- `flags` bit 0 (`1`): draw the FPS next to the top HUD bar. Other bits are reserved (ignored).
- `capabilities` (v1.5, appended to the hello; a 1.0.0 mod's hello ends before it and decodes as 0): bit 0 (`1`):
  the mod can draw the friend compass itself (`smallworld:compass` and `smallworld:friends`). Other bits are
  reserved. Mod 1.1.0 always sets bit 0.
- **Negative VarInts** (v1.5 coordinates): the same encoding as Java's `int`, two's complement, so a negative
  number takes 5 bytes.
- `interval_seconds` is clamped by the client to 2..60.
- The mod sends nothing to a server that has not registered the channel (Fabric's `canSend`, from the server's
  `minecraft:register` list): other servers learn nothing.

Worked example (the first fixture): a Medium-tier pack `26.3-0123456789ab` with mod 1.0.0 sends

```
01 05 312e302e30 06 6d656469756d 11 32362e332d303132333435363738396162
```

(`protocol` 1, then "1.0.0", "medium" and "26.3-0123456789ab", each with its length.) Mod 1.1.0 (v1.5) appends
`capabilities` 1, the client compass:

```
01 05 312e312e30 06 6d656469756d 11 32362e332d303132333435363738396162 01
```

## The client compass (v1.5)

mc-brand's friend compass is a boss bar the server redraws when the player turns (`plugin/mc-brand/.../Compass.java`):
it trails a turn by the player's ping and moves in whole GUI pixels. With mod 1.1.0 the mod draws it instead,
every frame, from the local yaw.

- **Who draws.** mc-brand decides per player (`Wayfinding.capability` and `Wayfinding.compassMode`, printed in its
  fixtures and selftest): first whether the player gets a compass at all (`branding.compass.enabled`, their
  `/compass on|off|auto`, else `default_on` and the client-mod precedence: off by default for the mod together
  with Xaero's maps, unchanged in v1.5), then who draws it. A hello with `capabilities` bit 0 from a client that
  also registered `smallworld:compass` and `smallworld:friends` = the mod (`CLIENT`); no hello, or a hello without
  the bit (mod 1.0.0) = the boss bar (`SERVER`), exactly as before. A client that registered a `smallworld:`
  channel but whose hello has not come yet waits (no compass) until the hello or the end of the decision window
  (2 s after the join), so a mod player never sees a boss bar flash up and vanish.
- **`smallworld:compass`** turns the mod's compass on (`flags` bit 0, `draw`) or off, with the server's
  `branding.compass` settings (`max_range` 16..100 000, `distance_rounding` 1..100, `height_threshold` 2..64:
  the mod clamps to the same limits) and the four branding colours mc-brand gives the strip (players: `text`,
  the faced player: `accent`, ticks: `secondary`, cardinal points and other dimensions: `subtle`; north is always
  red). mc-brand sends it on every change: `/compass on|off|auto`, a late channel registration, the precedence
  changing (Xaero's registering), and `draw` off when mc-brand is disabled. While the mod draws, mc-brand shows
  that player no boss-bar compass: never two compasses. A new connection starts with the mod's compass off.
- **`smallworld:friends`** carries exactly whom the boss bar would show this player (`Wayfinding.friends`): every
  other online player except spectators, invisible players, those who used `/compass hide` and those hidden
  from the viewer; players in the viewer's `dimension` only within `max_range` (horizontal), with their position
  in 1/16 block (`x`, `y`, `z`, the feet) and an empty `dimension`; players elsewhere with their dimension's key
  and `0, 0, 0` (no bearing across dimensions), nearest first, then elsewhere by name, at most 100. It goes out
  when the mod's compass is turned on, then at most every 4 ticks (5 a second) while anyone moves, joins, quits,
  hides or shows, and only when the bytes differ from the last ones sent. Nobody moving: nothing sent.
- **Drawing** (`CompassHud`, `CompassModel`): `CompassStrip.java`, the boss bar's own layout, carried verbatim
  by both sides (only the package line differs: `tests/test_social.py`), so the items, priorities, overlap rule,
  focus (`▼name 120m↑` within 8 degrees), distance rounding, arrows and `name (Nether)` list are the same; the
  mod draws each item at its unrounded position (a float translation of the GUI pose), so the strip glides. A
  friend whose player entity the client has (within entity tracking range) is placed at the entity's
  interpolated position every frame instead of the last message's; `max_range` is applied again from the local
  position. Just after a portal (the message is for another `dimension` than the local one) the players "here"
  in it show as elsewhere until the next message.
- **Where**: the HUD element is attached after vanilla's boss bars (Fabric's `HudElementRegistry`, no mixins); the
  strip's text goes in the next boss-bar row (y = 3 + 19 per bar drawn; the bar count by reflection on the
  overlay's private `events` map, the first row if that fails), which is where the boss-bar compass was: under
  mc-brand's HUD bar when that is shown, at the very top otherwise. The empty bar under the boss-bar compass's
  text is gone. Xaero's minimap sits in a screen corner and is unaffected, as with the boss bar. Hidden with F1.
- `tests/test_client_mod.py` decodes the mod's fixtures (its `compass` cases are mc-brand's own
  `client_compass` bytes, decoded and laid out by the mod) and checks the layouts equal the boss bar's.

Worked example (a fixture): mc-brand's defaults, drawing on, colours white, gold, grey and dark grey:

```
01 e807 05 08 ffffff07 80d4fe07 aad5aa05 f7eedd03
```

and the friends of a viewer in the overworld: Alex here at (3.5, 84, -119.5), Kim in the Nether:

```
13 6d696e6563726166743a6f766572776f726c64 02 04 416c6578 00 38 c00a 88f1ffff0f 03 4b696d 14 6d696e6563726166743a7468655f6e6574686572 00 00 00
```

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
  v1.5: the hello's `capabilities` decide who draws the compass (above).
  The answer is `smallworld:config` with server.yml `client.fps.interval_seconds` (v1.4.1: `flags` is always 0,
  below); Paper sends only on a channel the client registered, so it goes again on the client's
  `minecraft:register` if that came later.
- **mc-brand** (v1.4.1) also listens to `smallworld:fps`: the latest report is shown inside its HUD (the boss
  bar's line ends with "· N fps", the sidebar has a "Your FPS" line; the HUD's `fps` part), and the config's
  `flags` bit 0 is always cleared, for every player with the mod and whatever their `/hud` mode (`/hud off`
  included), so the mod's own element never appears: it sat on the boss bar's title (vanilla draws it at y = 3,
  the same row). `client.fps.hud` no longer reaches the mod: it is the server's default for the HUD's `fps` part
  (`false` takes `fps` out of `branding.hud.parts`; players can still `/hud show fps`). The mod's own FPS
  element and its `smallworld.json` switch stay for servers without mc-brand v1.4.1.
- **mc-telemetry** registers `smallworld:fps` itself (the reports are telemetry): the latest value per player is
  in each 10 s snapshot (`fps`, only for players with the mod), mc-agent's minute row has the average and the
  lowest (`fps`, `fps_min`), and the Worker's verdict says "Server fine, your PC: N FPS" (below 30 fair, below
  20 poor) instead of the generic F3 hint.
