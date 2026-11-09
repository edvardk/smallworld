package io.github.edvardk.smallworld.client;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;

/**
 * v1.5: the client compass's pure part (client/mod/WIRE.md "The client compass"; no Minecraft types, so
 * WireFixtures runs it on the plain JDK): mc-brand's {@code smallworld:friends} plus the local player's position
 * and dimension in, {@link CompassStrip}'s targets out. CompassHud draws {@link CompassStrip#strip} of them every
 * frame from the local yaw.
 *
 * <ul>
 *   <li>Players here: their position is the server's (1/16 block), or, when the client has that player's entity
 *       (within entity tracking range), the entity's interpolated position, which moves every frame instead of
 *       when the next message arrives. Only names the server sent are ever looked up: whom the compass shows is
 *       the server's decision (visibility, /compass hide, range), the client only moves them.
 *   <li>Players elsewhere: "name (Nether)", no position.
 *   <li>The local player in another dimension than the message was made for (they just went through a portal):
 *       the players "here" in that message are now elsewhere, in the message's dimension; the players listed
 *       as elsewhere have no known position yet (left out until the next message, a few ticks later).
 * </ul>
 *
 * <p>{@link CompassStrip#strip} applies {@code max_range} again from the local position, so a friend leaves the
 * strip the frame the local player walks out of range.
 */
final class CompassModel {
    private CompassModel() {}

    /** The strip config from the server's state (clamped to mc-brand's own limits, Settings). */
    static CompassStrip.Config config(Wire.Compass c) {
        return new CompassStrip.Config(clamp(c.maxRange(), 16, 100_000), clamp(c.rounding(), 1, 100),
                clamp(c.heightThreshold(), 2, 64));
    }

    private static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    /**
     * The targets for a local player at (x, y, z) in {@code dimension}. {@code local} gives the position of a
     * player entity the client has ({x, y, z} in blocks) or null.
     */
    static List<CompassStrip.Target> targets(Wire.Friends friends, String dimension, double x, double y, double z,
            Function<String, double[]> local) {
        List<CompassStrip.Target> out = new ArrayList<>();
        boolean same = Wire.fit(dimension).equals(friends.dimension());
        for (Wire.Friend f : friends.friends()) {
            if (!f.here()) {
                if (!f.dimension().equals(Wire.fit(dimension))) {
                    out.add(new CompassStrip.Target(f.name(), 0, 0, 0, f.dimension()));
                }
            } else if (!same) {
                out.add(new CompassStrip.Target(f.name(), 0, 0, 0, friends.dimension()));
            } else {
                double[] at = local.apply(f.name());
                double fx = at != null ? at[0] : f.x() / (double) Wire.FIXED;
                double fy = at != null ? at[1] : f.y() / (double) Wire.FIXED;
                double fz = at != null ? at[2] : f.z() / (double) Wire.FIXED;
                out.add(new CompassStrip.Target(f.name(), fx - x, fy - y, fz - z, null));
            }
        }
        return out;
    }

    /** ARGB (opaque) for a strip item of this kind, with the server's colours (RGB) as mc-brand's boss bar uses them. */
    static int color(CompassStrip.Kind kind, Wire.Compass c) {
        int rgb = switch (kind) {
            case NORTH -> 0xFF5555; // NamedTextColor.RED
            case TICK -> c.secondary();
            case CARDINAL, AWAY -> c.subtle();
            case PLAYER -> c.text();
            case FOCUS -> c.accent();
            case GAP, BOLD_GAP -> 0xFFFFFF;
        };
        return 0xFF000000 | (rgb & 0xFFFFFF);
    }
}
