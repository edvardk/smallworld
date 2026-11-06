package io.github.edvardk.mcbrand;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.SecureRandom;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.TextDecoration;
import org.bukkit.Bukkit;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.scheduler.BukkitTask;

/**
 * v1.1: /modpack and the join prompt for the optional client modpack (design docs/design/client-v1.1.md).
 *
 * <p>The setup page only opens with a personal one-time code. This process never holds a credential, so it never
 * talks to the Worker: {@code /modpack} writes a request (uuid, name, a random nonce) into the tmpfs hand-off
 * directory ({@code client.request_dir}, minecraft's); mc-agent (the ops user, the only holder of the agent token)
 * asks the Worker for a code bound to that UUID (15 minutes, single use) and writes it into {@code client.code_file}
 * (the verdict directory: minecraft may only read it). An async task polls that file once a second while a request
 * is open (at most {@link #WAIT_MS}), matches uuid + nonce, deletes the request and shows the code with a link.
 *
 * <p>Join prompt: players without the pack get one clickable line after the greeting, at every join, until they
 * use the pack or switch it off ({@code /modpack off}, kept in their PDC). v1.2: "has the pack" is exact, the
 * Smallworld mod's {@code smallworld:hello} ({@link Smallworld}; it arrives within a round trip of the join), no
 * longer a guess from the client brand (v1.1 prompted only "vanilla" brands, so a modded client without the pack
 * was never told). Main-thread cost: one PDC read and one message per join; file IO only on the async task.
 */
final class Modpack {
    static final long WAIT_MS = 30_000;
    static final long REQUEST_GAP_MS = 20_000; // one request per player per 20 s
    static final Pattern CODE = Pattern.compile("^[0-9]{6}$");
    static final Pattern NONCE = Pattern.compile("^[0-9a-f]{32}$");
    static final String PROMPT = "Want smoother FPS and to see further? Get the free {server} client (optional).";

    private final BrandPlugin plugin;
    private final Settings s;
    private final NamespacedKey offKey;
    private final SecureRandom random = new SecureRandom();
    private final Map<UUID, Request> open = new ConcurrentHashMap<>();
    private final Map<UUID, Long> lastRequest = new ConcurrentHashMap<>();
    private BukkitTask poller;
    long requests;
    long codes;
    long failures;

    record Request(String name, String nonce, long deadline) {}

    record Answer(UUID uuid, String nonce, String code, long expiresMs, String error) {}

    Modpack(BrandPlugin plugin, Settings settings) {
        this.plugin = plugin;
        this.s = settings;
        this.offKey = new NamespacedKey(plugin, "modpack_off");
    }

    /** The time after the join before the prompt: the greeting first, and the hello has arrived by then. */
    static final long PROMPT_DELAY_TICKS = 100L;

    static boolean shouldPrompt(boolean enabled, boolean prompt, boolean hasPack, boolean optedOut) {
        return enabled && prompt && !optedOut && !hasPack;
    }

    boolean optedOut(Player p) {
        return p.getPersistentDataContainer().has(offKey, PersistentDataType.BYTE);
    }

    /** At join (main thread): the prompt 5 s later, after the greeting, unless the hello said the pack is there. */
    void join(Player p, Smallworld smallworld) {
        if (!shouldPrompt(s.clientEnabled(), s.clientPrompt(), false, optedOut(p))) {
            return;
        }
        Bukkit.getScheduler().runTaskLater(plugin, () -> {
            if (p.isOnline() && !smallworld.hasPack(p)) {
                p.sendMessage(prompt());
            }
        }, PROMPT_DELAY_TICKS);
    }

    Component prompt() {
        return Component.text()
                .append(Texts.fill(PROMPT, s.text(), Map.of("server", Texts.bold(s.name(), s.accent()))))
                .append(Component.text(" "))
                .append(Component.text("[Get it]", s.accent()).decoration(TextDecoration.UNDERLINED, true)
                        .clickEvent(ClickEvent.runCommand("/modpack"))
                        .hoverEvent(HoverEvent.showText(Component.text("Gives you a personal link to the setup page"))))
                .append(Component.text("  /modpack off hides this", s.subtle()))
                .build();
    }

