package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/**
 * v1.3: the pure logic of the pocket map's cursors and the client-mod precedence (no Bukkit or Adventure types, so
 * {@code WayfindingFixtures} runs it on the plain JDK and tests/test_wayfinding.py checks its output with an
 * independent Python implementation). v1.5: the compass strip itself is {@link CompassStrip}, shared verbatim with
 * the client mod; {@link #compassMode} decides who draws a player's compass.
 */
final class Wayfinding {
    private Wayfinding() {}

    // --- who the compass shows (v1.5: one list for both compasses) ---------------------------------------------

    /**
     * Another online player the viewer's compass may show (Compass.visible: not the viewer, not /compass hide, not
     * a spectator or invisible, not hidden from the viewer), with their dimension key and position.
     */
    record Seen(String name, String dimension, double x, double y, double z) {}

    /** v1.5: the selftest's and the fixtures' scene, the viewer at (0.5, 64, 0.5) in the overworld. */
    static final List<Seen> SELFTEST_SCENE = List.of(
            new Seen("Alex", "minecraft:overworld", 3.5, 84, -119.5), // north, above
            new Seen("Sam", "minecraft:overworld", 200.5, 64, 0.5), // east
            new Seen("Far", "minecraft:overworld", 5000, 64, 5000), // out of range: not sent
            new Seen("Kim", "minecraft:the_nether", 10, 70, 10)); // elsewhere: no position

    /** The horizontal distance exactly as {@link CompassStrip.Target#horizontal} computes it. */
    private static double horizontal(Seen o, double x, double z) {
        double dx = o.x() - x;
        double dz = o.z() - z;
        return Math.sqrt(dx * dx + dz * dz);
    }

    /** The boss bar's targets for a viewer at (x, y, z) in {@code world}: offsets here, the dimension elsewhere. */
    static List<CompassStrip.Target> targets(String world, double x, double y, double z, List<Seen> others) {
        List<CompassStrip.Target> out = new ArrayList<>();
        for (Seen o : others) {
            out.add(o.dimension().equals(world)
                    ? new CompassStrip.Target(o.name(), o.x() - x, o.y() - y, o.z() - z, null)
                    : new CompassStrip.Target(o.name(), 0, 0, 0, o.dimension()));
        }
        return out;
    }

    /**
     * v1.5: the {@code smallworld:friends} payload for the same viewer: exactly whom the boss bar would show
     * ({@link CompassStrip#strip} drops players here beyond {@code maxRange}; players elsewhere carry no position),
     * those here nearest first, then those elsewhere by name, at most {@link Wire#MAX_FRIENDS}. The order is fixed,
     * so an unchanged scene encodes to the same bytes (Compass sends only changes).
     */
    static Wire.Friends friends(String world, double x, double z, int maxRange, List<Seen> others) {
        List<Seen> here = new ArrayList<>();
        List<Seen> away = new ArrayList<>();
        for (Seen o : others) {
            if (!o.dimension().equals(world)) {
                away.add(o);
            } else if (horizontal(o, x, z) <= maxRange) {
                here.add(o);
            }
        }
        here.sort(Comparator.comparingDouble((Seen o) -> horizontal(o, x, z)).thenComparing(Seen::name));
        away.sort(Comparator.comparing(Seen::name));
        List<Wire.Friend> out = new ArrayList<>();
        for (Seen o : here) {
            out.add(new Wire.Friend(o.name(), "", Wire.fixed(o.x()), Wire.fixed(o.y()), Wire.fixed(o.z())));
        }
        for (Seen o : away) {
            out.add(new Wire.Friend(o.name(), o.dimension(), 0, 0, 0));
        }
        return new Wire.Friends(world, out.subList(0, Math.min(Wire.MAX_FRIENDS, out.size())));
    }

    // --- the pocket map's cursors --------------------------------------------------------------------------------

    /** A map cursor: x/y in the map's half-pixels (-128..127), direction 0..15, and whether it's on the map. */
    record Cursor(int x, int y, int direction, boolean onMap) {}

    /**
     * Where vanilla would draw a player at (x, z) with this yaw on a map centred at (centerX, centerZ) with this scale
     * (MapItemSavedData.addDecoration's arithmetic): on the map within 63 pixels of the centre, else at the edge
     * (direction 0, an off-map dot).
     */
    static Cursor cursor(double x, double z, double yaw, int centerX, int centerZ, int scale) {
        int blocks = 1 << scale;
        double fx = (x - centerX) / blocks;
        double fz = (z - centerZ) / blocks;
        if (fx >= -63 && fz >= -63 && fx <= 63 && fz <= 63) {
            double r = yaw + (yaw < 0 ? -8.0 : 8.0);
            int dir = ((int) (r * 16.0 / 360.0)) & 15;
            return new Cursor(clampByte((int) (fx * 2 + 0.5)), clampByte((int) (fz * 2 + 0.5)), dir, true);
        }
        int cx = fx <= -63 ? -128 : fx >= 63 ? 127 : clampByte((int) (fx * 2 + 0.5));
        int cz = fz <= -63 ? -128 : fz >= 63 ? 127 : clampByte((int) (fz * 2 + 0.5));
        return new Cursor(cx, cz, 0, false);
    }

