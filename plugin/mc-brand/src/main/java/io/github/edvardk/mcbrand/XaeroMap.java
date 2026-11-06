package io.github.edvardk.mcbrand;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import net.kyori.adventure.text.Component;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.plugin.messaging.Messenger;
import org.bukkit.plugin.messaging.PluginMessageListener;
import org.bukkit.potion.PotionEffectType;

/**
 * v1.2: Xaero's Minimap and World Map (in the client pack) on this server, server.yml {@code client.map}.
 *
 * <ul>
 *   <li><b>Fair play</b> ({@code client.map.fair_play}): Xaero's code {@link #FAIR_PLAY} in a system chat message
 *       turns off the cave map and the entity radar for that session (the mod checks
 *       {@code Component.getString().contains(...)}: checked in the 26.3 minimap jar, ClientEvents). It is sent as
 *       LITERAL text in an Adventure component, never through legacy formatting (which would parse the section
 *       signs away), once per session to every client that has Xaero (it registered the Xaero channel) or the
 *       pack (the Smallworld hello).
 *   <li><b>Friends on the map</b> ({@code client.map.players}): Xaero's own server-side "synced player tracker",
 *       reimplemented here (the protocol as XaeroTracker (MIT) and the 26.3 jars use it; no code taken). Channels
 *       {@link #MINIMAP} and {@link #WORLD_MAP}; a payload is one byte (the packet index) and its body:
 *       0 level id (int: which map storage the client uses; a stable random id kept in the main world's PDC),
 *       1 handshake (int network version, {@link #NETWORK} = 3, both ways), 2 tracked player (network NBT: an
 *       unnamed compound with byte r = remove, int[4] i = UUID, doubles x/y/z, string d = dimension key),
 *       3 reset. Every online player in the same dimension shows on the others' maps at any distance (fair play
 *       still hides every entity on the radar: these are the mod's separate player markers). Spectators and
 *       invisible players are not shown.
 * </ul>
 *
 * <p>Cost: positions are sent by one task every {@link #PERIOD_TICKS} ticks, only for players who moved
 * (more than {@link #MIN_MOVE} blocks) and only to clients that answered the handshake; one encode per moved
 * player per run, no IO. With nobody using Xaero online the task does nothing but a map check.
 */
final class XaeroMap implements PluginMessageListener {
    static final String MINIMAP = "xaerominimap:main";
    static final String WORLD_MAP = "xaeroworldmap:main";
    static final String FAIR_PLAY = "\u00a7f\u00a7a\u00a7i\u00a7r\u00a7x\u00a7a\u00a7e\u00a7r\u00a7o";
    static final int NETWORK = 3;
    static final int LEVEL_ID = 0;
    static final int HANDSHAKE = 1;
    static final int TRACKED = 2;
    static final int RESET = 3;
    static final long PERIOD_TICKS = 10L;
    static final double MIN_MOVE = 0.5;

    /** One client's Xaero state: which channels answered the handshake (the minimap's is preferred). */
    private static final class Viewer {
        boolean fairPlaySent;
        boolean minimap; // the client answered on the minimap's channel
        boolean worldMap;

        String channel() {
            return minimap ? MINIMAP : worldMap ? WORLD_MAP : null;
        }
    }

    /** What the others were last told about one player (null world: nothing sent yet / untracked). */
    private record Sent(String world, double x, double y, double z) {}

    private final BrandPlugin plugin;
    private final Settings s;
    private final NamespacedKey levelKey;
    private final Map<UUID, Viewer> viewers = new HashMap<>(); // main thread only
    private final Map<UUID, Sent> sent = new HashMap<>();
    private int levelId;
    long handshakes;
    long updates;
    long fairPlay;

    XaeroMap(BrandPlugin plugin, Settings settings) {
        this.plugin = plugin;
        this.s = settings;
        this.levelKey = new NamespacedKey(plugin, "xaero_level_id");
    }

    boolean enabled() {
        return s.clientEnabled() && (s.mapPlayers() || s.mapFairPlay());
    }

    void start() {
        if (!enabled()) {
            return;
        }
        levelId = levelId();
        Messenger m = Bukkit.getMessenger();
        for (String ch : new String[] {MINIMAP, WORLD_MAP}) {
            m.registerOutgoingPluginChannel(plugin, ch);
            m.registerIncomingPluginChannel(plugin, ch, this);
        }
    }

    void stop() {
        Messenger m = Bukkit.getMessenger();
        for (String ch : new String[] {MINIMAP, WORLD_MAP}) {
            m.unregisterIncomingPluginChannel(plugin, ch, this);
            m.unregisterOutgoingPluginChannel(plugin, ch);
        }
        viewers.clear();
        sent.clear();
    }

