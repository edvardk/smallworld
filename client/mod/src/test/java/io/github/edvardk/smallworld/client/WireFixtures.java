package io.github.edvardk.smallworld.client;

import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Prints client/mod/src/test/wire-fixtures.json: Wire's bytes for a set of payloads, after checking that each one
 * decodes back and that malformed input is refused. scripts/build_client_mod.py check runs it (with the pinned JDK)
 * and compares the output with the committed file; tests/test_client_mod.py decodes the same bytes in Python.
 */
public final class WireFixtures {
    private WireFixtures() {}

    public static void main(String[] args) {
        List<String> cases = new ArrayList<>();
        String longText = "x".repeat(70);
        for (Wire.Hello h : List.of(
                new Wire.Hello(1, "1.0.0", "medium", "26.3-0123456789ab", 0),
                new Wire.Hello(1, "1.0.0", "none", "", 0),
                new Wire.Hello(1, "1.0.0-ü€", "maximum", longText, 0),
                new Wire.Hello(1, "1.1.0", "medium", "26.3-0123456789ab", Wire.CAP_COMPASS),
                new Wire.Hello(1, "1.1.0", "none", "", 300))) {
            byte[] b = Wire.encode(h);
            Wire.Hello back = Wire.decodeHello(b);
            check(back.equals(new Wire.Hello(h.protocol(), Wire.fit(h.modVersion()), Wire.fit(h.tier()),
                    Wire.fit(h.pack()), h.capabilities())), "hello round trip");
            cases.add(entry("hello", b, Map.of("protocol", back.protocol(), "mod_version", back.modVersion(),
                    "tier", back.tier(), "pack", back.pack(), "capabilities", back.capabilities(), "compass",
                    back.compass()), Map.of("protocol", h.protocol(), "mod_version", h.modVersion(), "tier",
                    h.tier(), "pack", h.pack(), "capabilities", h.capabilities())));
        }
        // a 1.0.0 mod's hello (no capabilities field) decodes as capabilities 0: the boss-bar compass
        byte[] old = HexFormat.of().parseHex("0105312e302e30066d656469756d1132362e332d303132333435363738396162");
        check(Wire.decodeHello(old).capabilities() == 0, "1.0.0 hello");
        cases.add(entry("hello", old, Map.of("protocol", 1, "mod_version", "1.0.0", "tier", "medium", "pack",
                "26.3-0123456789ab", "capabilities", 0, "compass", false), null));
        for (int fps : new int[] {0, 1, 127, 128, 300, 16_384, 20_000, -5}) {
            byte[] b = Wire.encode(new Wire.Fps(fps));
            Wire.Fps back = Wire.decodeFps(b);
            check(back.fps() == Math.max(0, Math.min(Wire.MAX_FPS, fps)), "fps round trip");
            cases.add(entry("fps", b, Map.of("fps", back.fps()), Map.of("fps", fps)));
        }
        for (Wire.Config c : List.of(
                new Wire.Config(1, 5, Wire.FLAG_HUD),
                new Wire.Config(1, 2, 0),
                new Wire.Config(1, 1, Wire.FLAG_HUD),
                new Wire.Config(1, 300, 3))) {
            byte[] b = Wire.encode(c);
            Wire.Config back = Wire.decodeConfig(b);
            check(back.equals(c), "config round trip");
            cases.add(entry("config", b, Map.of("protocol", back.protocol(), "interval_seconds",
                    back.intervalSeconds(), "flags", back.flags(), "hud", back.hud(), "clamped_interval",
                    back.clampedInterval()), Map.of("protocol", c.protocol(), "interval_seconds",
                    c.intervalSeconds(), "flags", c.flags())));
        }
        for (Wire.Compass c : List.of(
                new Wire.Compass(Wire.COMPASS_DRAW, 1000, 5, 8, 0xFFFFFF, 0xFFAA00, 0xAAAAAA, 0x777777),
                new Wire.Compass(0, 16, 1, 2, 0, 0, 0, 0))) {
            byte[] b = Wire.encode(c);
            Wire.Compass back = Wire.decodeCompass(b);
            check(back.equals(c), "compass round trip");
            cases.add(entry("compass", b, Map.of("flags", back.flags(), "draw", back.draw(), "max_range",
                    back.maxRange(), "rounding", back.rounding(), "height_threshold", back.heightThreshold(),
                    "colors", back.text() + "," + back.accent() + "," + back.secondary() + "," + back.subtle()),
                    Map.of("flags", c.flags(), "max_range", c.maxRange(), "rounding", c.rounding(),
                            "height_threshold", c.heightThreshold(), "colors", c.text() + "," + c.accent() + ","
                            + c.secondary() + "," + c.subtle())));
        }
        for (Wire.Friends f : List.of(
                new Wire.Friends("minecraft:overworld", List.of(
                        new Wire.Friend("Alex", "", Wire.fixed(3.5), Wire.fixed(84), Wire.fixed(-119.5)),
                        new Wire.Friend("Kim", "minecraft:the_nether", 0, 0, 0))),
                new Wire.Friends("minecraft:the_end", List.of()),
                new Wire.Friends("x:mine", List.of(new Wire.Friend("Edge", "", Wire.fixed(-29_999_984),
                        Wire.fixed(-64), Wire.fixed(29_999_984)))))) {
            byte[] b = Wire.encode(f);
            Wire.Friends back = Wire.decodeFriends(b);
            check(back.equals(f), "friends round trip");
            List<String> people = new ArrayList<>();
            for (Wire.Friend p : back.friends()) {
                people.add(p.name() + "@" + p.dimension() + ":" + p.x() + "," + p.y() + "," + p.z());
            }
            cases.add(entry("friends", b, Map.of("dimension", back.dimension(), "friends", String.join(";", people)),
                    null));
        }
        List<Wire.Friend> many = new ArrayList<>();
        for (int i = 0; i < Wire.MAX_FRIENDS + 5; i++) {
            many.add(new Wire.Friend("p" + i, "", i, 0, 0));
        }
        check(Wire.decodeFriends(Wire.encode(new Wire.Friends("w", many))).friends().size() == Wire.MAX_FRIENDS,
                "friends cut at MAX_FRIENDS");
        // trailing bytes (a newer peer's appended fields) are ignored
        byte[] longer = HexFormat.of().parseHex("0105010203");
        check(Wire.decodeConfig(longer).equals(new Wire.Config(1, 5, 1)), "trailing bytes");
        cases.add(entry("config", longer, Map.of("protocol", 1, "interval_seconds", 5, "flags", 1, "hud", true,
                "clamped_interval", 5), null));
        List<String> bad = new ArrayList<>();
        for (String[] c : new String[][] {
                {"fps", ""}, {"fps", "8080808080"}, {"config", "0105"}, {"hello", "0105312e302e30"},
                {"hello", "0141" + "78".repeat(65) + "0000"}, {"compass", "01e8070508000000"},
                {"friends", "0177" + "65"}, {"friends", "0177010161"}, {"friends", "00ffffffff0f"}}) {
            byte[] b = HexFormat.of().parseHex(c[1]);
            try {
                switch (c[0]) {
                    case "fps" -> Wire.decodeFps(b);
                    case "config" -> Wire.decodeConfig(b);
                    case "compass" -> Wire.decodeCompass(b);
                    case "friends" -> Wire.decodeFriends(b);
                    default -> Wire.decodeHello(b);
                }
                throw new AssertionError("accepted malformed " + c[0] + " " + c[1]);
            } catch (IllegalArgumentException expected) {
                bad.add("{\"channel\": " + json(c[0]) + ", \"hex\": " + json(c[1]) + "}");
            }
        }
        List<String> compass = compassCases();
        StringBuilder out = new StringBuilder();
        out.append("{\n  \"protocol\": ").append(Wire.PROTOCOL).append(",\n  \"max_utf_bytes\": ")
                .append(Wire.MAX_UTF_BYTES).append(",\n  \"cases\": [\n    ")
                .append(String.join(",\n    ", cases)).append("\n  ],\n  \"malformed\": [\n    ")
                .append(String.join(",\n    ", bad)).append("\n  ],\n  \"compass\": [\n    ")
                .append(String.join(",\n    ", compass)).append("\n  ]\n}\n");
        System.out.print(out);
    }

