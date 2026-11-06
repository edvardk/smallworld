package io.github.edvardk.smallworld.client;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.regex.Pattern;

/**
 * The two local files (client/mod/WIRE.md "Local files"), read once at start-up.
 *
 * <ul>
 *   <li>{@code config/smallworld-pack.json}, owned by the modpack (the .mrpack writes it, an update replaces it):
 *       {@code {"tier": "medium", "pack": "26.3-0123456789ab"}}. Missing or unreadable: tier {@code none}, pack
 *       empty (the mod was installed without the pack).
 *   <li>{@code config/smallworld.json}, owned by the player (created with the defaults if missing):
 *       {@code {"hud": true}}. {@code hud: false} hides the FPS whatever the server says.
 * </ul>
 *
 * <p>The JVM properties {@code smallworld.tier}, {@code smallworld.pack} and {@code smallworld.hud} override both.
 */
record LocalConfig(String tier, String pack, boolean hud) {
    static final String PACK_FILE = "smallworld-pack.json";
    static final String PLAYER_FILE = "smallworld.json";
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9._+-]{1,64}");

    static LocalConfig load(Path configDir) {
        JsonObject pack = read(configDir.resolve(PACK_FILE));
        Path playerFile = configDir.resolve(PLAYER_FILE);
        if (!Files.exists(playerFile)) {
            try {
                Files.createDirectories(configDir);
                Files.writeString(playerFile, "{\"hud\": true}\n", StandardCharsets.UTF_8);
            } catch (IOException e) {
                // a read-only instance still works with the defaults
            }
        }
        JsonObject player = read(playerFile);
        String tier = token(System.getProperty("smallworld.tier", string(pack, "tier")), "none");
        String version = token(System.getProperty("smallworld.pack", string(pack, "pack")), "");
        String hudProp = System.getProperty("smallworld.hud");
        boolean hud = hudProp != null ? !hudProp.equalsIgnoreCase("false") : bool(player, "hud", true);
        return new LocalConfig(tier, version, hud);
    }

    private static JsonObject read(Path file) {
        try {
            JsonElement e = JsonParser.parseString(Files.readString(file, StandardCharsets.UTF_8));
            return e.isJsonObject() ? e.getAsJsonObject() : new JsonObject();
        } catch (IOException | RuntimeException e) {
            return new JsonObject();
        }
    }

    private static String string(JsonObject o, String key) {
        JsonElement e = o.get(key);
        return e != null && e.isJsonPrimitive() ? e.getAsString() : null;
    }

    private static boolean bool(JsonObject o, String key, boolean fallback) {
        JsonElement e = o.get(key);
        return e != null && e.isJsonPrimitive() && e.getAsJsonPrimitive().isBoolean() ? e.getAsBoolean() : fallback;
    }

    /** Only short, plain tokens leave the machine; anything else becomes the fallback. */
    private static String token(String value, String fallback) {
        return value != null && TOKEN.matcher(value).matches() ? value : fallback;
    }
}
