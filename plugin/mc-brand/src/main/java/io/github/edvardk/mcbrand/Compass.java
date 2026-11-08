package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import net.kyori.adventure.bossbar.BossBar;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.TextComponent;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextDecoration;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.scheduler.BukkitTask;

/**
 * v1.3: the friend compass, a boss bar per player (the action bar stays free; Hud's top bar is another boss bar).
 * The line itself is {@link CompassStrip#strip}: cardinal points and every other online player's name at their
 * bearing, the one faced in the centre with ▼, the distance and ↑/↓; players in other dimensions after a bar as
 * "name (Nether)" without a distance (a bearing across dimensions means nothing).
 *
 * <p>Who sees it: {@code /compass on|off} (the player's PDC) always wins; otherwise the client-mod precedence
 * ({@link Wayfinding#compassByDefault}): off only for clients with the Smallworld mod <i>and</i> Xaero's (their
 * friends are on Xaero's maps), decided {@link #DECIDE_TICKS} after the join (the mod's hello and late channel
 * registrations come in the play phase) and again whenever a relevant channel registers.
 * {@code /compass hide} keeps the player off everyone else's compass and pocket map (PDC; spectators and invisible
 * players are never shown).
 *
 * <p>Cost: nothing per tick while nobody moves. A viewer who turns or moves (PlayerMoveEvent) is marked and
 * redrawn by a one-shot task scheduled on demand {@link #FAST_TICKS} tick later (v1.4.1: 1, was 4: the line
 * follows a turn at up to 20 updates a second instead of 5; several move events in one tick give one redraw);
 * other players' movement redraws every viewer at most every {@link #SLOW_TICKS} ticks, also on demand. With
 * nobody moving no task runs. A redraw is one strip layout per dirty viewer (a few dozen items, no IO); the boss
 * bar's name is sent only when the rendered line differs from the last one sent, so turning less than a pixel's
 * worth (0.75 degrees: {@link CompassStrip#WIDTH_PX} px for 2 x {@link CompassStrip#HALF_FOV} degrees) sends nothing.
 * The line is drawn by the server, so what a player sees still trails their turn by their ping plus up to a tick.
 *
 * <p>v1.5, the client compass: a player whose Smallworld mod announced the compass capability
 * ({@link Smallworld#capability}; {@link Wayfinding#compassMode}) gets no boss bar. mc-brand tells the mod to draw
 * ({@code smallworld:compass}: on/off, max_range, distance_rounding, height_threshold, the colours) and sends the
 * other players' positions ({@code smallworld:friends}: exactly whom the boss bar would show, {@link
 * Wayfinding#friends}), at most every {@link #FRIENDS_TICKS} ticks while someone moves and only when the bytes
 * changed; the mod draws {@link CompassStrip}'s layout every frame from the local yaw (no ping, no pixel steps).
 * Preferences ({@code /compass on|off|auto}, the precedence) decide whether there is a compass at all, exactly as
 * for the boss bar; the mod just follows the on/off it is sent. Players without the mod, or with mod 1.0.0, keep
 * the boss bar.
 */
final class Compass {
    static final long DECIDE_TICKS = 40L;
    static final long FAST_TICKS = 1L; // v1.4.1: the next tick (was 4: 5 updates a second felt laggy)
    static final long SLOW_TICKS = 20L;
    static final long FRIENDS_TICKS = 4L; // v1.5: the client compass's positions, at most 5 a second

    enum Pref { AUTO, ON, OFF }

    private final BrandPlugin plugin;
    private final Settings s;
    private final NamespacedKey prefKey;
    private final NamespacedKey hiddenKey;
    private final Map<UUID, BossBar> bars = new HashMap<>();
    private final Map<UUID, Component> lines = new HashMap<>();
    private final Set<UUID> dirty = new HashSet<>();
    private final Set<UUID> hidden = new HashSet<>(); // cache of the PDC flag for online players
    private final Set<UUID> drawn = new HashSet<>(); // v1.5: players whose mod draws the compass (we sent "draw")
    private final Map<UUID, byte[]> sentFriends = new HashMap<>(); // v1.5: the last positions sent to each
    private final Set<UUID> settled = new HashSet<>(); // v1.5: the decision window after the join has closed
    private BukkitTask fast;
    private BukkitTask slow;
    private BukkitTask friendsTask;
    private boolean othersMoved;
    long renders;
    long updates;
    long friendsSent; // v1.5

    Compass(BrandPlugin plugin, Settings settings) {
        this.plugin = plugin;
        this.s = settings;
        this.prefKey = new NamespacedKey(plugin, "compass");
        this.hiddenKey = new NamespacedKey(plugin, "compass_hidden");
    }

    // --- preferences -----------------------------------------------------------------------------------------------