    /**
     * v1.5: mc-brand's smallworld:friends bytes for its fixture scenes (plugin/mc-brand/src/test/
     * wayfinding-fixtures.json "client_compass", copied here): what this mod draws from them, the local player at
     * VIEWER in the overworld with no player entities in sight. tests/test_client_mod.py checks that the layouts
     * equal the boss bar's for the same scenes; the last case is the same first scene just after a portal.
     */
    static final double[] VIEWER = {100.25, 64, -50.75};
    static final String[][] SCENES = {
                {"facing north, Alex ahead and above, Sam east, Kim in the Nether", "180.0", "1000", "5", "8",
                        "136d696e6563726166743a6f766572776f726c640304416c657800f40cc00ad4eaffff0f0353616d00c4258008d4f9ffff0f034b696d146d696e6563726166743a7468655f6e6574686572000000"},
                {"facing east, nobody", "-90.0", "1000", "5", "8",
                        "136d696e6563726166743a6f766572776f726c6400"},
                {"facing south-west, one out of range, one behind, one faced below", "45.0", "1000", "5", "8",
                        "136d696e6563726166743a6f766572776f726c6402034c6f7700e4088003c4fdffff0f06426568696e6400e4128008b4f3ffff0f"},
                {"two near the centre: the closer to it is faced", "180.0", "1000", "5", "8",
                        "136d696e6563726166743a6f766572776f726c640503426f6200840d800894edffff0f03416e6e00a40b800894edffff0f024379116d696e6563726166743a7468655f656e6400000003446565116d696e6563726166743a7468655f656e6400000003457665146d696e6563726166743a7468655f6e6574686572000000"},
                {"a sixteenth of a block: 1/16 positions, turned a little", "183.37", "100", "1", "8",
                        "136d696e6563726166743a6f766572776f726c64010350697000c511e80685f2ffff0f"}};