    void command(Player p, String[] args) {
        if (!s.clientEnabled()) {
            p.sendMessage(Component.text("There's no client modpack on this server.", s.subtle()));
            return;
        }
        String sub = args.length > 0 ? args[0].toLowerCase(Locale.ROOT) : "";
        switch (sub) {
            case "off" -> {
                p.getPersistentDataContainer().set(offKey, PersistentDataType.BYTE, (byte) 1);
                p.sendMessage(Component.text("OK, no more reminders. /modpack still works whenever you want it.",
                        s.subtle()));
            }
            case "on" -> {
                p.getPersistentDataContainer().remove(offKey);
                p.sendMessage(Component.text("Reminders are back on.", s.subtle()));
            }
            case "" -> request(p);
            default -> p.sendMessage(Component.text("Usage: /modpack (your setup link), /modpack off|on", s.subtle()));
        }
    }

    private void request(Player p) {
        UUID id = p.getUniqueId();
        long now = System.currentTimeMillis();
        Request r = open.get(id);
        if (r != null && r.deadline() > now) {
            p.sendMessage(Component.text("Your code is on its way...", s.subtle()));
            return;
        }
        Long last = lastRequest.get(id);
        if (last != null && now - last < REQUEST_GAP_MS) {
            p.sendMessage(Component.text("Just asked: wait a few seconds, then try /modpack again.", s.subtle()));
            return;
        }
        lastRequest.put(id, now);
        byte[] raw = new byte[16];
        random.nextBytes(raw);
        Request req = new Request(p.getName(), HexFormat.of().formatHex(raw), now + WAIT_MS);
        open.put(id, req);
        requests++;
        p.sendMessage(Component.text("Getting your personal setup link...", s.subtle()));
        Bukkit.getScheduler().runTaskAsynchronously(plugin, () -> writeRequest(id, req));
        startPoller();
    }

    /** The request file: one line, tab-separated (names are [A-Za-z0-9_]: no tabs). Atomic (tmp + move). */
    static String requestLine(UUID id, String name, String nonce, long nowMs) {
        return "mc-client-request 1\n" + id + "\t" + name + "\t" + nonce + "\t" + nowMs + "\n";
    }

