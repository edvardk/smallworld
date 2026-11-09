package io.github.edvardk.smallworld.client;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * The smallworld channels' byte layout (client/mod/WIRE.md), with no Minecraft or Bukkit types so the server
 * plugin can use the same code on the raw plugin-message bytes.
 *
 * <p>Fields are VarInts (Minecraft's: 7 bits per byte, least significant group first, high bit = more, at most 5
 * bytes; v1.5's coordinates use the same encoding for negative numbers, two's complement in 5 bytes) and UTFs (a
 * VarInt byte length, then that many UTF-8 bytes). Decoders ignore trailing bytes, so a newer peer may append
 * fields without breaking an older one (v1.5: the hello's {@code capabilities}).
 */
public final class Wire {
    /** Bumped only on an incompatible change; new fields are appended instead. */
    public static final int PROTOCOL = 1;

    /** The longest UTF field either side writes or accepts, in bytes. */
    public static final int MAX_UTF_BYTES = 64;

    public static final int MIN_INTERVAL = 2;
    public static final int MAX_INTERVAL = 60;
    public static final int DEFAULT_INTERVAL = 5;
    public static final int MAX_FPS = 10_000;

    /** {@code smallworld:config} flag: draw the FPS next to the top HUD bar. */
    public static final int FLAG_HUD = 1;

    /** v1.5 {@code smallworld:hello} capability: the mod draws the compass itself (smallworld:compass/friends). */
    public static final int CAP_COMPASS = 1;

    /** v1.5 {@code smallworld:compass} flag: the mod draws the compass (else it draws nothing). */
    public static final int COMPASS_DRAW = 1;

    /** v1.5: {@code smallworld:friends} coordinates are in 1/16 block. */
    public static final int FIXED = 16;

    /** v1.5: the most friends one {@code smallworld:friends} carries (writers cut, readers refuse more). */
    public static final int MAX_FRIENDS = 100;

    private Wire() {}

    /**
     * {@code smallworld:hello}, client to server, once per join. v1.5 appended {@code capabilities} (bit 0:
     * {@link #CAP_COMPASS}); a hello without it (mod 1.0.0) decodes as 0, no capabilities.
     */
    public record Hello(int protocol, String modVersion, String tier, String pack, int capabilities) {
        public boolean compass() {
            return (capabilities & CAP_COMPASS) != 0;
        }
    }

    /** {@code smallworld:fps}, client to server, every interval. */
    public record Fps(int fps) {}

    /** {@code smallworld:config}, server to client, any time. */
    public record Config(int protocol, int intervalSeconds, int flags) {
        public boolean hud() {
            return (flags & FLAG_HUD) != 0;
        }

        /** The interval the client uses: clamped to 2..60 s. */
        public int clampedInterval() {
            return Math.max(MIN_INTERVAL, Math.min(MAX_INTERVAL, intervalSeconds));
        }
    }

    /**
     * v1.5 {@code smallworld:compass}, server to client, whenever it changes: whether the mod draws the compass
     * (flags bit 0, {@link #COMPASS_DRAW}; mc-brand then shows no boss-bar compass to this player), the server's
     * compass settings and its four branding colours (RGB).
     */
    public record Compass(int flags, int maxRange, int rounding, int heightThreshold, int text, int accent,
            int secondary, int subtle) {
        public boolean draw() {
            return (flags & COMPASS_DRAW) != 0;
        }
    }

    /**
     * v1.5: one other player in {@code smallworld:friends}. {@code dimension} is "" in the viewer's own dimension
     * (then x, y, z are their position in 1/16 block, {@link #FIXED}), else their dimension's key (x, y, z are 0).
     */
    public record Friend(String name, String dimension, int x, int y, int z) {
        public boolean here() {
            return dimension.isEmpty();
        }
    }

    /**
     * v1.5 {@code smallworld:friends}, server to client, rate-limited: who the viewer's compass shows, exactly the
     * players mc-brand's own compass would (same visibility, same range), and the viewer's dimension they are
     * relative to (a client that has already changed dimension ignores the positions until the next message).
     */
    public record Friends(String dimension, List<Friend> friends) {
        public Friends {
            friends = List.copyOf(friends);
        }
    }

    public static byte[] encode(Hello h) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writeVarInt(out, h.protocol());
        writeUtf(out, h.modVersion());
        writeUtf(out, h.tier());
        writeUtf(out, h.pack());
        writeVarInt(out, h.capabilities());
        return out.toByteArray();
    }

    public static byte[] encode(Compass c) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        for (int v : new int[] {c.flags(), c.maxRange(), c.rounding(), c.heightThreshold(), c.text(), c.accent(),
                c.secondary(), c.subtle()}) {
            writeVarInt(out, v);
        }
        return out.toByteArray();
    }

    public static byte[] encode(Friends f) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writeUtf(out, f.dimension());
        int n = Math.min(MAX_FRIENDS, f.friends().size());
        writeVarInt(out, n);
        for (Friend p : f.friends().subList(0, n)) {
            writeUtf(out, p.name());
            writeUtf(out, p.dimension());
            writeVarInt(out, p.x());
            writeVarInt(out, p.y());
            writeVarInt(out, p.z());
        }
        return out.toByteArray();
    }

    /** A coordinate in blocks as {@code smallworld:friends} carries it (1/16 block, rounded). */
    public static int fixed(double blocks) {
        return (int) Math.max(Integer.MIN_VALUE, Math.min(Integer.MAX_VALUE, Math.round(blocks * FIXED)));
    }

    public static byte[] encode(Fps f) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writeVarInt(out, Math.max(0, Math.min(MAX_FPS, f.fps())));
        return out.toByteArray();
    }

    public static byte[] encode(Config c) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writeVarInt(out, c.protocol());
        writeVarInt(out, c.intervalSeconds());
        writeVarInt(out, c.flags());
        return out.toByteArray();
    }

    public static Hello decodeHello(byte[] data) {
        Reader r = new Reader(data);
        return new Hello(r.varInt(), r.utf(), r.utf(), r.utf(), r.more() ? r.varInt() : 0);
    }

    public static Compass decodeCompass(byte[] data) {
        Reader r = new Reader(data);
        return new Compass(r.varInt(), r.varInt(), r.varInt(), r.varInt(), r.varInt(), r.varInt(), r.varInt(),
                r.varInt());
    }

    public static Friends decodeFriends(byte[] data) {
        Reader r = new Reader(data);
        String dimension = r.utf();
        int n = r.varInt();
        if (n < 0 || n > MAX_FRIENDS) {
            throw new IllegalArgumentException("bad friend count " + n);
        }
        List<Friend> friends = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            friends.add(new Friend(r.utf(), r.utf(), r.varInt(), r.varInt(), r.varInt()));
        }
        return new Friends(dimension, friends);
    }

    public static Fps decodeFps(byte[] data) {
        return new Fps(new Reader(data).varInt());
    }

    public static Config decodeConfig(byte[] data) {
        Reader r = new Reader(data);
        return new Config(r.varInt(), r.varInt(), r.varInt());
    }

    /** Cuts a string to at most MAX_UTF_BYTES of UTF-8 without splitting a character. */
    public static String fit(String s) {
        String v = s == null ? "" : s;
        while (v.getBytes(StandardCharsets.UTF_8).length > MAX_UTF_BYTES) {
            v = v.substring(0, v.offsetByCodePoints(v.length(), -1));
        }
        return v;
    }

    static void writeVarInt(ByteArrayOutputStream out, int value) {
        int v = value;
        while ((v & ~0x7F) != 0) {
            out.write((v & 0x7F) | 0x80);
            v >>>= 7;
        }
        out.write(v);
    }

    static void writeUtf(ByteArrayOutputStream out, String s) {
        byte[] b = fit(s).getBytes(StandardCharsets.UTF_8);
        writeVarInt(out, b.length);
        out.write(b, 0, b.length);
    }

    private static final class Reader {
        private final byte[] data;
        private int pos;

        Reader(byte[] data) {
            this.data = data;
        }

        boolean more() {
            return pos < data.length;
        }

        int varInt() {
            int value = 0;
            for (int i = 0; i < 5; i++) {
                if (pos >= data.length) {
                    throw new IllegalArgumentException("truncated VarInt");
                }
                int b = data[pos++] & 0xFF;
                value |= (b & 0x7F) << (7 * i);
                if ((b & 0x80) == 0) {
                    return value;
                }
            }
            throw new IllegalArgumentException("VarInt longer than 5 bytes");
        }

        String utf() {
            int n = varInt();
            if (n < 0 || n > MAX_UTF_BYTES || pos + n > data.length) {
                throw new IllegalArgumentException("bad UTF length " + n);
            }
            String s = new String(data, pos, n, StandardCharsets.UTF_8);
            pos += n;
            return s;
        }
    }
}
