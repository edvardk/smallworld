package io.github.edvardk.mcbrand;

import java.awt.Color;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.bukkit.Bukkit;
import org.bukkit.World;

/**
 * v1.4: the world map pre-render's tiles (rust/smallworld-map, nightly; docs/reference.md "World map"), read for
 * the pocket map ({@link PocketMap}) and the spawn map room ({@link MapRoom}).
 *
 * <ul>
 *   <li><b>Where:</b> {@code <prerender_dir>/<ns>/<dim>/s<scale>/<tx>.<tz>.swt}, 512 x 512 packed colour ids
 *       ({@code colour * 4 + brightness}, 0 = no data) behind a 32-byte header; never for the Nether.
 *   <li><b>Trust:</b> the directory belongs to the renderer's user ({@code mcmap}); a tile is accepted only with the
 *       exact size, magic, scale, reserved field and the tx/tz/scale of its file name, else it counts as missing. A
 *       bad tile can only paint pixels.
 *   <li><b>Identity:</b> {@code meta.json}'s {@code format} must be 1 and its {@code world} (the world folder's
 *       st_dev/st_ino when the render ran) must equal the folder's now ({@code unix:dev}/{@code unix:ino}): after a
 *       world switch there is no pre-render until the next night's run.
 *   <li><b>Cache:</b> at most {@link #MAX_TILES} decoded tiles (256 KB each: 4 MB), least recently used out; dropped
 *       when meta.json's {@code generation} changes (checked at most every {@link #META_MS} ms, only when a window is
 *       wanted). Every file read runs off the main thread: a miss schedules the read and answers "not yet" (the
 *       callers fall back to vanilla and ask again).
 *   <li><b>Colours:</b> {@code map-colors.tsv} (the pinned jar's MapColor RGB, scripts/map_palette.py), shaded as
 *       vanilla does: {@code channel * {180, 220, 255, 135}[b] / 255}.
 * </ul>
 */
final class PreRender {
    static final int TILE = 512;
    static final int HEADER = 32;
    static final int SIZE = HEADER + TILE * TILE;
    static final int MAX_TILES = 16;
    static final long META_MS = 60_000;
    static final int[] SHADE = {180, 220, 255, 135};
    private static final byte[] MAGIC = "SWMAPT\0\1".getBytes(StandardCharsets.ISO_8859_1);
    private static final byte[] NONE = new byte[0];
    private static final Pattern FORMAT = Pattern.compile("\"format\"\\s*:\\s*(\\d+)");
    private static final Pattern GENERATION = Pattern.compile("\"generation\"\\s*:\\s*(\\d+)");
    private static final Pattern WORLD = Pattern.compile(
            "\"world\"\\s*:\\s*\\{\\s*\"dev\"\\s*:\\s*(\\d+)\\s*,\\s*\"ino\"\\s*:\\s*(\\d+)\\s*}");

    record Key(String dim, int scale, int tx, int tz) {}

    /** What meta.json said, and whether it fits the world on disk now. */
    private static final class Meta {
        volatile long generation = -1;
        volatile boolean usable;
        volatile long checked; // 0 = never: the first request reads it
        volatile boolean refreshing;
    }

    private final BrandPlugin plugin;
    private final boolean enabled;
    private final Path dir;
    private final Path colorsFile;
    private volatile Color[] palette;
    private final Map<String, Meta> metas = new ConcurrentHashMap<>();
    private final Set<Key> loading = ConcurrentHashMap.newKeySet();
    private final LinkedHashMap<Key, byte[]> cache = new LinkedHashMap<>(32, 0.75f, true);
    long loads;
    long rejected;

    PreRender(BrandPlugin plugin, boolean enabled, String dir, String colorsFile) {
        this.plugin = plugin;
        this.enabled = enabled && !dir.isEmpty();
        this.dir = Path.of(dir.isEmpty() ? "/nonexistent" : dir);
        this.colorsFile = Path.of(colorsFile.isEmpty() ? "/nonexistent" : colorsFile);
    }

    boolean enabled() {
        return enabled;
    }

    /** The world's key as a directory: minecraft:the_end -> minecraft/the_end. */
    static String dimDir(String key) {
        return key.replace(':', '/');
    }

    /** Never the Nether (vanilla draws it as noise); and only with a usable meta.json and colours. */
    boolean usable(World w) {
        if (!enabled || w == null || w.getEnvironment() == World.Environment.NETHER) {
            return false;
        }
        Meta m = meta(w);
        return m.usable && palette != null;
    }

    /** The output generation of the world's dimension (-1: none / not usable). Refreshes meta.json when due. */
    long generation(World w) {
        if (!enabled || w == null || w.getEnvironment() == World.Environment.NETHER) {
            return -1;
        }
        Meta m = meta(w);
        return m.usable ? m.generation : -1;
    }

