package io.github.edvardk.mcbrand;

import java.util.EnumSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
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
        Set<Hud.Part> hudParts,
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
        boolean mapPlayers,
        boolean mapFairPlay,
        boolean viewEnabled,
        int viewMax,
        int viewCheckSeconds,
        List<Holograms.Spec> holograms,
        boolean compassEnabled,
        boolean compassDefaultOn,
        int compassMaxRange,
        int compassRounding,
        int compassHeight,
        boolean pocketMapEnabled,
        boolean pocketMapPrerender,
        String prerenderDir,
        String colorsFile,
        MapRoom.Spec mapRoom) {

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
        ConfigurationSection compass = section(cfg, "compass");
        ConfigurationSection pocketMap = section(cfg, "pocket_map");
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
                // v1.4.1: the server's default HUD parts (branding.hud.parts; players keep deltas on top); a
                // config from before it has none: every part
                hud.isList("parts") ? Hud.parseParts(hud.getStringList("parts")) : EnumSet.allOf(Hud.Part.class),
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
                // repo's client lock, rendered) and the FPS report interval sent to the client mod (v1.4.1: the
                // HUD flag is always off, mc-brand's HUD shows the FPS; client.fps.hud is rendered into hud.parts)
                packs(client.getConfigurationSection("packs")),
                clamp(client.getInt("fps_interval_seconds", 5), 2, 60),
                // v1.2: Xaero's maps (XaeroMap): friends on the map, the fair-play code
                map.getBoolean("players", true),
                map.getBoolean("fair_play", true),
                view.getBoolean("enabled", false),
                clamp(view.getInt("max", 16), 2, 32),
                clamp(view.getInt("check_seconds", 5), 2, 60),
                holograms(cfg.getMapList("holograms")),
                // v1.3: the friend compass and the pocket map (Compass, PocketMap)
                compass.getBoolean("enabled", true),
                compass.getBoolean("default_on", true),
                clamp(compass.getInt("max_range", 1000), 16, 100_000),
                clamp(compass.getInt("distance_rounding", 5), 1, 100),
                clamp(compass.getInt("height_threshold", 8), 2, 64),
                pocketMap.getBoolean("enabled", true),
                // v1.4: the world map pre-render (PreRender) and the spawn map room (MapRoom)
                pocketMap.getBoolean("prerender", true),
                file(pocketMap.getString("prerender_dir"), "/var/lib/"),
                file(pocketMap.getString("colors_file"), "/opt/minecraft/"),
                mapRoom(section(cfg, "map_room")));
    }

    /** v1.4: branding.map_room, clamped (sizes, offsets, scale); anything malformed disables the room. */
    static MapRoom.Spec mapRoom(ConfigurationSection room) {
        boolean spawn = "spawn".equals(room.getString("at"));
        boolean coords = room.isInt("x") && room.isInt("y") && room.isInt("z");
        List<Integer> off = room.getIntegerList("offset");
        int[] o = off.size() == 3 ? new int[] {clamp(off.get(0), -64, 64), clamp(off.get(1), -64, 64),
            clamp(off.get(2), -64, 64)} : new int[] {0, 0, -6};
        String world = text(room.getString("world"), "minecraft:overworld");
        String facing = text(room.getString("facing"), "south");
        String view = text(room.getString("view"), "border");
        return new MapRoom.Spec(
                room.getBoolean("enabled", false) && (spawn || coords) && world.matches("minecraft:(overworld|the_end)"),
                world, spawn, o[0], o[1], o[2],
                clamp(room.getInt("x", 0), -29_999_984, 29_999_984),
                clamp(room.getInt("y", 64), -64, 319),
                clamp(room.getInt("z", 0), -29_999_984, 29_999_984),
                facing.matches("north|south|east|west") ? facing : "south",
                clamp(room.getInt("width", 3), 1, 8),
                clamp(room.getInt("height", 3), 1, 6),
                view.equals("spawn") ? "spawn" : "border",
                clamp(room.getInt("scale", 3), 0, 4),
                clamp(room.getInt("radius", 0), 16, 30_000_000),
                room.getBoolean("cursors", false));
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

    /**
     * v1.2: branding.holograms (block coordinates -> the block's centre, half a block up), clamped. v1.3: {@code at:
     * spawn} instead of x/y/z (placed above the world spawn when it spawns: {@link Holograms}), {@code help: true}
     * appends /help's list.
     */
    static List<Holograms.Spec> holograms(List<Map<?, ?>> in) {
        List<Holograms.Spec> out = new java.util.ArrayList<>();
        for (Map<?, ?> m : in) {
            boolean spawn = "spawn".equals(m.get("at"));
            boolean coords = m.get("x") instanceof Integer && m.get("y") instanceof Integer
                    && m.get("z") instanceof Integer;
            if (out.size() >= Holograms.MAX || !(spawn || coords)) {
                continue;
            }
            int x = spawn ? 0 : (Integer) m.get("x");
            int y = spawn ? 0 : (Integer) m.get("y");
            int z = spawn ? 0 : (Integer) m.get("z");
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
            boolean help = Boolean.TRUE.equals(m.get("help"));
            if (!lines.isEmpty() || health || help) {
                out.add(new Holograms.Spec(world, x + 0.5, y + 0.5, z + 0.5, List.copyOf(lines), health, spawn,
                        help));
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

    /** v1.4: an absolute path under {@code prefix} (product constants from the template), else "" (off). */
    private static String file(String s, String prefix) {
        String v = text(s, "");
        return v.startsWith(prefix) && !v.contains("..") && v.indexOf(' ') < 0 ? v : "";
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
