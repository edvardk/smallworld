package io.github.edvardk.mcbrand;

import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.TextDecoration;
import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.plugin.messaging.Messenger;
import org.bukkit.plugin.messaging.PluginMessageListener;

/**
 * v1.2: the server side of the smallworld channels (client/mod/WIRE.md; the byte layout is {@link Wire}, the
 * client mod's own class).
 *
 * <ul>
 *   <li>{@code smallworld:hello} (once per join): the player has the pack, which tier, which pack version. That is
 *       the exact "has the pack" test for the join prompt ({@link Modpack}); a pack version other than the
 *       server's current one for that tier (rendered from the config repo's client lock into
 *       {@code client.packs}) gets one "your pack is outdated" line. Answered with {@code smallworld:config}
 *       (the FPS report interval and the HUD flag).
 *   <li>{@code smallworld:fps} is telemetry: plugin/mc-telemetry listens to it itself (the per-player FPS in its
 *       snapshots: mc-agent's minute rows, the Worker's "server fine / your PC" diagnosis).
 * </ul>
 *
 * <p>Bukkit's Messenger calls listeners on the main thread. Per hello: one decode of a few bytes, one map put and
 * one small payload back (no IO). Only short tokens ({@code [A-Za-z0-9._+-]{1,64}}, = the client's own rule) are kept; anything else is
 * treated as missing. Nothing is stored beyond the session.
 */
final class Smallworld implements PluginMessageListener {
    static final String HELLO = "smallworld:hello";
    static final String CONFIG = "smallworld:config";
    static final Pattern TOKEN = Pattern.compile("^[A-Za-z0-9._+-]{1,64}$");
    static final String OUTDATED = "Your {server} pack is out of date: a newer one is on the setup page.";

    enum PackStatus { NONE, CURRENT, OUTDATED, UNKNOWN }

    record Client(String modVersion, String tier, String pack) {}

    private final BrandPlugin plugin;
    private final Settings s;
    private final Map<UUID, Client> clients = new ConcurrentHashMap<>();
    long hellos;
    long malformed;
    long outdatedPrompts;

    Smallworld(BrandPlugin plugin, Settings settings) {
        this.plugin = plugin;
        this.s = settings;
    }

    void start() {
        Messenger m = Bukkit.getMessenger();
        m.registerIncomingPluginChannel(plugin, HELLO, this);
        m.registerOutgoingPluginChannel(plugin, CONFIG);
    }

    void stop() {
        Messenger m = Bukkit.getMessenger();
        m.unregisterIncomingPluginChannel(plugin, HELLO, this);
        m.unregisterOutgoingPluginChannel(plugin, CONFIG);
        clients.clear();
    }

    static String token(String v) {
        return v != null && TOKEN.matcher(v).matches() ? v : "";
    }

    /** The pack the player reported against the server's current one for that tier (packs: tier -> version id). */
    static PackStatus status(Map<String, String> packs, String tier, String pack) {
        if (pack.isEmpty()) {
            return PackStatus.NONE; // the mod on its own (no pack file): nothing to compare
        }
        String current = packs.get(tier);
        if (current == null || current.isEmpty()) {
            return PackStatus.UNKNOWN; // a tier the server doesn't offer (or no client lock rendered)
        }
        return current.equals(pack) ? PackStatus.CURRENT : PackStatus.OUTDATED;
    }

    /** The config payload this server sends (WIRE.md: protocol, interval seconds, flags). */
    static byte[] config(int intervalSeconds, boolean hud) {
        return Wire.encode(new Wire.Config(Wire.PROTOCOL, intervalSeconds, hud ? Wire.FLAG_HUD : 0));
    }

    @Override
    public void onPluginMessageReceived(String channel, Player player, byte[] message) {
        try {
            if (channel.equals(HELLO)) {
                hello(player, Wire.decodeHello(message));
            }
        } catch (IllegalArgumentException e) {
            malformed++; // a broken or foreign payload: ignored
        }
    }

    private void hello(Player p, Wire.Hello h) {
        hellos++;
        Client c = new Client(token(h.modVersion()), token(h.tier()), token(h.pack()));
        clients.put(p.getUniqueId(), c);
        plugin.onSmallworldHello(p);
        sendConfig(p);
        if (s.clientEnabled() && s.clientPrompt()
                && status(s.clientPacks(), c.tier(), c.pack()) == PackStatus.OUTDATED) {
            outdatedPrompts++;
            Bukkit.getScheduler().runTaskLater(plugin, () -> {
                if (p.isOnline()) {
                    p.sendMessage(outdated());
                }
            }, 60L); // after the greeting
        }
    }

    /**
     * The config payload. Paper sends a plugin message only on a channel the client registered
     * ({@code minecraft:register}); Fabric registers its receivers when the play phase starts, so this normally
     * goes through at the hello, and {@link #registered} sends it again if the registration came later.
     */
    void sendConfig(Player p) {
        p.sendPluginMessage(plugin, CONFIG, config(s.clientFpsInterval(), s.clientFpsHud()));
    }

    /** PlayerRegisterChannelEvent: the client can take the config now (only after its hello: a known client). */
    void registered(Player p, String channel) {
        if (channel.equals(CONFIG) && clients.containsKey(p.getUniqueId())) {
            sendConfig(p);
        }
    }

    Component outdated() {
        return Component.text()
                .append(Texts.fill(OUTDATED, s.text(), Map.of("server", Texts.bold(s.name(), s.accent()))))
                .append(Component.text(" "))
                .append(Component.text("[Update]", s.accent()).decoration(TextDecoration.UNDERLINED, true)
                        .clickEvent(ClickEvent.runCommand("/modpack"))
                        .hoverEvent(HoverEvent.showText(Component.text("Gives you a personal link to the setup page"))))
                .build();
    }

    /** Whether this player's client said hello with a pack (the exact test; v1.1 guessed from the brand). */
    boolean hasPack(Player p) {
        Client c = clients.get(p.getUniqueId());
        return c != null && !c.pack().isEmpty();
    }

    Client client(UUID id) {
        return clients.get(id);
    }

    void forget(Player p) {
        clients.remove(p.getUniqueId());
    }

    /** For /mcbrand selftest: the decoder on WIRE.md's worked example and the pack rule. */
    String selftest() {
        byte[] example = java.util.HexFormat.of().parseHex(
                "0105312e302e3006" + "6d656469756d" + "11" + "32362e332d303132333435363738396162");
        Wire.Hello h = Wire.decodeHello(example);
        Map<String, String> packs = Map.of("medium", "26.3-0123456789ab");
        return "smallworld hello " + (h.protocol() == 1 && h.tier().equals("medium")
                && h.pack().equals("26.3-0123456789ab")) + ", pack " + status(packs, "medium", h.pack()) + "/"
                + status(packs, "medium", "26.3-ffffffffffff") + "/" + status(packs, "medium", "") + "/"
                + status(Map.of(), "medium", h.pack()) + ", config " + java.util.HexFormat.of().formatHex(config(5, true))
                + ", hellos " + hellos + " malformed " + malformed;
    }
}
