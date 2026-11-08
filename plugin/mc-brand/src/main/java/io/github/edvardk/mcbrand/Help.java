package io.github.edvardk.mcbrand;

import java.util.ArrayList;
import java.util.List;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.TextComponent;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.event.HoverEvent;
import org.bukkit.command.CommandSender;

/**
 * v1.3: {@code /help} (alias {@code /commands}) and the help hologram: one curated list of mc-brand's commands, built
 * from the enabled features. <b>Every new player command registers here</b> ({@link #entries}; tests/test_brand.py
 * checks that each plugin.yml command but /help is listed). /help shows the ones the sender may use (permission +
 * feature on), each clickable (the click types the command into the chat box, it doesn't run it); the hologram shows
 * the ones every player may use (one source, never out of sync). Vanilla's list stays reachable as
 * {@code /minecraft:help} (a plugin command replaces only the bare label: Paper's CommandMap, checked in the pinned
 * jar: VanillaCommandWrapper.canBeOverriden).
 */
final class Help {
    private Help() {}

    /** One command: {@code admin} ones (operators) never go on the hologram. */
    record Entry(String command, String usage, String description, String permission, boolean admin) {
        String suggestion() {
            return "/" + command + (usage.isEmpty() ? "" : " ");
        }
    }

    /** THE list: in the order players see it. */
    static List<Entry> entries(Settings s) {
        List<Entry> out = new ArrayList<>();
        if (!s.rules().isEmpty()) {
            out.add(new Entry("rules", "", "the house rules", "mcbrand.rules", false));
        }
        if (!s.discordInvite().isEmpty()) {
            out.add(new Entry("discord", "", "our Discord invite", "mcbrand.discord", false));
        }
        if (s.pocketMapEnabled()) {
            out.add(new Entry("map", "[zoom]", "a pocket map with your friends on it", "mcbrand.map", false));
        }
        if (s.compassEnabled()) {
            out.add(new Entry("compass", "[on|off|hide]", "the friend compass at the top", "mcbrand.compass",
                    false));
        }
        if (s.hudEnabled()) {
            out.add(new Entry("hud", "[top|side|off|minimal|full|parts]", "server speed and your ping", "mcbrand.hud",
                    false));
        }
        if (s.viewEnabled()) {
            out.add(new Entry("view", "[number|reset]", "your own view distance", "mcbrand.view", false));
        }
        if (s.clientEnabled()) {
            out.add(new Entry("modpack", "", "your link to the optional client pack", "mcbrand.modpack", false));
        }
        if (s.drinkEnabled()) {
            out.add(new Entry(s.drinkCommand(), "", "get your " + s.drinkName() + " back", "mcbrand.drink", false));
        }
        out.add(new Entry("mcbrand", "status|selftest", "mc-brand status (operators)", "mcbrand.admin", true));
        return List.copyOf(out);
    }

    static List<Entry> visible(Settings s, CommandSender who) {
        return entries(s).stream().filter(e -> who.hasPermission(e.permission())).toList();
    }

    static List<Entry> everyone(Settings s) {
        return entries(s).stream().filter(e -> !e.admin()).toList();
    }

    /** The chat answer: a heading, one clickable line per command, vanilla's list last. */
    static Component chat(Settings s, List<Entry> entries) {
        TextComponent.Builder b = Component.text()
                .append(Texts.bold(s.name(), s.accent()))
                .append(Component.text(" commands (click one to type it)", s.subtle()));
        for (Entry e : entries) {
            b.append(Component.newline()).append(line(s, e));
        }
        b.append(Component.newline()).append(Component.text("All commands: ", s.subtle()))
                .append(Component.text("/minecraft:help", s.accent())
                        .clickEvent(ClickEvent.suggestCommand("/minecraft:help"))
                        .hoverEvent(HoverEvent.showText(Component.text("Vanilla's full list"))));
        return b.build();
    }

    static Component line(Settings s, Entry e) {
        String cmd = "/" + e.command() + (e.usage().isEmpty() ? "" : " " + e.usage());
        return Component.text()
                .append(Component.text(cmd, s.accent()))
                .append(Component.text(" - " + e.description(), s.text()))
                .clickEvent(ClickEvent.suggestCommand(e.suggestion()))
                .hoverEvent(HoverEvent.showText(Component.text("Click to type " + e.suggestion().strip())))
                .build();
    }

    /** The hologram's part (branding.holograms[].help): a heading and one line per command everyone may use. */
    static List<Component> hologram(Settings s) {
        List<Component> out = new ArrayList<>();
        out.add(Component.text("Commands", s.subtle()));
        for (Entry e : everyone(s)) {
            out.add(Component.text()
                    .append(Component.text("/" + e.command(), s.accent()))
                    .append(Component.text(" " + e.description(), s.text()))
                    .build());
        }
        return List.copyOf(out);
    }

    /** For /mcbrand selftest: the commands every player gets (tests/e2e/brand_check.py). */
    static String selftest(Settings s) {
        return "help " + String.join(" ", everyone(s).stream().map(e -> "/" + e.command()).toList())
                + ", hologram lines " + hologram(s).size();
    }
}
