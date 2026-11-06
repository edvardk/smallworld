package io.github.edvardk.smallworld.client;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * The smallworld channels' byte layout (client/mod/WIRE.md), with no Minecraft or Bukkit types so the server
 * plugin can use the same code on the raw plugin-message bytes.
 *
 * <p>Fields are VarInts (Minecraft's: 7 bits per byte, least significant group first, high bit = more, at most 5
 * bytes) and UTFs (a VarInt byte length, then that many UTF-8 bytes). Decoders ignore trailing bytes, so a newer
 * peer may append fields without breaking an older one.
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

    private Wire() {}

    /** {@code smallworld:hello}, client to server, once per join. */
    public record Hello(int protocol, String modVersion, String tier, String pack) {}

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

    public static byte[] encode(Hello h) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writeVarInt(out, h.protocol());
        writeUtf(out, h.modVersion());
        writeUtf(out, h.tier());
        writeUtf(out, h.pack());
        return out.toByteArray();
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
        return new Hello(r.varInt(), r.utf(), r.utf(), r.utf());
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
