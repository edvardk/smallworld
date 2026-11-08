package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.block.BlockFace;
import org.bukkit.entity.Entity;
import org.bukkit.entity.ItemFrame;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.hanging.HangingBreakEvent;
import org.bukkit.event.player.PlayerInteractAtEntityEvent;
import org.bukkit.event.player.PlayerInteractEntityEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.MapMeta;
import org.bukkit.map.MapCanvas;
import org.bukkit.map.MapCursor;
import org.bukkit.map.MapCursorCollection;
import org.bukkit.map.MapRenderer;
import org.bukkit.map.MapView;
import org.bukkit.persistence.PersistentDataType;

/**
 * v1.4: the spawn map room, server.yml {@code branding.map_room}: a wall of item frames holding maps of the
 * pre-rendered world ({@link PreRender}), refreshed when the nightly render's generation changes.
 *
 * <ul>
 *   <li><b>Placement:</b> {@code width x height} frames; the bottom centre frame at the world spawn + offset
 *       ({@code at: spawn}, read when the frames spawn) or at x/y/z; facing the viewer ({@code facing}). The wall
 *       reads like a map whatever it faces: north at the top, west on the viewer's left.
 *   <li><b>Content:</b> {@code view: spawn} = vanilla's map grid at {@code scale} around the spawn, one map per
 *       frame (the tiles' pixels exactly); {@code view: border} = the square of the world border (radius from
 *       server.yml) fitted to the whole wall, sampled from the coarsest tile scale that is fine enough.
 *   <li><b>Entities:</b> like the holograms, fixed + invulnerable item frames that are never saved with the world
 *       ({@code setPersistent(false)}) and tagged in their PDC ({@code mcbrand:map_room} = config hash + slot): a
 *       sweep at start-up and on every EntitiesLoadEvent removes anything of ours that isn't the current wall (an
 *       older config, a crash), and a periodic check spawns what is missing in loaded chunks. So re-creating never
 *       duplicates and a changed config moves the wall.
 *   <li><b>Maps:</b> one MapView per slot, created once and remembered in the world's PDC ({@code
 *       mcbrand:map_room_maps}), locked (vanilla never draws on them), with a non-contextual renderer painting the
 *       tile pixels; Paper sends the pixels once and then only what changed.
 *   <li><b>Protected:</b> breaking, damaging, rotating or taking from the frames is cancelled for everyone.
 * </ul>
 */
final class MapRoom implements Listener {
    static final int ENSURE_TICKS = 100;
    static final int CURSOR_TICKS = 20;
    static final int RETRY_TICKS = 20;

    record Spec(boolean enabled, String world, boolean spawn, int dx, int dy, int dz, int x, int y, int z,
            String facing, int width, int height, String view, int scale, int radius, boolean cursors) {
        /** What makes a wall "the same": a changed hash sweeps the old frames. */
        String hash() {
            return Integer.toHexString(Arrays.hashCode(new Object[] {world, spawn, dx, dy, dz, x, y, z, facing,
                width, height, view, scale, radius}));
        }
    }

    private final BrandPlugin plugin;
    private final Spec spec;
    private final PreRender prerender;
    private final Compass compass;
    private final NamespacedKey tag;
    private final NamespacedKey idsKey;
    private final String hash;
    private ItemFrame[] frames = new ItemFrame[0];
    private MapView[] views = new MapView[0];
    long spawned;
    long swept;
    long repaints;

    MapRoom(BrandPlugin plugin, Spec spec, PreRender prerender, Compass compass) {
        this.plugin = plugin;
        this.spec = spec;
        this.prerender = prerender;
        this.compass = compass;
        this.tag = new NamespacedKey(plugin, "map_room");
        this.idsKey = new NamespacedKey(plugin, "map_room_maps");
        this.hash = spec.hash();
    }

    // --- geometry (pure; printed by WayfindingFixtures, recomputed in tests/test_wayfinding.py) -------------------

    /** The viewer's right, as (dx, dz), for a wall whose maps face {@code facing}. */
    static int[] right(String facing) {
        return switch (facing) {
            case "north" -> new int[] {-1, 0};
            case "east" -> new int[] {0, -1};
            case "west" -> new int[] {0, 1};
            default -> new int[] {1, 0}; // south
        };
    }

    static BlockFace face(String facing) {
        return switch (facing) {
            case "north" -> BlockFace.NORTH;
            case "east" -> BlockFace.EAST;
            case "west" -> BlockFace.WEST;
            default -> BlockFace.SOUTH;
        };
    }

