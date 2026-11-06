package io.github.edvardk.mctelemetry;

/**
 * Tick-duration distribution for one window, HDR-style: fixed buckets, preallocated, no allocation per record.
 *
 * <p>Buckets: 0.1 ms wide up to 100 ms (1000 buckets), 1 ms wide up to 1000 ms (900 buckets), one overflow
 * bucket. Two banks: the main thread records into the active bank; {@link #swap()} (main thread, O(1)) makes it
 * the closed bank, which the writer thread reads while the main thread records into the other one. The writer
 * must call {@link #release()} before the next swap, otherwise the window is merged (never torn). The same class
 * measures the plugin's own per-tick time (recorded in units of 10 microseconds: 1 us resolution).
 */
final class TickHistogram {
    static final int FINE = 1000; // 0.1 ms buckets: [0, 100) ms
    static final int COARSE = 900; // 1 ms buckets: [100, 1000) ms
    static final int BUCKETS = FINE + COARSE + 1;

    private final long[][] counts = {new long[BUCKETS], new long[BUCKETS]};
    private final double[] max = new double[2];
    private final double[] sum = new double[2];
    private final long[] n = new long[2];
    private int active;
    private volatile boolean closedInUse;

    static int bucket(double ms) {
        if (!(ms >= 0)) {
            return 0;
        }
        if (ms < 100.0) {
            return (int) (ms * 10.0);
        }
        if (ms < 1000.0) {
            return FINE + (int) (ms - 100.0);
        }
        return BUCKETS - 1;
    }

    /** Upper edge of a bucket in ms (what a percentile reports: never optimistic). */
    static double upper(int b) {
        if (b < FINE) {
            return (b + 1) / 10.0;
        }
        if (b < FINE + COARSE) {
            return 100.0 + (b - FINE + 1);
        }
        return Double.POSITIVE_INFINITY;
    }

    /** Main thread, every tick. */
    void record(double ms) {
        int a = active;
        counts[a][bucket(ms)]++;
        n[a]++;
        sum[a] += ms;
        if (ms > max[a]) {
            max[a] = ms;
        }
    }

    /** Main thread: closes the window and returns the closed bank, or -1 (and keeps recording into the same
     * bank) while the writer still reads the previous one. */
    int swap() {
        if (closedInUse) {
            return -1;
        }
        int next = active ^ 1;
        java.util.Arrays.fill(counts[next], 0L);
        max[next] = 0;
        sum[next] = 0;
        n[next] = 0;
        closedInUse = true;
        int closed = active;
        active = next;
        return closed;
    }

    /** Writer thread: fills {@code out} (n, mean, p50, p95, p99, p999, max) from a closed bank (handed over with
     * the snapshot, so the queue between the threads orders these reads after the main thread's writes). */
    void summarize(int c, double[] out) {
        long total = n[c];
        out[0] = total;
        out[1] = total == 0 ? 0 : sum[c] / total;
        double[] qs = {0.50, 0.95, 0.99, 0.999};
        for (int i = 0; i < qs.length; i++) {
            out[2 + i] = Math.min(quantile(counts[c], total, qs[i]), max[c]);
        }
        out[6] = max[c];
    }

    static double quantile(long[] buckets, long total, double q) {
        if (total == 0) {
            return 0;
        }
        long rank = (long) Math.ceil(q * total);
        long seen = 0;
        for (int b = 0; b < buckets.length; b++) {
            seen += buckets[b];
            if (seen >= rank) {
                return upper(b);
            }
        }
        return upper(buckets.length - 1);
    }

    /** Writer thread: done with the closed bank. */
    void release() {
        closedInUse = false;
    }
}
