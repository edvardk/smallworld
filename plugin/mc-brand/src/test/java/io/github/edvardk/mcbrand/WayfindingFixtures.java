package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Prints plugin/mc-brand/src/test/wayfinding-fixtures.json: {@link Wayfinding}'s answers for fixed inputs (bearings,
 * distances, the compass strip, the map cursors, the client-mod precedence), after checking the strip's own
 * invariants. scripts/build_plugin.py check runs it with the pinned JDK and compares the output with the committed
 * file; tests/test_wayfinding.py recomputes the arithmetic in Python and checks the layout rules on the same data.
 */
public final class WayfindingFixtures {
    private WayfindingFixtures() {}

    public static void main(String[] args) {
        Map<String, Object> out = new LinkedHashMap<>();
        List<Object> bearings = new ArrayList<>();
        for (double[] d : new double[][] {{0, -10}, {10, 0}, {0, 10}, {-10, 0}, {5, -5}, {-3, 7}, {0.0001, 100}}) {
            bearings.add(List.of(d[0], d[1], CompassStrip.bearing(d[0], d[1])));
        }
        out.put("bearing", bearings);
        List<Object> facings = new ArrayList<>();
        for (double yaw : new double[] {0, 90, -90, 180, -180, 359.5, -725}) {
            facings.add(List.of(yaw, CompassStrip.facing(yaw)));
        }
        out.put("facing", facings);
        List<Object> rel = new ArrayList<>();
        for (double[] r : new double[][] {{10, 350}, {350, 10}, {180, 0}, {0, 180}, {90, 90}, {271, 90}}) {
            rel.add(List.of(r[0], r[1], CompassStrip.relative(r[0], r[1])));
        }
        out.put("relative", rel);
        List<Object> dist = new ArrayList<>();
        for (Object[] d : new Object[][] {{123.4, 5}, {127.5, 5}, {2.0, 5}, {0.4, 1}, {1499.0, 100}, {7.5, 1}}) {
            dist.add(List.of(d[0], d[1], CompassStrip.distance((double) d[0], (int) d[1])));
        }
        out.put("distance", dist);
        List<Object> arrows = new ArrayList<>();
        for (double dy : new double[] {0, 8, 8.5, -9, 30}) {
            arrows.add(List.of(dy, 8, CompassStrip.arrow(dy, 8)));
        }
        out.put("arrow", arrows);
        List<Object> dims = new ArrayList<>();
        for (String k : new String[] {"minecraft:the_nether", "minecraft:the_end", "minecraft:overworld", "x:mine"}) {
            dims.add(List.of(k, CompassStrip.dimensionName(k)));
        }
        out.put("dimension", dims);
        List<Object> widths = new ArrayList<>();
        for (String w : new String[] {"N", "Alex", "illi", "Steve_01", " ", "·"}) {
            widths.add(List.of(w, CompassStrip.width(w)));
        }
        out.put("width", widths);
        out.put("strip_constants", Map.of("width_px", CompassStrip.WIDTH_PX, "half_fov", CompassStrip.HALF_FOV,
                "focus_deg", CompassStrip.FOCUS_DEG, "min_spacing_px", CompassStrip.MIN_SPACING_PX));

        CompassStrip.Config cfg = new CompassStrip.Config(1000, 5, 8);
        List<Object> strips = new ArrayList<>();
        strips.add(strip("facing north, Alex ahead and above, Sam east, Kim in the Nether", 180, List.of(
                new CompassStrip.Target("Alex", 3, 20, -120, null),
                new CompassStrip.Target("Sam", 200, 0, 0, null),
                new CompassStrip.Target("Kim", 0, 0, 0, "minecraft:the_nether")), cfg));
        strips.add(strip("facing east, nobody", -90, List.of(), cfg));
        strips.add(strip("facing south-west, one out of range, one behind, one faced below", 45, List.of(
                new CompassStrip.Target("Far", -5000, 0, 5000, null),
                new CompassStrip.Target("Behind", 50, 0, -50, null),
                new CompassStrip.Target("Low", -30, -40, 31, null)), cfg));
        strips.add(strip("two near the centre: the closer to it is faced", 180, List.of(
                new CompassStrip.Target("Ann", -10, 0, -100, null),
                new CompassStrip.Target("Bob", 4, 0, -100, null),
                new CompassStrip.Target("Cy", 0, 0, 0, "minecraft:the_end"),
                new CompassStrip.Target("Dee", 0, 0, 0, "minecraft:the_end"),
                new CompassStrip.Target("Eve", 0, 0, 0, "minecraft:the_nether")), cfg));
        out.put("strip", strips);

        // v1.5: the client compass. The same scenes in absolute positions (the viewer at VIEWER in the overworld):
        // the boss bar's strip (Wayfinding.targets, as Compass.render) and the smallworld:friends payload the mod
        // gets (Wayfinding.friends). The mod's WireFixtures decodes these bytes and prints its own strip:
        // tests/test_client_mod.py checks the bytes and the layouts are the same.
        List<Object> client = new ArrayList<>();
        client.add(clientScene("facing north, Alex ahead and above, Sam east, Kim in the Nether", 180, List.of(
                new CompassStrip.Target("Alex", 3, 20, -120, null),
                new CompassStrip.Target("Sam", 200, 0, 0, null),
                new CompassStrip.Target("Kim", 0, 0, 0, "minecraft:the_nether")), cfg));
        client.add(clientScene("facing east, nobody", -90, List.of(), cfg));
        client.add(clientScene("facing south-west, one out of range, one behind, one faced below", 45, List.of(
                new CompassStrip.Target("Far", -5000, 0, 5000, null),
                new CompassStrip.Target("Behind", 50, 0, -50, null),
                new CompassStrip.Target("Low", -30, -40, 31, null)), cfg));
        client.add(clientScene("two near the centre: the closer to it is faced", 180, List.of(
                new CompassStrip.Target("Ann", -10, 0, -100, null),
                new CompassStrip.Target("Bob", 4, 0, -100, null),
                new CompassStrip.Target("Cy", 0, 0, 0, "minecraft:the_end"),
                new CompassStrip.Target("Dee", 0, 0, 0, "minecraft:the_end"),
                new CompassStrip.Target("Eve", 0, 0, 0, "minecraft:the_nether")), cfg));
        client.add(clientScene("a sixteenth of a block: 1/16 positions, turned a little", 183.37, List.of(
                new CompassStrip.Target("Pip", 40.0625, -9.5, -60.9375, null)), new CompassStrip.Config(100, 1, 8)));
        out.put("client_compass", client);
        Map<String, Object> modes = new LinkedHashMap<>();
        for (boolean want : new boolean[] {false, true}) {
            for (boolean capable : new boolean[] {false, true}) {
                for (boolean pending : new boolean[] {false, true}) {
                    modes.put(want + "/" + capable + "/" + pending,
                            Wayfinding.compassMode(want, capable, pending).name());
                }
            }
        }
        out.put("compass_mode", modes);
        Map<String, Object> caps = new LinkedHashMap<>();
        for (Integer c : new Integer[] {null, 0, 1, 3, 2}) {
            for (boolean ours : new boolean[] {false, true}) {
                for (boolean channels : new boolean[] {false, true}) {
                    caps.put(c + "/" + ours + "/" + channels, Wayfinding.capability(c, ours, channels).name());
                }
            }
        }
        out.put("capability", caps);
        out.put("selftest_friends", java.util.HexFormat.of().formatHex(Wire.encode(Wayfinding.friends(
                "minecraft:overworld", 0.5, 0.5, 1000, Wayfinding.SELFTEST_SCENE))));

        List<Object> cursors = new ArrayList<>();
        for (double[] c : new double[][] {{10, -20, 90, 0, 0, 0}, {1000, 0, 0, 0, 0, 0}, {100, 100, -45, 64, 64, 1},
                {-200, 30, 179, 0, 0, 2}, {63.9, -63.9, -1, 0, 0, 0}}) {
            Wayfinding.Cursor cur = Wayfinding.cursor(c[0], c[1], c[2], (int) c[3], (int) c[4], (int) c[5]);
            cursors.add(List.of(List.of(c[0], c[1], c[2], (int) c[3], (int) c[4], (int) c[5]),
                    List.of(cur.x(), cur.y(), cur.direction(), cur.onMap())));
        }
        out.put("cursor", cursors);
        List<Object> snaps = new ArrayList<>();
        for (double[] sn : new double[][] {{-0.5, 0}, {13.9, 2}, {-17, 3}, {64, 4}}) {
            snaps.add(List.of(sn[0], (int) sn[1], Wayfinding.snap(sn[0], (int) sn[1])));
        }
        out.put("snap", snaps);
        // v1.4: the pocket map's centre snaps to 2 pixels (Wayfinding.snap(x, scale + 1)); the pre-render window
        // and shading; the map room's geometry
        List<Object> centres = new ArrayList<>();
        for (double[] c : new double[][] {{-0.5, 0}, {13.9, 0}, {13.9, 2}, {-17, 3}, {1000.2, 4}, {-1, 1}}) {
            centres.add(List.of(c[0], (int) c[1], Wayfinding.snap(c[0], (int) c[1] + 1)));
        }
        out.put("pocket_centre", centres);
        out.put("due", List.of(Wayfinding.due(5000, Wayfinding.NEVER, 20), Wayfinding.due(5000, 4981, 20),
                Wayfinding.due(5000, 4980, 20), Wayfinding.due(0, Wayfinding.NEVER, 100)));
        out.put("prerender", PreRender.fixtures());
        out.put("map_room", MapRoom.fixtures());

        List<Object> mods = new ArrayList<>();
        for (boolean ours : new boolean[] {false, true}) {
            for (boolean xaero : new boolean[] {false, true}) {
                mods.add(List.of(ours, xaero, Wayfinding.mods(ours, xaero), Wayfinding.compassByDefault(ours, xaero),
                        Wayfinding.suggestMap(ours, xaero)));
            }
        }
        out.put("precedence", mods);
        List<Object> channels = new ArrayList<>();
        for (String ch : new String[] {"smallworld:config", "xaerominimap:main", "xaeroworldmap:main",
                "xaerobetterpvp:main", "xaerolib:main", "fabric:registry/sync", "minecraft:brand", "notxaero:main"}) {
            channels.add(List.of(ch, Wayfinding.isOurs(ch), Wayfinding.isXaero(ch)));
        }
        out.put("channels", channels);
        System.out.print(json(out, 0));
        System.out.print("\n");
    }