    private void writeRequest(UUID id, Request req) {
        try {
            Path dir = Path.of(s.clientRequestDir());
            Files.createDirectories(dir);
            Path tmp = dir.resolve("." + id + ".tmp");
            Files.writeString(tmp, requestLine(id, req.name(), req.nonce(), System.currentTimeMillis()),
                    StandardCharsets.UTF_8);
            Files.move(tmp, dir.resolve(id + ".req"), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException | RuntimeException e) {
            plugin.getLogger().warning("/modpack: couldn't write the request (" + e.getClass().getSimpleName() + ")");
        }
    }

    private synchronized void startPoller() {
        if (poller == null) {
            poller = Bukkit.getScheduler().runTaskTimerAsynchronously(plugin, this::poll, 20L, 20L);
        }
    }

    /**
     * The agent's answers: {@code C <uuid> <nonce> <code> <expires-ms>} or {@code E <uuid> <nonce> <reason>}
     * (tab-separated, after a {@code mc-client-codes 1} header). Unknown lines are skipped.
     */
    static Answer parse(String line) {
        String[] f = line.split("\t", -1);
        try {
            if (f.length == 5 && f[0].equals("C") && NONCE.matcher(f[2]).matches() && CODE.matcher(f[3]).matches()) {
                return new Answer(UUID.fromString(f[1]), f[2], f[3], Long.parseLong(f[4]), null);
            }
            if (f.length == 4 && f[0].equals("E") && NONCE.matcher(f[2]).matches()) {
                String why = f[3].replaceAll("[^A-Za-z0-9 .,:'()-]", "");
                return new Answer(UUID.fromString(f[1]), f[2], null, 0, why.length() > 120 ? why.substring(0, 120) : why);
            }
        } catch (IllegalArgumentException e) {
            return null;
        }
        return null;
    }

    private void poll() { // async
        if (open.isEmpty()) {
            return;
        }
        List<String> lines = List.of();
        try {
            Path file = Path.of(s.clientCodeFile());
            if (Files.isReadable(file) && Files.size(file) < 262_144) {
                lines = Files.readAllLines(file, StandardCharsets.UTF_8);
            }
        } catch (IOException | RuntimeException e) {
            lines = List.of();
        }
        if (!lines.isEmpty() && !lines.get(0).equals("mc-client-codes 1")) {
            lines = List.of();
        }
        long now = System.currentTimeMillis();
        for (String line : lines) {
            Answer a = parse(line);
            if (a == null) {
                continue;
            }
            Request r = open.get(a.uuid());
            if (r == null || !r.nonce().equals(a.nonce())) {
                continue;
            }
            open.remove(a.uuid());
            deleteRequest(a.uuid());
            if (a.code() != null) {
                codes++;
            } else {
                failures++;
            }
            Bukkit.getScheduler().runTask(plugin, () -> answer(a));
        }
        for (Map.Entry<UUID, Request> e : open.entrySet()) {
            if (e.getValue().deadline() <= now && open.remove(e.getKey(), e.getValue())) {
                failures++;
                deleteRequest(e.getKey());
                UUID id = e.getKey();
                Bukkit.getScheduler().runTask(plugin, () -> {
                    Player p = Bukkit.getPlayer(id);
                    if (p != null) {
                        p.sendMessage(Component.text("Couldn't get a link right now. Try /modpack again in a minute.",
                                s.subtle()));
                    }
                });
            }
        }
    }

    private void deleteRequest(UUID id) {
        try {
            Files.deleteIfExists(Path.of(s.clientRequestDir()).resolve(id + ".req"));
        } catch (IOException | RuntimeException e) {
            // the agent ignores answered or stale requests (its own nonce record), so a leftover is harmless
        }
    }

    /** The link the code opens (the page reads `c`, exchanges it and removes it from the address bar). */
    String link(String code) {
        String base = s.clientUrl();
        return base + (base.contains("?") ? "&" : "?") + "c=" + code;
    }

    private void answer(Answer a) { // main thread
        Player p = Bukkit.getPlayer(a.uuid());
        if (p == null) {
            return;
        }
        if (a.code() == null) {
            p.sendMessage(Component.text("Couldn't get a link: " + a.error(), s.subtle()));
            return;
        }
        p.sendMessage(code(a.code(), Math.max(1, (a.expiresMs() - System.currentTimeMillis()) / 60_000)));
    }

    Component code(String code, long minutes) {
        String url = link(code);
        return Component.text()
                .append(Component.text("Your personal setup link: ", s.text()))
                .append(Component.text("[Open the " + s.name() + " client page]", s.accent())
                        .decoration(TextDecoration.UNDERLINED, true)
                        .clickEvent(ClickEvent.openUrl(url))
                        .hoverEvent(HoverEvent.showText(Component.text("Opens your browser"))))
                .append(Component.newline())
                .append(Component.text("Or open " + s.clientUrl() + " and type the code ", s.subtle()))
                .append(Component.text(code.substring(0, 3) + " " + code.substring(3), s.accent())
                        .decoration(TextDecoration.BOLD, true))
                .append(Component.text(" (works once, for " + minutes + " min; just for you, don't share it).",
                        s.subtle()))
                .build();
    }

    void forget(UUID id) {
        lastRequest.remove(id);
    }

    void stop() {
        if (poller != null) {
            poller.cancel();
        }
    }

    /** For /mcbrand selftest: the prompt rule and the answer parser on fixed inputs. */
    String selftest() {
        UUID u = new UUID(1, 2);
        String n = "0123456789abcdef0123456789abcdef";
        Answer ok = parse("C\t" + u + "\t" + n + "\t123456\t99");
        Answer bad = parse("C\t" + u + "\t" + n + "\t12345x\t99");
        return "modpack prompt no pack " + shouldPrompt(true, true, false, false) + ", pack "
                + shouldPrompt(true, true, true, false) + ", off " + shouldPrompt(true, true, false, true)
                + ", parse " + (ok != null && ok.code().equals("123456")) + "/" + (bad == null) + ", requests "
                + requests + " codes " + codes + " failures " + failures;
    }
}
