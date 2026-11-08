package io.github.edvardk.mcbrand;

import java.util.Arrays;
import java.util.Locale;
import net.kyori.adventure.bossbar.BossBar;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextColor;
import org.bukkit.Server;

/**
 * Every player-facing word about the server's speed, in one place (v1.0.1: players don't know what TPS or
 * MSPT mean). The numbers stay, always with their scale and a plain meaning: "20.0/20 TPS", "8.3 of 50 ms", a
 * health word, and a one-sentence hover where the client shows hovers (chat; not the boss bar, the sidebar or the
 * tab list). The thresholds are the same numbers as mc-telemetry's TelemetryPlugin (tests/test_brand.py checks).
 */
final class Msg {
    private Msg() {}

    // Health thresholds: Smooth = p95 tick time < SMOOTH_P95_MS and TPS >= SMOOTH_TPS; A bit busy = p95 < BUDGET_MS.
    static final double SMOOTH_P95_MS = 35.0;
    static final double SMOOTH_TPS = 19.5;
    static final double BUDGET_MS = 50.0; // one tick's budget at 20 TPS

    static final String TPS_HOVER = "TPS (ticks per second): how many game updates the server manages each second. "
            + "20 is full speed; below 20 the world slows down.";
    static final String MSPT_HOVER = "Tick time (MSPT): milliseconds each update takes. Must stay under 50 ms for "
            + "full speed; lower is better.";
    static final String PING_HOVER = "Your connection (ping): the time for data to reach the server and back. "
            + "Lower is better; under 80 ms feels instant.";
    static final String HEALTH_HOVER = "Smooth: full speed with room to spare. A bit busy: still full speed, close "
            + "to the limit. Laggy: the server can't keep up and the world slows down.";

    // v1.0.1 (decision): the elimination answer when the server is smooth and the connection fine (= the Worker's
    // MESSAGES.fps and mc-telemetry's FPS_TIP); the sidebar shows FPS_HINT (no room for the sentence there).
    static final String FPS_TIP = "The server and your connection look fine, so stutter is likely your PC's frame rate. Press F3 to see your FPS (top-left). Lowering render distance or installing Sodium usually helps.";
    static final String FPS_HINT = "FPS: press F3";
    // v1.4.1: the client mod's reported FPS inside the HUD; the thresholds are the Worker's RULES.fps_fair/fps_poor
    static final String YOUR_FPS = "Your FPS";
    static final int FPS_FAIR = 30;
    static final int FPS_POOR = 20;
    static final int PING_FINE_MS = 150;

    static final String SPEED = "Speed";
    static final String TICK_TIME = "Tick time";
    static final String YOUR_CONNECTION = "Your connection";
    static final String SERVER = "Server";

    enum Health {
        SMOOTH("Smooth", NamedTextColor.GREEN, BossBar.Color.GREEN),
        BUSY("A bit busy", NamedTextColor.YELLOW, BossBar.Color.YELLOW),
        LAGGY("Laggy", NamedTextColor.RED, BossBar.Color.RED);

        final String word;
        final TextColor color;
        final BossBar.Color bar;

        Health(String word, TextColor color, BossBar.Color bar) {
            this.word = word;
            this.color = color;
            this.bar = bar;
        }
    }

    static final String SETTLING = "Settling in\u2026";
    static final String SETTLING_HOVER = "You just joined: your connection and the world around you are still loading. "
            + "The real numbers show in a few seconds.";
    static final int SETTLE_PING_MAX_MS = 2000; // a reading above this is not a settled connection

    /**
     * v1.1 (decision): the health word everyone sees, smoothed. Getting better shows at once; getting worse needs
     * {@code DOWNGRADE_SAMPLES} readings in a row (one per HUD run), so one slow tick burst (a player joining and
     * loading chunks) doesn't flip everyone's HUD to "Laggy" for a moment. One instance per HUD task (main thread).
     */
    static final class Smoother {
        static final int DOWNGRADE_SAMPLES = 2;
        private Health shown = Health.SMOOTH;
        private int worse;

        Health update(Health raw) {
            if (raw.ordinal() <= shown.ordinal()) {
                shown = raw;
                worse = 0;
            } else if (++worse >= DOWNGRADE_SAMPLES) {
                shown = raw;
                worse = 0;
            }
            return shown;
        }

        Health current() {
            return shown;
        }
    }

