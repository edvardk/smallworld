package io.github.edvardk.mcbrand;

import io.papermc.paper.scoreboard.numbers.NumberFormat;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.function.Predicate;
import java.util.function.ToIntFunction;
import java.util.logging.Level;
import java.util.logging.Logger;
import net.kyori.adventure.bossbar.BossBar;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.TextComponent;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Bukkit;
import org.bukkit.NamespacedKey;
import org.bukkit.Server;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.scoreboard.Criteria;
import org.bukkit.scoreboard.DisplaySlot;
import org.bukkit.scoreboard.Objective;
import org.bukkit.scoreboard.Score;
import org.bukkit.scoreboard.Scoreboard;
import org.bukkit.scoreboard.ScoreboardManager;

/**
 * The optional HUD (per player, {@code /hud top|side|off}; the choice is kept in the player's PDC) and the tab
 * list's header/footer. One repeating task every {@code interval_seconds} (default 5 s; never per tick) reads the
 * server's speed once and sends only what changed.
 *
 * <ul>
 *   <li><b>top</b> (v1.0.1, the default): a boss bar, top centre, one line: "My Server · 3 online · Smooth ·
 *       20/20 TPS · 8 of 50 ms · you 23 ms" (the viewer's own ping); its fill is the share of the 50 ms tick
 *       budget used, its colour the health (Adventure BossBar, one per player: Audience.showBossBar).
 *   <li><b>side</b>: the sidebar (client-fixed at the right): players with their ping, then the server's health,
 *       speed and tick time. A sidebar player gets their own Scoreboard (Paper: a player's scoreboard replaces the
 *       main one for them). Each line is a score entry with a custom name; the right-hand number is a fixed
 *       format or blank. Entries are "mcb.N": vanilla hides score holders starting with '#'
 *       (Scoreboard.HIDDEN_SCORE_PREFIX; the v1.0.0 HUD showed its title and no line because of it), and '.'
 *       can't be in a player name.
 *   <li>v1.4.1: the viewer's own FPS, as the Smallworld client mod last reported it ({@link Smallworld#fps}, every
 *       client.fps.interval_seconds): "· 144 fps" at the end of the boss bar's line, and a "Your FPS" line in the
 *       sidebar in place of the "FPS: press F3" hint. Players without the mod see neither. The mod's own FPS
 *       element is then switched off for them ({@link Smallworld#clientHud}): it was drawn at the boss bar
 *       title's height, right of the 182-px bar, so it sat on this line's text.
 * </ul>
 *
 * <p>The {glyph} icon (resource pack font) is shown only to players whose client loaded the pack ({@code glyphs}).
 */
final class Hud {
    static final int MAX_LINES = 15; // the client shows at most 15 sidebar lines
    private static final String OBJECTIVE = "mcbrand";
    static final String ENTRY_PREFIX = "mcb."; // never '#': the client hides those
    private static final long LOG_EVERY_NS = 60_000_000_000L; // a failing write is logged at most once a minute
    private static final Component DOT = Component.text("● "); // ● before a player's name, coloured by ping

    enum Mode {
        TOP, SIDE, OFF;

        static Mode parse(String s, Mode fallback) {
            if (s == null) {
                return fallback;
            }
            return switch (s.toLowerCase(Locale.ROOT)) {
                case "top", "on" -> TOP;
                case "side", "sidebar" -> SIDE;
                case "off" -> OFF;
                default -> fallback;
            };
        }

        String word() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    private final Settings s;
    private final Logger log;
    private final Predicate<Player> glyphs;
    private final ToIntFunction<Player> fps; // v1.4.1: the client mod's latest report, -1 none
    private long lastLogNs = -LOG_EVERY_NS;
    long writeErrors;
    private final NamespacedKey modeKey;
    private final NamespacedKey legacyKey; // v1.0.0: BYTE 1 = sidebar on, 0 = off
    private final Map<UUID, Board> boards = new HashMap<>();
    private final Map<UUID, BossBar> bars = new HashMap<>();
    // v1.1: players in their join grace period: {join time ms, previous ping sample} (Msg.settled)
    private final Map<UUID, long[]> settling = new HashMap<>();
    private final Msg.Smoother smoother = new Msg.Smoother();
    private Component lastFooter;
    long runs;
    long lastNs;
    long maxNs;
    Msg.Speed lastSpeed;