    private static List<String> compassCases() {
        List<String> out = new ArrayList<>();
        for (String[] sc : SCENES) {
            out.add(compassCase(sc, "minecraft:overworld"));
        }
        out.add(compassCase(SCENES[0], "minecraft:the_nether"));
        return out;
    }

    private static String compassCase(String[] sc, String dimension) {
        Wire.Friends friends = Wire.decodeFriends(HexFormat.of().parseHex(sc[5]));
        CompassStrip.Config cfg = CompassModel.config(new Wire.Compass(Wire.COMPASS_DRAW, Integer.parseInt(sc[2]),
                Integer.parseInt(sc[3]), Integer.parseInt(sc[4]), 0, 0, 0, 0));
        CompassStrip.Strip st = CompassStrip.strip(Double.parseDouble(sc[1]),
                CompassModel.targets(friends, dimension, VIEWER[0], VIEWER[1], VIEWER[2], name -> null), cfg);
        List<String> placed = new ArrayList<>();
        for (CompassStrip.Placed p : st.placed()) {
            check(Math.abs(p.exact() - p.left()) <= 0.5 + 1e-9, "exact is the unrounded left");
            placed.add("[" + json(p.kind().name()) + ", " + json(p.text()) + ", " + p.left() + ", " + p.width() + "]");
        }
        return "{\"what\": " + json(sc[0]) + ", \"dimension\": " + json(dimension) + ", \"friends\": "
                + json(sc[5]) + ", \"focus\": " + json(st.focus() == null ? "" : st.focus()) + ", \"placed\": ["
                + String.join(", ", placed) + "], \"plain\": " + json(st.plain()) + "}";
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            throw new AssertionError(what);
        }
    }

    private static String entry(String channel, byte[] b, Map<String, Object> decoded, Map<String, Object> input) {
        StringBuilder s = new StringBuilder("{\"channel\": ").append(json(channel));
        if (input != null) {
            s.append(", \"input\": ").append(object(input));
        }
        return s.append(", \"hex\": ").append(json(HexFormat.of().formatHex(b))).append(", \"decoded\": ")
                .append(object(decoded)).append('}').toString();
    }

    private static String object(Map<String, Object> m) {
        List<String> parts = new ArrayList<>();
        for (Map.Entry<String, Object> e : new TreeMap<>(m).entrySet()) {
            Object v = e.getValue();
            parts.add(json(e.getKey()) + ": " + (v instanceof String str ? json(str) : String.valueOf(v)));
        }
        return "{" + String.join(", ", parts) + "}";
    }

    private static String json(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') {
                b.append('\\').append(c);
            } else if (c < 0x20 || c > 0x7e) {
                b.append(String.format("\\u%04x", (int) c));
            } else {
                b.append(c);
            }
        }
        return b.append('"').toString();
    }
}
