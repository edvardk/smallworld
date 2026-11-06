package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.TextComponent;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.entity.Display;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.entity.TextDisplay;
import org.bukkit.persistence.PersistentDataType;

/**
 * v1.2: spawn holograms, server.yml {@code branding.holograms} (in-house, vanilla clients: text display entities).
 *
 * <ul>
 *   <li>One {@link TextDisplay} per hologram ({@code world.spawn}, {@code text(Component)}, {@code Billboard.CENTER}),
 *       never saved with the world ({@code setPersistent(false)}) and tagged in its PDC ({@link #TAG} = the
 *       hologram's index), so a sweep finds every display of ours: at start-up and whenever a chunk's entities
 *       load (EntitiesLoadEvent; a display left from a crash or an older config is removed), after which the
 *       configured one is spawned again if its chunk is the one that loaded. Unloading a chunk drops the display
 *       with it (not persistent); the next load brings it back.
 *   <li>Placeholders: {server}, {players} (online count), {tps}, {glyph}. A text display looks the same to every
 *       viewer, so a hologram with {glyph} gets a second display with the glyph that only players with the
 *       server resource pack see (Entity.setVisibleByDefault + Player.showEntity / hideEntity).
 *   <li>The optional live health line ("Server: Smooth", the HUD's smoothed word) and {players}/{tps} are
 *       refreshed by the HUD task; setText runs only when the text actually changed (one Component compare per
 *       hologram per run otherwise).
 * </ul>
 */
final class Holograms {
    static final int MAX = 10;
    static final int MAX_LINES = 8;

    record Spec(String world, double x, double y, double z, List<String> lines, boolean health) {
        boolean glyph() {
            return lines.stream().anyMatch(l -> l.contains("{glyph}"));
        }
    }

    /** What is in the world for one spec: the plain display and (with {glyph}) the glyph one, and their text. */
    private static final class Live {
        TextDisplay plain;
        TextDisplay glyph;
        Component plainText;
        Component glyphText;
    }

    private final BrandPlugin plugin;
    private final Settings s;
    private final NamespacedKey tag;
    private final Msg.Smoother smoother = new Msg.Smoother();
    private final List<Live> live = new ArrayList<>();
    long spawned;
    long swept;
    long updates;

    Holograms(BrandPlugin plugin, Settings settings) {
        this.plugin = plugin;
        this.s = settings;
        this.tag = new NamespacedKey(plugin, "hologram");
        for (int i = 0; i < settings.holograms().size(); i++) {
            live.add(new Live());
        }
    }

    // --- the text --------------------------------------------------------------------------------------------------

    /** The display's text for one spec: lines joined with newlines, placeholders filled, the health line last. */
    Component text(Spec spec, int players, double tps, Msg.Health health, boolean glyph) {
        TextComponent.Builder out = Component.text();
        Map<String, Component> values = Map.of(
                "server", Texts.bold(s.name(), s.accent()),
                "players", Component.text(Integer.toString(players), s.accent()),
                "tps", Component.text(String.format(Locale.ROOT, "%.1f", tps), Texts.tpsColor(tps)));
        boolean first = true;
        for (String line : spec.lines()) {
            if (!first) {
                out.append(Component.newline());
            }
            first = false;
            out.append(Texts.fill(line, s.text(), values, glyph));
        }
        if (spec.health()) {
            if (!first) {
                out.append(Component.newline());
            }
            out.append(Component.text(Msg.SERVER + ": ", s.subtle())).append(Component.text(health.word, health.color));
        }
        return out.build();
    }

    // --- spawning and sweeping ---------------------------------------------------------------------------------------

    static String worldKey(World w) {
        return w.getKey().toString();
    }

    private Integer index(Entity e) {
        return e instanceof TextDisplay ? e.getPersistentDataContainer().get(tag, PersistentDataType.INTEGER) : null;
    }

    /** Start-up: every display of ours in the loaded chunks goes, then each hologram whose chunk is loaded spawns. */
    void start() {
        for (World w : Bukkit.getWorlds()) {
            for (TextDisplay d : w.getEntitiesByClass(TextDisplay.class)) {
                if (index(d) != null) {
                    d.remove();
                    swept++;
                }
            }
        }
        for (int i = 0; i < live.size(); i++) {
            ensure(i);
        }
    }

    void stop() {
        for (Live l : live) {
            for (TextDisplay d : new TextDisplay[] {l.plain, l.glyph}) {
                if (d != null && d.isValid()) {
                    d.remove();
                }
            }
            l.plain = null;
            l.glyph = null;
        }
    }