    record Line(Component text, Component number) {}

    private static final class Board {
        final Scoreboard sb;
        final Objective obj;
        final Line[] shown = new Line[MAX_LINES];

        Board(Scoreboard sb, Objective obj) {
            this.sb = sb;
            this.obj = obj;
        }
    }

    Hud(BrandPlugin plugin, Settings settings, Predicate<Player> glyphs, ToIntFunction<Player> fps) {
        this.s = settings;
        this.log = plugin.getLogger();
        this.glyphs = glyphs;
        this.fps = fps;
        this.modeKey = new NamespacedKey(plugin, "hud_mode");
        this.legacyKey = new NamespacedKey(plugin, "hud");
    }

    /** The player's mode: their /hud choice, else the server's default (OFF while the HUD is switched off). */
    Mode mode(Player p) {
        if (!s.hudEnabled()) {
            return Mode.OFF;
        }
        PersistentDataContainer pdc = p.getPersistentDataContainer();
        String v = pdc.get(modeKey, PersistentDataType.STRING);
        if (v != null) {
            return Mode.parse(v, Mode.OFF);
        }
        Mode def = Mode.parse(s.hudDefaultMode(), Mode.TOP);
        Byte legacy = pdc.get(legacyKey, PersistentDataType.BYTE);
        if (legacy != null) { // v1.1: migrated once to hud_mode, the v1.0.0 key removed
            Mode m = legacyMode(legacy, def);
            pdc.set(modeKey, PersistentDataType.STRING, m.word());
            pdc.remove(legacyKey);
            return m;
        }
        return def;
    }

    /**
     * v1.1 (live): a v1.0.0 HUD choice. 1 meant "show the HUD" when the sidebar was the only HUD, not "the
     * sidebar" (v1.0.1 mapped it to SIDE, so those players kept the sidebar although the default became top):
     * it gets the server's default mode now; 0 (they hid it) stays OFF.
     */
    static Mode legacyMode(byte legacy, Mode serverDefault) {
        return legacy != 0 ? serverDefault : Mode.OFF;
    }

    /** For /mcbrand selftest (tests/e2e/brand_check.py): the legacy mapping. */
    static String legacySelftest() {
        return "legacy hud 1->" + legacyMode((byte) 1, Mode.TOP).word() + "/" + legacyMode((byte) 1, Mode.SIDE).word()
                + " 0->" + legacyMode((byte) 0, Mode.TOP).word();
    }

    Mode showing(Player p) {
        UUID id = p.getUniqueId();
        return bars.containsKey(id) ? Mode.TOP : boards.containsKey(id) ? Mode.SIDE : Mode.OFF;
    }

    void set(Player p, Mode m) {
        p.getPersistentDataContainer().set(modeKey, PersistentDataType.STRING, m.word());
        apply(p, m);
    }

    private void apply(Player p, Mode m) {
        hide(p);
        Msg.Speed speed = Msg.Speed.of(Bukkit.getServer());
        boolean unsettled = settling.containsKey(p.getUniqueId());
        if (m == Mode.TOP) {
            BossBar bar = BossBar.bossBar(Component.empty(), 0f, BossBar.Color.WHITE, BossBar.Overlay.PROGRESS);
            bars.put(p.getUniqueId(), bar);
            updateBar(p, bar, speed, smoother.current(), Bukkit.getOnlinePlayers().size(), unsettled);
            p.showBossBar(bar);
        } else if (m == Mode.SIDE) {
            Board b = newBoard(glyphs.test(p));
            boards.put(p.getUniqueId(), b);
            p.setScoreboard(b.sb);
            update(b, withFps(lines(Bukkit.getServer(), speed, smoother.current(), settling.keySet(), unsettled),
                    fps.applyAsInt(p)));
        }
    }

    /**
     * v1.1: one settle sample for a player in the join grace period (once per HUD run): true while they're still
     * settling; the first settled sample ends the grace period for good (until the next join).
     */
    private boolean stillSettling(Player p, long nowMs) {
        long[] st = settling.get(p.getUniqueId());
        if (st == null) {
            return false;
        }
        int ping = p.getPing();
        boolean done = Msg.settled(nowMs - st[0], s.hudSettleSeconds() * 1000L, (int) st[1], ping);
        st[1] = ping;
        if (done) {
            settling.remove(p.getUniqueId());
        }
        return !done;
    }

