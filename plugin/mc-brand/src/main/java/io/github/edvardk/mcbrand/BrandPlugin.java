package io.github.edvardk.mcbrand;

import java.io.File;
import java.net.URI;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import net.kyori.adventure.key.Key;
import net.kyori.adventure.sound.Sound;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.ClickEvent;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.TextDecoration;
import net.kyori.adventure.title.Title;
import org.bukkit.Bukkit;
import org.bukkit.ServerLinks;
import org.bukkit.command.Command;
import org.bukkit.command.CommandMap;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.command.TabCompleter;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.inventory.BrewEvent;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerItemConsumeEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerChangedWorldEvent;
import org.bukkit.event.world.EntitiesLoadEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerRegisterChannelEvent;
import org.bukkit.event.player.PlayerResourcePackStatusEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

/**
 * mc-brand: the server's branding, server side only (vanilla clients, no gameplay changes; design §8 P17).
 *
 * <ul>
 *   <li>Join: a title + subtitle, a chat greeting and a sound, 1 s after the join. First join (no saved player data:
 *       {@code hasPlayedBefore()}) vs. welcome back with "last seen ... ago": {@code OfflinePlayer.getLastSeen()},
 *       read in AsyncPlayerPreLoginEvent (off the main thread, before the player is online: CraftOfflinePlayer
 *       then reads Paper.LastSeen from the player's data file; once online it returns "now"; checked in the
 *       pinned jar). Nothing else is stored.
 *   <li>/rules (paged), /discord (a clickable https link), server links in the pause menu (Bukkit ServerLinks:
 *       Discord = COMMUNITY, rules link = COMMUNITY_GUIDELINES, website = WEBSITE; only https URLs).
 *   <li>Optional per-player sidebar (/hud) and the tab list's header/footer ({@link Hud}); optional signature
 *       drink ({@link Drink}, /drink or the server's own command name: v1.2, branding.drink.command).
 *   <li>v1.1: /modpack and the join prompt for the client modpack ({@link Modpack}: a one-time code through the
 *       tmpfs hand-off to mc-agent, file IO off the main thread), /view up to 16 chunks while the server is
 *       Smooth ({@link View}: one health reading per check_seconds).
 *   <li>v1.2: the smallworld channels ({@link Smallworld}: the client mod's hello and FPS reports, exact pack
 *       detection, the outdated-pack line, the config payload) and Xaero's maps ({@link XaeroMap}: fair play,
 *       friends on the map).
 * </ul>
 *
 * <p>Tail-latency rule: no per-tick work. The HUD/tab task runs every interval_seconds and measures itself
 * ({@code /mcbrand status}); joins, commands and drinks are single events. No network; the only files written
 * are /modpack's requests (tmpfs, async).
 */
public final class BrandPlugin extends JavaPlugin implements Listener, CommandExecutor, TabCompleter {
    private static final int RULES_PER_PAGE = 8;

    private Settings s;
    private Hud hud;
    private Drink glass;
    private Command drinkAlias;
    private BukkitTask task;
    private BukkitTask viewTask;
    private Modpack modpack;
    private View view;
    private Smallworld smallworld;
    private XaeroMap xaero;
    private BukkitTask mapTask;
    private Holograms holograms;
    private BukkitTask hologramTask;
    private final List<ServerLinks.ServerLink> links = new ArrayList<>();
    private Sound joinSound;
    private final Map<UUID, Long> lastSeen = new ConcurrentHashMap<>(); // pre-login -> join (async -> main)

