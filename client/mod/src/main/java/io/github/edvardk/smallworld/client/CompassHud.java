package io.github.edvardk.smallworld.client;

import java.lang.reflect.Field;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import net.minecraft.client.DeltaTracker;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.gui.components.BossHealthOverlay;
import net.minecraft.client.player.AbstractClientPlayer;
import net.minecraft.world.entity.Entity;

/**
 * v1.5: the client compass (client/mod/WIRE.md "The client compass"): a HUD element right after vanilla's boss
 * bars, drawn every frame while mc-brand says so ({@code smallworld:compass}), from the local player's
 * interpolated yaw and position and mc-brand's {@code smallworld:friends}.
 *
 * <p>Where: the row the boss-bar compass used: vanilla draws boss bars from y = 12 down, one every 19 px, each
 * name 9 px above its bar, until a third of the screen height; the strip's text goes where the next bar's name
 * would (y = 3 + 19 x the bars drawn), so below mc-brand's HUD bar when that is on, at the top otherwise, as
 * before. Only the text, without the empty bar the boss-bar compass needed. The number of bars comes from the
 * overlay's private {@code events} map by reflection (Minecraft 26.x keeps its own names at runtime; no mixin); if
 * that ever fails the strip goes in the first row.
 *
 * <p>How: {@link CompassStrip#strip}, the boss bar's own layout (same items, same priorities, same overlap rule),
 * each item drawn at its unrounded position (the GUI pose takes a float translation), centred with the real font
 * width, in mc-brand's colours, with the shadow vanilla gives boss-bar names. Hidden with the HUD (F1).
 */
final class CompassHud {
    private static final int ROW = 19; // vanilla: 10 + font.lineHeight between bars
    private static final Field EVENTS = eventsField();

    // client thread only
    private Wire.Compass state;
    private Wire.Friends friends = new Wire.Friends("", List.of());

    void reset() {
        state = null;
        friends = new Wire.Friends("", List.of());
    }

    void state(Wire.Compass c) {
        state = c;
        if (!c.draw()) {
            friends = new Wire.Friends("", List.of());
        }
    }

    void friends(Wire.Friends f) {
        friends = f;
    }

    boolean drawing() {
        return state != null && state.draw();
    }

    void draw(GuiGraphicsExtractor g, DeltaTracker delta) {
        Minecraft mc = Minecraft.getInstance();
        if (!drawing() || mc.player == null || mc.level == null || mc.gui.hud.isHidden()) {
            return;
        }
        float pt = delta.getGameTimeDeltaPartialTick(false);
        double[] me = position(mc.player, pt);
        Map<String, double[]> seen = new HashMap<>();
        for (AbstractClientPlayer p : mc.level.players()) {
            if (p != mc.player) {
                seen.put(p.getScoreboardName(), position(p, pt));
            }
        }
        String dimension = mc.level.dimension().identifier().toString();
        List<CompassStrip.Target> targets = CompassModel.targets(friends, dimension, me[0], me[1], me[2], seen::get);
        CompassStrip.Strip strip = CompassStrip.strip(mc.player.getViewYRot(pt), targets, CompassModel.config(state));

        int y = 3 + ROW * bossRows(mc, g.guiHeight());
        float left = g.guiWidth() / 2f - CompassStrip.WIDTH_PX / 2f;
        for (CompassStrip.Placed p : strip.placed()) {
            String text = p.text();
            float x = left + (float) (p.exact() + p.width() / 2.0) - mc.font.width(text) / 2f;
            g.pose().pushMatrix();
            g.pose().translate(x, y);
            g.text(mc.font, text, 0, 0, CompassModel.color(p.kind(), state), true);
            g.pose().popMatrix();
        }
        for (CompassStrip.Segment s : strip.segments()) {
            if (s.kind() == CompassStrip.Kind.AWAY) {
                g.text(mc.font, s.text(), Math.round(left) + CompassStrip.WIDTH_PX, y,
                        CompassModel.color(s.kind(), state), true);
            }
        }
    }

    /** The entity's position between the last tick and this one (what vanilla renders it at). */
    private static double[] position(Entity e, float pt) {
        return new double[] {e.xo + (e.getX() - e.xo) * pt, e.yo + (e.getY() - e.yo) * pt,
                e.zo + (e.getZ() - e.zo) * pt};
    }

    /** How many boss bars vanilla draws now (its loop: from y = 12, one every 19 px, while above a third). */
    private static int bossRows(Minecraft mc, int guiHeight) {
        int n = bossBars(mc.gui.hud.getBossOverlay());
        int rows = 0;
        for (int y = 12; rows < n; ) {
            rows++;
            y += ROW;
            if (y >= guiHeight / 3) {
                break;
            }
        }
        return rows;
    }

    private static int bossBars(BossHealthOverlay overlay) {
        if (EVENTS == null) {
            return 0;
        }
        try {
            return EVENTS.get(overlay) instanceof Map<?, ?> m ? m.size() : 0;
        } catch (IllegalAccessException | RuntimeException e) {
            return 0;
        }
    }

    private static Field eventsField() {
        try {
            Field f = BossHealthOverlay.class.getDeclaredField("events");
            f.setAccessible(true);
            return f;
        } catch (ReflectiveOperationException | RuntimeException e) {
            return null;
        }
    }
}
