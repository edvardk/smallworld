# Player guide

For the people who play on the server. Nothing here needs a terminal. (Server admins: the setup is in
[operations.md](operations.md#the-client-modpack-and-view).)

## Playing with normal Minecraft

Nothing to install: start Minecraft Java Edition (the same version as the server), choose **Multiplayer**,
**Add Server**, and type the address you were given. When the server offers its resource pack, say **Yes**: it's
small and only adds the server's own textures (the server's drink, for example).

Useful commands in game:

| Type | What it does |
|---|---|
| `/rules` | The house rules |
| `/hud` | The status bar at the top (`/hud side` for the sidebar, `/hud off` to hide it) |
| `/lag` | Why the game feels slow right now: the server, your connection or your computer |
| `/view 16` | See further (see below) |
| `/modpack` | Your personal link to the optional client (see below) |
| `/drink` | A new copy of the server's drink if you lost yours (only on servers that have one; it may have its own name too) |

Right after you join, the status bar says **Settling in…** for about ten seconds: your connection and the
world around you are still loading, so the numbers wouldn't mean much yet.

## The optional client (smoother, further)

The server has its own free client: Minecraft with a few well-known performance mods (Sodium, Lithium,
FerriteCore and friends), a minimap and world map (Xaero's), Chat Heads, and settings made for this server. Most computers get noticeably more frames per second,
and stronger computers can see much further. Your normal Minecraft keeps working; this is a separate profile.

1. In game, type **`/modpack`** and click **Open the client page**. (Or open the address it shows and type the
   6-digit code. A code works once, for 15 minutes, and only for you: don't share it.)
2. **Pick your computer.** Four choices, each with an example:
   - **Low**: older laptops and computers without a gaming graphics card.
   - **Medium**: a typical laptop or desktop. **Pick this if you're not sure.**
   - **High**: a gaming PC. Adds Distant Horizons (far-away terrain) and Iris (shaders, off until you choose
     one).
   - **Maximum**: a powerful graphics card. Far terrain and shaders on (Complementary Shaders).
3. **Download** your client for the official Minecraft Launcher (a zip file).
4. **Close the Minecraft Launcher** and unzip the file into your Minecraft folder: Windows `%appdata%\.minecraft`,
   macOS `~/Library/Application Support/minecraft`, Linux `~/.minecraft` (the Microsoft Store launcher uses the
   same folder).
5. In the launcher: **Installations**, **New installation**, **Version** `fabric-loader-…`, **Game directory**
   the `smallworld-<option>` folder from the zip, **More options**: change `-Xmx2G` in **JVM arguments** to the
   memory the page shows (Low 4 GB, Medium and High 6, Maximum 8), **Create**, **Play**.
6. A few mods may only be downloaded from their makers' page (Xaero's maps, the Maximum shader): the page (and
   `MISSING-MODS.txt` in the zip) links each one and says which folder it goes in.
7. In Minecraft choose **Multiplayer**: the server is already in your list.

Using the Modrinth App or Prism instead? The page also has a `.mrpack` file: double-click it, **Install**,
**Play**; it fetches every mod by itself. Give the profile the same memory (its Settings, Java and memory).

The settings (render distance, graphics, frame rate) are set once, when you install. After that they're yours:
change anything you like, nothing overwrites it.

Want another option, for example Low on a laptop and High on your desktop, or a lower one because the game
is slow? Download that one too and make a second installation for its folder: they live side by side.

Not interested? Type **`/modpack off`** and the reminder at join stops (`/modpack` still works any time).
You only get the reminder while you play with normal Minecraft.

### I'm stuck

- **No `fabric-loader` in the version list.** The zip wasn't unzipped into the Minecraft folder (its `versions`
  folder must land next to the launcher's own): close the launcher, unzip again.
- **Slow, stutters, "out of memory".** Give it the memory from step 5, or install a lower option (step 2).
- **Tearing** (a line across the picture): Video Settings, VSync ON. **Input lag**: VSync OFF with Max Framerate
  at your screen's refresh rate.
- **The camera suddenly moves slowly and smoothly**: that's the cinematic camera; the client leaves its key
  unbound (Options, Controls, Key Binds).
- **A laptop with two graphics cards**: Windows Settings, Display, Graphics: set the launcher's `javaw.exe` to
  High performance, and plug the laptop in.
- **The file didn't open in the Modrinth App.** Open the app, click **+** (Create), **Import from file**, and
  pick the downloaded `.mrpack` file.
- **The code doesn't work.** It's used up or older than 15 minutes: type `/modpack` again.
- **I'm on a phone or tablet.** The client is for computers: open the page on your computer.

## Seeing further: /view

The server sends everyone the same distance (its view distance, for example 12 chunks). You can ask for more
for yourself, up to **16** (the server admin may set another limit):
`/view 16` (or any number from 2 to 16; `/view reset` goes back to normal, `/view` alone shows your current
distance).

- More distance only works while the server runs **smoothly**. If it gets busy, your distance goes back to
  normal by itself for a while and comes back when things are smooth again; you get a message both times.
- Lower numbers always work right away (handy on a slow connection).
- Your own Minecraft setting must be at least as high: **Options > Video Settings > Render Distance**.
- The server remembers your choice for next time.
