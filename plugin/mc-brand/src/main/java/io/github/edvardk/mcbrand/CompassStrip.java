package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * The friend compass's strip (v1.3, moved out of mc-brand's Wayfinding in v1.5): angles, distances, the font's
 * advance widths and the layout. No Minecraft, Bukkit or Adventure types: mc-brand draws it as a boss bar's name
 * for players without the Smallworld mod, and the mod (v1.5, client/mod/WIRE.md "The client compass") draws the
 * same layout every frame from the local yaw. Both carry this file verbatim (only the package line differs:
 * tests/test_social.py), so the two compasses cannot drift; WayfindingFixtures and the mod's WireFixtures print
 * the same scenes (tests/test_client_mod.py compares them, tests/test_wayfinding.py recomputes them in Python).
 *
 * <p>Angles: Minecraft's yaw is 0 = south (+Z), 90 = west, -90 = east, 180 = north. Compass bearings here are
 * degrees clockwise from north (north = -Z): 0 N, 90 E, 180 S, 270 W. A viewer facing yaw {@code y} faces bearing
 * {@code y + 180}.
 *
 * <p>The strip is laid out in font pixels (the vanilla default font's advance widths: {@link #advance}), because the
 * client centres the boss bar's text as a whole and the font is not monospaced. {@link #WIDTH_PX} pixels cover
 * {@link #HALF_FOV} degrees each side of the facing direction; every item is centred on its bearing, items never
 * overlap (lower priority ones are left out: the faced player, then the others nearest first, then N/E/S/W, then
 * NE/SE/SW/NW, then the 15-degree ticks), and the gaps are filled with spaces (4 px) and bold spaces (5 px), so
 * the centre of the strip is the centre of the screen. Glyphs outside ASCII (the arrows) are estimates: a pixel or
 * two off at most. {@link Placed#exact} is an item's unrounded left edge: the mod draws there (sub-pixel), so the
 * strip glides instead of stepping a pixel at a time.
 */
final class CompassStrip {
    private CompassStrip() {}

    static final int WIDTH_PX = 240;
    static final double HALF_FOV = 90.0;
    /** A target within this many degrees of the facing direction is "the one faced": ▼ name, distance. */
    static final double FOCUS_DEG = 8.0;
    static final int MIN_SPACING_PX = 3;
    static final int MAX_AWAY = 2;
    static final String FOCUS_MARK = "▼"; // ▼
    static final String UP = "↑"; // ↑
    static final String DOWN = "↓"; // ↓
    static final String TICK = "·"; // ·

    // --- angles and distances ------------------------------------------------------------------------------------

    /** Compass bearing (0..360, clockwise from north) of the offset (dx, dz). */
    static double bearing(double dx, double dz) {
        double deg = Math.toDegrees(Math.atan2(dx, -dz));
        return deg < 0 ? deg + 360.0 : deg;
    }

    /** The bearing a viewer with this yaw faces (0..360). */
    static double facing(double yaw) {
        return normalize360(yaw + 180.0);
    }

    static double normalize360(double deg) {
        double d = deg % 360.0;
        return d < 0 ? d + 360.0 : d;
    }

    /** {@code bearing} relative to {@code facing}: -180 (exclusive) .. 180, negative = to the left. */
    static double relative(double bearing, double facing) {
        double r = normalize360(bearing - facing);
        return r > 180.0 ? r - 360.0 : r;
    }

    /** Distance rounded to a multiple of {@code rounding} (half up); at least {@code rounding} = "&lt;N". */
    static long rounded(double distance, int rounding) {
        int r = Math.max(1, rounding);
        return Math.round(distance / r) * r;
    }

    static String distance(double distance, int rounding) {
        long v = rounded(distance, rounding);
        return v < Math.max(1, rounding) ? "<" + Math.max(1, rounding) + "m" : v + "m";
    }

    /** ↑ / ↓ when the target is more than {@code threshold} blocks above / below, else "". */
    static String arrow(double dy, int threshold) {
        return dy > threshold ? UP : dy < -threshold ? DOWN : "";
    }

    /** "Nether", "End", "Overworld", or the key's path for a custom dimension. */
    static String dimensionName(String key) {
        return switch (key) {
            case "minecraft:the_nether" -> "Nether";
            case "minecraft:the_end" -> "End";
            case "minecraft:overworld" -> "Overworld";
            default -> key.substring(key.indexOf(':') + 1);
        };
    }

    // --- the font ------------------------------------------------------------------------------------------------

    /** Advance width in GUI pixels of one character in the vanilla default font (not bold; bold adds 1). */
    static int advance(int cp) {
        return switch (cp) {
            case ' ' -> 4;
            case '!', ',', '.', ':', ';', '|', 'i', '\'', 0x00b7 -> 2;
            case '`', 'l' -> 3;
            case '"', '(', ')', '*', 'I', '[', ']', 't', '{', '}' -> 4;
            case '<', '>', 'f', 'k' -> 5;
            case '@', '~' -> 7;
            default -> 6; // letters, digits, '_', and (estimated) the arrows
        };
    }

    static int width(String s) {
        return s.codePoints().map(CompassStrip::advance).sum();
    }

    // --- the strip -----------------------------------------------------------------------------------------------

    /** One other player as the viewer sees them; {@code dimension} is null in the viewer's own dimension. */
    record Target(String name, double dx, double dy, double dz, String dimension) {
        double horizontal() {
            return Math.sqrt(dx * dx + dz * dz);
        }
    }

    record Config(int maxRange, int rounding, int heightThreshold) {}

    enum Kind { GAP, BOLD_GAP, TICK, NORTH, CARDINAL, PLAYER, FOCUS, AWAY }

    /** A piece of the line: text of one kind (the plugin colours each kind). */
    record Segment(Kind kind, String text) {}

    /**
     * A placed item: its text, its left edge in whole pixels (the boss bar's layout, the fixtures) and, v1.5, the
     * unrounded left edge the mod draws at.
     */
    record Placed(Kind kind, String text, int left, int width, double exact) {}

    record Strip(List<Segment> segments, List<Placed> placed, String focus) {
        String plain() {
            StringBuilder b = new StringBuilder();
            for (Segment s : segments) {
                b.append(s.text());
            }
            return b.toString();
        }
    }

    private record Item(Kind kind, String text, double centre, int priority, double order) {}

    static final String[] POINTS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"};

    static double pixel(double rel) {
        return WIDTH_PX / 2.0 + rel / HALF_FOV * (WIDTH_PX / 2.0);
    }

    /** The compass line for a viewer with this yaw. */
    static Strip strip(double yaw, List<Target> targets, Config c) {
        double face = facing(yaw);
        List<Item> items = new ArrayList<>();
        Target focus = null;
        double focusRel = 0;
        List<Target> here = new ArrayList<>();
        List<Target> away = new ArrayList<>();
        for (Target t : targets) {
            if (t.dimension() != null) {
                away.add(t);
            } else if (t.horizontal() <= c.maxRange()) {
                here.add(t);
            }
        }
        here.sort(Comparator.comparingDouble(Target::horizontal).thenComparing(Target::name));
        for (Target t : here) {
            double rel = relative(bearing(t.dx(), t.dz()), face);
            if (Math.abs(rel) <= FOCUS_DEG && (focus == null || Math.abs(rel) < Math.abs(focusRel) - 1e-9)) {
                focus = t;
                focusRel = rel;
            }
        }
        String focusText = null;
        if (focus != null) {
            focusText = FOCUS_MARK + focus.name() + " " + distance(focus.horizontal(), c.rounding())
                    + arrow(focus.dy(), c.heightThreshold());
            items.add(new Item(Kind.FOCUS, focusText, pixel(0), 0, 0));
        }
        for (int i = 0; i < POINTS.length; i++) {
            double rel = relative(i * 45.0, face);
            if (Math.abs(rel) <= HALF_FOV) {
                items.add(new Item(i == 0 ? Kind.NORTH : Kind.CARDINAL, POINTS[i], pixel(rel), i % 2 == 0 ? 2 : 3, i));
            }
        }
        int rank = 0;
        for (Target t : here) {
            rank++;
            if (t == focus) {
                continue;
            }
            double rel = relative(bearing(t.dx(), t.dz()), face);
            if (Math.abs(rel) <= HALF_FOV) {
                items.add(new Item(Kind.PLAYER, t.name() + arrow(t.dy(), c.heightThreshold()), pixel(rel), 1, rank));
            }
        }
        for (int deg = 15; deg < 360; deg += 15) {
            if (deg % 45 != 0) {
                double rel = relative(deg, face);
                if (Math.abs(rel) <= HALF_FOV) {
                    items.add(new Item(Kind.TICK, TICK, pixel(rel), 4, deg));
                }
            }
        }
        items.sort(Comparator.comparingInt(Item::priority).thenComparingDouble(Item::order));
        List<Placed> placed = new ArrayList<>();
        for (Item it : items) {
            int w = width(it.text());
            int left = (int) Math.round(it.centre() - w / 2.0);
            left = Math.max(0, Math.min(WIDTH_PX - w, left));
            boolean free = true;
            for (Placed p : placed) {
                if (left < p.left() + p.width() + MIN_SPACING_PX && p.left() < left + w + MIN_SPACING_PX) {
                    free = false;
                    break;
                }
            }
            if (free) {
                double exact = Math.max(0, Math.min(WIDTH_PX - w, it.centre() - w / 2.0));
                placed.add(new Placed(it.kind(), it.text(), left, w, exact));
            }
        }
        placed.sort(Comparator.comparingInt(Placed::left));

        String awayText = awayText(away);
        List<Segment> out = new ArrayList<>();
        int awayWidth = awayText.isEmpty() ? 0 : width(awayText);
        gap(out, awayWidth); // mirrors the suffix, so the strip's centre stays the screen's
        int x = 0;
        for (Placed p : placed) {
            gap(out, p.left() - x);
            out.add(new Segment(p.kind(), p.text()));
            x = p.left() + p.width();
        }
        gap(out, WIDTH_PX - x);
        if (!awayText.isEmpty()) {
            out.add(new Segment(Kind.AWAY, awayText));
        }
        return new Strip(List.copyOf(out), List.copyOf(placed), focusText);
    }

    /** " | Alex (Nether), Sam (End) +2": players in other dimensions, no bearing, no distance. */
    static String awayText(List<Target> away) {
        if (away.isEmpty()) {
            return "";
        }
        List<Target> sorted = new ArrayList<>(away);
        sorted.sort(Comparator.comparing(Target::name, String.CASE_INSENSITIVE_ORDER));
        StringBuilder b = new StringBuilder(" | ");
        for (int i = 0; i < Math.min(MAX_AWAY, sorted.size()); i++) {
            if (i > 0) {
                b.append(", ");
            }
            b.append(sorted.get(i).name()).append(" (").append(dimensionName(sorted.get(i).dimension())).append(')');
        }
        if (sorted.size() > MAX_AWAY) {
            b.append(" +").append(sorted.size() - MAX_AWAY);
        }
        return b.toString();
    }

    /** Fills {@code px} pixels with spaces (4 px) and bold spaces (5 px); a gap of 1-3, 6, 7 or 11 px is rounded. */
    static void gap(List<Segment> out, int px) {
        if (px <= 0) {
            return;
        }
        int[] best = null;
        for (int target : new int[] {px, px - 1, px + 1, px - 2, px + 2, px - 3}) {
            for (int bold = 0; bold < 4 && target >= 0; bold++) {
                int rest = target - 5 * bold;
                if (rest >= 0 && rest % 4 == 0) {
                    best = new int[] {rest / 4, bold};
                    break;
                }
            }
            if (best != null) {
                break;
            }
        }
        if (best == null) {
            return;
        }
        if (best[0] > 0) {
            out.add(new Segment(Kind.GAP, " ".repeat(best[0])));
        }
        if (best[1] > 0) {
            out.add(new Segment(Kind.BOLD_GAP, " ".repeat(best[1])));
        }
    }

    /** The pixel width the client draws for these segments (bold spaces are 5 px). */
    static int lineWidth(List<Segment> segments) {
        int w = 0;
        for (Segment s : segments) {
            w += width(s.text()) + (s.kind() == Kind.BOLD_GAP ? s.text().length() : 0);
        }
        return w;
    }
}
