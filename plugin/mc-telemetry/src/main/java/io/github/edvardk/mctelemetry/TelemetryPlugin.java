package io.github.edvardk.mctelemetry;

import com.destroystokyo.paper.event.server.ServerTickEndEvent;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Predicate;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Bukkit;
import org.bukkit.Chunk;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.command.TabCompleter;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.block.Block;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Item;
import org.bukkit.entity.Player;
import org.bukkit.entity.Villager;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.messaging.PluginMessageListener;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.util.BoundingBox;

/**
 * mc-telemetry: the server side of the product's player-experience analytics (design §8 P13).
 *
 * <p>Tail-latency rule: telemetry must not show in the tick-time distribution. On the main thread the plugin
 * only (a) records each tick's duration into a preallocated histogram (ServerTickEndEvent: one array increment)
 * and (b) collects one window's snapshot a few cheap reads at a time, spread over ticks and cut off at a per-tick
 * budget (default 100 us, self-measured; what doesn't fit continues next tick). Serializing, file IO and parsing
 * run on one background thread ({@link Writer}). No network listener, no permissions beyond its commands, no IP
 * addresses anywhere. Verdicts and messages come from mc-agent (the Worker decides; this plugin only shows).
 */
public final class TelemetryPlugin extends JavaPlugin
        implements Listener, CommandExecutor, TabCompleter, PluginMessageListener {
    private static final String TAG = "[Lag check] ";

    private TickHistogram ticks;
    private TickHistogram self;
    private Writer writer;
    private Thread writerThread;
    private Set<UUID> optOut;
    private final Map<UUID, Long> joinedAt = new ConcurrentHashMap<>();
    static final String FPS_CHANNEL = "smallworld:fps"; // v1.2: the client mod's reports
    private final Map<UUID, Integer> clientFps = new ConcurrentHashMap<>();

    // settings (config.yml, written by Ansible; the plugin never writes it)
    private long intervalMs;
    private long budgetNs;
    private double spikeMs;
    private double nearRadius;
    private boolean collectPlayers;
    private boolean chunkScan;
    private boolean messages;

    // main-thread collection state (preallocated)
    private long seq;
    private long windowStart;
    private int lostWindows;
    private int budgetSkips;
    private Snapshot current; // being filled; null when idle
    private World[] worldBuf = new World[4];
    private int worldCount;
    private int worldIdx;
    private Player[] playerBuf = new Player[16];
    private int playerCount;
    private int playerIdx;
    private final Location loc = new Location(null, 0, 0, 0);
    private final BoundingBox box = new BoundingBox();
    private Chunk[] chunkBuf = new Chunk[0];
    private int chunkCount;
    private int chunkIdx;
    private int chunkWorld;
    private int scanWorldIdx;
    private int tileCount;
    private final Predicate<Block> tileCounter = b -> {
        tileCount++;
        return false; // count only: no BlockState is created for a tile entity that isn't collected
    };
    private int nearCount;
    private final Predicate<Entity> counter = e -> {
        nearCount++;
        return false; // count only: nothing is collected into the result
    };
    private static final long ERROR_LOG_MS = 600_000;
    private int collectErrors;
    private long lastErrorLog = Long.MIN_VALUE / 2;
    private final long[] spikeTs = new long[Snapshot.MAX_SPIKES];
    private final double[] spikeMs2 = new double[Snapshot.MAX_SPIKES];
    private int spikes;

    @Override
    public void onEnable() {
        File cfgFile = new File(getDataFolder(), "config.yml");
        YamlConfiguration cfg = YamlConfiguration.loadConfiguration(cfgFile);
        Path snapDir = Path.of(cfg.getString("snapshot-dir", "/run/mc-telemetry"));
        Path verdictDir = Path.of(cfg.getString("verdict-dir", "/run/mc-verdicts"));
        intervalMs = Math.max(2, Math.min(60, cfg.getInt("interval-seconds", 10))) * 1000L;
        budgetNs = Math.max(20, Math.min(2000, cfg.getInt("budget-micros", 100))) * 1000L;
        spikeMs = Math.max(50, cfg.getDouble("spike-ms", 100));
        nearRadius = Math.max(8, Math.min(128, cfg.getDouble("near-radius", 48)));
        collectPlayers = cfg.getBoolean("collect-players", true);
        chunkScan = cfg.getBoolean("chunk-scan", true);
        messages = cfg.getBoolean("player-messages", true);
        int keep = Math.max(6, Math.min(360, cfg.getInt("keep-snapshots", 60)));
        if (!Files.isDirectory(snapDir)) {
            getLogger().warning("snapshot-dir " + snapDir + " doesn't exist: telemetry is off (lag checks still work)");
        }
        ticks = new TickHistogram();
        self = new TickHistogram();
        optOut = Writer.loadOptOut(getDataFolder().toPath().resolve("tips-off.txt"), getLogger());
        writer = new Writer(getLogger(), snapDir, verdictDir, getDataFolder().toPath().resolve("tips-off.txt"), keep,
                ticks, self, optOut, this::deliverLater);
        if (Files.isDirectory(snapDir)) {
            writer.clean();
        }
        writerThread = new Thread(writer, "mc-telemetry-writer");
        writerThread.setDaemon(true);
        writerThread.setPriority(Thread.MIN_PRIORITY);
        writerThread.start();
        long now = System.currentTimeMillis();
        for (Player p : Bukkit.getOnlinePlayers()) {
            joinedAt.put(p.getUniqueId(), now); // after a reload: "joined" = now (no messages for a minute)
        }
        windowStart = now;
        getServer().getPluginManager().registerEvents(this, this);
        if (collectPlayers) {
            Bukkit.getMessenger().registerIncomingPluginChannel(this, FPS_CHANNEL, this);
        }
        PluginCommand lag = getCommand("lag");
        if (lag != null) {
            lag.setExecutor(this);
            lag.setTabCompleter(this);
        }
        getLogger().info("telemetry every " + intervalMs / 1000 + " s to " + snapDir + ", per-tick budget "
                + budgetNs / 1000 + " us");
    }

    @Override
    public void onDisable() {
        Bukkit.getMessenger().unregisterIncomingPluginChannel(this);
        clientFps.clear();
        if (writer != null) {
            writer.stop();
            try {
                writerThread.join(3000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    // --- main thread: per tick -------------------------------------------------------------------------------

    @EventHandler(priority = EventPriority.MONITOR)
    public void onTickEnd(ServerTickEndEvent event) {
        long t0 = System.nanoTime();
        double ms = event.getTickDuration();
        ticks.record(ms);
        if (ms >= spikeMs && spikes < spikeTs.length) {
            spikeTs[spikes] = System.currentTimeMillis();
            spikeMs2[spikes] = ms;
            spikes++;
        }
        if (current == null) {
            long now = System.currentTimeMillis();
            if (now - windowStart >= intervalMs) {
                startWindow(now);
            }
        }
        if (current != null) {
            step(t0);
        }
        long dt = System.nanoTime() - t0;
        self.record(dt / 10_000.0); // units of 10 us
    }

    private void startWindow(long now) {
        Snapshot s = writer.free.poll();
        if (s == null) {
            lostWindows++; // the writer is behind: skip this window, keep the histogram running
            windowStart = now;
            return;
        }
        try {
            fillWindow(s, now);
        } catch (RuntimeException e) {
            fail(s, e); // the snapshot goes back to the pool: the two instances never leak
        }
    }

    private void fillWindow(Snapshot s, long now) {
        s.seq = seq++;
        s.ts = now;
        s.windowMs = now - windowStart;
        windowStart = now;
        s.tickBank = ticks.swap();
        s.selfBank = self.swap();
        double[] tps = Bukkit.getTPS();
        s.tps = tps.length > 0 ? tps[0] : Double.NaN;
        Runtime rt = Runtime.getRuntime();
        s.heapMax = rt.maxMemory();
        s.heapUsed = rt.totalMemory() - rt.freeMemory();
        s.spikes = spikes;
        System.arraycopy(spikeTs, 0, s.spikeTs, 0, spikes);
        System.arraycopy(spikeMs2, 0, s.spikeMs, 0, spikes);
        spikes = 0;
        s.skippedWindows = lostWindows;
        lostWindows = 0;
        s.budgetSkips = budgetSkips;
        budgetSkips = 0;
        List<World> ws = Bukkit.getWorlds();
        worldCount = ws.size();
        if (worldBuf.length < worldCount) {
            worldBuf = new World[worldCount * 2];
        }
        for (int i = 0; i < worldCount; i++) {
            worldBuf[i] = ws.get(i);
        }
        s.ensureWorlds(worldCount);
        worldIdx = 0;
        scanWorldIdx = chunkScan ? 0 : worldCount;
        chunkCount = 0;
        chunkIdx = 0;
        playerCount = 0;
        if (collectPlayers) {
            Collection<? extends Player> online = Bukkit.getOnlinePlayers();
            if (playerBuf.length < online.size()) {
                playerBuf = new Player[online.size() * 2];
            }
            for (Player p : online) {
                if (playerCount < playerBuf.length) {
                    playerBuf[playerCount++] = p;
                }
            }
        }
        s.ensurePlayers(playerCount);
        playerIdx = 0;
        current = s;
    }

    /**
     * One slice of the collection: one world or one player at a time, until the budget for this tick is spent.
     * A getter that throws (a plugin or server bug, a world unloading mid-window) abandons this window only: the
     * snapshot goes back to the free pool and the next window starts clean (P13 review L3; before, the nulled
     * slot made every later tick fail). Errors are logged at most once per ERROR_LOG_MS, with a count.
     */
    private void step(long t0) {
        Snapshot s = current;
        try {
            while (true) {
                if (worldIdx < worldCount) {
                    World w = worldBuf[worldIdx];
                    worldBuf[worldIdx] = null;
                    worldIdx++; // first: a getter that throws below never leaves a nulled slot to read again
                    int i = s.worlds++;
                    s.worldName[i] = w.getName();
                    s.worldChunks[i] = w.getChunkCount();
                    s.worldEntities[i] = w.getEntityCount();
                    s.worldTiles[i] = w.getTileEntityCount();
                    s.worldTicking[i] = w.getTickableTileEntityCount();
                    s.worldPlayers[i] = w.getPlayerCount();
                } else if (chunkIdx < chunkCount) {
                    Chunk c = chunkBuf[chunkIdx];
                    chunkBuf[chunkIdx] = null;
                    chunkIdx++;
                    scanChunk(s, c);
                } else if (scanWorldIdx < s.worlds) {
                    // the next world's loaded chunks (one array per world and window), scanned one per step
                    int wi = scanWorldIdx++;
                    World w = Bukkit.getWorld(s.worldName[wi]);
                    Chunk[] cs = w == null ? null : w.getLoadedChunks();
                    if (cs != null) {
                        chunkBuf = cs;
                        chunkCount = cs.length;
                        chunkIdx = 0;
                        chunkWorld = wi;
                    }
                } else if (playerIdx < playerCount) {
                    Player p = playerBuf[playerIdx];
                    playerBuf[playerIdx] = null;
                    playerIdx++;
                    if (p.isOnline()) {
                        collectPlayer(s, p);
                    }
                } else {
                    current = null;
                    if (!writer.full.offer(s)) { // the writer is behind: drop this window, free its banks
                        abandon(s);
                    }
                    return;
                }
                if (System.nanoTime() - t0 >= budgetNs) {
                    budgetSkips++;
                    return; // the rest next tick
                }
            }
        } catch (RuntimeException e) {
            fail(s, e);
        }
    }

    /** A window that failed mid-collection: drop it (back to the pool), log at most once per ERROR_LOG_MS. */
    private void fail(Snapshot s, RuntimeException e) {
        current = null;
        java.util.Arrays.fill(worldBuf, null);
        java.util.Arrays.fill(playerBuf, null);
        chunkBuf = new Chunk[0];
        chunkCount = 0;
        chunkIdx = 0;
        scanWorldIdx = 0;
        worldCount = 0;
        worldIdx = 0;
        playerCount = 0;
        playerIdx = 0;
        abandon(s);
        collectErrors++;
        long now = System.currentTimeMillis();
        if (now - lastErrorLog >= ERROR_LOG_MS) {
            lastErrorLog = now;
            getLogger().warning("telemetry window dropped (" + collectErrors + " since the last report): " + e);
            collectErrors = 0;
        }
    }

    /** A window that won't reach the writer: free its histogram banks, back to the pool, counted as lost. */
    private void abandon(Snapshot s) {
        if (s.tickBank >= 0) {
            ticks.release();
        }
        if (s.selfBank >= 0) {
            self.release();
        }
        s.reset();
        writer.free.offer(s);
        lostWindows++;
    }

    /**
     * One loaded chunk: its entities (villagers and dropped items counted apart) and tile entities. Both reads
     * are of what the chunk already holds; a chunk that unloaded since the list was taken is skipped (Paper's
     * getTileEntities would load it again).
     */
    private void scanChunk(Snapshot s, Chunk c) {
        if (!c.isLoaded() || !c.isEntitiesLoaded()) {
            return;
        }
        Entity[] es = c.getEntities();
        int vil = 0;
        int itm = 0;
        for (Entity e : es) {
            if (e instanceof Villager) {
                vil++;
            } else if (e instanceof Item) {
                itm++;
            }
        }
        tileCount = 0;
        c.getTileEntities(tileCounter, false);
        s.chunksScanned++;
        s.byEntities.offer(es.length, chunkWorld, c.getX(), c.getZ(), es.length, tileCount, vil, itm);
        s.byTiles.offer(tileCount, chunkWorld, c.getX(), c.getZ(), es.length, tileCount, vil, itm);
    }

    /**
     * v1.2: {@code smallworld:fps} from the Smallworld client mod (client/mod/WIRE.md: one VarInt, 0..10 000;
     * trailing bytes ignored). mc-brand handles the mod's hello and config; the FPS reports are telemetry, so this
     * plugin listens to that channel itself (Bukkit allows several plugins on one channel). Main thread; one map
     * put per report (every 5 s per player by default).
     */
    @Override
    public void onPluginMessageReceived(String channel, Player player, byte[] message) {
        if (FPS_CHANNEL.equals(channel)) {
            int fps = decodeFps(message);
            if (fps >= 0) {
                clientFps.put(player.getUniqueId(), fps);
            }
        }
    }

    /** WIRE.md's VarInt (at most 5 bytes), clamped to 0..10 000; -1 for a malformed payload. */
    static int decodeFps(byte[] data) {
        int value = 0;
        for (int i = 0; i < 5 && i < data.length; i++) {
            int b = data[i] & 0xFF;
            value |= (b & 0x7F) << (7 * i);
            if ((b & 0x80) == 0) {
                return Math.max(0, Math.min(10_000, value));
            }
        }
        return -1;
    }

    private void collectPlayer(Snapshot s, Player p) {
        int i = s.players++;
        UUID id = p.getUniqueId();
        s.playerUuid[i] = id;
        s.playerName[i] = p.getName();
        s.playerBrand[i] = p.getClientBrandName();
        s.playerLocale[i] = p.locale();
        s.ping[i] = p.getPing();
        s.clientView[i] = p.getClientViewDistance();
        s.sendView[i] = p.getSendViewDistance();
        p.getLocation(loc);
        World w = loc.getWorld();
        s.playerWorld[i] = w == null ? "" : w.getName();
        s.chunkX[i] = loc.getBlockX() >> 4;
        s.chunkZ[i] = loc.getBlockZ() >> 4;
        s.gliding[i] = p.isGliding();
        s.tips[i] = !optOut.contains(id);
        Long j = joinedAt.get(id);
        s.joined[i] = j == null ? 0 : j;
        Integer fps = clientFps.get(id);
        s.fps[i] = fps == null ? -1 : fps;
        nearCount = 0;
        if (w != null) {
            double x = loc.getX();
            double y = loc.getY();
            double z = loc.getZ();
            box.resize(x - nearRadius, y - nearRadius, z - nearRadius, x + nearRadius, y + nearRadius, z + nearRadius);
            w.getNearbyEntities(box, counter);
        }
        s.nearEntities[i] = nearCount;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        joinedAt.put(event.getPlayer().getUniqueId(), System.currentTimeMillis());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        joinedAt.remove(event.getPlayer().getUniqueId());
        clientFps.remove(event.getPlayer().getUniqueId());
    }

    // --- messages queued by the agent (writer thread -> main thread) -----------------------------------------

    private void deliverLater(Verdicts.Message m) {
        if (!messages) {
            return;
        }
        Bukkit.getScheduler().runTask(this, () -> deliver(m));
    }

    private Component messageComponent(Verdicts.Message m) {
        Component c = Component.text(TAG, NamedTextColor.GOLD).append(Component.text(m.text(), NamedTextColor.YELLOW));
        Component hover = Component.text(m.hover().isEmpty() ? "Click for details" : m.hover())
                .append(Component.newline())
                .append(Component.text("/lag tips off stops these messages", NamedTextColor.GRAY));
        return c.hoverEvent(HoverEvent.showText(hover)).clickEvent(ClickEvent.runCommand("/lag"));
    }

    private void deliver(Verdicts.Message m) {
        long now = System.currentTimeMillis();
        if (m.to().equals("*")) {
            Component c = messageComponent(m);
            for (Player p : Bukkit.getOnlinePlayers()) {
                if (!optOut.contains(p.getUniqueId())) {
                    p.sendMessage(c);
                }
            }
            return;
        }
        UUID id;
        try {
            id = UUID.fromString(m.to());
        } catch (IllegalArgumentException e) {
            return;
        }
        Player p = Bukkit.getPlayer(id);
        Long joined = joinedAt.get(id);
        // The agent and the Worker check these too; the plugin is the last line (it alone knows the live state).
        if (p == null || optOut.contains(id) || (joined != null && now - joined < 60_000 && !m.id().startsWith("join-"))) {
            return;
        }
        p.sendMessage(messageComponent(m));
    }

    // --- /lag ---------------------------------------------------------------------------------------------

    private static NamedTextColor colour(String verdict) {
        return switch (verdict) {
            case "good" -> NamedTextColor.GREEN;
            case "fair" -> NamedTextColor.YELLOW;
            case "poor" -> NamedTextColor.RED;
            default -> NamedTextColor.GRAY;
        };
    }

    private static String label(String verdict) {
        return switch (verdict) {
            case "good" -> "Good";
            case "fair" -> "Fair";
            case "poor" -> "Poor";
            default -> "Unknown";
        };
    }

    private void showVerdict(CommandSender to, String who, Verdicts.Verdict v, long ts) {
        long age = Math.max(0, (System.currentTimeMillis() - ts) / 1000);
        to.sendMessage(Component.text(TAG, NamedTextColor.GOLD)
                .append(Component.text(who + ": ", NamedTextColor.WHITE))
                .append(Component.text(label(v.verdict()), colour(v.verdict())))
                .append(Component.text(v.headline().isEmpty() ? "" : " - " + v.headline(), NamedTextColor.WHITE))
                .append(Component.text(" (" + age + " s ago)", NamedTextColor.GRAY)));
        for (String r : v.reasons()) {
            to.sendMessage(Component.text("  - " + r, NamedTextColor.WHITE));
        }
        for (String t : v.tips()) {
            if (t.equals(FPS_TIP)) {
                continue; // the "Now:" line says it, from this moment's numbers
            }
            to.sendMessage(Component.text("  Tip: " + t, NamedTextColor.AQUA));
        }
    }

    // v1.0.1 plain language (decision): the numbers with their scale, a health word and a hover each. The thresholds and
    // the hover texts are mc-brand's (plugin/mc-brand Msg.java; tests/test_brand.py keeps the two equal).
    static final double SMOOTH_P95_MS = 35.0;
    static final double SMOOTH_TPS = 19.5;
    static final double BUDGET_MS = 50.0;
    static final String TPS_HOVER = "TPS (ticks per second): how many game updates the server manages each second. "
            + "20 is full speed; below 20 the world slows down.";
    static final String MSPT_HOVER = "Tick time (MSPT): milliseconds each update takes. Must stay under 50 ms for "
            + "full speed; lower is better.";
    static final String PING_HOVER = "Your connection (ping): the time for data to reach the server and back. "
            + "Lower is better; under 80 ms feels instant.";

    static final String FPS_TIP = "The server and your connection look fine, so stutter is likely your PC's frame rate. Press F3 to see your FPS (top-left). Lowering render distance or installing Sodium usually helps.";
    static final int PING_FINE_MS = 150;

    private static Component hovered(String text, NamedTextColor color, String hover) {
        return Component.text(text, color).hoverEvent(HoverEvent.showText(Component.text(hover)));
    }

    private Component now(Player p) {
        double[] tpsAll = Bukkit.getTPS();
        double tps = tpsAll.length > 0 ? Math.min(20.0, tpsAll[0]) : 0.0;
        double mspt = Bukkit.getAverageTickTime();
        long[] times = Bukkit.getServer().getTickTimes();
        double p95 = 0;
        if (times.length > 0) {
            long[] sorted = java.util.Arrays.copyOf(times, times.length);
            java.util.Arrays.sort(sorted);
            p95 = sorted[Math.min(sorted.length - 1, (int) Math.ceil(sorted.length * 0.95) - 1)] / 1e6;
        }
        String word = p95 < SMOOTH_P95_MS && tps >= SMOOTH_TPS ? "Smooth" : p95 < BUDGET_MS ? "A bit busy" : "Laggy";
        NamedTextColor wc = word.equals("Smooth") ? NamedTextColor.GREEN : word.equals("Laggy") ? NamedTextColor.RED : NamedTextColor.YELLOW;
        Component sep = Component.text(" · ", NamedTextColor.DARK_GRAY);
        var b = Component.text().append(Component.text("  Now: ", NamedTextColor.GRAY));
        Component elimination = null;
        if (p != null) {
            int ping = p.getPing();
            if (word.equals("Smooth") && ping < PING_FINE_MS) { // nothing on our side: say so, and what to look at
                elimination = Component.newline().append(Component.text("  " + FPS_TIP, NamedTextColor.AQUA)
                        .hoverEvent(HoverEvent.showText(Component.text("F3 shows your FPS at the top left (Fn+F3 on some laptops)."))));
            }
            b.append(Component.text("your connection ", NamedTextColor.GRAY))
                    .append(hovered(ping + " ms", ping < 80 ? NamedTextColor.GREEN : ping < 200 ? NamedTextColor.YELLOW : NamedTextColor.RED, PING_HOVER))
                    .append(sep);
        }
        b.append(Component.text("server ", NamedTextColor.GRAY)).append(Component.text(word, wc)).append(sep)
                .append(Component.text("speed ", NamedTextColor.GRAY))
                .append(hovered(String.format(java.util.Locale.ROOT, "%.1f/20 TPS", tps), tps >= 19 ? NamedTextColor.GREEN : tps >= 17 ? NamedTextColor.YELLOW : NamedTextColor.RED, TPS_HOVER))
                .append(sep)
                .append(Component.text("tick time ", NamedTextColor.GRAY))
                .append(hovered(String.format(java.util.Locale.ROOT, "%.1f of 50 ms", mspt), mspt < SMOOTH_P95_MS ? NamedTextColor.GREEN : mspt < BUDGET_MS ? NamedTextColor.YELLOW : NamedTextColor.RED, MSPT_HOVER));
        if (elimination != null) {
            b.append(elimination);
        }
        return b.build();
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        Verdicts v = writer.verdicts;
        boolean fresh = v.ts > 0 && System.currentTimeMillis() - v.ts < 5 * 60_000;
        if (args.length == 2 && args[0].equalsIgnoreCase("tips") && sender instanceof Player p) {
            boolean off = args[1].equalsIgnoreCase("off");
            if (!off && !args[1].equalsIgnoreCase("on")) {
                return false;
            }
            if (off) {
                optOut.add(p.getUniqueId());
            } else {
                optOut.remove(p.getUniqueId());
            }
            writer.optOutDirty = true;
            sender.sendMessage(Component.text(TAG + (off ? "Automatic lag tips are off. /lag still works anytime; /lag tips on turns them back on."
                    : "Automatic lag tips are on. /lag tips off turns them off."), NamedTextColor.GOLD));
            return true;
        }
        if (args.length > 1 || (args.length == 1 && args[0].equalsIgnoreCase("tips"))) {
            return false;
        }
        if (sender instanceof Player p && args.length == 0) {
            Verdicts.Verdict mine = fresh ? v.players.get(p.getUniqueId()) : null;
            if (mine != null) {
                showVerdict(sender, "Your connection", mine, v.ts);
            } else {
                sender.sendMessage(Component.text(TAG + "No analysis for you yet (it needs about a minute of play).", NamedTextColor.GOLD));
            }
            if (fresh && v.server != null) {
                showVerdict(sender, "Server", v.server, v.ts);
            }
            sender.sendMessage(now(p));
            sender.sendMessage(Component.text("  " + (optOut.contains(p.getUniqueId()) ? "/lag tips on - get automatic tips again" : "/lag tips off - no automatic tips"), NamedTextColor.GRAY));
            return true;
        }
        // console / RCON / ops: the whole server, or one player
        if (args.length == 1) {
            if (!sender.hasPermission("mctelemetry.lag.others")) {
                sender.sendMessage(Component.text(TAG + "You may only check yourself (/lag).", NamedTextColor.RED));
                return true;
            }
            Player target = Bukkit.getPlayerExact(args[0]);
            Verdicts.Verdict pv = target == null || !fresh ? null : v.players.get(target.getUniqueId());
            if (pv == null) {
                sender.sendMessage(Component.text(TAG + "No analysis for " + args[0] + " right now.", NamedTextColor.GOLD));
            } else {
                showVerdict(sender, target.getName(), pv, v.ts);
            }
            sender.sendMessage(now(target));
            return true;
        }
        if (!fresh || v.server == null) {
            sender.sendMessage(Component.text(TAG + "No analysis available right now (mc-agent or the Worker isn't reporting).", NamedTextColor.GOLD));
        } else {
            showVerdict(sender, "Server", v.server, v.ts);
            for (Player p : Bukkit.getOnlinePlayers()) {
                Verdicts.Verdict pv = v.players.get(p.getUniqueId());
                if (pv != null) {
                    sender.sendMessage(Component.text("  " + p.getName() + ": ", NamedTextColor.WHITE)
                            .append(Component.text(label(pv.verdict()), colour(pv.verdict())))
                            .append(Component.text(pv.headline().isEmpty() ? "" : " - " + pv.headline(), NamedTextColor.GRAY)));
                }
            }
        }
        sender.sendMessage(now(null));
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<>();
        if (args.length == 1) {
            if ("tips".startsWith(args[0].toLowerCase())) {
                out.add("tips");
            }
        } else if (args.length == 2 && args[0].equalsIgnoreCase("tips")) {
            for (String s : new String[] {"on", "off"}) {
                if (s.startsWith(args[1].toLowerCase())) {
                    out.add(s);
                }
            }
        }
        return out;
    }
}
