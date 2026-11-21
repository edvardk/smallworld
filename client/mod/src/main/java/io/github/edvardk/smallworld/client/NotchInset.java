package io.github.edvardk.smallworld.client;

import java.nio.IntBuffer;
import java.util.Locale;
import net.fabricmc.fabric.api.client.rendering.v1.hud.HudElement;
import net.minecraft.client.Minecraft;
import org.lwjgl.sdl.SDLVideo;
import org.lwjgl.sdl.SDL_Rect;
import org.lwjgl.system.MemoryStack;

/**
 * mod 1.2.0: keeps the top-centre HUD clear of a MacBook's camera notch (client/mod/WIRE.md "The notch").
 *
 * <p>In fullscreen on a notched MacBook the game covers the whole panel, notch strip included, so vanilla's boss
 * bars (mc-brand's HUD bar among them), the compass, the FPS and the tab list draw under the camera. Every tick
 * this works out how tall the notch strip is, in GUI px: {@link #shifted} moves the elements that would collide
 * with the notch (everything centred: the boss bars, the compass, the tab list) down by it, and what fits beside
 * the notch stays in the strip ({@link #ear}: the FPS, right of the notch). Everyone else gets 0 and an unchanged
 * HUD.
 *
 * <p>How far: SDL's safe area first ({@code SDL_GetWindowSafeArea}: SDL 3 fills it from
 * {@code NSScreen.safeAreaInsets} in exclusive fullscreen). In a fullscreen Space (the game's default fullscreen)
 * SDL leaves it empty, so then, only on macOS and only in fullscreen, the window's shape decides
 * ({@link NotchGeometry#strip}). Windowed, macOS keeps the window below the menu bar: 0. The player's
 * {@code top_inset} ({@link LocalConfig}) replaces all of it (a fixed shift, nothing placed beside a notch).
 */
final class NotchInset {
    private final int override; // GUI px, or -1: automatic
    // what Minecraft's Util.getPlatform() reads (Util itself drags DataFixerUpper onto javac's classpath)
    private final boolean mac = System.getProperty("os.name", "").toLowerCase(Locale.ROOT).contains("mac");
    // client thread only (ticks and the HUD)
    private int px;
    private boolean ear; // px is a notch strip (automatic), so there is room beside the notch

    NotchInset(int override) {
        this.override = override;
    }

    void tick(Minecraft mc) {
        if (override >= 0) {
            px = override;
            ear = false;
            return;
        }
        try {
            px = automatic(mc);
        } catch (RuntimeException | LinkageError e) {
            px = 0; // never let the HUD offset break the game
        }
        ear = px > 0;
    }

    /** The notch strip's height in GUI px (the shift), 0 without a notch. */
    int px() {
        return px;
    }

    /** True when the top {@link #px} GUI px are a notch strip with room left and right of the notch. */
    boolean ear() {
        return ear;
    }

    /** The element, drawn {@link #px} GUI px lower (as it is when there is no notch). */
    HudElement shifted(HudElement element) {
        return (graphics, delta) -> {
            int dy = px;
            if (dy == 0) {
                element.extractRenderState(graphics, delta);
                return;
            }
            graphics.pose().pushMatrix();
            graphics.pose().translate(0, dy);
            try {
                element.extractRenderState(graphics, delta);
            } finally {
                graphics.pose().popMatrix();
            }
        };
    }

    private int automatic(Minecraft mc) {
        long window = mc.getWindow().handle();
        if (window == 0 || (SDLVideo.SDL_GetWindowFlags(window) & SDLVideo.SDL_WINDOW_FULLSCREEN) == 0) {
            return 0;
        }
        try (MemoryStack stack = MemoryStack.stackPush()) {
            IntBuffer w = stack.mallocInt(1);
            IntBuffer h = stack.mallocInt(1);
            if (!SDLVideo.SDL_GetWindowSize(window, w, h)) {
                return 0;
            }
            SDL_Rect safe = SDL_Rect.malloc(stack);
            int top = SDLVideo.SDL_GetWindowSafeArea(window, safe) ? safe.y() : 0;
            if (top <= 0 && mac) {
                top = NotchGeometry.strip(w.get(0), h.get(0));
            }
            return NotchGeometry.guiPx(top, h.get(0), mc.getWindow().getGuiScaledHeight());
        }
    }
}