    /**
     * Slot i (row-major from the top-left as the viewer sees it): {col, rowFromTop, blockX, blockY, blockZ} for a
     * wall whose bottom centre frame is at (ax, ay, az).
     */
    static int[] slot(int i, int ax, int ay, int az, String facing, int width, int height) {
        int col = i % width;
        int top = i / width;
        int[] r = right(facing);
        int off = col - (width - 1) / 2;
        return new int[] {col, top, ax + r[0] * off, ay + (height - 1 - top), az + r[1] * off};
    }

    /** Vanilla's map grid centre for a coordinate at scale s: floor((c + 64) / w) * w + w / 2 - 64, w = 128 * 2^s. */
    static int gridCentre(int c, int scale) {
        int w = 128 << scale;
        return Math.floorDiv(c + 64, w) * w + w / 2 - 64;
    }

    /** view: spawn, the centre of the map in column col / row (from the top) of the wall. */
    static int[] mapCentre(int sx, int sz, int scale, int width, int height, int col, int top) {
        int w = 128 << scale;
        return new int[] {gridCentre(sx, scale) + (col - (width - 1) / 2) * w,
            gridCentre(sz, scale) + (top - (height - 1) / 2) * w};
    }

    /** view: border, {blocks per wall pixel, source tile scale} that fit the square [-r, r] on the wall. */
    static int[] borderScale(int radius, int width, int height) {
        int px = 128 * Math.min(width, height);
        int bpp = Math.max(1, (2 * radius + px - 1) / px);
        int scale = Math.min(4, 31 - Integer.numberOfLeadingZeros(bpp));
        return new int[] {bpp, scale};
    }

    /** view: border, the world block (x or z) under wall pixel {@code p} of {@code size} pixels along that axis. */
    static int borderBlock(int p, int size, int bpp) {
        return (p - size / 2) * bpp + bpp / 2;
    }

    // --- life cycle -----------------------------------------------------------------------------------------------

    World world() {
        return Bukkit.getWorld(NamespacedKey.fromString(spec.world()));
    }

    /** The wall's bottom centre block (the spawn is read now: /setworldspawn moves the wall on the next check). */
    int[] anchor(World w) {
        if (!spec.spawn()) {
            return new int[] {spec.x(), spec.y(), spec.z()};
        }
        Location sp = w.getSpawnLocation();
        return new int[] {sp.getBlockX() + spec.dx(), sp.getBlockY() + spec.dy(), sp.getBlockZ() + spec.dz()};
    }

    /** Start-up: maps set up, every frame of ours in loaded chunks swept, the wall spawned where loaded. */
    void start() {
        for (World w : Bukkit.getWorlds()) {
            for (ItemFrame f : w.getEntitiesByClass(ItemFrame.class)) {
                if (ours(f)) {
                    f.remove();
                    swept++;
                }
            }
        }
        World w = world();
        if (w == null) {
            return;
        }
        int n = spec.width() * spec.height();
        frames = new ItemFrame[n];
        views = new MapView[n];
        int[] ids = w.getPersistentDataContainer().getOrDefault(idsKey, PersistentDataType.INTEGER_ARRAY, new int[0]);
        int[] keep = Arrays.copyOf(ids, Math.max(ids.length, n));
        for (int i = 0; i < n; i++) {
            MapView v = i < ids.length ? Bukkit.getMap(ids[i]) : null;
            if (v == null) {
                v = Bukkit.createMap(w);
                keep[i] = v.getId();
            }
            for (MapRenderer r : new ArrayList<>(v.getRenderers())) {
                v.removeRenderer(r);
            }
            int[] sl = slot(i, 0, 0, 0, spec.facing(), spec.width(), spec.height());
            v.addRenderer(new Painter(sl[0], sl[1]));
            v.setLocked(true);
            v.setTrackingPosition(false);
            v.setUnlimitedTracking(false);
            v.setWorld(w);
            views[i] = v;
        }
        w.getPersistentDataContainer().set(idsKey, PersistentDataType.INTEGER_ARRAY, keep);
        ensure();
    }

    void stop() {
        for (ItemFrame f : frames) {
            if (f != null && f.isValid()) {
                f.remove();
            }
        }
        Arrays.fill(frames, null);
    }

    boolean ours(Entity e) {
        return e instanceof ItemFrame && e.getPersistentDataContainer().has(tag, PersistentDataType.STRING);
    }

    private boolean current(Entity e) {
        for (ItemFrame f : frames) {
            if (e.equals(f)) {
                return true;
            }
        }
        return false;
    }