    /**
     * Is meta.json due for a re-read at {@code now} (ms) after the last one at {@code checked} (0 = never)? Never
     * compare with a sentinel by subtraction: {@code now - Long.MIN_VALUE} overflows to a negative number, which
     * kept the first read from ever happening (the v1.4 e2e found it).
     */
    static boolean due(long now, long checked) {
        return checked == 0 || now - checked >= META_MS;
    }

    private Meta meta(World w) {
        String key = w.getKey().toString();
        Meta m = metas.computeIfAbsent(key, k -> new Meta());
        long now = System.currentTimeMillis();
        if (!m.refreshing && due(now, m.checked)) {
            m.refreshing = true;
            Path world = Bukkit.getWorldContainer().toPath().resolve(Bukkit.getWorlds().get(0).getName());
            Bukkit.getScheduler().runTaskAsynchronously(plugin, () -> refresh(key, m, world));
        }
        return m;
    }

    /** Off the main thread: meta.json, the world folder's identity and (once) the colours. */
    private void refresh(String key, Meta m, Path world) {
        try {
            if (palette == null) {
                palette = loadColors(colorsFile);
            }
            String text = Files.readString(dir.resolve(dimDir(key)).resolve("meta.json"));
            Matcher f = FORMAT.matcher(text);
            Matcher g = GENERATION.matcher(text);
            Matcher id = WORLD.matcher(text);
            boolean ok = f.find() && f.group(1).equals("1") && g.find() && id.find()
                    && Long.parseLong(id.group(1)) == ((Number) Files.getAttribute(world, "unix:dev")).longValue()
                    && Long.parseLong(id.group(2)) == ((Number) Files.getAttribute(world, "unix:ino")).longValue();
            long gen = ok ? Long.parseLong(g.group(1)) : -1;
            if (gen != m.generation || ok != m.usable) {
                drop(key);
            }
            m.generation = gen;
            m.usable = ok;
        } catch (IOException | RuntimeException e) {
            if (m.usable) {
                drop(key);
            }
            m.usable = false;
            m.generation = -1;
        } finally {
            m.checked = System.currentTimeMillis();
            m.refreshing = false;
        }
    }

    private synchronized void drop(String dim) {
        cache.keySet().removeIf(k -> k.dim().equals(dim));
    }

    /** The colours file: {@code id TAB rrggbb} per line (# comments), -> Color per packed id (null = transparent). */
    static Color[] loadColors(Path file) throws IOException {
        Map<Integer, Integer> base = new HashMap<>();
        for (String line : Files.readAllLines(file)) {
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            String[] f = line.split("\t");
            base.put(Integer.parseInt(f[0].strip()), Integer.parseInt(f[1].strip(), 16));
        }
        Color[] out = new Color[256];
        for (int v = 4; v < 256; v++) {
            Integer rgb = base.get(v >> 2);
            if (rgb != null) {
                out[v] = shade(rgb, v & 3);
            }
        }
        return out;
    }

    /** MapColor.calculateARGBColor: each channel * {180, 220, 255, 135}[brightness] / 255. */
    static Color shade(int rgb, int brightness) {
        int m = SHADE[brightness];
        return new Color((rgb >> 16 & 255) * m / 255, (rgb >> 8 & 255) * m / 255, (rgb & 255) * m / 255);
    }

    /** The colour of a packed id, null for 0..3 (transparent) or an unknown colour. */
    Color color(int v) {
        Color[] p = palette;
        return p == null || v < 4 || v > 255 ? null : p[v];
    }

    /** Tile coordinate of a pixel grid coordinate (floor division by 512). */
    static int tileOf(int g) {
        return Math.floorDiv(g, TILE);
    }

    /** Pixel grid coordinate of canvas column k for a map centred on {@code c} at scale s (Java int division). */
    static int grid(int c, int scale, int k) {
        return c / (1 << scale) + k - 64;
    }

    /**
     * One pixel (gx, gz) at scale s: its packed id, 0 without data, -1 while its tile is being read (asked again
     * later). Main thread.
     */
    int pixel(World w, int scale, int gx, int gz) {
        byte[] t = tile(w.getKey().toString(), scale, tileOf(gx), tileOf(gz));
        if (t == null) {
            return -1;
        }
        return t == NONE ? 0 : t[HEADER + Math.floorMod(gz, TILE) * TILE + Math.floorMod(gx, TILE)] & 255;
    }

