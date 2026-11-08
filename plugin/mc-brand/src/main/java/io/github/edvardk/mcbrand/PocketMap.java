package io.github.edvardk.mcbrand;

import io.papermc.paper.datacomponent.DataComponentTypes;
import io.papermc.paper.datacomponent.item.MapId;
import java.awt.Color;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.entity.ItemFrame;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.inventory.ClickType;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.inventory.InventoryCloseEvent;
import org.bukkit.event.inventory.InventoryDragEvent;
import org.bukkit.event.inventory.InventoryType;
import org.bukkit.event.inventory.PrepareItemCraftEvent;
import org.bukkit.event.player.PlayerDropItemEvent;
import org.bukkit.event.player.PlayerInteractEntityEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.inventory.meta.MapMeta;
import org.bukkit.map.MapCanvas;
import org.bukkit.map.MapCursor;
import org.bukkit.map.MapCursorCollection;
import org.bukkit.map.MapRenderer;
import org.bukkit.map.MapView;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;

/**
 * v1.3: {@code /map}, a personal pocket map (server.yml {@code branding.pocket_map}; vanilla clients).
 *
 * <ul>
 *   <li><b>One map per player</b>: its MapView id lives in the player's PDC ({@code mcbrand:pocket_map_id}), so
 *       {@code /map} reuses it (no new map_N.dat per request); the item carries {@code mcbrand:pocket_map = owner
 *       uuid} in its PDC. {@code /map} removes every pocket map from the inventory and gives one fresh copy.
 *   <li><b>Drop-safe, no copies</b>: a dropped one vanishes (the item entity is removed; /map is free), it never
 *       drops on death, can't go into item frames, containers, bundles, the crafting grid (map cloning) or a
 *       cartography table; the join and /map sweep any stray copy and other players' maps.
 *   <li><b>Terrain is vanilla's</b>: the default renderer draws the map's colours, which vanilla fills while the map
 *       is held (main or off hand: the off hand is vanilla's minimap). While held, {@link Render} re-centres the
 *       view on the player about once a second (whole pixels of the scale, when the player is
 *       {@link #RECENTRE_PX}+ pixels off centre) and, because the stored colours belong to the old centre, draws
 *       the old picture shifted over them until vanilla has redrawn each pixel (the colour changed, or the pixel
 *       sat 20 ticks inside the held map's update circle: vanilla's 128-block radius, 64 in the Nether); unknown
 *       pixels show as unexplored.
 *   <li><b>Friends</b>: a cursor with their name for every other online player in the same dimension (not
 *       hidden by /compass hide, not a spectator or invisible), vanilla's arithmetic ({@link Wayfinding#cursor}):
 *       a pointer on the map, an off-map dot at the edge beyond it. Updated every {@link #CURSOR_TICKS} ticks
 *       while the map renders.
 *   <li>{@code /map zoom} cycles the scale (1, 2, 4, 8, 16 blocks per pixel); the picture starts over.
 *   <li>v1.4: <b>pre-rendered terrain</b> ({@link PreRender}, branding.pocket_map.prerender): where the picture
 *       has no pixel (after a re-centre, a zoom, a world change or at start-up where vanilla has none), the
 *       nightly render's pixel is painted instead of "unexplored", through the same overlay, so vanilla's live
 *       colour still takes over wherever it redraws. Centres snap to whole multiples of 2 pixels
 *       ({@code Wayfinding.snap(x, scale + 1)}): the tiles' dither parity is then vanilla's own. Never in the
 *       Nether; nothing until the tiles are read (off the main thread) or when they belong to another world.
 * </ul>
 *
 * <p>Cost: Paper renders a map only while it is in an inventory (at most every 5 ticks unless vanilla changed it);
 * the overlay scan (16 384 pixels) runs only after a re-centre, until every pixel is vanilla's again. Pixels go
 * through the Color calls (getBasePixelColor returns the palette's own Color objects; setPixelColor maps them back
 * through MapPalette's cache, null = transparent): the byte calls are deprecated for removal in the pinned API.
 */