    private static Map<String, Object> strip(String what, double yaw, List<CompassStrip.Target> targets,
            CompassStrip.Config cfg) {
        CompassStrip.Strip st = CompassStrip.strip(yaw, targets, cfg);
        int strip = 0;
        for (CompassStrip.Segment seg : st.segments()) {
            if (seg.kind() != CompassStrip.Kind.AWAY) {
                strip += CompassStrip.lineWidth(List.of(seg));
            }
        }
        int away = st.segments().stream().filter(x -> x.kind() == CompassStrip.Kind.AWAY)
                .mapToInt(x -> CompassStrip.width(x.text())).sum();
        // invariant: the strip proper (after the mirror gap) spans WIDTH_PX within the gap rounding (<= 3 px)
        check(Math.abs(strip - away - CompassStrip.WIDTH_PX) <= 3 * (st.placed().size() + 2), "strip width " + what);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("what", what);
        m.put("yaw", yaw);
        List<Object> ts = new ArrayList<>();
        for (CompassStrip.Target t : targets) {
            ts.add(List.of(t.name(), t.dx(), t.dy(), t.dz(), t.dimension() == null ? "" : t.dimension()));
        }
        m.put("targets", ts);
        m.put("config", List.of(cfg.maxRange(), cfg.rounding(), cfg.heightThreshold()));
        m.put("focus", st.focus() == null ? "" : st.focus());
        List<Object> placed = new ArrayList<>();
        for (CompassStrip.Placed p : st.placed()) {
            placed.add(List.of(p.kind().name(), p.text(), p.left(), p.width()));
        }
        m.put("placed", placed);
        List<Object> segs = new ArrayList<>();
        for (CompassStrip.Segment seg : st.segments()) {
            segs.add(List.of(seg.kind().name(), seg.text()));
        }
        m.put("segments", segs);
        m.put("line_width", CompassStrip.lineWidth(st.segments()));
        m.put("plain", st.plain());
        return m;
    }