    /** Shows the player's HUD again (after their resource pack loaded: the titles get the glyph). */
    void refresh(Player p) {
        Mode m = showing(p);
        if (m != Mode.OFF) {
            apply(p, m);
        }
    }

    void hide(Player p) {
        UUID id = p.getUniqueId();
        Board b = boards.remove(id);
        if (b != null && p.isOnline()) {
            p.setScoreboard(Bukkit.getScoreboardManager().getMainScoreboard());
        }
        BossBar bar = bars.remove(id);
        if (bar != null) {
            p.hideBossBar(bar);
        }
    }

    void forget(UUID id) {
        boards.remove(id);
        bars.remove(id);
        settling.remove(id);
    }

    void hideAll() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            hide(p);
        }
        boards.clear();
        bars.clear();
    }

    private Board newBoard(boolean glyph) {
        ScoreboardManager mgr = Bukkit.getScoreboardManager();
        Scoreboard sb = mgr.getNewScoreboard();
        Objective obj = sb.registerNewObjective(OBJECTIVE, Criteria.DUMMY,
                Texts.withGlyph(Texts.bold(s.name(), s.accent()), glyph));
        obj.setDisplaySlot(DisplaySlot.SIDEBAR);
        obj.numberFormat(NumberFormat.blank());
        return new Board(sb, obj);
    }

    /** The repeating task: tab footer for everyone, the HUD for those who have one. */
    void tick() {
        long t0 = System.nanoTime();
        Server server = Bukkit.getServer();
        Collection<? extends Player> online = server.getOnlinePlayers();
        Msg.Speed speed = Msg.Speed.of(server);
        lastSpeed = speed;
        Msg.Health health = smoother.update(speed.health()); // v1.1: one burst doesn't flip everyone's word
        if (!online.isEmpty()) {
            long now = System.currentTimeMillis();
            Set<UUID> unsettled = new HashSet<>();
            Set<UUID> justSettled = new HashSet<>();
            for (Player p : online) {
                boolean was = settling.containsKey(p.getUniqueId());
                if (stillSettling(p, now)) {
                    unsettled.add(p.getUniqueId());
                } else if (was) {
                    justSettled.add(p.getUniqueId());
                }
            }
            if (s.tabList()) {
                Component footer = footer(server, online.size(), speed, health);
                boolean changed = !footer.equals(lastFooter);
                lastFooter = footer;
                Component neutral = unsettled.isEmpty() ? null : footerSettling(server, online.size());
                for (Player p : online) {
                    UUID id = p.getUniqueId();
                    if (unsettled.contains(id)) {
                        p.sendPlayerListHeaderAndFooter(header(glyphs.test(p)), neutral);
                    } else if (changed || justSettled.contains(id)) {
                        p.sendPlayerListHeaderAndFooter(header(glyphs.test(p)), footer);
                    }
                }
            }
            List<Line> lines = boards.isEmpty() ? null : lines(server, speed, health, unsettled, false);
            List<Line> settlingLines = null;
            for (Player p : online) {
                UUID id = p.getUniqueId();
                boolean mine = unsettled.contains(id);
                Board b = boards.get(id);
                if (b != null) {
                    if (mine && settlingLines == null) {
                        settlingLines = lines(server, speed, health, unsettled, true);
                    }
                    update(b, mine ? settlingLines : withFps(lines, fps.applyAsInt(p)));
                }
                BossBar bar = bars.get(id);
                if (bar != null) {
                    updateBar(p, bar, speed, health, online.size(), mine);
                }
            }
        }
        lastNs = System.nanoTime() - t0;
        maxNs = Math.max(maxNs, lastNs);
        runs++;
    }

    /** On join: the tab list right away (the footer is resent by the task only when it changes), then the HUD. */
    void join(Player p) {
        settling.put(p.getUniqueId(), new long[] {System.currentTimeMillis(), -1}); // v1.1: the grace period
        if (s.tabList()) {
            Server server = Bukkit.getServer();
            int n = server.getOnlinePlayers().size();
            lastFooter = footer(server, n, Msg.Speed.of(server), smoother.current());
            Component neutral = footerSettling(server, n);
            for (Player o : server.getOnlinePlayers()) {
                o.sendPlayerListHeaderAndFooter(header(glyphs.test(o)),
                        settling.containsKey(o.getUniqueId()) ? neutral : lastFooter);
            }
        }
        Mode m = mode(p);
        if (m != Mode.OFF) {
            apply(p, m);
        }
    }

    /** After a player's resource pack loaded: their tab header and HUD title with the glyph. */
    void packLoaded(Player p) {
        if (s.tabList() && lastFooter != null) {
            p.sendPlayerListHeaderAndFooter(header(true), lastFooter);
        }
        refresh(p);
    }

    Component header(boolean glyph) {
        return Component.text()
                .append(Component.newline())
                .append(Texts.withGlyph(Texts.bold(s.tabHeader(), s.accent()), glyph))
                .append(Component.newline())
                .build();
    }

    Component footer(Server server, int online, Msg.Speed speed) {
        return footer(server, online, speed, smoother.current());
    }

    Component footer(Server server, int online, Msg.Speed speed, Msg.Health health) {
        Component sep = Component.text("  ·  ", s.secondary());
        return Component.text()
                .append(Component.newline())
                .append(Component.text(online + "/" + server.getMaxPlayers() + " online", s.text()))
                .append(sep)
                .append(Msg.healthWord(health))
                .append(sep)
                .append(Msg.tpsText(speed.tps(), true))
                .append(sep)
                .append(Msg.tickText(speed.mspt(), true))
                .append(Component.newline())
                .build();
    }

    /** v1.1: the footer a player in their join grace period sees: no health word, no colours, no numbers. */
    Component footerSettling(Server server, int online) {
        Component sep = Component.text("  ·  ", s.secondary());
        return Component.text()
                .append(Component.newline())
                .append(Component.text(online + "/" + server.getMaxPlayers() + " online", s.text()))
                .append(sep)
                .append(Msg.hover(Component.text(Msg.SETTLING, s.subtle()), Msg.SETTLING_HOVER))
                .append(Component.newline())
                .build();
    }

    /** v1.1: the boss bar of a player in their join grace period: name · online · Settling in... */
    Component barTextSettling(int online, boolean glyph) {
        Component sep = Component.text(" · ", s.secondary());
        return Component.text()
                .append(Texts.withGlyph(Texts.bold(s.name(), s.accent()), glyph))
                .append(sep)
                .append(Component.text(online + " online", s.text()))
                .append(sep)
                .append(Component.text(Msg.SETTLING, s.subtle()))
                .build();
    }

    Component barText(int online, Msg.Speed speed, int ping, boolean glyph) {
        return barText(online, speed, speed.health(), ping, glyph);
    }

    /** The boss bar's one line: name · online · health · speed · tick time · the viewer's ping. */
    Component barText(int online, Msg.Speed speed, Msg.Health health, int ping, boolean glyph) {
        return barText(online, speed, health, ping, -1, glyph);
    }

    /** v1.4.1: ... · the viewer's ping · the viewer's FPS (the client mod's report; {@code fps < 0}: none). */
    Component barText(int online, Msg.Speed speed, Msg.Health health, int ping, int fps, boolean glyph) {
        Component sep = Component.text(" · ", s.secondary());
        TextComponent.Builder b = Component.text()
                .append(Texts.withGlyph(Texts.bold(s.name(), s.accent()), glyph))
                .append(sep)
                .append(Component.text(online + " online", s.text()))
                .append(sep)
                .append(Component.text(health.word, health.color))
                .append(sep)
                .append(Component.text(Msg.tps(speed.tps(), false), Texts.tpsColor(speed.tps())))
                .append(sep)
                .append(Component.text(Msg.tick(speed.mspt(), false), Msg.tickColor(speed.mspt())));
        if (ping >= 0) {
            b.append(sep).append(Component.text("you " + ping + " ms", Texts.pingColor(ping)));
        }
        if (fps >= 0) {
            b.append(sep).append(Component.text(Msg.fps(fps), Msg.fpsColor(fps)));
        }
        return b.build();
    }

    private void updateBar(Player p, BossBar bar, Msg.Speed speed, Msg.Health health, int online, boolean unsettled) {
        try {
            if (unsettled) {
                bar.name(barTextSettling(online, glyphs.test(p)));
                bar.progress(0f);
                bar.color(BossBar.Color.WHITE);
                return;
            }
            bar.name(barText(online, speed, health, p.getPing(), fps.applyAsInt(p), glyphs.test(p)));
            bar.progress(speed.load());
            bar.color(health.bar);
        } catch (RuntimeException e) {
            failed("the boss bar", e);
        }
    }

    /** The sidebar's lines (same for every sidebar player): players with their ping, then health, speed, tick time. */
    List<Line> lines(Server server, Msg.Speed speed) {
        return lines(server, speed, smoother.current(), Set.of(), false);
    }

    /**
     * v1.1: players still settling show "…" instead of a ping (theirs isn't a real reading yet); the viewer's own
     * variant while they settle ({@code forSettling}) shows "Settling in…" instead of the health word and no FPS hint.
     */
    List<Line> lines(Server server, Msg.Speed speed, Msg.Health h, Set<UUID> unsettled, boolean forSettling) {
        List<Line> out = new ArrayList<>();
        List<Player> players = new ArrayList<>(server.getOnlinePlayers());
        players.sort((a, b) -> a.getName().compareToIgnoreCase(b.getName()));
        out.add(new Line(Component.text("Online " + players.size() + "/" + server.getMaxPlayers(), s.subtle()), null));
        int shown = Math.min(players.size(), s.hudMaxPlayers());
        for (int i = 0; i < shown; i++) {
            Player p = players.get(i);
            if (unsettled.contains(p.getUniqueId())) {
                out.add(new Line(Component.text().append(DOT.color(s.subtle()))
                        .append(Component.text(p.getName(), s.text())).build(), Component.text("\u2026", s.subtle())));
                continue;
            }
            int ping = p.getPing();
            out.add(new Line(Component.text().append(DOT.color(Texts.pingColor(ping)))
                    .append(Component.text(p.getName(), s.text())).build(),
                    Component.text(ping + " ms", Texts.pingColor(ping))));
        }
        if (players.size() > shown) {
            out.add(new Line(Component.text(" +" + (players.size() - shown) + " more", s.subtle()), null));
        }
        out.add(new Line(Component.text(" "), null));
        if (forSettling) {
            out.add(new Line(Component.text(Msg.SERVER, s.subtle()), Component.text(Msg.SETTLING, s.subtle())));
            return out.subList(0, Math.min(out.size(), MAX_LINES));
        }
        out.add(new Line(Component.text(Msg.SERVER, s.subtle()), Component.text(h.word, h.color)));
        out.add(new Line(Component.text(Msg.SPEED, s.subtle()),
                Component.text(Msg.tps(speed.tps(), true), Texts.tpsColor(speed.tps()))));
        out.add(new Line(Component.text(Msg.TICK_TIME, s.subtle()),
                Component.text(Msg.tick(speed.mspt(), true), Msg.tickColor(speed.mspt()))));
        out.add(new Line(Component.text(Msg.FPS_HINT, s.subtle()), null));
        return out.subList(0, Math.min(out.size(), MAX_LINES));
    }

    /**
     * v1.4.1: one viewer's sidebar: the shared lines with the "FPS: press F3" hint replaced by their reported FPS
     * ({@code fps < 0}, or no hint line (settling, cut at MAX_LINES): the shared list itself).
     */
    List<Line> withFps(List<Line> lines, int fps) {
        if (fps < 0) {
            return lines;
        }
        Component hint = Component.text(Msg.FPS_HINT, s.subtle());
        for (int i = lines.size() - 1; i >= 0; i--) {
            if (hint.equals(lines.get(i).text())) {
                List<Line> out = new ArrayList<>(lines);
                out.set(i, new Line(Component.text(Msg.YOUR_FPS, s.subtle()),
                        Component.text(Msg.fps(fps), Msg.fpsColor(fps))));
                return out;
            }
        }
        return lines;
    }

    /** Writes the lines that changed. Each line on its own: one that fails is logged (rate-limited) and retried
     * next run (shown[i] is set only after a successful write); the other lines still get theirs. */
    private void update(Board b, List<Line> lines) {
        for (int i = 0; i < MAX_LINES; i++) {
            Line want = i < lines.size() ? lines.get(i) : null;
            if (Objects.equals(want, b.shown[i])) {
                continue;
            }
            String entry = ENTRY_PREFIX + i;
            try {
                if (want == null) {
                    b.sb.resetScores(entry);
                } else {
                    Score score = b.obj.getScore(entry);
                    score.setScore(MAX_LINES - i); // first: creates the score the name and format belong to
                    score.customName(want.text());
                    // null = no override: the objective's blank format (CraftScore: numberFormatOverride(null))
                    score.numberFormat(want.number() == null ? null : NumberFormat.fixed(want.number()));
                }
                b.shown[i] = want;
            } catch (RuntimeException e) {
                b.shown[i] = null;
                failed("sidebar line " + i, e);
            }
        }
    }

    private void failed(String what, RuntimeException e) {
        writeErrors++;
        long now = System.nanoTime();
        if (now - lastLogNs >= LOG_EVERY_NS) {
            lastLogNs = now;
            log.log(Level.WARNING, "HUD: " + what + " couldn't be written (" + writeErrors + " so far)", e);
        }
    }

    /** For /mcbrand selftest: a sidebar board of its own (no player needed), filled like a player's; the lines a
     * client would show (score set, entry not hidden, the right name) out of the lines built; the boss bar text. */
    String selftest() {
        Server server = Bukkit.getServer();
        Msg.Speed speed = Msg.Speed.of(server);
        Board b = newBoard(false);
        List<Line> lines = lines(server, speed);
        update(b, lines);
        int visible = 0;
        for (int i = 0; i < lines.size(); i++) {
            String entry = ENTRY_PREFIX + i;
            Score score = b.obj.getScore(entry);
            if (score.isScoreSet() && !entry.startsWith("#") && Objects.equals(score.customName(), lines.get(i).text())) {
                visible++;
            }
        }
        b.obj.unregister();
        BossBar bar = BossBar.bossBar(barText(server.getOnlinePlayers().size(), speed, 23, false), speed.load(),
                speed.health().bar, BossBar.Overlay.PROGRESS);
        return "sidebar scores " + visible + "/" + lines.size() + " visible, write errors " + writeErrors
                + "; boss bar: " + Texts.flat(bar.name()) + " (fill " + String.format(Locale.ROOT, "%.2f", bar.progress())
                + ", " + bar.color() + "); default mode " + Mode.parse(s.hudDefaultMode(), Mode.TOP).word() + "; "
                + legacySelftest() + "; " + Msg.selftest() + ", settle " + s.hudSettleSeconds() + " s; settling bar: "
                + Texts.flat(barTextSettling(1, false)) + "; " + fpsSelftest(server, speed);
    }

    /**
     * v1.4.1, for /mcbrand selftest: the FPS in the boss bar's line (last, only with a report), the sidebar's FPS
     * line in place of the hint, and the colours at the Worker's thresholds.
     */
    String fpsSelftest(Server server, Msg.Speed speed) {
        String with = Texts.flat(barText(1, speed, speed.health(), 23, 144, false));
        String without = Texts.flat(barText(1, speed, speed.health(), 23, -1, false));
        List<Line> base = lines(server, speed);
        List<Line> mine = withFps(base, 24);
        Line last = null;
        for (Line l : mine) {
            if (Texts.flat(l.text()).equals(Msg.YOUR_FPS)) {
                last = l;
            }
        }
        return "fps bar " + with.endsWith(" · you 23 ms · 144 fps") + "/" + !without.contains("fps")
                + ", fps line " + (last == null ? "-" : Texts.flat(last.text()) + " " + Texts.flat(last.number()))
                + " hint " + (withFps(base, -1) == base) + ", fps colours "
                + NamedTextColor.NAMES.key(Msg.fpsColor(Msg.FPS_FAIR)) + "/"
                + NamedTextColor.NAMES.key(Msg.fpsColor(Msg.FPS_FAIR - 1)) + "/"
                + NamedTextColor.NAMES.key(Msg.fpsColor(Msg.FPS_POOR - 1));
    }
}