final class PocketMap implements Listener {
    static final int RECENTRE_PX = 4;
    static final int RECENTRE_TICKS = 20;
    static final int CURSOR_TICKS = 10;
    static final int SETTLE_TICKS = 20;
    static final int SAMPLE_TICKS = 100;
    static final MapView.Scale[] SCALES = MapView.Scale.values();

    private final BrandPlugin plugin;
    private final Settings s;
    private final Compass compass;
    private final PreRender prerender;
    private final NamespacedKey tagKey;
    private final NamespacedKey idKey;
    private final Map<Integer, Render> renderers = new HashMap<>();
    long given;
    long removed;
    long recentres;
    long prerendered;
    long matched;
    long compared;

    PocketMap(BrandPlugin plugin, Settings settings, Compass compass, PreRender prerender) {
        this.plugin = plugin;
        this.s = settings;
        this.compass = compass;
        this.prerender = prerender;
        this.tagKey = new NamespacedKey(plugin, "pocket_map");
        this.idKey = new NamespacedKey(plugin, "pocket_map_id");
    }

    // --- the item --------------------------------------------------------------------------------------------------

    /** The owner tag of a pocket map item, or null for anything else. */
    String tag(ItemStack item) {
        if (item == null || item.getType() != Material.FILLED_MAP || !item.hasItemMeta()) {
            return null;
        }
        return item.getItemMeta().getPersistentDataContainer().get(tagKey, PersistentDataType.STRING);
    }

    boolean isPocketMap(ItemStack item) {
        return tag(item) != null;
    }

    Integer mapId(Player p) {
        return p.getPersistentDataContainer().get(idKey, PersistentDataType.INTEGER);
    }

    /** The player's map view (created once), with our renderer attached and in their world. */
    MapView view(Player p) {
        Integer id = mapId(p);
        MapView view = id == null ? null : Bukkit.getMap(id);
        if (view == null) {
            view = Bukkit.createMap(p.getWorld());
            view.setScale(MapView.Scale.CLOSEST);
            view.setCenterX(Wayfinding.snap(p.getX(), 1)); // v1.4: even pixels (scale 0 + 1)
            view.setCenterZ(Wayfinding.snap(p.getZ(), 1));
            p.getPersistentDataContainer().set(idKey, PersistentDataType.INTEGER, view.getId());
        }
        view.setTrackingPosition(true);
        view.setUnlimitedTracking(false);
        view.setLocked(false);
        if (!p.getWorld().equals(view.getWorld())) {
            view.setWorld(p.getWorld());
        }
        attach(view, p.getUniqueId());
        return view;
    }

    private void attach(MapView view, UUID owner) {
        Render r = renderers.computeIfAbsent(view.getId(), k -> new Render(owner));
        if (!view.getRenderers().contains(r)) {
            view.addRenderer(r);
        }
    }

    ItemStack create(Player p, MapView view) {
        ItemStack item = new ItemStack(Material.FILLED_MAP);
        MapMeta meta = (MapMeta) item.getItemMeta();
        meta.setMapView(view);
        meta.customName(Texts.plain("Pocket map", s.accent()));
        meta.lore(List.of(Texts.plain("Follows you; shows your friends.", s.subtle()),
                Texts.plain("/map zoom changes the scale.", s.subtle())));
        meta.setMaxStackSize(1);
        meta.getPersistentDataContainer().set(tagKey, PersistentDataType.STRING, p.getUniqueId().toString());
        item.setItemMeta(meta);
        return item;
    }

    /** Removes every pocket map but one current one of the player's own; returns whether one is left. */
    boolean sweep(Player p) {
        PlayerInventory inv = p.getInventory();
        Integer id = mapId(p);
        String own = p.getUniqueId().toString();
        boolean kept = false;
        ItemStack[] items = inv.getContents();
        for (int i = 0; i < items.length; i++) {
            String t = tag(items[i]);
            if (t == null) {
                continue;
            }
            MapId mid = items[i].getData(DataComponentTypes.MAP_ID);
            if (!kept && t.equals(own) && id != null && mid != null && mid.id() == id && items[i].getAmount() == 1) {
                kept = true;
                continue;
            }
            inv.setItem(i, null);
            removed++;
        }
        if (isPocketMap(p.getItemOnCursor())) {
            p.setItemOnCursor(null);
            removed++;
        }
        return kept;
    }