    Pref pref(Player p) {
        String v = p.getPersistentDataContainer().get(prefKey, PersistentDataType.STRING);
        return "on".equals(v) ? Pref.ON : "off".equals(v) ? Pref.OFF : Pref.AUTO;
    }

    boolean hidden(Player p) {
        return hidden.contains(p.getUniqueId());
    }

    /** Whether this player's compass should show now (their choice, else the precedence). */
    boolean wants(Player p) {
        if (!s.compassEnabled()) {
            return false;
        }
        return switch (pref(p)) {
            case ON -> true;
            case OFF -> false;
            case AUTO -> s.compassDefaultOn() && Wayfinding.compassByDefault(plugin.hasOurMod(p), plugin.hasXaero(p));
        };
    }

    /** v1.5: who draws this player's compass now: nobody, wait (hello pending), the boss bar or their mod. */
    Wayfinding.CompassMode mode(Player p) {
        Wayfinding.Capability cap = plugin.clientCapability(p);
        return Wayfinding.compassMode(wants(p), cap == Wayfinding.Capability.YES,
                cap == Wayfinding.Capability.UNKNOWN && !settled.contains(p.getUniqueId()));
    }

    // --- lifecycle -------------------------------------------------------------------------------------------------

    void join(Player p) {
        if (p.getPersistentDataContainer().has(hiddenKey, PersistentDataType.BYTE)) {
            hidden.add(p.getUniqueId());
        }
        othersMoved();
        if (!s.compassEnabled()) {
            return;
        }
        Bukkit.getScheduler().runTaskLater(plugin, () -> {
            if (p.isOnline()) {
                settled.add(p.getUniqueId()); // v1.5: no hello by now = no client compass (the boss bar)
                apply(p);
            }
        }, DECIDE_TICKS);
    }

    /** The client's mods may have changed (a channel registered late, the mod's hello): decide again. */
    void clientsChanged(Player p) {
        if (s.compassEnabled() && p.isOnline()) {
            apply(p);
        }
    }

    private void apply(Player p) {
        Wayfinding.CompassMode mode = mode(p);
        UUID id = p.getUniqueId();
        boolean bar = bars.containsKey(id);
        if (mode == Wayfinding.CompassMode.SERVER && !bar) {
            BossBar b = BossBar.bossBar(Component.empty(), 0f, BossBar.Color.WHITE, BossBar.Overlay.PROGRESS);
            bars.put(id, b);
            render(p, b);
            p.showBossBar(b);
        } else if (mode != Wayfinding.CompassMode.SERVER && bar) {
            hideBar(p);
        }
        // v1.5: the mod draws it; turned on and off with smallworld:compass, never both compasses at once
        boolean client = drawn.contains(id);
        if (mode == Wayfinding.CompassMode.CLIENT && !client) {
            drawn.add(id);
            p.sendPluginMessage(plugin, Smallworld.COMPASS, state(true));
            sendFriends(p, true);
        } else if (mode != Wayfinding.CompassMode.CLIENT && client) {
            stopClient(p);
        }
    }

    private void hideBar(Player p) {
        BossBar bar = bars.remove(p.getUniqueId());
        lines.remove(p.getUniqueId());
        dirty.remove(p.getUniqueId());
        if (bar != null) {
            p.hideBossBar(bar);
        }
    }

    /** v1.5: tell the mod to stop drawing (its last positions are dropped with it). */
    private void stopClient(Player p) {
        drawn.remove(p.getUniqueId());
        sentFriends.remove(p.getUniqueId());
        if (p.isOnline()) {
            p.sendPluginMessage(plugin, Smallworld.COMPASS, state(false));
        }
    }

    /** v1.5: the smallworld:compass payload: on/off, this server's compass settings and colours. */
    byte[] state(boolean draw) {
        return Wire.encode(new Wire.Compass(draw ? Wire.COMPASS_DRAW : 0, s.compassMaxRange(), s.compassRounding(),
                s.compassHeight(), s.text().value(), s.accent().value(), s.secondary().value(), s.subtle().value()));
    }

    void quit(Player p) {
        hideBar(p);
        drawn.remove(p.getUniqueId());
        sentFriends.remove(p.getUniqueId());
        settled.remove(p.getUniqueId());
        hidden.remove(p.getUniqueId());
        othersMoved();
    }

