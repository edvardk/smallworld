package io.github.edvardk.smallworld.client;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.fabricmc.fabric.api.client.rendering.v1.hud.HudElementRegistry;
import net.fabricmc.fabric.api.client.rendering.v1.hud.VanillaHudElements;
import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.DeltaTracker;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphicsExtractor;
import net.minecraft.client.multiplayer.ClientPacketListener;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.Identifier;

/**
 * The Smallworld client mod (v1.2; client/mod/WIRE.md): networking and two HUD elements, no mixins.
 *
 * <p>Once the server has registered the channel (its {@code minecraft:register} list, checked every tick through
 * {@link ClientPlayNetworking#canSend}), the mod sends {@code smallworld:hello} once per join and then
 * {@code smallworld:fps} every interval (default 5 s; the server may change it with {@code smallworld:config},
 * clamped to 2..60 s). A server without the channel gets nothing. The local FPS is drawn every frame next to the
 * top HUD bar unless the player's config or the server's config turns it off.
 *
 * <p>v1.5 (mod 1.1.0): the hello announces the compass capability ({@link Wire#CAP_COMPASS}); a server with
 * mc-brand 1.5 then stops drawing its boss-bar compass for this player and sends {@code smallworld:compass} (draw
 * on/off and the settings) and {@code smallworld:friends} (positions), and {@link CompassHud} draws the compass
 * every frame. A server that sends neither gets no compass from the mod.
 */
public final class SmallworldClient implements ClientModInitializer {
    private static final int TEXT_COLOR = 0xFFE0E0E0; // ARGB: the alpha byte is required
    private static final int BOSS_BAR_HALF_WIDTH = 91;

    private final CompassHud compass = new CompassHud();
    private LocalConfig local;
    private String modVersion = "";

    // client thread only (ticks, the payload handler and the HUD all run there)
    private ClientPacketListener connection; // a new one = a new join: start over
    private boolean helloSent;
    private boolean serverHud = true;
    private int intervalSeconds = Wire.DEFAULT_INTERVAL;
    private long nextFpsNanos;

    @Override
    public void onInitializeClient() {
        FabricLoader loader = FabricLoader.getInstance();
        local = LocalConfig.load(loader.getConfigDir());
        modVersion = loader.getModContainer(Payloads.NAMESPACE)
                .map(c -> c.getMetadata().getVersion().getFriendlyString())
                .orElse("");

        PayloadTypeRegistry.serverboundPlay().register(Payloads.Hello.TYPE, Payloads.Hello.CODEC);
        PayloadTypeRegistry.serverboundPlay().register(Payloads.Fps.TYPE, Payloads.Fps.CODEC);
        PayloadTypeRegistry.clientboundPlay().register(Payloads.Config.TYPE, Payloads.Config.CODEC);
        PayloadTypeRegistry.clientboundPlay().register(Payloads.Compass.TYPE, Payloads.Compass.CODEC);
        PayloadTypeRegistry.clientboundPlay().register(Payloads.Friends.TYPE, Payloads.Friends.CODEC);
        ClientPlayNetworking.registerGlobalReceiver(Payloads.Config.TYPE, (payload, context) -> {
            sync(context.client());
            apply(payload.value());
        });
        ClientPlayNetworking.registerGlobalReceiver(Payloads.Compass.TYPE, (payload, context) -> {
            sync(context.client());
            compass.state(payload.value());
        });
        ClientPlayNetworking.registerGlobalReceiver(Payloads.Friends.TYPE, (payload, context) -> {
            sync(context.client());
            compass.friends(payload.value());
        });

        ClientTickEvents.END_CLIENT_TICK.register(this::tick);
        HudElementRegistry.attachElementAfter(
                VanillaHudElements.BOSS_BAR, Identifier.fromNamespaceAndPath(Payloads.NAMESPACE, "fps"), this::draw);
        HudElementRegistry.attachElementAfter(VanillaHudElements.BOSS_BAR,
                Identifier.fromNamespaceAndPath(Payloads.NAMESPACE, "compass"), compass::draw);
    }

    private void reset() {
        compass.reset();
        helloSent = false;
        serverHud = true;
        intervalSeconds = Wire.DEFAULT_INTERVAL;
        nextFpsNanos = System.nanoTime() + intervalSeconds * 1_000_000_000L;
    }

    private void apply(Wire.Config config) {
        if (config.protocol() != Wire.PROTOCOL) {
            return; // a server speaking another major protocol: keep the defaults
        }
        serverHud = config.hud();
        intervalSeconds = config.clampedInterval();
        nextFpsNanos = Math.min(nextFpsNanos, System.nanoTime() + intervalSeconds * 1_000_000_000L);
    }

    /** The current connection; a different one than last time is a new join, so the state starts over. */
    private ClientPacketListener sync(Minecraft client) {
        ClientPacketListener now = client.getConnection();
        if (now != connection) {
            connection = now;
            reset();
        }
        return now;
    }

    private void tick(Minecraft client) {
        if (sync(client) == null) {
            return;
        }
        if (!helloSent && canSend(Payloads.Hello.TYPE)) {
            ClientPlayNetworking.send(new Payloads.Hello(
                    new Wire.Hello(Wire.PROTOCOL, Wire.fit(modVersion), local.tier(), local.pack(), Wire.CAP_COMPASS)));
            helloSent = true;
        }
        long nanos = System.nanoTime();
        if (nanos - nextFpsNanos >= 0) {
            nextFpsNanos = nanos + intervalSeconds * 1_000_000_000L;
            if (canSend(Payloads.Fps.TYPE)) {
                ClientPlayNetworking.send(new Payloads.Fps(new Wire.Fps(client.getFps())));
            }
        }
    }

    private static boolean canSend(CustomPacketPayload.Type<?> type) {
        try {
            return ClientPlayNetworking.canSend(type);
        } catch (IllegalStateException e) {
            return false; // not in the play phase (yet)
        }
    }

    private void draw(GuiGraphicsExtractor graphics, DeltaTracker delta) {
        Minecraft client = Minecraft.getInstance();
        if (!local.hud() || !serverHud || client.getDebugOverlay().showDebugScreen()) {
            return;
        }
        String text = client.getFps() + " fps";
        int width = client.font.width(text);
        int x = Math.min(graphics.guiWidth() / 2 + BOSS_BAR_HALF_WIDTH + 6, graphics.guiWidth() - width - 2);
        graphics.text(client.font, text, x, 3, TEXT_COLOR, true);
    }
}