    void join(Player p) {
        if (sweep(p)) {
            view(p); // re-attach the renderer after a restart
        }
    }

    /** The one-line join tip (clients with neither mod; only without a pocket map in the inventory). */
    Component tip() {
        return Component.text("Tip: ", s.subtle())
                .append(Component.text("/map", s.accent()).clickEvent(ClickEvent.suggestCommand("/map")))
                .append(Component.text(" gives you a pocket map with your friends on it.", s.subtle()));
    }

    boolean hasOne(Player p) {
        for (ItemStack it : p.getInventory().getContents()) {
            if (isPocketMap(it)) {
                return true;
            }
        }
        return false;
    }

    // --- /map ------------------------------------------------------------------------------------------------------

    void command(Player p, String[] args) {
        if (!s.pocketMapEnabled()) {
            p.sendMessage(Component.text("The pocket map is off on this server.", s.subtle()));
            return;
        }
        if (args.length > 0 && args[0].equalsIgnoreCase("zoom")) {
            MapView view = view(p);
            int next = (view.getScale().ordinal() + 1) % SCALES.length;
            view.setScale(SCALES[next]);
            p.sendMessage(Component.text("Pocket map: " + (1 << next) + " block" + (next == 0 ? "" : "s")
                    + " per pixel (" + (128 << next) + " blocks across).", s.subtle()));
            return;
        }
        if (args.length > 0) {
            p.sendMessage(Component.text("Usage: /map [zoom]", s.subtle()));
            return;
        }
        sweep(p);
        PlayerInventory inv = p.getInventory();
        if (inv.firstEmpty() < 0) {
            p.sendMessage(Component.text("Make room in your inventory first.", s.subtle()));
            return;
        }
        ItemStack item = create(p, view(p));
        if (inv.getItemInMainHand().getType() == Material.AIR) {
            inv.setItemInMainHand(item);
        } else {
            inv.addItem(item);
        }
        given++;
        p.sendMessage(Component.text("Here's your pocket map: it follows you and shows your friends. Hold it in "
                + "your off hand for a minimap; /map zoom changes the scale.", s.text()));
    }

    // --- rendering -------------------------------------------------------------------------------------------------

    static boolean held(Player p, int mapId) {
        PlayerInventory inv = p.getInventory();
        for (ItemStack it : new ItemStack[] {inv.getItemInMainHand(), inv.getItemInOffHand()}) {
            if (it.getType() == Material.FILLED_MAP) {
                MapId id = it.getData(DataComponentTypes.MAP_ID);
                if (id != null && id.id() == mapId) {
                    return true;
                }
            }
        }
        return false;
    }

    /** The player's map: re-centring, the shifted picture until vanilla redraws it, the friends' cursors. */
    final class Render extends MapRenderer {
        private static final int N = 128 * 128;
        /** Alpha 0 = palette index 0 (MapPalette.matchColor): the client's "not explored" background. */
        private static final Color UNEXPLORED = new Color(0, 0, 0, 0);
        private final UUID owner;
        private final Color[] shown = new Color[N];
        private final Color[] stale = new Color[N];
        private final boolean[] overlay = new boolean[N];
        private final int[] entered = new int[N];
        private int overlayCount;
        private int pending;
        private int cx;
        private int cz;
        private int scale = -1;
        private UUID world;
        private int lastPx = Integer.MIN_VALUE;
        private int lastPz;
        private int lastCursors = Integer.MIN_VALUE;
        private int lastRecentre = Integer.MIN_VALUE;
        private int lastSample = Integer.MIN_VALUE;
        private long preGen = Long.MIN_VALUE;
        private boolean wantPre = true;

        Render(UUID owner) {
            super(true);
            this.owner = owner;
        }

        @Override
        public boolean isExplorerMap() {
            return false;
        }