    static final double[] VIEWER = {100.25, 64, -50.75};
    static final String WORLD = "minecraft:overworld";

    private static Map<String, Object> clientScene(String what, double yaw, List<CompassStrip.Target> rel,
            CompassStrip.Config cfg) {
        List<Wayfinding.Seen> seen = new ArrayList<>();
        for (CompassStrip.Target t : rel) {
            seen.add(t.dimension() == null
                    ? new Wayfinding.Seen(t.name(), WORLD, VIEWER[0] + t.dx(), VIEWER[1] + t.dy(), VIEWER[2] + t.dz())
                    : new Wayfinding.Seen(t.name(), t.dimension(), 1, 2, 3));
        }
        CompassStrip.Strip st = CompassStrip.strip(yaw,
                Wayfinding.targets(WORLD, VIEWER[0], VIEWER[1], VIEWER[2], seen), cfg);
        Wire.Friends friends = Wayfinding.friends(WORLD, VIEWER[0], VIEWER[2], cfg.maxRange(), seen);
        check(Wire.decodeFriends(Wire.encode(friends)).equals(friends), "friends round trip " + what);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("what", what);
        m.put("yaw", yaw);
        m.put("viewer", List.of(VIEWER[0], VIEWER[1], VIEWER[2], WORLD));
        m.put("config", List.of(cfg.maxRange(), cfg.rounding(), cfg.heightThreshold()));
        m.put("friends", java.util.HexFormat.of().formatHex(Wire.encode(friends)));
        m.put("focus", st.focus() == null ? "" : st.focus());
        List<Object> placed = new ArrayList<>();
        for (CompassStrip.Placed p : st.placed()) {
            placed.add(List.of(p.kind().name(), p.text(), p.left(), p.width()));
        }
        m.put("placed", placed);
        m.put("plain", st.plain());
        return m;
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            throw new AssertionError(what);
        }
    }

    private static String json(Object o, int indent) {
        String pad = "  ".repeat(indent + 1);
        if (o instanceof Map<?, ?> m) {
            List<String> parts = new ArrayList<>();
            List<String> keys = m.keySet().stream().map(String::valueOf).sorted().toList();
            for (String k : keys) {
                parts.add(pad + quote(k) + ": " + json(m.get(k), indent + 1));
            }
            return "{\n" + String.join(",\n", parts) + "\n" + "  ".repeat(indent) + "}";
        }
        if (o instanceof List<?> l) {
            boolean flat = l.stream().noneMatch(x -> x instanceof Map<?, ?> || x instanceof List<?> y
                    && y.stream().anyMatch(z -> z instanceof List<?> || z instanceof Map<?, ?>));
            List<String> parts = l.stream().map(x -> json(x, indent + 1)).toList();
            if (flat) {
                return "[" + String.join(", ", parts) + "]";
            }
            return "[\n" + pad + String.join(",\n" + pad, parts) + "\n" + "  ".repeat(indent) + "]";
        }
        if (o instanceof String s) {
            return quote(s);
        }
        if (o instanceof Double d) {
            return String.format(Locale.ROOT, "%.6f", d);
        }
        return String.valueOf(o);
    }

    private static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') {
                b.append('\\').append(c);
            } else if (c < 0x20 || c > 0x7e) {
                b.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            } else {
                b.append(c);
            }
        }
        return b.append('"').toString();
    }
}