    @Override
    public void onEnable() {
        s = Settings.load(YamlConfiguration.loadConfiguration(new File(getDataFolder(), "config.yml")));
        hud = new Hud(this, s, BrandPlugin::glyphs);
        glass = new Drink(this, s);
        modpack = new Modpack(this, s);
        view = new View(this, s);
        smallworld = new Smallworld(this, s);
        smallworld.start();
        xaero = new XaeroMap(this, s);
        xaero.start();
        holograms = new Holograms(this, s);
        if (!s.holograms().isEmpty()) {
            holograms.start();
            long every = s.hudSeconds() * 20L;
            hologramTask = Bukkit.getScheduler().runTaskTimer(this,
                    () -> holograms.tick(Msg.Speed.of(Bukkit.getServer()), Bukkit.getOnlinePlayers().size()), every,
                    every);
        }
        if (xaero.enabled() && s.mapPlayers()) {
            mapTask = Bukkit.getScheduler().runTaskTimer(this, xaero::tick, XaeroMap.PERIOD_TICKS,
                    XaeroMap.PERIOD_TICKS);
        }
        try {
            joinSound = s.sound().isEmpty() ? null : Sound.sound(Key.key(s.sound()), Sound.Source.MASTER, 0.8f, 1.0f);
        } catch (RuntimeException e) { // an invalid key: no sound (server.yml's pattern should have caught it)
            getLogger().warning("join sound " + s.sound() + " is not a valid key: no join sound");
        }
        addLinks();
        getServer().getPluginManager().registerEvents(this, this);
        for (String name : List.of("rules", "discord", "drink", "hud", "mcbrand", "modpack", "view")) {
            PluginCommand cmd = getCommand(name);
            if (cmd != null) {
                cmd.setExecutor(this);
                cmd.setTabCompleter(this);
            }
        }
        registerDrinkAlias();
        if (s.tabList() || s.hudEnabled()) {
            long period = s.hudSeconds() * 20L;
            task = Bukkit.getScheduler().runTaskTimer(this, hud::tick, period, period);
        }
        if (s.viewEnabled()) {
            long every = s.viewCheckSeconds() * 20L;
            viewTask = Bukkit.getScheduler().runTaskTimer(this, view::check, every, every);
        }
        for (Player p : Bukkit.getOnlinePlayers()) { // after a reload
            hud.join(p);
            view.join(p);
        }
        getLogger().info("branding " + s.name() + ": " + s.rules().size() + " rules, discord "
                + (s.discordInvite().isEmpty() ? "not set" : "set") + ", hud " + (s.hudEnabled() ? "available" : "off")
                + ", drink " + (s.drinkEnabled() ? "on (/" + s.drinkCommand() + ")" : "off") + ", " + links.size() + " server links"
                + ", modpack " + (s.clientEnabled() ? "on" : "off") + ", /view " + (s.viewEnabled() ? "up to "
                + s.viewMax() : "off"));
    }

    @Override
    public void onDisable() {
        if (task != null) {
            task.cancel();
        }
        if (viewTask != null) {
            viewTask.cancel();
        }
        if (modpack != null) {
            modpack.stop();
        }
        if (smallworld != null) {
            smallworld.stop();
        }
        if (mapTask != null) {
            mapTask.cancel();
        }
        if (hologramTask != null) {
            hologramTask.cancel();
        }
        if (holograms != null) {
            holograms.stop();
        }
        if (xaero != null) {
            xaero.stop();
        }
        if (hud != null) {
            hud.hideAll();
        }
        if (drinkAlias != null) {
            drinkAlias.unregister(Bukkit.getCommandMap());
            Command gone = drinkAlias;
            Bukkit.getCommandMap().getKnownCommands().values().removeIf(c -> c == gone);
            drinkAlias = null;
        }
        ServerLinks serverLinks = Bukkit.getServerLinks();
        for (ServerLinks.ServerLink l : links) {
            serverLinks.removeLink(l);
        }
        links.clear();
    }

    /**
     * v1.2: branding.drink.command (e.g. /cocoa) next to plugin.yml's /drink. A plugin.yml can't name a command
     * per deployment, so the plugin registers it in the server's CommandMap (Paper's Bukkit.getCommandMap();
     * registered in onEnable, before the server builds the client's command tree, like plugin.yml commands) as a
     * Command that runs /drink's code with /drink's permission, also as /mc-brand:&lt;name&gt;. If the name is
     * taken by another plugin, register() keeps only the prefixed form and the log says so; if registering fails
     * on a future server, /drink still works (fallback: plugin.yml aliases, a product change).
     */
    private void registerDrinkAlias() {
        String name = s.drinkCommand();
        if (name.equals("drink")) {
            return;
        }
        Command alias = new Command(name, "Get your " + s.drinkName() + " back if you lost it (one at a time).",
                "/" + name, List.of()) {
            @Override
            public boolean execute(CommandSender sender, String label, String[] args) {
                if (testPermission(sender)) {
                    drink(sender);
                }
                return true;
            }
        };
        alias.setPermission("mcbrand.drink");
        try {
            CommandMap map = Bukkit.getCommandMap();
            if (map.register(name, getName().toLowerCase(Locale.ROOT), alias)) {
                drinkAlias = alias;
                getLogger().info("/" + name + " registered for the drink (as /drink)");
            } else {
                drinkAlias = alias;
                getLogger().warning("/" + name + " is taken by another command: the drink is /drink and /"
                        + getName().toLowerCase(Locale.ROOT) + ":" + name);
            }
        } catch (RuntimeException | LinkageError e) {
            getLogger().warning("/" + name + " could not be registered (" + e + "): the drink is /drink");
        }
    }