    /** EntitiesLoadEvent: anything of ours there that isn't the current wall goes; then spawn what is missing. */
    void loaded(List<Entity> entities) {
        for (Entity e : entities) {
            if (ours(e) && !current(e)) {
                e.remove();
                swept++;
            }
        }
        ensure();
    }

    /** Spawns every missing frame whose chunk is loaded (never twice: the live references are kept). */
    void ensure() {
        World w = world();
        if (w == null || views.length == 0) {
            return;
        }
        int[] a = anchor(w);
        BlockFace face = face(spec.facing());
        for (int i = 0; i < frames.length; i++) {
            int[] sl = slot(i, a[0], a[1], a[2], spec.facing(), spec.width(), spec.height());
            ItemFrame f = frames[i];
            if (f != null && f.isValid()) {
                Location at = f.getLocation();
                if (at.getBlockX() == sl[2] && at.getBlockY() == sl[3] && at.getBlockZ() == sl[4]) {
                    continue;
                }
                f.remove(); // the spawn moved
                swept++;
            }
            if (!w.isChunkLoaded(sl[2] >> 4, sl[4] >> 4)) {
                continue;
            }
            ItemStack item = new ItemStack(Material.FILLED_MAP);
            MapMeta meta = (MapMeta) item.getItemMeta();
            meta.setMapView(views[i]);
            item.setItemMeta(meta);
            String id = hash + ":" + i;
            frames[i] = w.spawn(new Location(w, sl[2] + 0.5, sl[3] + 0.5, sl[4] + 0.5), ItemFrame.class, fr -> {
                fr.setFacingDirection(face, true);
                fr.setItem(item, false);
                fr.setFixed(true);
                fr.setInvulnerable(true);
                fr.setPersistent(false);
                fr.setSilent(true);
                fr.getPersistentDataContainer().set(tag, PersistentDataType.STRING, id);
            });
            spawned++;
        }
    }

    int live() {
        int n = 0;
        for (ItemFrame f : frames) {
            if (f != null && f.isValid()) {
                n++;
            }
        }
        return n;
    }

    // --- painting --------------------------------------------------------------------------------------------------

    /** The packed id for pixel (k1, l1) of the map in wall column col / row top; 0 = none, -1 = tile not read yet. */
    int source(World w, int col, int top, int k1, int l1) {
        if (spec.view().equals("spawn")) {
            Location sp = w.getSpawnLocation();
            int[] c = mapCentre(sp.getBlockX(), sp.getBlockZ(), spec.scale(), spec.width(), spec.height(), col, top);
            return prerender.pixel(w, spec.scale(), PreRender.grid(c[0], spec.scale(), k1),
                    PreRender.grid(c[1], spec.scale(), l1));
        }
        int[] b = borderScale(spec.radius(), spec.width(), spec.height());
        int x = borderBlock(col * 128 + k1, spec.width() * 128, b[0]);
        int z = borderBlock(top * 128 + l1, spec.height() * 128, b[0]);
        if (Math.abs(x) > spec.radius() || Math.abs(z) > spec.radius()) {
            return 0;
        }
        return prerender.pixel(w, b[1], x >> b[1], z >> b[1]);
    }

    /** One frame's map: repainted when the render's generation changes (or until every tile has been read). */
    final class Painter extends MapRenderer {
        private final int col;
        private final int top;
        private long painted = Long.MIN_VALUE;
        private boolean complete;
        private int lastTry = Wayfinding.NEVER;
        private int lastCursors = Wayfinding.NEVER;

        Painter(int col, int top) {
            super(false);
            this.col = col;
            this.top = top;
        }

        @Override
        public boolean isExplorerMap() {
            return false;
        }

        @Override
        public void render(MapView view, MapCanvas canvas, Player player) {
            World w = world();
            if (w == null) {
                return;
            }
            int now = Bukkit.getCurrentTick();
            long gen = prerender.generation(w);
            if ((gen != painted || !complete) && Wayfinding.due(now, lastTry, RETRY_TICKS)) {
                lastTry = now;
                complete = true;
                for (int l1 = 0; l1 < 128; l1++) {
                    for (int k1 = 0; k1 < 128; k1++) {
                        int v = gen < 0 ? 0 : source(w, col, top, k1, l1);
                        if (v < 0) {
                            complete = false;
                            continue;
                        }
                        canvas.setPixelColor(k1, l1, prerender.color(v));
                    }
                }
                painted = gen;
                repaints++;
            }
            if (spec.cursors() && Wayfinding.due(now, lastCursors, CURSOR_TICKS)) {
                lastCursors = now;
                canvas.setCursors(cursors(w));
            }
        }

