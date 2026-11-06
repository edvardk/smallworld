package io.github.edvardk.mcbrand;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import net.kyori.adventure.text.format.TextColor;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;

/**
 * plugins/mc-brand/config.yml, written by Ansible from server.yml's {@code branding:} section (mcctl render; the
 * plugin only reads it). Every value is clamped here as well, so a hand-edited file can't make the plugin do
 * expensive work.
 */
record Settings(
        String name,
        TextColor text,
        TextColor accent,
        TextColor secondary,
        TextColor subtle,
        boolean title,
        String subtitleFirst,
        String subtitleBack,
        String sound,
        String firstJoin,
        String welcomeBack,
        List<String> rules,
        String discordInvite,
        String rulesUrl,
        String website,
        boolean hudEnabled,
        String hudDefaultMode,
        int hudSeconds,
        int hudMaxPlayers,
        int hudSettleSeconds,
        boolean tabList,
        String tabHeader,
        boolean drinkEnabled,
        String drinkName,
        List<String> drinkLore,
        String drinkCommand,
        int drinkColor,
        int cooldownSeconds,
        int nauseaSeconds,
        boolean giveOnFirstJoin,
        boolean clientEnabled,
        boolean clientPrompt,
        String clientUrl,
        String clientRequestDir,
        String clientCodeFile,
        Map<String, String> clientPacks,
        int clientFpsInterval,
        boolean clientFpsHud,
        boolean mapPlayers,
        boolean mapFairPlay,
        boolean viewEnabled,
        int viewMax,
        int viewCheckSeconds,
        List<Holograms.Spec> holograms) {

    static final int MAX_RULES = 20;
    static final int MAX_TEXT = 256;

    static Settings load(YamlConfiguration cfg) {
        ConfigurationSection colors = section(cfg, "colors");
        ConfigurationSection join = section(cfg, "join");
        ConfigurationSection hud = section(cfg, "hud");
        ConfigurationSection drink = section(cfg, "drink");
        ConfigurationSection client = section(cfg, "client");
        ConfigurationSection view = section(cfg, "view");
        ConfigurationSection map = section(cfg, "map");
        String name = text(cfg.getString("name"), "Minecraft");
        return new Settings(
                name,
                color(colors.getString("text"), 0xFFFFFF),
                color(colors.getString("accent"), 0x55AAFF),
                color(colors.getString("secondary"), 0x2E6FB0),
                color(colors.getString("subtle"), 0xC8D0D8),
                join.getBoolean("title", true),
                text(join.getString("subtitle_first"), "Welcome, {player}!"),
                text(join.getString("subtitle_back"), "Welcome back, {player}!"),
                text(join.getString("sound"), ""),
                text(join.getString("first_join"), "Welcome to {server}, {player}!"),
                text(join.getString("welcome_back"), "Welcome back, {player}! Last seen {ago} ago."),
                lines(cfg.getStringList("rules"), MAX_RULES),
                url(cfg.getString("discord_invite")),
                url(cfg.getString("rules_url")),
                url(cfg.getString("website")),
                hud.getBoolean("enabled", true),
                // v1.0.1: top|side|off; a config from before it has only default_on (true = the sidebar)
                hud.getString("default_mode", hud.getBoolean("default_on", false) ? "side" : "top"),
                clamp(hud.getInt("interval_seconds", 5), 2, 30),
                clamp(hud.getInt("max_players", 8), 1, 10),
                clamp(hud.getInt("settle_seconds", 10), 3, 30), // v1.1: the join grace period
                hud.getBoolean("tab_list", true),
                text(hud.getString("tab_header"), name),
                drink.getBoolean("enabled", false),
                text(drink.getString("name"), name + " Reserve"),
                lines(drink.getStringList("lore"), 4),
                command(drink.getString("command")), // v1.2: /drink or the server's own name
                color(drink.getString("color"), 0x3A6EA5).value(),
                clamp(drink.getInt("cooldown_seconds", 30), 5, 600),
                clamp(drink.getInt("nausea_seconds", 10), 0, 30),
                drink.getBoolean("give_on_first_join", true),
                // v1.1: /modpack (the client modpack's setup page) and /view
                client.getBoolean("enabled", false),
                client.getBoolean("prompt", true),
                url(client.getString("url")),
                path(client.getString("request_dir"), "/run/mc-telemetry/client"),
                path(client.getString("code_file"), "/run/mc-verdicts/client-codes.tsv"),
                // v1.2: the smallworld channels (Smallworld): the current pack version per tier (the config
                // repo's client lock, rendered), the FPS report interval and HUD flag sent to the client mod
                packs(client.getConfigurationSection("packs")),
                clamp(client.getInt("fps_interval_seconds", 5), 2, 60),
                client.getBoolean("fps_hud", true),
                // v1.2: Xaero's maps (XaeroMap): friends on the map, the fair-play code
                map.getBoolean("players", true),
                map.getBoolean("fair_play", true),
                view.getBoolean("enabled", false),
                clamp(view.getInt("max", 16), 2, 32),
                clamp(view.getInt("check_seconds", 5), 2, 60),
                holograms(cfg.getMapList("holograms")));
    }

    private static ConfigurationSection section(YamlConfiguration cfg, String key) {
        ConfigurationSection s = cfg.getConfigurationSection(key);
        return s != null ? s : cfg.createSection(key);
    }

    static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    private static String text(String s, String fallback) {
        if (s == null) {
            return fallback;
        }
        String one = s.replaceAll("[\\p{Cntrl}]", " ").strip();
        return one.length() > MAX_TEXT ? one.substring(0, MAX_TEXT) : one;
    }

    private static List<String> lines(List<String> in, int max) {
        return in.stream().limit(max).map(s -> text(s, "")).filter(s -> !s.isEmpty()).toList();
    }

    /** Only https:// links are shown or sent (the client opens them after a confirmation screen). */
    private static String url(String s) {
        String v = text(s, "");
        return v.startsWith("https://") && v.indexOf(' ') < 0 ? v : "";
    }

    /** v1.2: the drink's command: 3-16 lowercase letters (= server.yml's pattern), else "drink". */
    static String command(String s) {
        String v = text(s, "");
        return v.matches("[a-z]{3,16}") ? v : "drink";
    }

    /** v1.2: branding.holograms (block coordinates -> the block's centre, half a block up), clamped. */
    static List<Holograms.Spec> holograms(List<Map<?, ?>> in) {
        List<Holograms.Spec> out = new java.util.ArrayList<>();
        for (Map<?, ?> m : in) {
            if (out.size() >= Holograms.MAX || !(m.get("x") instanceof Integer x) || !(m.get("y") instanceof Integer y)
                    || !(m.get("z") instanceof Integer z)) {
                continue;
            }
            String world = m.get("world") instanceof String w && w.matches("minecraft:[a-z_]{1,32}") ? w
                    : "minecraft:overworld";
            List<String> lines = new java.util.ArrayList<>();
            if (m.get("lines") instanceof List<?> l) {
                for (Object o : l) {
                    if (o instanceof String str && lines.size() < Holograms.MAX_LINES && !text(str, "").isEmpty()) {
                        lines.add(text(str, ""));
                    }
                }
            }
            boolean health = Boolean.TRUE.equals(m.get("health"));
            if (!lines.isEmpty() || health) {
                out.add(new Holograms.Spec(world, x + 0.5, y + 0.5, z + 0.5, List.copyOf(lines), health));
            }
        }
        return List.copyOf(out);
    }

    /** tier -> pack version id; only short tokens (= what the client mod sends), at most 8 tiers. */
    private static Map<String, String> packs(ConfigurationSection sec) {
        Map<String, String> out = new TreeMap<>();
        if (sec == null) {
            return Map.of();
        }
        for (String tier : sec.getKeys(false)) {
            String v = sec.getString(tier);
            if (out.size() < 8 && Smallworld.token(tier).equals(tier) && v != null && Smallworld.token(v).equals(v)) {
                out.put(tier, v);
            }
        }
        return Map.copyOf(out);
    }

    /** An absolute path under /run (the tmpfs hand-off), else the default. */
    private static String path(String s, String fallback) {
        String v = text(s, "");
        return v.startsWith("/run/") && !v.contains("..") && v.indexOf(' ') < 0 ? v : fallback;
    }

    private static TextColor color(String hex, int fallback) {
        TextColor c = hex == null ? null : TextColor.fromHexString(hex.strip());
        return c != null ? c : TextColor.color(fallback);
    }
}