    /**
     * The 128 x 128 packed ids a map centred on (cx, cz) at scale s shows (cx, cz multiples of 2 * 2^s), or null
     * while a tile is still being read or the dimension has no usable pre-render. At most 2 x 2 tiles.
     */
    byte[] window(World w, int scale, int cx, int cz) {
        if (!usable(w)) {
            return null;
        }
        String dim = w.getKey().toString();
        int gx0 = grid(cx, scale, 0);
        int gz0 = grid(cz, scale, 0);
        byte[] out = new byte[128 * 128];
        boolean complete = true;
        for (int tz = tileOf(gz0); tz <= tileOf(gz0 + 127); tz++) {
            for (int tx = tileOf(gx0); tx <= tileOf(gx0 + 127); tx++) {
                byte[] t = tile(dim, scale, tx, tz);
                if (t == null) {
                    complete = false;
                    continue;
                }
                if (t == NONE) {
                    continue;
                }
                for (int l1 = 0; l1 < 128; l1++) {
                    int gz = gz0 + l1;
                    if (tileOf(gz) != tz) {
                        continue;
                    }
                    int x0 = Math.max(gx0, tx * TILE);
                    int x1 = Math.min(gx0 + 127, tx * TILE + TILE - 1);
                    if (x0 > x1) {
                        continue;
                    }
                    System.arraycopy(t, HEADER + Math.floorMod(gz, TILE) * TILE + Math.floorMod(x0, TILE), out,
                            l1 * 128 + (x0 - gx0), x1 - x0 + 1);
                }
            }
        }
        return complete ? out : null;
    }

    /** A cached tile, NONE when it doesn't exist, null while it is being read (the read is scheduled). */
    private byte[] tile(String dim, int scale, int tx, int tz) {
        Key k = new Key(dim, scale, tx, tz);
        synchronized (this) {
            byte[] t = cache.get(k);
            if (t != null) {
                return t;
            }
        }
        if (loading.add(k)) {
            Path file = dir.resolve(dimDir(dim)).resolve("s" + scale).resolve(tx + "." + tz + ".swt");
            Bukkit.getScheduler().runTaskAsynchronously(plugin, () -> {
                byte[] data = read(file, scale, tx, tz);
                synchronized (this) {
                    cache.put(k, data);
                    while (cache.size() > MAX_TILES) {
                        cache.remove(cache.keySet().iterator().next());
                    }
                }
                loading.remove(k);
            });
        }
        return null;
    }

    /** Off the main thread: a tile file, checked (anything else counts as missing). */
    private byte[] read(Path file, int scale, int tx, int tz) {
        byte[] data;
        try {
            if (!Files.isRegularFile(file)) {
                return NONE;
            }
            data = Files.readAllBytes(file);
        } catch (IOException e) {
            return NONE;
        }
        loads++;
        if (!valid(data, scale, tx, tz)) {
            rejected++;
            return NONE;
        }
        return data;
    }

    /** docs/reference.md "Tile file": exact size, magic, scale, reserved 0, tx/tz of the name. */
    static boolean valid(byte[] data, int scale, int tx, int tz) {
        if (data.length != SIZE) {
            return false;
        }
        for (int i = 0; i < MAGIC.length; i++) {
            if (data[i] != MAGIC[i]) {
                return false;
            }
        }
        ByteBuffer b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
        return data[8] == scale && b.getShort(10) == 0 && b.getInt(12) == tx && b.getInt(16) == tz;
    }

    synchronized int cached() {
        return (int) cache.values().stream().filter(t -> t != NONE).count();
    }

    /** For /mcbrand selftest: per rendered dimension its generation, and the cache. */
    String selftest() {
        StringBuilder dims = new StringBuilder();
        for (World w : Bukkit.getWorlds()) {
            if (w.getEnvironment() == World.Environment.NETHER) {
                continue;
            }
            long g = generation(w);
            dims.append(dims.isEmpty() ? "" : " ").append(w.getKey().getKey()).append('=')
                    .append(g < 0 ? "none" : "gen" + g);
        }
        return "prerender " + (enabled ? "on" : "off") + " [" + dims + "] colours " + (palette != null)
                + " tiles cached " + cached() + " read " + loads + " rejected " + rejected;
    }

    /** Fixed inputs for the fixtures (tests/test_wayfinding.py): window coordinates and shading. */
    static List<Object> fixtures() {
        return List.of(grid(-64, 0, 0), grid(128, 1, 127), grid(-4096, 4, 64), tileOf(-1), tileOf(511),
                tileOf(512), shade(0x7fb238, 0).getRGB() & 0xffffff, shade(0x7fb238, 3).getRGB() & 0xffffff,
                due(1_762_579_467_000L, 0), due(1_762_579_467_000L, 1_762_579_467_000L - META_MS + 1),
                due(1_762_579_467_000L, 1_762_579_467_000L - META_MS));
    }
}