        private MapCursorCollection cursors(World w) {
            MapCursorCollection out = new MapCursorCollection();
            for (Player o : w.getPlayers()) {
                if (compass.hidden(o) || !XaeroMap.shown(o) || o.isInvisible()
                        || o.getGameMode() == org.bukkit.GameMode.SPECTATOR) {
                    continue;
                }
                Location l = o.getLocation();
                double px;
                double pz;
                if (spec.view().equals("spawn")) {
                    Location sp = w.getSpawnLocation();
                    int[] c = mapCentre(sp.getBlockX(), sp.getBlockZ(), spec.scale(), spec.width(), spec.height(),
                            col, top);
                    px = (l.getX() - c[0]) / (1 << spec.scale()) + 64;
                    pz = (l.getZ() - c[1]) / (1 << spec.scale()) + 64;
                } else {
                    int bpp = borderScale(spec.radius(), spec.width(), spec.height())[0];
                    px = l.getX() / bpp + spec.width() * 64 - col * 128;
                    pz = l.getZ() / bpp + spec.height() * 64 - top * 128;
                }
                if (px < 0 || px >= 128 || pz < 0 || pz >= 128) {
                    continue;
                }
                Wayfinding.Cursor c = Wayfinding.cursor(px, pz, l.getYaw(), 64, 64, 0);
                out.addCursor(new MapCursor((byte) c.x(), (byte) c.y(), (byte) c.direction(), MapCursor.Type.PLAYER,
                        true, Component.text(o.getName())));
            }
            return out;
        }
    }

    // --- protection -----------------------------------------------------------------------------------------------

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onBreak(HangingBreakEvent e) {
        if (ours(e.getEntity())) {
            e.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent e) {
        if (ours(e.getEntity())) {
            e.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onInteract(PlayerInteractEntityEvent e) {
        if (ours(e.getRightClicked())) {
            e.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onInteractAt(PlayerInteractAtEntityEvent e) {
        if (ours(e.getRightClicked())) {
            e.setCancelled(true);
        }
    }

    /** Where the wall's bottom centre frame is ("-" without its world), for tests/e2e/brand_check.py. */
    String at() {
        World w = world();
        if (w == null) {
            return "-";
        }
        int[] a = anchor(w);
        return a[0] + " " + a[1] + " " + a[2];
    }

    /** For /mcbrand selftest. */
    String selftest() {
        return String.format(Locale.ROOT, "map room %s %dx%d %s at [%s] live %d/%d spawned %d swept %d repaints %d",
                spec.enabled() ? "on" : "off", spec.width(), spec.height(), spec.view(), at(), live(), frames.length,
                spawned, swept, repaints);
    }

    /** Fixed inputs for the fixtures (tests/test_wayfinding.py). */
    static List<Object> fixtures() {
        List<Object> out = new ArrayList<>();
        for (String facing : new String[] {"south", "north", "east", "west"}) {
            List<Object> slots = new ArrayList<>();
            for (int i = 0; i < 12; i++) {
                slots.add(Arrays.stream(slot(i, 10, 64, -20, facing, 4, 3)).boxed().toList());
            }
            out.add(List.of(facing, slots));
        }
        List<Object> centres = new ArrayList<>();
        for (int[] c : new int[][] {{0, 0, 0}, {-65, 63, 0}, {100, -3000, 3}, {5000, 5000, 4}}) {
            centres.add(List.of(c[0], c[1], c[2], gridCentre(c[0], c[2]), gridCentre(c[1], c[2]),
                    Arrays.stream(mapCentre(c[0], c[1], c[2], 4, 3, 0, 0)).boxed().toList(),
                    Arrays.stream(mapCentre(c[0], c[1], c[2], 4, 3, 3, 2)).boxed().toList()));
        }
        out.add(List.of("centres", centres));
        List<Object> border = new ArrayList<>();
        for (int[] b : new int[][] {{5000, 3, 3}, {5000, 4, 3}, {300, 3, 3}, {30000, 2, 2}, {100, 1, 1}}) {
            int[] sc = borderScale(b[0], b[1], b[2]);
            border.add(List.of(b[0], b[1], b[2], sc[0], sc[1], borderBlock(0, b[1] * 128, sc[0]),
                    borderBlock(b[1] * 128 - 1, b[1] * 128, sc[0])));
        }
        out.add(List.of("border", border));
        return out;
    }
}
