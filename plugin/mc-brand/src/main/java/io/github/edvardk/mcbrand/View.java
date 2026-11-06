package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataType;

/**
 * v1.1: {@code /view <n>}, a player's own view distance (Paper's per-player API, verified in the pinned
 * purpur-26.3 jar: CraftPlayer.setViewDistance/setSendViewDistance -> moonrise's per-player ViewDistanceHolder;
 * -1 = the world's own distance again). Every player may ask for up to {@code view.max} (default 16) chunks.
 *
 * <ul>
 *   <li>Lowering (n &lt;= the server's distance) always applies at once.
 *   <li>Raising applies only while the server's health is Smooth (Msg.health: p95 tick &lt; 35 ms and TPS &gt;=
 *       19.5) and the guard isn't holding raises back; otherwise the wish is kept and applied later.
 *   <li>The guard (every {@code view.check_seconds}, main thread, one health reading): {@link #LOWER_AFTER}
 *       non-Smooth readings in a row put every raised player back to the server's distance (their wish stays);
 *       after {@link #RAISE_AFTER} Smooth readings in a row, ONE waiting player per check is raised again, so
 *       the extra chunk loading never comes as one burst.
 *   <li>The wish is kept in the player's PDC (per UUID): it survives restarts and applies again after a join
 *       (through the guard, one player per check).
 * </ul>
 */
final class View {
    static final int LOWER_AFTER = 2;
    static final int RAISE_AFTER = 6;
    static final int MIN = 2;

    private final Settings s;
    private final NamespacedKey key;
    private final Map<UUID, Integer> raised = new HashMap<>(); // applied distance > the server's
    private final Guard guard = new Guard();
    long lowered;
    long raisedCount;
    long checks;

    View(BrandPlugin plugin, Settings settings) {
        this.s = settings;
        this.key = new NamespacedKey(plugin, "view");
    }

    static int serverDistance(Player p) {
        return p.getWorld().getViewDistance();
    }

    Integer wish(Player p) {
        return p.getPersistentDataContainer().get(key, PersistentDataType.INTEGER);
    }

    /** What /view n may do: "lower" (always), "raise" (now), "wait" (kept for later) or "invalid". */
    static String decide(int n, int server, int max, boolean smooth, boolean holding) {
        if (n < MIN || n > Math.max(max, server)) {
            return "invalid";
        }
        if (n <= server) {
            return "lower";
        }
        return smooth && !holding ? "raise" : "wait";
    }

    /** The guard's state machine, one health reading per step: "lower" (everyone raised goes back), "raise"
     * (one waiting player may go up) or "" (nothing). */
    static final class Guard {
        private int bad;
        private int good;
        boolean holding; // lowered: raises wait for RAISE_AFTER Smooth readings in a row

        String step(boolean smooth) {
            if (!smooth) {
                good = 0;
                if (++bad >= LOWER_AFTER) {
                    holding = true;
                    return "lower";
                }
                return "";
            }
            bad = 0;
            if (holding && ++good < RAISE_AFTER) {
                return "";
            }
            holding = false;
            good = 0;
            return "raise";
        }
    }

    private void apply(Player p, int n) {
        int server = serverDistance(p);
        if (n <= server) {
            p.setViewDistance(-1); // the world's own distance (FeatureHooks.setViewDistance: -1 = reset)
            p.setSendViewDistance(-1);
            raised.remove(p.getUniqueId());
        } else {
            p.setViewDistance(n);
            p.setSendViewDistance(n);
            raised.put(p.getUniqueId(), n);
        }
    }