    static int clampByte(int v) {
        return Math.max(-128, Math.min(127, v));
    }

    /** "Never happened" for a tick timestamp; only ever compared through {@link #due}. */
    static final int NEVER = Integer.MIN_VALUE;

    /**
     * v1.4: has {@code every} ticks passed since {@code last} ({@link #NEVER}: yes)? Never subtract a sentinel:
     * {@code now - Integer.MIN_VALUE} overflows to a negative number, which kept the pocket map from ever
     * re-centring and the map room from ever painting (found by the v1.4 e2e).
     */
    static boolean due(int now, int last, int every) {
        return last == NEVER || now - last >= every;
    }

    /** The map centre for a player at (x, z): whole pixels of this scale, so a re-centre shifts whole pixels. */
    static int snap(double coord, int scale) {
        int blocks = 1 << scale;
        return Math.floorDiv((int) Math.floor(coord), blocks) * blocks;
    }

    // --- client mods -----------------------------------------------------------------------------------------------

    /** XaeroMap's channels (xaerominimap:main checked in the 26.3 minimap jar: HudMod, modId + "main"). */
    static boolean isXaero(String channel) {
        String ns = channel.substring(0, Math.max(0, channel.indexOf(':'))).toLowerCase(Locale.ROOT);
        return channel.equals(XaeroMap.MINIMAP) || channel.equals(XaeroMap.WORLD_MAP) || ns.startsWith("xaero");
    }

    static boolean isOurs(String channel) {
        return channel.startsWith("smallworld:");
    }

    /** The compass by default (without /compass on|off): off only for the Smallworld mod together with Xaero's. */
    static boolean compassByDefault(boolean ourMod, boolean xaero) {
        return !(ourMod && xaero);
    }

    /** v1.5: can this client draw the compass itself? UNKNOWN until its hello and channel registrations are in. */
    enum Capability { YES, NO, UNKNOWN }

    /**
     * v1.5: the capability decision, pure (the selftest, WayfindingFixtures): a hello with
     * {@link Wire#CAP_COMPASS} and both compass channels registered is YES; that hello without the channels yet,
     * or no hello yet from a client that registered a smallworld channel, is UNKNOWN (Compass waits, at most until
     * its decision window closes); everything else (no mod, mod 1.0.0) is NO: the boss bar, as before.
     */
    static Capability capability(Integer capabilities, boolean ourChannel, boolean compassChannels) {
        if (capabilities == null) {
            return ourChannel ? Capability.UNKNOWN : Capability.NO;
        }
        if ((capabilities & Wire.CAP_COMPASS) == 0) {
            return Capability.NO;
        }
        return compassChannels ? Capability.YES : Capability.UNKNOWN;
    }

    /** v1.5: who draws a player's compass (Compass.apply). */
    enum CompassMode { NONE, WAIT, SERVER, CLIENT }

    /**
     * v1.5: who draws a player's compass. {@code want}: their /compass choice, else the precedence
     * ({@link #compassByDefault}). {@code capable}: the mod's hello carried the compass capability (WIRE.md
     * {@code capabilities} bit 0) and the client registered the compass channels, so the mod draws it every frame
     * (CLIENT); otherwise mc-brand's boss bar does (SERVER), exactly as before v1.5 (no mod, or a mod older than
     * 1.1.0). {@code pending}: the client registered a smallworld channel but its hello has not come yet and the
     * decision window after the join is open: WAIT rather than flash a boss bar the hello would take away.
     */
    static CompassMode compassMode(boolean want, boolean capable, boolean pending) {
        if (!want) {
            return CompassMode.NONE;
        }
        if (capable) {
            return CompassMode.CLIENT;
        }
        return pending ? CompassMode.WAIT : CompassMode.SERVER;
    }

    /** The one-line /map tip at join: only for clients with neither mod. */
    static boolean suggestMap(boolean ourMod, boolean xaero) {
        return !ourMod && !xaero;
    }

    /** "none", "xaero", "smallworld" or "smallworld+xaero" (for /mcbrand status and the fixtures). */
    static String mods(boolean ourMod, boolean xaero) {
        return ourMod ? xaero ? "smallworld+xaero" : "smallworld" : xaero ? "xaero" : "none";
    }
}