    /** EntitiesLoadEvent: sweep stale displays of ours in that chunk, then respawn what belongs there. */
    void loaded(World w, int chunkX, int chunkZ, List<Entity> entities) {
        if (live.isEmpty()) {
            return;
        }
        for (Entity e : entities) {
            Integer i = index(e);
            if (i != null && !isCurrent(i, e)) {
                e.remove();
                swept++;
            }
        }
        String key = worldKey(w);
        for (int i = 0; i < live.size(); i++) {
            Spec spec = s.holograms().get(i);
            if (spec.world().equals(key) && ((int) Math.floor(spec.x())) >> 4 == chunkX
                    && ((int) Math.floor(spec.z())) >> 4 == chunkZ) {
                ensure(i);
            }
        }
    }

    private boolean isCurrent(int i, Entity e) {
        if (i < 0 || i >= live.size()) {
            return false;
        }
        Live l = live.get(i);
        return e.equals(l.plain) || e.equals(l.glyph);
    }

    /** Spawns hologram i if its chunk is loaded and its display is gone (never twice). */
    private void ensure(int i) {
        Spec spec = s.holograms().get(i);
        Live l = live.get(i);
        World w = Bukkit.getWorld(NamespacedKey.fromString(spec.world()));
        if (w == null || !w.isChunkLoaded(((int) Math.floor(spec.x())) >> 4, ((int) Math.floor(spec.z())) >> 4)) {
            return;
        }
        Location at = new Location(w, spec.x(), spec.y(), spec.z());
        Msg.Speed speed = Msg.Speed.of(Bukkit.getServer());
        int players = Bukkit.getOnlinePlayers().size();
        if (l.plain == null || !l.plain.isValid()) {
            l.plainText = text(spec, players, speed.tps(), smoother.current(), false);
            l.plain = spawn(w, at, i, l.plainText, true);
        }
        if (spec.glyph() && (l.glyph == null || !l.glyph.isValid())) {
            l.glyphText = text(spec, players, speed.tps(), smoother.current(), true);
            l.glyph = spawn(w, at, i, l.glyphText, false);
        }
        for (Player p : Bukkit.getOnlinePlayers()) {
            viewer(p, BrandPlugin.glyphs(p));
        }
    }

    private TextDisplay spawn(World w, Location at, int i, Component text, boolean visible) {
        spawned++;
        return w.spawn(at, TextDisplay.class, d -> {
            d.text(text);
            d.setBillboard(Display.Billboard.CENTER);
            d.setPersistent(false);
            d.setVisibleByDefault(visible);
            d.getPersistentDataContainer().set(tag, PersistentDataType.INTEGER, i);
        });
    }

    /** Which variant a player sees: the glyph one only with the server resource pack loaded. */
    void viewer(Player p, boolean glyph) {
        for (int i = 0; i < live.size(); i++) {
            Live l = live.get(i);
            if (l.glyph == null || !l.glyph.isValid()) {
                continue;
            }
            if (glyph) {
                p.showEntity(plugin, l.glyph);
                if (l.plain != null) {
                    p.hideEntity(plugin, l.plain);
                }
            } else {
                p.hideEntity(plugin, l.glyph);
                if (l.plain != null) {
                    p.showEntity(plugin, l.plain);
                }
            }
        }
    }

    /** The HUD task (every interval_seconds): live values; setText only when the text changed. */
    void tick(Msg.Speed speed, int players) {
        Msg.Health health = smoother.update(speed.health());
        for (int i = 0; i < live.size(); i++) {
            Spec spec = s.holograms().get(i);
            Live l = live.get(i);
            if (l.plain != null && l.plain.isValid()) {
                Component t = text(spec, players, speed.tps(), health, false);
                if (!t.equals(l.plainText)) {
                    l.plain.text(t);
                    l.plainText = t;
                    updates++;
                }
            }
            if (l.glyph != null && l.glyph.isValid()) {
                Component t = text(spec, players, speed.tps(), health, true);
                if (!t.equals(l.glyphText)) {
                    l.glyph.text(t);
                    l.glyphText = t;
                    updates++;
                }
            }
        }
    }

    /** For /mcbrand selftest: the text rules on a fixed spec, and the counters. */
    String selftest() {
        Spec spec = new Spec("minecraft:overworld", 0.5, 70, 0.5, List.of("Welcome to {server} {glyph}",
                "{players} online at {tps} TPS"), true);
        String plain = Texts.flat(text(spec, 3, 19.96, Msg.Health.SMOOTH, false));
        String withGlyph = Texts.flat(text(spec, 3, 19.96, Msg.Health.SMOOTH, true));
        return "holograms " + plain.replace("\n", " | ") + ", glyph " + withGlyph.contains(Texts.GLYPH_CHAR)
                + "/" + !plain.contains(Texts.GLYPH_CHAR) + ", configured " + live.size() + " spawned "
                + spawned + " swept " + swept + " updates " + updates;
    }
}