    void command(Player p, String[] args) {
        if (!s.viewEnabled()) {
            p.sendMessage(Component.text("/view is off on this server.", s.subtle()));
            return;
        }
        int server = serverDistance(p);
        int max = Math.max(s.viewMax(), server);
        if (args.length == 0) {
            Integer w = wish(p);
            p.sendMessage(Component.text("Your view distance: " + p.getViewDistance() + " chunks" + (w != null
                    && w > p.getViewDistance() ? " (you asked for " + w + ": it comes once the server runs smoothly)" : "")
                    + ". The server's is " + server + "; you may choose " + MIN + "-" + max
                    + " with /view <number>, or /view reset.", s.subtle()));
            return;
        }
        int n;
        if (args[0].equalsIgnoreCase("reset")) {
            n = server;
        } else {
            try {
                n = Integer.parseInt(args[0]);
            } catch (NumberFormatException e) {
                n = -1;
            }
        }
        Msg.Speed speed = Msg.Speed.of(Bukkit.getServer());
        String d = decide(n, server, max, speed.health() == Msg.Health.SMOOTH, guard.holding);
        switch (d) {
            case "invalid" -> p.sendMessage(Component.text("Usage: /view <" + MIN + "-" + max + "> or /view reset",
                    s.subtle()));
            case "lower" -> {
                if (n == server) {
                    p.getPersistentDataContainer().remove(key);
                } else {
                    p.getPersistentDataContainer().set(key, PersistentDataType.INTEGER, n);
                }
                apply(p, n);
                p.sendMessage(Component.text("View distance " + n + " chunks.", s.text()));
            }
            case "raise" -> {
                p.getPersistentDataContainer().set(key, PersistentDataType.INTEGER, n);
                apply(p, n);
                raisedCount++;
                p.sendMessage(Component.text("View distance " + n + " chunks. Your own render distance (Options > "
                        + "Video Settings) must be at least " + n + " to see it. If the server gets busy it goes "
                        + "back to " + server + " for a while by itself.", s.text()));
            }
            default -> {
                p.getPersistentDataContainer().set(key, PersistentDataType.INTEGER, n);
                p.sendMessage(Component.text("Saved: you'll get " + n + " chunks as soon as the server runs "
                        + "smoothly (it's busy right now).", s.subtle()));
            }
        }
    }

    /** At join: a wish at or below the server's distance applies now; a raise waits for the guard. */
    void join(Player p) {
        Integer w = wish(p);
        if (s.viewEnabled() && w != null && w < serverDistance(p)) {
            apply(p, Math.max(MIN, w));
        }
    }

    void forget(UUID id) {
        raised.remove(id);
    }

    /** The guard (main thread, every check_seconds). */
    void check() {
        if (!s.viewEnabled()) {
            return;
        }
        boolean smooth = Msg.Speed.of(Bukkit.getServer()).health() == Msg.Health.SMOOTH;
        checks++;
        switch (guard.step(smooth)) {
            case "lower" -> {
                for (UUID id : new ArrayList<>(raised.keySet())) {
                    Player p = Bukkit.getPlayer(id);
                    if (p == null) {
                        raised.remove(id);
                        continue;
                    }
                    int server = serverDistance(p);
                    apply(p, server);
                    lowered++;
                    p.sendMessage(Component.text("The server is busy, so your view distance is back to " + server
                            + " for now. It goes up again by itself once things are smooth.", s.subtle()));
                }
            }
            case "raise" -> raiseOne();
            default -> { }
        }
    }

    private void raiseOne() {
        List<? extends Player> online = List.copyOf(Bukkit.getOnlinePlayers());
        for (Player p : online) {
            Integer w = wish(p);
            if (w == null || raised.containsKey(p.getUniqueId())) {
                continue;
            }
            int n = Math.min(w, Math.max(s.viewMax(), serverDistance(p)));
            if (n > serverDistance(p)) {
                apply(p, n);
                raisedCount++;
                p.sendMessage(Component.text("Your view distance is " + n + " chunks now.", s.subtle()));
                return;
            }
        }
    }

    int raisedNow() {
        return raised.size();
    }

    /** For /mcbrand selftest (tests/e2e/brand_check.py): the rules on fixed inputs. */
    static String selftest() {
        Guard g = new Guard();
        StringBuilder steps = new StringBuilder();
        // smooth, one bad, two bad (lower), then Smooth readings: the raise comes on the RAISE_AFTER-th
        for (boolean smooth : new boolean[] {true, false, false, true, true, true, true, true, true}) {
            String r = g.step(smooth);
            steps.append(r.isEmpty() ? "-" : r.substring(0, 1));
        }
        return "view decide " + decide(16, 12, 16, true, false) + "/" + decide(16, 12, 16, false, false) + "/"
                + decide(8, 12, 16, false, true) + "/" + decide(17, 12, 16, true, false) + ", guard " + steps;
    }
}
