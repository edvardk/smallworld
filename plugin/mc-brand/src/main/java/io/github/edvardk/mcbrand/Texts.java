package io.github.edvardk.mcbrand;

import java.util.HashMap;
import java.util.Map;
import net.kyori.adventure.key.Key;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.TextComponent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextColor;
import net.kyori.adventure.text.format.TextDecoration;

/**
 * Text helpers. Server owners write plain text with {placeholders}; nothing is parsed as markup (no MiniMessage,
 * no legacy codes), so a player name or a rule can never inject formatting, clicks or hovers.
 */
final class Texts {
    private Texts() {}

    /** v1.0.1: the {glyph} icon of the server resource pack (mcctl/brandpack.py: font mcbrand:icons, U+E001).
     * Only for players whose client loaded the pack; everyone else gets nothing (an emoji would be a box). */
    static final Key ICONS = Key.key("mcbrand", "icons");
    static final String GLYPH_CHAR = "\uE001";
    // LEGACY_GLYPH_EMOJI: U+1F377, the v1.1 way to write the glyph (not in Minecraft's fonts). mcctl's 1.2.0
    // migration rewrites it to {glyph}; a hand-written config that still has it gets the glyph, never the box.
    static final String LEGACY_GLYPH_EMOJI = "\uD83C\uDF77";
    static final String LEGACY_GLYPH = "{wine}"; // the v1.1 placeholder, read as {glyph}

    static Component glyph(boolean glyph) {
        return glyph ? Component.text(GLYPH_CHAR, NamedTextColor.WHITE).font(ICONS) : Component.empty();
    }

    /** "<glyph> " + c, or c alone. */
    static Component withGlyph(Component c, boolean glyph) {
        return glyph ? Component.text().append(glyph(true)).append(Component.text(" ")).append(c).build() : c;
    }

    /** fill() with {glyph} (and the v1.1 forms {wine} and U+1F377) rendered for this player: the glyph, or
     * nothing. */
    static Component fill(String template, TextColor color, Map<String, Component> values, boolean glyph) {
        Map<String, Component> all = new HashMap<>(values);
        all.put("glyph", glyph(glyph));
        String t = template.replace(LEGACY_GLYPH_EMOJI, "{glyph}").replace(LEGACY_GLYPH, "{glyph}");
        if (!glyph) { // no glyph: drop the placeholder with one of its spaces ("fun {glyph}" -> "fun")
            t = t.replace(" {glyph}", "").replace("{glyph} ", "").replace("{glyph}", "");
        }
        return fill(t, color, all);
    }

    /**
     * {@code template} with each {key} replaced by its component (unknown keys stay as typed). Literal text gets
     * {@code color}.
     */
    static Component fill(String template, TextColor color, Map<String, Component> values) {
        TextComponent.Builder out = Component.text();
        int i = 0;
        while (i < template.length()) {
            int open = template.indexOf('{', i);
            int close = open < 0 ? -1 : template.indexOf('}', open);
            if (open < 0 || close < 0) {
                out.append(Component.text(template.substring(i), color));
                break;
            }
            if (open > i) {
                out.append(Component.text(template.substring(i, open), color));
            }
            Component v = values.get(template.substring(open + 1, close));
            out.append(v != null ? v : Component.text(template.substring(open, close + 1), color));
            i = close + 1;
        }
        return out.build();
    }

    /** The plain text of a component tree (for /mcbrand selftest's output). */
    static String flat(Component c) {
        StringBuilder b = new StringBuilder();
        if (c instanceof TextComponent t) {
            b.append(t.content());
        }
        for (Component child : c.children()) {
            b.append(flat(child));
        }
        return b.toString();
    }

    /** "5 minutes", "1 hour", "3 days": how long ago {@code thenMs} was (never negative). */
    static String ago(long nowMs, long thenMs) {
        long s = Math.max(0, (nowMs - thenMs) / 1000);
        if (s < 3600) {
            return plural(Math.max(1, s / 60), "minute");
        }
        if (s < 48 * 3600) {
            return plural(s / 3600, "hour");
        }
        return plural(s / 86400, "day");
    }

    private static String plural(long n, String unit) {
        return n + " " + unit + (n == 1 ? "" : "s");
    }

    static Component plain(String s, TextColor color) {
        return Component.text(s, color).decoration(TextDecoration.ITALIC, false);
    }

    static Component bold(String s, TextColor color) {
        return Component.text(s, color).decoration(TextDecoration.BOLD, true).decoration(TextDecoration.ITALIC, false);
    }

    static TextColor pingColor(int ms) {
        return ms < 80 ? NamedTextColor.GREEN : ms < 200 ? NamedTextColor.YELLOW : NamedTextColor.RED;
    }

    static TextColor tpsColor(double tps) {
        return tps >= 19 ? NamedTextColor.GREEN : tps >= 17 ? NamedTextColor.YELLOW : NamedTextColor.RED;
    }
}
