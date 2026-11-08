package io.github.edvardk.mcbrand;

import java.util.ArrayList;
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
 * The line itself is {@link Wayfinding#strip}: cardinal points and every other online player's name at their
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
 * worth (0.75 degrees: {@link Wayfinding#WIDTH_PX} px for 2 x {@link Wayfinding#HALF_FOV} degrees) sends nothing.
 * The line is drawn by the server, so what a player sees still trails their turn by their ping plus up to a tick.
 */
final class Compass {
    static final long DECIDE_TICKS = 40L;
    static final long FAST_TICKS = 1L; // v1.4.1: the next tick (was 4: 5 updates a second felt laggy)
    static final long SLOW_TICKS = 20L;

    enum Pref { AUTO, ON, OFF }

    private final BrandPlugin plugin;
    private final Settings s;
    private final NamespacedKey prefKey;
    private final NamespacedKey hiddenKey;
    private final Map<UUID, BossBar> bars = new HashMap<>();
    private final Map<UUID, Component> lines = new HashMap<>();
    private final Set<UUID> dirty = new HashSet<>();
    private final Set<UUID> hidden = new HashSet<>(); // cache of the PDC flag for online players
    private BukkitTask fast;
    private BukkitTask slow;
    private boolean othersMoved;
    long renders;
    long updates;

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
        boolean want = wants(p);
        boolean showing = bars.containsKey(p.getUniqueId());
        if (want && !showing) {
            BossBar bar = BossBar.bossBar(Component.empty(), 0f, BossBar.Color.WHITE, BossBar.Overlay.PROGRESS);
            bars.put(p.getUniqueId(), bar);
            render(p, bar);
            p.showBossBar(bar);
        } else if (!want && showing) {
            hide(p);
        }
    }

    private void hide(Player p) {
        BossBar bar = bars.remove(p.getUniqueId());
        lines.remove(p.getUniqueId());
        dirty.remove(p.getUniqueId());
        if (bar != null) {
            p.hideBossBar(bar);
        }
    }

    void quit(Player p) {
        hide(p);
        hidden.remove(p.getUniqueId());
        othersMoved();
    }

    void hideAll() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            hide(p);
        }
        bars.clear();
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
        if (bars.isEmpty()) {
            return;
        }
        if (bars.containsKey(p.getUniqueId())) {
            dirty.add(p.getUniqueId());
            if (fast == null) {
                fast = Bukkit.getScheduler().runTaskLater(plugin, this::flushFast, FAST_TICKS);
            }
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

    private void render(Player v, BossBar bar) {
        renders++;
        Location at = v.getLocation();
        String world = v.getWorld().getKey().toString();
        List<Wayfinding.Target> targets = new ArrayList<>();
        for (Player o : Bukkit.getOnlinePlayers()) {
            if (o == v || hidden(o) || !XaeroMap.shown(o) || !v.canSee(o)) {
                continue;
            }
            String ow = o.getWorld().getKey().toString();
            Location ol = o.getLocation();
            targets.add(ow.equals(world)
                    ? new Wayfinding.Target(o.getName(), ol.getX() - at.getX(), ol.getY() - at.getY(),
                            ol.getZ() - at.getZ(), null)
                    : new Wayfinding.Target(o.getName(), 0, 0, 0, ow));
        }
        Component line = component(Wayfinding.strip(at.getYaw(), targets, config()));
        if (!line.equals(lines.get(v.getUniqueId()))) {
            lines.put(v.getUniqueId(), line);
            bar.name(line);
            updates++;
        }
    }

    Wayfinding.Config config() {
        return new Wayfinding.Config(s.compassMaxRange(), s.compassRounding(), s.compassHeight());
    }

    Component component(Wayfinding.Strip strip) {
        TextComponent.Builder b = Component.text();
        for (Wayfinding.Segment seg : strip.segments()) {
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
        String sub = args.length > 0 ? args[0].toLowerCase(Locale.ROOT)
                : bars.containsKey(p.getUniqueId()) ? "off" : "on";
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
                        + " for your client).", s.subtle()));
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

    /** For /mcbrand selftest: a fixed scene (tests/e2e/brand_check.py compares the line). */
    String selftest() {
        List<Wayfinding.Target> scene = List.of(
                new Wayfinding.Target("Alex", 3, 20, -120, null), // north, above
                new Wayfinding.Target("Sam", 200, 0, 0, null), // east
                new Wayfinding.Target("Kim", 0, 0, 0, "minecraft:the_nether"));
        Wayfinding.Strip st = Wayfinding.strip(180f, scene, new Wayfinding.Config(1000, 5, 8));
        return "compass [" + st.plain().strip().replaceAll(" {2,}", " ") + "] focus " + st.focus() + ", width "
                + Wayfinding.lineWidth(st.segments()) + ", default " + Wayfinding.compassByDefault(false, false) + "/"
                + Wayfinding.compassByDefault(false, true) + "/" + Wayfinding.compassByDefault(true, true)
                + ", showing " + bars.size() + " renders " + renders + " updates " + updates;
    }
}