    /** A stable id per world save (Xaero keeps one map per server + level id): created once, in the PDC. */
    private int levelId() {
        World main = Bukkit.getWorlds().isEmpty() ? null : Bukkit.getWorlds().get(0);
        if (main == null) {
            return 0;
        }
        PersistentDataContainer pdc = main.getPersistentDataContainer();
        Integer id = pdc.get(levelKey, PersistentDataType.INTEGER);
        if (id == null) {
            id = ThreadLocalRandom.current().nextInt();
            pdc.set(levelKey, PersistentDataType.INTEGER, id);
        }
        return id;
    }

    // --- events (main thread) ------------------------------------------------------------------------------------

    /** The client registered a Xaero channel (minecraft:register): it has the mod. */
    void registered(Player p, String channel) {
        if (!enabled() || !(channel.equals(MINIMAP) || channel.equals(WORLD_MAP))) {
            return;
        }
        viewers.computeIfAbsent(p.getUniqueId(), k -> new Viewer());
        sendFairPlay(p);
        if (s.mapPlayers()) {
            p.sendPluginMessage(plugin, channel, intPacket(HANDSHAKE, NETWORK));
            p.sendPluginMessage(plugin, channel, intPacket(LEVEL_ID, levelId));
        }
    }

    /** The client mod's hello: the pack includes Xaero, so fair play applies even before its channel shows. */
    void hello(Player p) {
        if (enabled()) {
            viewers.computeIfAbsent(p.getUniqueId(), k -> new Viewer());
            sendFairPlay(p);
        }
    }

    private void sendFairPlay(Player p) {
        Viewer v = viewers.get(p.getUniqueId());
        if (!s.mapFairPlay() || v == null || v.fairPlaySent) {
            return;
        }
        v.fairPlaySent = true;
        fairPlay++;
        p.sendMessage(fairPlayMessage());
    }

    Component fairPlayMessage() {
        return Component.text("Map: fair play here (no cave map, no entity radar)"
                + (s.mapPlayers() ? "; your friends show on it." : "."), s.subtle())
                .append(Component.text(FAIR_PLAY)); // literal: Xaero reads the message's plain string
    }

    @Override
    public void onPluginMessageReceived(String channel, Player player, byte[] message) {
        if (!s.mapPlayers() || message.length < 5 || message[0] != HANDSHAKE) {
            return; // only the client's handshake answer matters here
        }
        Viewer v = viewers.computeIfAbsent(player.getUniqueId(), k -> new Viewer());
        boolean first = v.channel() == null;
        if (channel.equals(MINIMAP)) {
            v.minimap = true;
        } else if (channel.equals(WORLD_MAP)) {
            v.worldMap = true;
        }
        handshakes++;
        if (first || channel.equals(MINIMAP)) { // (re)start on the preferred channel
            player.sendPluginMessage(plugin, v.channel(), new byte[] {RESET});
            for (Player other : player.getWorld().getPlayers()) {
                if (other != player && shown(other)) {
                    player.sendPluginMessage(plugin, v.channel(), tracked(other.getUniqueId(), false, other.getX(),
                            other.getY(), other.getZ(), other.getWorld().getKey().toString()));
                }
            }
        }
    }

    /** Not on anyone's map: spectators and invisible players (an admin watching, a hiding friend). */
    static boolean shown(Player p) {
        return p.getGameMode() != GameMode.SPECTATOR && !p.isInvisible()
                && !p.hasPotionEffect(PotionEffectType.INVISIBILITY);
    }

    /** The periodic task: every moved (or changed) player's position to the others in the same world. */
    void tick() {
        if (!s.mapPlayers() || viewers.isEmpty()) {
            return;
        }
        Location loc = new Location(null, 0, 0, 0);
        for (Player p : Bukkit.getOnlinePlayers()) {
            UUID id = p.getUniqueId();
            Sent last = sent.get(id);
            boolean show = shown(p);
            p.getLocation(loc);
            String world = p.getWorld().getKey().toString();
            if (!show) {
                if (last != null && last.world() != null) {
                    broadcast(p, last.world(), tracked(id, true, 0, 0, 0, ""));
                    sent.remove(id);
                }
                continue;
            }
            if (last != null && last.world().equals(world) && Math.abs(last.x() - loc.getX()) < MIN_MOVE
                    && Math.abs(last.y() - loc.getY()) < MIN_MOVE && Math.abs(last.z() - loc.getZ()) < MIN_MOVE) {
                continue;
            }
            if (last != null && !last.world().equals(world)) {
                broadcast(p, last.world(), tracked(id, true, 0, 0, 0, "")); // left their dimension
            }
            broadcast(p, world, tracked(id, false, loc.getX(), loc.getY(), loc.getZ(), world));
            sent.put(id, new Sent(world, loc.getX(), loc.getY(), loc.getZ()));
            updates++;
        }
    }

