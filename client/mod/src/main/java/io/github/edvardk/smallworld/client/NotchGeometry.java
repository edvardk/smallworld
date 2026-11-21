package io.github.edvardk.smallworld.client;

/**
 * mod 1.2.0: the arithmetic behind {@link NotchInset}, with no Minecraft or SDL types (WireFixtures checks
 * it on the plain JDK).
 *
 * <p>A MacBook with a camera notch has a 16:10 panel plus a strip on top as tall as the menu bar, with the notch in
 * the middle of it (that is why macOS offers 16:10 modes "below the camera"). In points: 1512 x 982 (14", 16:10
 * would be 945: a 37 pt strip), 1728 x 1117, 1470 x 956, 1710 x 1112; in pixels 3024 x 1964, 3456 x 2234,
 * 2560 x 1664, 2880 x 1864. The strip is 2.1 to 2.6 % of the width on every model and scaling; 16:10 and 16:9
 * screens have none, 3:2 (4.2 %), 4:3 and 5:4 far more, so only a 1.5 to 3.5 % excess counts as a notch strip.
 *
 * <p>The notch itself is about 12 % of the panel's width, centred (macOS reports it only through
 * {@code NSScreen.auxiliaryTopLeftArea}, which SDL doesn't expose); {@link #halfWidth} assumes 16 %, so text
 * placed beside it stays clear on every model and scaling.
 */
final class NotchGeometry {
    static final int MAX_OVERRIDE = 64; // smallworld.json top_inset: GUI px, 0..64
    static final double HALF_WIDTH = 0.08; // the notch's assumed half width, of the screen's (real: about 0.06)

    private NotchGeometry() {}

    /** The notch strip's height of a {@code w} x {@code h} screen (any unit), 0 when it has none. */
    static int strip(int w, int h) {
        if (w <= 0 || h <= 0) {
            return 0;
        }
        long excess = h - Math.round(w * 10.0 / 16.0);
        return excess * 1000 >= w * 15L && excess * 1000 <= w * 35L ? (int) excess : 0;
    }

    /** {@code top} window units (of a window {@code windowH} tall) in GUI px of a GUI {@code guiH} tall, rounded up. */
    static int guiPx(int top, int windowH, int guiH) {
        if (top <= 0 || windowH <= 0 || guiH <= 0) {
            return 0;
        }
        return (int) ((top * (long) guiH + windowH - 1) / windowH);
    }

    /** Half the notch's width in GUI px of a GUI {@code guiW} wide, rounded up (with the margin of HALF_WIDTH). */
    static int halfWidth(int guiW) {
        return guiW <= 0 ? 0 : (int) Math.ceil(guiW * HALF_WIDTH);
    }

    /** smallworld.json {@code top_inset}: a whole number 0..64 is fixed GUI px; anything else is automatic (-1). */
    static int override(String value) {
        if (value == null) {
            return -1;
        }
        try {
            int n = Integer.parseInt(value.trim());
            return n >= 0 && n <= MAX_OVERRIDE ? n : -1;
        } catch (NumberFormatException e) {
            return -1;
        }
    }
}