    /**
     * v1.1 (decision): a player who just joined sees "Settling in..." (no health word, colour or tips) until both
     * {@code settleMs} have passed since the join and their ping is a real reading: this and the previous sample
     * &gt; 0, at most SETTLE_PING_MAX_MS, and within max(30 ms, half the previous) of each other.
     */
    static boolean settled(long sinceJoinMs, long settleMs, int previousPing, int ping) {
        if (sinceJoinMs < settleMs) {
            return false;
        }
        if (previousPing <= 0 || ping <= 0 || previousPing > SETTLE_PING_MAX_MS || ping > SETTLE_PING_MAX_MS) {
            return false;
        }
        return Math.abs(ping - previousPing) <= Math.max(30, previousPing / 2);
    }

    /** For /mcbrand selftest (tests/e2e/brand_check.py): the smoother and the settle rule on fixed inputs. */
    static String selftest() {
        Smoother sm = new Smoother();
        String a = sm.update(Health.LAGGY).word; // one bad reading: still Smooth
        String b = sm.update(Health.LAGGY).word; // the second: Laggy
        String c = sm.update(Health.SMOOTH).word; // better: at once
        return "smoothing " + a + "/" + b + "/" + c + ", settle " + settled(5_000, 10_000, 40, 40) + "/"
                + settled(12_000, 10_000, 0, 40) + "/" + settled(12_000, 10_000, 40, 400) + "/"
                + settled(12_000, 10_000, 40, 45);
    }

    static Health health(double tps, double p95Ms) {
        if (p95Ms < SMOOTH_P95_MS && tps >= SMOOTH_TPS) {
            return Health.SMOOTH;
        }
        return p95Ms < BUDGET_MS ? Health.BUSY : Health.LAGGY;
    }

    /** One reading of the server's speed (the HUD task computes it once per run). */
    record Speed(double tps, double mspt, double p95) {
        static Speed of(Server server) {
            double tps = Math.min(20.0, server.getTPS()[0]);
            long[] times = server.getTickTimes(); // nanoseconds, the last 100 ticks (a copy)
            double p95 = 0;
            if (times.length > 0) {
                long[] sorted = Arrays.copyOf(times, times.length);
                Arrays.sort(sorted);
                p95 = sorted[Math.min(sorted.length - 1, (int) Math.ceil(sorted.length * 0.95) - 1)] / 1e6;
            }
            return new Speed(tps, server.getAverageTickTime(), p95);
        }

        Health health() {
            return Msg.health(tps, p95);
        }

        /** The boss bar's fill: the share of the 50 ms budget a tick uses (0..1). */
        float load() {
            return (float) Math.max(0.0, Math.min(1.0, mspt / BUDGET_MS));
        }
    }

    static String tps(double tps, boolean decimals) {
        return String.format(Locale.ROOT, decimals ? "%.1f/20 TPS" : "%.0f/20 TPS", tps);
    }

    static String tick(double mspt, boolean decimals) {
        return String.format(Locale.ROOT, decimals ? "%.1f of 50 ms" : "%.0f of 50 ms", mspt);
    }

    /** v1.4.1: green from FPS_FAIR, yellow from FPS_POOR, red below (the Worker's fair / poor verdicts). */
    static NamedTextColor fpsColor(int fps) {
        return fps >= FPS_FAIR ? NamedTextColor.GREEN : fps >= FPS_POOR ? NamedTextColor.YELLOW : NamedTextColor.RED;
    }

    static String fps(int fps) {
        return fps + " fps";
    }

    static TextColor tickColor(double mspt) {
        return mspt < SMOOTH_P95_MS ? NamedTextColor.GREEN : mspt < BUDGET_MS ? NamedTextColor.YELLOW : NamedTextColor.RED;
    }

    static Component hover(Component c, String text) {
        return c.hoverEvent(HoverEvent.showText(Component.text(text)));
    }

    static Component healthWord(Health h) {
        return hover(Component.text(h.word, h.color), HEALTH_HOVER);
    }

    static Component tpsText(double tps, boolean decimals) {
        return hover(Component.text(tps(tps, decimals), Texts.tpsColor(tps)), TPS_HOVER);
    }

    static Component tickText(double mspt, boolean decimals) {
        return hover(Component.text(tick(mspt, decimals), tickColor(mspt)), MSPT_HOVER);
    }

    static Component pingText(int ms) {
        return hover(Component.text(ms + " ms", Texts.pingColor(ms)), PING_HOVER);
    }
}