    /** One player's packet to every handshaken viewer in that world (not to the player). */
    private void broadcast(Player about, String world, byte[] packet) {
        for (Player v : Bukkit.getOnlinePlayers()) {
            Viewer viewer = viewers.get(v.getUniqueId());
            if (v != about && viewer != null && viewer.channel() != null
                    && v.getWorld().getKey().toString().equals(world)) {
                v.sendPluginMessage(plugin, viewer.channel(), packet);
            }
        }
    }

    /** A player changed world: their own map starts over with the players of the new one. */
    void changedWorld(Player p) {
        Viewer v = viewers.get(p.getUniqueId());
        if (!s.mapPlayers() || v == null || v.channel() == null) {
            return;
        }
        p.sendPluginMessage(plugin, v.channel(), new byte[] {RESET});
        for (Player other : p.getWorld().getPlayers()) {
            if (other != p && shown(other)) {
                p.sendPluginMessage(plugin, v.channel(), tracked(other.getUniqueId(), false, other.getX(),
                        other.getY(), other.getZ(), other.getWorld().getKey().toString()));
            }
        }
    }

    void quit(Player p) {
        Sent last = sent.remove(p.getUniqueId());
        viewers.remove(p.getUniqueId());
        if (last != null && s.mapPlayers()) {
            broadcast(p, last.world(), tracked(p.getUniqueId(), true, 0, 0, 0, ""));
        }
    }

    // --- the payloads ---------------------------------------------------------------------------------------------

    static byte[] intPacket(int index, int value) {
        return new byte[] {(byte) index, (byte) (value >>> 24), (byte) (value >>> 16), (byte) (value >>> 8),
                (byte) value};
    }

    /**
     * Packet 2: the index byte, then network NBT (since 1.20.2: the root compound has no name): TAG_Byte "r",
     * TAG_Int_Array "i" (the UUID as 4 ints, most significant first, like Minecraft's UUIDUtil), and unless
     * removing TAG_Double "x"/"y"/"z" and TAG_String "d"; TAG_End. Strings are Java's modified UTF-8 with a u16
     * length (DataOutput.writeUTF), as NBT writes them.
     */
    static byte[] tracked(UUID id, boolean remove, double x, double y, double z, String dimension) {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(96);
        try (DataOutputStream out = new DataOutputStream(buf)) {
            out.writeByte(TRACKED);
            out.writeByte(10); // TAG_Compound (unnamed root)
            out.writeByte(1);
            out.writeUTF("r");
            out.writeByte(remove ? 1 : 0);
            out.writeByte(11);
            out.writeUTF("i");
            out.writeInt(4);
            long msb = id.getMostSignificantBits();
            long lsb = id.getLeastSignificantBits();
            out.writeInt((int) (msb >> 32));
            out.writeInt((int) msb);
            out.writeInt((int) (lsb >> 32));
            out.writeInt((int) lsb);
            if (!remove) {
                out.writeByte(6);
                out.writeUTF("x");
                out.writeDouble(x);
                out.writeByte(6);
                out.writeUTF("y");
                out.writeDouble(y);
                out.writeByte(6);
                out.writeUTF("z");
                out.writeDouble(z);
                out.writeByte(8);
                out.writeUTF("d");
                out.writeUTF(dimension);
            }
            out.writeByte(0); // TAG_End
        } catch (IOException e) {
            throw new UncheckedIOException(e); // a ByteArrayOutputStream never throws
        }
        return buf.toByteArray();
    }

    /** For /mcbrand selftest: the payloads on fixed inputs (tests/e2e/brand_check.py compares the bytes). */
    String selftest() {
        UUID u = new UUID(0x0123456789abcdefL, 0xfedcba9876543210L);
        return "xaero tracked " + java.util.HexFormat.of().formatHex(tracked(u, false, 1.5, 64, -2.25,
                "minecraft:overworld")) + " untracked " + java.util.HexFormat.of().formatHex(tracked(u, true, 0, 0,
                0, "")) + " handshake " + java.util.HexFormat.of().formatHex(intPacket(HANDSHAKE, NETWORK))
                + ", fair play code " + fairPlayMessage().children().get(0).equals(Component.text(FAIR_PLAY))
                + ", viewers " + viewers.size() + " handshakes " + handshakes + " updates " + updates;
    }
}