    /** For /mcbrand selftest: which command runs the drink code (tests/e2e/brand_check.py). */
    private String drinkCommandStatus() {
        String name = s.drinkCommand();
        if (name.equals("drink")) {
            return "drink command /drink";
        }
        Command known = Bukkit.getCommandMap().getCommand(name);
        return "drink command /" + name + " " + (known != null && known == drinkAlias) + " + /drink";
    }

    private void addLinks() {
        ServerLinks serverLinks = Bukkit.getServerLinks();
        for (Map.Entry<ServerLinks.Type, String> e : List.of(
                Map.entry(ServerLinks.Type.COMMUNITY, s.discordInvite()),
                Map.entry(ServerLinks.Type.COMMUNITY_GUIDELINES, s.rulesUrl()),
                Map.entry(ServerLinks.Type.WEBSITE, s.website()))) {
            if (e.getValue().isEmpty()) {
                continue;
            }
            try {
                links.add(serverLinks.addLink(e.getKey(), URI.create(e.getValue())));
            } catch (IllegalArgumentException ex) {
                getLogger().warning("server link " + e.getValue() + " is not a valid URI: left out");
            }
        }
    }

    // --- join ----------------------------------------------------------------------------------------------------

    /** Whether this player's client shows the pack's {glyph} icon: it loaded the server resource pack (Paper keeps
     * the status from the configuration phase too: Player.getResourcePackStatus, checked in the pinned jar). */
    static boolean glyphs(Player p) {
        return p.getResourcePackStatus() == PlayerResourcePackStatusEvent.Status.SUCCESSFULLY_LOADED;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPack(PlayerResourcePackStatusEvent e) {
        if (e.getStatus() == PlayerResourcePackStatusEvent.Status.SUCCESSFULLY_LOADED) {
            hud.packLoaded(e.getPlayer()); // a pack accepted in game: the titles get their glyph now
            holograms.viewer(e.getPlayer(), true); // v1.2: and the holograms
        }
    }

    Component greeting(String player, boolean first, long nowMs, long lastPlayedMs, boolean glyph) {
        String template = first ? s.firstJoin() : s.welcomeBack();
        String ago = lastPlayedMs > 0 ? Texts.ago(nowMs, lastPlayedMs) : "a while";
        return Texts.fill(template, s.text(), Map.of(
                "player", Component.text(player, s.accent()),
                "server", Texts.bold(s.name(), s.accent()),
                "ago", Component.text(ago, s.subtle())), glyph);
    }

    Title title(String player, boolean first, boolean glyph) {
        Component sub = Texts.fill(first ? s.subtitleFirst() : s.subtitleBack(), s.subtle(),
                Map.of("player", Component.text(player, s.accent()),
                        "server", Component.text(s.name(), s.accent())), glyph);
        return Title.title(Texts.withGlyph(Texts.bold(s.name(), s.text()), glyph), sub,
                Title.Times.times(Duration.ofMillis(500), Duration.ofMillis(3500), Duration.ofMillis(1000)));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPreLogin(AsyncPlayerPreLoginEvent e) {
        if (e.getLoginResult() == AsyncPlayerPreLoginEvent.Result.ALLOWED) {
            lastSeen.put(e.getUniqueId(), Bukkit.getOfflinePlayer(e.getUniqueId()).getLastSeen());
        }
        if (lastSeen.size() > 1000) { // logins that never joined (refused later): never grows without bound
            lastSeen.clear();
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent e) {
        Player p = e.getPlayer();
        boolean first = !p.hasPlayedBefore();
        Long seen = lastSeen.remove(p.getUniqueId());
        long lastPlayed = seen == null ? 0 : seen;
        hud.join(p);
        view.join(p);
        modpack.join(p, smallworld);
        holograms.viewer(p, glyphs(p));
        if (s.drinkEnabled()) {
            boolean has = glass.sweep(p);
            // v1.0.1: not only on the first join. Everyone who never got a glass gets one (serial 0 in their PDC:
            // issue() sets it to 1+), so players from before mc-brand get theirs too; never a second one (sweep +
            // the serial). A full inventory: no glass and no serial, so the next join tries again.
            if (!has && glass.neverIssued(p) && s.giveOnFirstJoin()) {
                glass.issue(p);
            }
        }
        Bukkit.getScheduler().runTaskLater(this, () -> {
            if (!p.isOnline()) {
                return;
            }
            if (s.title()) {
                p.showTitle(title(p.getName(), first, glyphs(p)));
            }
            p.sendMessage(greeting(p.getName(), first, System.currentTimeMillis(), lastPlayed, glyphs(p)));
            if (first && !s.rules().isEmpty()) {
                p.sendMessage(Component.text("Type /rules for the house rules.", s.subtle()));
            }
            if (joinSound != null) {
                p.playSound(joinSound);
            }
        }, 20L);
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent e) {
        hud.forget(e.getPlayer().getUniqueId());
        glass.forget(e.getPlayer().getUniqueId());
        view.forget(e.getPlayer().getUniqueId());
        modpack.forget(e.getPlayer().getUniqueId());
        smallworld.forget(e.getPlayer());
        xaero.quit(e.getPlayer());
    }

    /** v1.2: the client mod said hello (main thread): the pack includes Xaero's maps. */
    void onSmallworldHello(Player p) {
        xaero.hello(p);
    }

    /** v1.2: a chunk's entities loaded: sweep stale holograms there, respawn the configured one. */
    @EventHandler(priority = EventPriority.MONITOR)
    public void onEntitiesLoad(EntitiesLoadEvent e) {
        holograms.loaded(e.getWorld(), e.getChunk().getX(), e.getChunk().getZ(), e.getEntities());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onWorldChange(PlayerChangedWorldEvent e) {
        xaero.changedWorld(e.getPlayer());
    }

    /** v1.2: a channel the client registered (minecraft:register), e.g. smallworld:config after the hello. */
    @EventHandler
    public void onRegisterChannel(PlayerRegisterChannelEvent e) {
        smallworld.registered(e.getPlayer(), e.getChannel());
        xaero.registered(e.getPlayer(), e.getChannel());
    }

    // --- the drink -------------------------------------------------------------------------------------------------

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onConsume(PlayerItemConsumeEvent e) {
        String tag = glass.tag(e.getItem());
        if (tag == null) {
            return;
        }
        Player p = e.getPlayer();
        if (!s.drinkEnabled()) {
            return; // switched off: it's an ordinary (empty) potion now
        }
        switch (glass.drink(p, tag, System.currentTimeMillis())) {
            case OK -> {
                e.setReplacement(glass.refill(p));
                Bukkit.getScheduler().runTask(this, () -> glass.sweep(p));
            }
            case COOLDOWN -> e.setCancelled(true);
            case STALE -> {
                e.setCancelled(true);
                Bukkit.getScheduler().runTask(this, () -> glass.sweep(p));
                p.sendMessage(Component.text("That " + s.drinkName() + " isn't yours (or it's an old one): it's gone. /"
                        + s.drinkCommand() + " gets yours.", s.subtle()));
            }
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onBrew(BrewEvent e) {
        for (int i = 0; i < 3; i++) {
            if (glass.tag(e.getContents().getItem(i)) != null) {
                e.setCancelled(true);
                return;
            }
        }
    }

    // --- commands --------------------------------------------------------------------------------------------------

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "rules" -> rules(sender, args.length > 0 ? parse(args[0]) : 1);
            case "discord" -> discord(sender);
            case "drink" -> drink(sender);
            case "hud" -> hudCommand(sender, args);
            case "mcbrand" -> admin(sender, args);
            case "modpack" -> {
                if (sender instanceof Player p) {
                    modpack.command(p, args);
                } else {
                    sender.sendMessage(Component.text("/modpack is per player."));
                }
            }
            case "view" -> {
                if (sender instanceof Player p) {
                    view.command(p, args);
                } else {
                    sender.sendMessage(Component.text("/view is per player."));
                }
            }
            default -> {
                return false;
            }
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        if (args.length != 1) {
            return List.of();
        }
        return switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "hud" -> List.of("top", "side", "off");
            case "mcbrand" -> List.of("status", "selftest");
            case "modpack" -> List.of("off", "on");
            case "view" -> List.of("8", "10", "12", "14", "16", "reset");
            default -> List.of();
        };
    }

    private static int parse(String s) {
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return 1;
        }
    }

    void rules(CommandSender to, int page) {
        List<String> rules = s.rules();
        if (rules.isEmpty()) {
            to.sendMessage(Component.text("No rules set. Be nice!", s.text()));
            return;
        }
        int pages = (rules.size() + RULES_PER_PAGE - 1) / RULES_PER_PAGE;
        int pg = Math.max(1, Math.min(pages, page));
        to.sendMessage(Component.text()
                .append(Texts.bold(s.name(), s.accent()))
                .append(Component.text(" rules" + (pages > 1 ? " (" + pg + "/" + pages + ")" : ""), s.subtle()))
                .build());
        for (int i = (pg - 1) * RULES_PER_PAGE; i < Math.min(rules.size(), pg * RULES_PER_PAGE); i++) {
            to.sendMessage(Component.text()
                    .append(Component.text((i + 1) + ". ", s.accent()))
                    .append(Texts.fill(rules.get(i), s.text(), Map.of(), to instanceof Player p && glyphs(p)))
                    .build());
        }
        if (pg < pages) {
            to.sendMessage(Component.text("/rules " + (pg + 1) + " for more", s.subtle())
                    .clickEvent(ClickEvent.runCommand("/rules " + (pg + 1))));
        }
    }

    void discord(CommandSender to) {
        String url = s.discordInvite();
        if (url.isEmpty()) {
            to.sendMessage(Component.text("No Discord link yet.", s.subtle()));
            return;
        }
        to.sendMessage(Component.text()
                .append(Component.text("Join us on Discord: ", s.text()))
                .append(Component.text(url, s.accent()).decoration(TextDecoration.UNDERLINED, true)
                        .clickEvent(ClickEvent.openUrl(url))
                        .hoverEvent(HoverEvent.showText(Component.text("Open the invite"))))
                .build());
    }

    void drink(CommandSender sender) {
        if (!(sender instanceof Player p)) {
            sender.sendMessage(Component.text("Only players can hold a " + s.drinkName() + "."));
            return;
        }
        if (!s.drinkEnabled()) {
            p.sendMessage(Component.text("The " + s.drinkName() + " is off on this server.", s.subtle()));
            return;
        }
        if (glass.sweep(p)) {
            p.sendMessage(Component.text("You already have your " + s.drinkName() + ".", s.subtle()));
            return;
        }
        ItemStack given = glass.issue(p);
        p.sendMessage(given == null
                ? Component.text("Make room in your inventory first.", s.subtle())
                : Component.text("Here's your " + s.drinkName() + ". Cheers!", s.text()));
    }

    private void hudCommand(CommandSender sender, String[] args) {
        if (!(sender instanceof Player p)) {
            sender.sendMessage(Component.text("The HUD is per player."));
            return;
        }
        if (!s.hudEnabled()) {
            p.sendMessage(Component.text("The HUD is off on this server.", s.subtle()));
            return;
        }
        // /hud top|side|off (on = top); /hud alone: off if one is showing, else top
        Hud.Mode m = args.length > 0 ? Hud.Mode.parse(args[0], null)
                : hud.showing(p) == Hud.Mode.OFF ? Hud.Mode.TOP : Hud.Mode.OFF;
        if (m == null) {
            p.sendMessage(Component.text("Usage: /hud [top|side|off]", s.subtle()));
            return;
        }
        hud.set(p, m);
        p.sendMessage(Component.text(switch (m) {
            case TOP -> "HUD at the top of the screen (/hud side for the sidebar, /hud off to hide it)";
            case SIDE -> "HUD in the sidebar (/hud top for the top of the screen, /hud off to hide it)";
            case OFF -> "HUD off (/hud to show it again)";
        }, s.subtle()));
    }

    private void admin(CommandSender to, String[] args) {
        String sub = args.length > 0 ? args[0].toLowerCase(Locale.ROOT) : "status";
        if (sub.equals("selftest")) {
            long now = System.currentTimeMillis();
            to.sendMessage(Component.text("mc-brand selftest: first join: ").append(greeting("Steve", true, now, 0, false)));
            to.sendMessage(Component.text("mc-brand selftest: welcome back: ")
                    .append(greeting("Steve", false, now, now - 3 * 86_400_000L, false)));
            to.sendMessage(Component.text("mc-brand selftest: title: ").append(title("Steve", true, false).title())
                    .append(Component.text(" / ")).append(title("Steve", true, false).subtitle()));
            // the glyph: only what a player with the pack gets (font mcbrand:icons, U+E001), never the emoji
            Component withGlyph = greeting("Steve", true, now, 0, true);
            to.sendMessage(Component.text("mc-brand selftest: glyph " + Texts.ICONS.asString() + " "
                    + (Texts.flat(withGlyph).contains(Texts.GLYPH_CHAR) || !s.firstJoin().contains("{glyph}"))
                    + ", no emoji " + !(Texts.flat(withGlyph) + Texts.flat(greeting("Steve", true, now, 0, false)))
                            .contains(Texts.LEGACY_GLYPH_EMOJI)
                    + ", without pack " + !Texts.flat(greeting("Steve", true, now, 0, false)).contains(Texts.GLYPH_CHAR)));
            Msg.Speed speed = Msg.Speed.of(Bukkit.getServer());
            to.sendMessage(Component.text("mc-brand selftest: tab: ").append(hud.header(false))
                    .append(hud.footer(Bukkit.getServer(), Bukkit.getOnlinePlayers().size(), speed)));
            to.sendMessage(Component.text("mc-brand selftest: sidebar lines "
                    + hud.lines(Bukkit.getServer(), speed).size() + ", " + hud.selftest()));
            to.sendMessage(Component.text("mc-brand selftest: " + glass.selftest() + ", " + drinkCommandStatus()));
            to.sendMessage(Component.text("mc-brand selftest: " + modpack.selftest() + "; " + View.selftest()
                    + "; prompt: ").append(modpack.prompt()));
            to.sendMessage(Component.text("mc-brand selftest: code: ").append(modpack.code("123456", 15)));
            to.sendMessage(Component.text("mc-brand selftest: " + smallworld.selftest() + "; outdated: ")
                    .append(smallworld.outdated()));
            to.sendMessage(Component.text("mc-brand selftest: " + xaero.selftest() + "; map: ")
                    .append(xaero.fairPlayMessage()));
            to.sendMessage(Component.text("mc-brand selftest: " + holograms.selftest()));
            to.sendMessage(Component.text("mc-brand selftest: links " + Bukkit.getServerLinks().getLinks().size()
                    + " (ours " + links.size() + "), rules " + s.rules().size()));
            return;
        }
        Msg.Speed sp = Msg.Speed.of(Bukkit.getServer()); // for ops: the technical numbers
        to.sendMessage(Component.text(String.format(Locale.ROOT,
                "mc-brand %s: hud task runs=%d last=%dus max=%dus, hud write errors=%d, drinks=%d, glasses removed=%d;"
                        + " TPS %.1f, MSPT %.1f (p95 %.1f) = %s; modpack requests=%d codes=%d failures=%d;"
                        + " view checks=%d raised=%d (now %d) lowered=%d; smallworld hellos=%d"
                        + " malformed=%d outdated=%d; xaero handshakes=%d updates=%d fair play=%d",
                s.name(), hud.runs, hud.lastNs / 1000, hud.maxNs / 1000, hud.writeErrors, glass.drinks, glass.removed,
                sp.tps(), sp.mspt(), sp.p95(), sp.health().word, modpack.requests, modpack.codes, modpack.failures,
                view.checks, view.raisedCount, view.raisedNow(), view.lowered, smallworld.hellos,
                smallworld.malformed, smallworld.outdatedPrompts, xaero.handshakes, xaero.updates,
                xaero.fairPlay)));
    }
}