    void hideAll() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            hideBar(p);
            if (drawn.contains(p.getUniqueId())) {
                stopClient(p); // a disabled or reloading mc-brand leaves no compass behind
            }
        }
        bars.clear();
        drawn.clear();
        sentFriends.clear();
        if (friendsTask != null) {
            friendsTask.cancel();
        }
        if (fast != null) {
            fast.cancel();
        }
        if (slow != null) {
            slow.cancel();
        }
    }

    // --- movement (main thread) ----------------------------------------------------------------------------------

    /** PlayerMoveEvent / teleport / world change: this player turned or moved. */
    void moved(Player p) {
        if (bars.isEmpty() && drawn.isEmpty()) {
            return;
        }
        if (bars.containsKey(p.getUniqueId())) {
            dirty.add(p.getUniqueId());
            if (fast == null) {
                fast = Bukkit.getScheduler().runTaskLater(plugin, this::flushFast, FAST_TICKS);
            }
        }
        if (drawn.contains(p.getUniqueId())) {
            friendsDue(); // v1.5: the mod turns by itself; moving changes who is in range, or the dimension
        }
        if (!hidden(p)) {
            othersMoved();
        }
    }

    private void othersMoved() {
        othersMoved = true;
        if (slow == null && !bars.isEmpty()) {
            slow = Bukkit.getScheduler().runTaskLater(plugin, this::flushSlow, SLOW_TICKS);
        }
        friendsDue();
    }

    /** v1.5: positions go out at most every FRIENDS_TICKS ticks, one-shot, only while someone moves. */
    private void friendsDue() {
        if (friendsTask == null && !drawn.isEmpty()) {
            friendsTask = Bukkit.getScheduler().runTaskLater(plugin, this::flushFriends, FRIENDS_TICKS);
        }
    }

    private void flushFriends() {
        friendsTask = null;
        for (UUID id : new ArrayList<>(drawn)) {
            Player v = Bukkit.getPlayer(id);
            if (v != null) {
                sendFriends(v, false);
            }
        }
    }

    /** v1.5: this viewer's positions, sent when they differ from the last ones sent (or {@code force}). */
    private void sendFriends(Player v, boolean force) {
        Location at = v.getLocation();
        byte[] b = Wire.encode(Wayfinding.friends(v.getWorld().getKey().toString(), at.getX(), at.getZ(),
                s.compassMaxRange(), visible(v)));
        if (force || !Arrays.equals(b, sentFriends.get(v.getUniqueId()))) {
            sentFriends.put(v.getUniqueId(), b);
            v.sendPluginMessage(plugin, Smallworld.FRIENDS, b);
            friendsSent++;
        }
    }

    private void flushFast() {
        fast = null;
        for (UUID id : new ArrayList<>(dirty)) {
            Player v = Bukkit.getPlayer(id);
            BossBar bar = bars.get(id);
            if (v != null && bar != null) {
                render(v, bar);
            }
        }
        dirty.clear();
    }

    private void flushSlow() {
        slow = null;
        if (!othersMoved) {
            return;
        }
        othersMoved = false;
        for (Map.Entry<UUID, BossBar> e : new ArrayList<>(bars.entrySet())) {
            Player v = Bukkit.getPlayer(e.getKey());
            if (v != null && !dirty.contains(e.getKey())) {
                render(v, e.getValue());
            }
        }
    }

    // --- the line --------------------------------------------------------------------------------------------------

    /**
     * The other players this viewer's compass may show, boss bar and client compass alike: not the viewer, not
     * {@code /compass hide}, not a spectator or invisible (XaeroMap.shown), not hidden from the viewer.
     */
    private List<Wayfinding.Seen> visible(Player v) {
        List<Wayfinding.Seen> out = new ArrayList<>();
        for (Player o : Bukkit.getOnlinePlayers()) {
            if (o == v || hidden(o) || !XaeroMap.shown(o) || !v.canSee(o)) {
                continue;
            }
            Location ol = o.getLocation();
            out.add(new Wayfinding.Seen(o.getName(), o.getWorld().getKey().toString(), ol.getX(), ol.getY(),
                    ol.getZ()));
        }
        return out;
    }

    private void render(Player v, BossBar bar) {
        renders++;
        Location at = v.getLocation();
        List<CompassStrip.Target> targets = Wayfinding.targets(v.getWorld().getKey().toString(), at.getX(),
                at.getY(), at.getZ(), visible(v));
        Component line = component(CompassStrip.strip(at.getYaw(), targets, config()));
        if (!line.equals(lines.get(v.getUniqueId()))) {
            lines.put(v.getUniqueId(), line);
            bar.name(line);
            updates++;
        }
    }

    CompassStrip.Config config() {
        return new CompassStrip.Config(s.compassMaxRange(), s.compassRounding(), s.compassHeight());
    }

    Component component(CompassStrip.Strip strip) {
        TextComponent.Builder b = Component.text();
        for (CompassStrip.Segment seg : strip.segments()) {
            b.append(switch (seg.kind()) {
                case GAP -> Component.text(seg.text());
                case BOLD_GAP -> Component.text(seg.text()).decoration(TextDecoration.BOLD, true);
                case TICK -> Component.text(seg.text(), s.secondary());
                case NORTH -> Component.text(seg.text(), NamedTextColor.RED);
                case CARDINAL -> Component.text(seg.text(), s.subtle());
                case PLAYER -> Component.text(seg.text(), s.text());
                case FOCUS -> Component.text(seg.text(), s.accent());
                case AWAY -> Component.text(seg.text(), s.subtle());
            });
        }
        return b.build();
    }

    // --- /compass --------------------------------------------------------------------------------------------------

    void command(Player p, String[] args) {
        if (!s.compassEnabled()) {
            p.sendMessage(Component.text("The compass is off on this server.", s.subtle()));
            return;
        }
        String sub = args.length > 0 ? args[0].toLowerCase(Locale.ROOT) : showing(p) ? "off" : "on";
        PersistentDataContainer pdc = p.getPersistentDataContainer();
        switch (sub) {
            case "on", "off" -> {
                pdc.set(prefKey, PersistentDataType.STRING, sub);
                apply(p);
                p.sendMessage(Component.text(sub.equals("on")
                        ? "Compass on: friends show at the top of the screen (/compass off hides it)."
                        : "Compass off (/compass on shows it again).", s.subtle()));
            }
            case "auto" -> {
                pdc.remove(prefKey);
                apply(p);
                p.sendMessage(Component.text("Compass: automatic (" + (wants(p) ? "on" : "off")
                        + " for your client" + (mode(p) == Wayfinding.CompassMode.CLIENT ? ", drawn by the Smallworld mod"
                        : "") + ").", s.subtle()));
            }
            case "hide" -> {
                pdc.set(hiddenKey, PersistentDataType.BYTE, (byte) 1);
                hidden.add(p.getUniqueId());
                othersMoved();
                p.sendMessage(Component.text("You no longer show on other players' compasses and maps "
                        + "(/compass show undoes it).", s.subtle()));
            }
            case "show" -> {
                pdc.remove(hiddenKey);
                hidden.remove(p.getUniqueId());
                othersMoved();
                p.sendMessage(Component.text("You show on other players' compasses and maps again.", s.subtle()));
            }
            default -> p.sendMessage(Component.text("Usage: /compass [on|off|hide|show|auto]", s.subtle())
                    .clickEvent(ClickEvent.suggestCommand("/compass ")));
        }
    }

    int showing() {
        return bars.size();
    }

    /** v1.5: players whose mod draws the compass. */
    int clientShowing() {
        return drawn.size();
    }

    /** Whether this player has a compass now, boss bar or client-drawn (the bare /compass toggles it). */
    boolean showing(Player p) {
        return bars.containsKey(p.getUniqueId()) || drawn.contains(p.getUniqueId());
    }

    /** For /mcbrand selftest: a fixed scene (tests/e2e/brand_check.py compares the line). */
    String selftest() {
        List<CompassStrip.Target> scene = List.of(
                new CompassStrip.Target("Alex", 3, 20, -120, null), // north, above
                new CompassStrip.Target("Sam", 200, 0, 0, null), // east
                new CompassStrip.Target("Kim", 0, 0, 0, "minecraft:the_nether"));
        CompassStrip.Strip st = CompassStrip.strip(180f, scene, new CompassStrip.Config(1000, 5, 8));
        return "compass [" + st.plain().strip().replaceAll(" {2,}", " ") + "] focus " + st.focus() + ", width "
                + CompassStrip.lineWidth(st.segments()) + ", default " + Wayfinding.compassByDefault(false, false) + "/"
                + Wayfinding.compassByDefault(false, true) + "/" + Wayfinding.compassByDefault(true, true)
                + ", showing " + bars.size() + " renders " + renders + " updates " + updates
                + "; client compass " + clientSelftest();
    }

    /**
     * v1.5, for /mcbrand selftest: the mode table (no compass wanted; a vanilla client; a mod client before its
     * hello; mod 1.0.0; a mod with the capability), the state and friends payloads for a fixed scene
     * (WayfindingFixtures prints the same; tests/e2e/brand_check.py compares) and the counters.
     */
    String clientSelftest() {
        String modes = Wayfinding.compassMode(false, true, false) + "/" + Wayfinding.compassMode(true, false, false)
                + "/" + Wayfinding.compassMode(true, false, true) + "/" + Wayfinding.compassMode(true, false, false)
                + "/" + Wayfinding.compassMode(true, true, false);
        java.util.HexFormat hex = java.util.HexFormat.of();
        byte[] st = Wire.encode(new Wire.Compass(Wire.COMPASS_DRAW, 1000, 5, 8, 0xFFFFFF, 0xFFAA00, 0xAAAAAA,
                0x777777));
        byte[] fr = Wire.encode(Wayfinding.friends("minecraft:overworld", 0.5, 0.5, 1000, Wayfinding.SELFTEST_SCENE));
        return "modes " + modes + ", state " + hex.formatHex(st) + ", friends " + hex.formatHex(fr) + ", drawn "
                + drawn.size() + " sent " + friendsSent;
    }
}