        @Override
        public void render(MapView view, MapCanvas canvas, Player viewer) {
            if (!viewer.getUniqueId().equals(owner)) {
                return; // a stray copy in someone else's hands: plain vanilla
            }
            int now = Bukkit.getCurrentTick();
            boolean held = held(viewer, view.getId());
            if (held && scale != -1 && now - lastRecentre >= RECENTRE_TICKS) {
                lastRecentre = now;
                recentre(view, viewer);
            }
            boolean changed = pixels(view, canvas, viewer, held, now);
            if (held && now - lastSample >= SAMPLE_TICKS && prerender.usable(view.getWorld())) {
                lastSample = now;
                sample(view, canvas);
            }
            if (changed || now - lastCursors >= CURSOR_TICKS) {
                lastCursors = now;
                cursors(view, canvas, viewer);
            }
        }

        private void recentre(MapView view, Player p) {
            World w = p.getWorld();
            if (!w.equals(view.getWorld())) {
                view.setWorld(w);
            }
            int sc = view.getScale().getValue();
            Location at = p.getLocation();
            int nx = Wayfinding.snap(at.getX(), sc + 1); // v1.4: even pixels, the tiles' dither parity
            int nz = Wayfinding.snap(at.getZ(), sc + 1);
            int limit = RECENTRE_PX << sc;
            if (Math.abs(nx - view.getCenterX()) >= limit || Math.abs(nz - view.getCenterZ()) >= limit) {
                view.setCenterX(nx);
                view.setCenterZ(nz);
                recentres++;
            }
        }

        /** Returns whether the picture was reset or shifted (the cursors follow at once). */
        private boolean pixels(MapView view, MapCanvas canvas, Player p, boolean held, int now) {
            int sc = view.getScale().getValue();
            World w = view.getWorld();
            UUID wid = w == null ? null : w.getUID();
            boolean reset = sc != scale || wid == null || !wid.equals(world);
            boolean shift = !reset && (view.getCenterX() != cx || view.getCenterZ() != cz);
            if (scale == -1) { // first render since start-up: the stored colours match the stored centre
                scale = sc;
                world = wid;
                cx = view.getCenterX();
                cz = view.getCenterZ();
                wantPre = true;
                return true;
            }
            if (reset || shift) {
                byte[] pre = prerender.window(w, sc, view.getCenterX(), view.getCenterZ());
                Color[] cur = new Color[N];
                for (int i = 0; i < N; i++) {
                    Color base = canvas.getBasePixelColor(i & 127, i >> 7);
                    cur[i] = overlay[i] ? shown[i] : base;
                    stale[i] = base;
                }
                int dx = (view.getCenterX() - cx) >> sc;
                int dz = (view.getCenterZ() - cz) >> sc;
                for (int y = 0; y < 128; y++) {
                    for (int x = 0; x < 128; x++) {
                        int ox = x + dx;
                        int oz = y + dz;
                        Color c = !reset && ox >= 0 && ox < 128 && oz >= 0 && oz < 128 ? cur[oz * 128 + ox]
                                : UNEXPLORED;
                        if ((c == UNEXPLORED || c.getAlpha() == 0) && pre != null) {
                            Color pc = prerender.color(pre[y * 128 + x] & 255);
                            c = pc != null ? pc : c;
                        }
                        shown[y * 128 + x] = c;
                        canvas.setPixelColor(x, y, c);
                    }
                }
                Arrays.fill(overlay, true);
                Arrays.fill(entered, -1);
                overlayCount = N;
                pending = 1;
                scale = sc;
                world = wid;
                cx = view.getCenterX();
                cz = view.getCenterZ();
                wantPre = pre == null;
                preGen = prerender.generation(w);
                return true;
            }
            long gen = prerender.generation(w);
            if (gen != preGen) {
                preGen = gen;
                wantPre = gen >= 0;
            }
            if (wantPre && prerender.usable(w)) {
                byte[] pre = prerender.window(w, sc, cx, cz);
                if (pre != null) {
                    wantPre = false;
                    if (fill(canvas, pre)) {
                        return true;
                    }
                }
            }
            if (overlayCount == 0) {
                return false;
            }
            int bpp = 1 << sc;
            Location at = p.getLocation();
            int px = Math.floorDiv((int) Math.floor(at.getX()) - cx, bpp) + 64;
            int pz = Math.floorDiv((int) Math.floor(at.getZ()) - cz, bpp) + 64;
            if (px == lastPx && pz == lastPz && pending == 0) {
                return false;
            }
            lastPx = px;
            lastPz = pz;
            pending = 0;
            int radius = 128 / bpp;
            if (w.getEnvironment() == World.Environment.NETHER) {
                radius /= 2;
            }
            int r2 = (radius - 1) * (radius - 1);
            for (int i = 0; i < N; i++) {
                if (!overlay[i]) {
                    continue;
                }
                int x = i & 127;
                int y = i >> 7;
                boolean release = !canvas.getBasePixelColor(x, y).equals(stale[i]);
                if (!release) {
                    int ddx = x - px;
                    int ddz = y - pz;
                    if (held && ddx * ddx + ddz * ddz < r2) {
                        if (entered[i] < 0) {
                            entered[i] = now;
                            pending++;
                        } else if (now - entered[i] >= SETTLE_TICKS) {
                            release = true;
                        } else {
                            pending++;
                        }
                    } else {
                        entered[i] = -1;
                    }
                }
                if (release) {
                    overlay[i] = false;
                    overlayCount--;
                    canvas.setPixelColor(x, y, null); // transparent: vanilla's colour shows
                }
            }
            return false;
        }

