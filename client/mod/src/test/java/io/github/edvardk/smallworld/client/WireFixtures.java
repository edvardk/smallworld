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
                new Wire.Hello(1, "1.0.0", "medium", "26.3-0123456789ab"),
                new Wire.Hello(1, "1.0.0", "none", ""),
                new Wire.Hello(1, "1.0.0-ü€", "maximum", longText))) {
            byte[] b = Wire.encode(h);
            Wire.Hello back = Wire.decodeHello(b);
            check(back.equals(new Wire.Hello(h.protocol(), Wire.fit(h.modVersion()), Wire.fit(h.tier()),
                    Wire.fit(h.pack()))), "hello round trip");
            cases.add(entry("hello", b, Map.of("protocol", back.protocol(), "mod_version", back.modVersion(),
                    "tier", back.tier(), "pack", back.pack()), Map.of("protocol", h.protocol(),
                    "mod_version", h.modVersion(), "tier", h.tier(), "pack", h.pack())));
        }
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
        // trailing bytes (a newer peer's appended fields) are ignored
        byte[] longer = HexFormat.of().parseHex("0105010203");
        check(Wire.decodeConfig(longer).equals(new Wire.Config(1, 5, 1)), "trailing bytes");
        cases.add(entry("config", longer, Map.of("protocol", 1, "interval_seconds", 5, "flags", 1, "hud", true,
                "clamped_interval", 5), null));
        List<String> bad = new ArrayList<>();
        for (String[] c : new String[][] {
                {"fps", ""}, {"fps", "8080808080"}, {"config", "0105"}, {"hello", "0105312e302e30"},
                {"hello", "0141" + "78".repeat(65) + "0000"}}) {
            byte[] b = HexFormat.of().parseHex(c[1]);
            try {
                switch (c[0]) {
                    case "fps" -> Wire.decodeFps(b);
                    case "config" -> Wire.decodeConfig(b);
                    default -> Wire.decodeHello(b);
                }
                throw new AssertionError("accepted malformed " + c[0] + " " + c[1]);
            } catch (IllegalArgumentException expected) {
                bad.add("{\"channel\": " + json(c[0]) + ", \"hex\": " + json(c[1]) + "}");
            }
        }
        StringBuilder out = new StringBuilder();
        out.append("{\n  \"protocol\": ").append(Wire.PROTOCOL).append(",\n  \"max_utf_bytes\": ")
                .append(Wire.MAX_UTF_BYTES).append(",\n  \"cases\": [\n    ")
                .append(String.join(",\n    ", cases)).append("\n  ],\n  \"malformed\": [\n    ")
                .append(String.join(",\n    ", bad)).append("\n  ]\n}\n");
        System.out.print(out);
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