        /**
         * v1.4: paints the pre-render where the picture shows nothing: an overlay pixel still "unexplored", or a
         * pixel vanilla has never drawn (transparent base). Those become overlay pixels, released to vanilla as
         * usual. Returns whether anything was painted.
         */
        private boolean fill(MapCanvas canvas, byte[] pre) {
            int painted = 0;
            for (int i = 0; i < N; i++) {
                Color pc = prerender.color(pre[i] & 255);
                if (pc == null) {
                    continue;
                }
                int x = i & 127;
                int y = i >> 7;
                if (overlay[i]) {
                    if (shown[i] != UNEXPLORED && shown[i].getAlpha() != 0) {
                        continue;
                    }
                } else {
                    Color base = canvas.getBasePixelColor(x, y);
                    if (base.getAlpha() != 0) {
                        continue;
                    }
                    overlay[i] = true;
                    overlayCount++;
                    stale[i] = base;
                    entered[i] = -1;
                }
                shown[i] = pc;
                canvas.setPixelColor(x, y, pc);
                painted++;
            }
            if (painted > 0) {
                pending = 1;
                prerendered += painted;
            }
            return painted > 0;
        }

        /**
         * v1.4, /mcbrand selftest: where vanilla has drawn the current picture (released pixels with a colour) and
         * the pre-render has a pixel, how often the two agree (100 % unless the world changed since the render).
         */
        private void sample(MapView view, MapCanvas canvas) {
            byte[] pre = prerender.window(view.getWorld(), scale, cx, cz);
            if (pre == null) {
                return;
            }
            for (int i = 0; i < N; i++) {
                if (overlay[i]) {
                    continue;
                }
                Color pc = prerender.color(pre[i] & 255);
                Color base = canvas.getBasePixelColor(i & 127, i >> 7);
                if (pc == null || base.getAlpha() == 0) {
                    continue;
                }
                compared++;
                if (base.getRGB() == pc.getRGB()) {
                    matched++;
                }
            }
        }

        private void cursors(MapView view, MapCanvas canvas, Player p) {
            MapCursorCollection out = new MapCursorCollection();
            World w = view.getWorld();
            int sc = view.getScale().getValue();
            for (Player o : Bukkit.getOnlinePlayers()) {
                if (o == p || !o.getWorld().equals(w) || compass.hidden(o) || !XaeroMap.shown(o) || !p.canSee(o)) {
                    continue;
                }
                Location l = o.getLocation();
                Wayfinding.Cursor c = Wayfinding.cursor(l.getX(), l.getZ(), l.getYaw(), view.getCenterX(),
                        view.getCenterZ(), sc);
                out.addCursor(new MapCursor((byte) c.x(), (byte) c.y(), (byte) c.direction(),
                        c.onMap() ? MapCursor.Type.PLAYER : MapCursor.Type.PLAYER_OFF_MAP, true,
                        Component.text(o.getName())));
            }
            canvas.setCursors(out);
        }
    }

    // --- no copies, drop-safe (events) ---------------------------------------------------------------------------

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onDrop(PlayerDropItemEvent e) {
        if (isPocketMap(e.getItemDrop().getItemStack())) {
            e.getItemDrop().remove(); // gone, not lost: /map is free
            removed++;
            e.getPlayer().sendMessage(Component.text("Pocket map put away (/map gets it back).", s.subtle()));
        }
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onDeath(PlayerDeathEvent e) {
        if (e.getDrops().removeIf(this::isPocketMap)) {
            removed++;
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onFrame(PlayerInteractEntityEvent e) {
        if (e.getRightClicked() instanceof ItemFrame
                && isPocketMap(e.getPlayer().getInventory().getItem(e.getHand()))) {
            e.setCancelled(true);
        }
    }

    /** Only the player's own inventory (and its 2x2 grid, guarded by onCraft) may hold a pocket map. */
    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onClick(InventoryClickEvent e) {
        ItemStack hotbar = e.getHotbarButton() >= 0 ? e.getWhoClicked().getInventory().getItem(e.getHotbarButton())
                : null;
        boolean ours = isPocketMap(e.getCurrentItem()) || isPocketMap(e.getCursor()) || isPocketMap(hotbar)
                || (e.getClick() == ClickType.SWAP_OFFHAND
                        && isPocketMap(e.getWhoClicked().getInventory().getItemInOffHand()));
        if (!ours) {
            return;
        }
        InventoryType top = e.getView().getTopInventory().getType();
        boolean own = top == InventoryType.CRAFTING || top == InventoryType.CREATIVE;
        if (!own || bundle(e.getCurrentItem()) || bundle(e.getCursor())) {
            e.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onDrag(InventoryDragEvent e) {
        if (!isPocketMap(e.getOldCursor())) {
            return;
        }
        int topSize = e.getView().getTopInventory().getSize();
        InventoryType top = e.getView().getTopInventory().getType();
        boolean own = top == InventoryType.CRAFTING || top == InventoryType.CREATIVE;
        for (int slot : e.getRawSlots()) {
            if (slot < topSize && !own) {
                e.setCancelled(true);
                return;
            }
        }
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onCraft(PrepareItemCraftEvent e) {
        for (ItemStack it : e.getInventory().getMatrix()) {
            if (isPocketMap(it)) {
                e.getInventory().setResult(null);
                return;
            }
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onClose(InventoryCloseEvent e) {
        if (e.getPlayer() instanceof Player p && s.pocketMapEnabled()) {
            sweep(p);
        }
    }

    private static boolean bundle(ItemStack it) {
        return it != null && it.getType().name().endsWith("BUNDLE");
    }

    /** For /mcbrand selftest: build an item and read it back, and vanilla's cursor arithmetic on fixed inputs. */
    String selftest() {
        List<String> c = new ArrayList<>();
        for (Wayfinding.Cursor cur : List.of(Wayfinding.cursor(10, -20, 90, 0, 0, 0),
                Wayfinding.cursor(1000, 0, 0, 0, 0, 0), Wayfinding.cursor(100, 100, -45, 64, 64, 1))) {
            c.add(cur.x() + "," + cur.y() + "," + cur.direction() + (cur.onMap() ? "" : "e"));
        }
        return "pocket map cursors " + String.join(" ", c) + ", snap " + Wayfinding.snap(-0.5, 0) + "/"
                + Wayfinding.snap(13.9, 2) + ", maps " + renderers.size() + " given " + given + " removed " + removed
                + " recentres " + recentres + "; prerender painted " + prerendered + " match "
                + (compared == 0 ? "n/a" : String.format(Locale.ROOT, "%.1f%%", 100.0 * matched / compared))
                + " of " + compared + " px";
    }
}
