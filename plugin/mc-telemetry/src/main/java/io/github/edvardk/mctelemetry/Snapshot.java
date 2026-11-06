package io.github.edvardk.mctelemetry;

/**
 * One window's data, filled on the main thread and serialized on the writer thread. Two instances circulate
 * (a free pool and a hand-off queue, both preallocated), so the main thread never allocates one. Fields hold
 * primitives or references to strings the server already has (names, brands): nothing is copied per tick.
 */
final class Snapshot {
    static final int MAX_SPIKES = 32;

    long seq;
    long ts;
    long windowMs;
    int tickBank = -1; // closed TickHistogram bank (-1: none, the writer skips the tick summary)
    int selfBank = -1;
    double tps;
    long heapUsed;
    long heapMax;
    int skippedWindows; // windows lost since the last snapshot (pool empty, writer slow)
    int budgetSkips; // ticks whose collection step stopped at the per-tick budget

    final long[] spikeTs = new long[MAX_SPIKES];
    final double[] spikeMs = new double[MAX_SPIKES];
    int spikes;

    int worlds;
    String[] worldName = new String[4];
    int[] worldChunks = new int[4];
    int[] worldEntities = new int[4];
    int[] worldTiles = new int[4];
    int[] worldTicking = new int[4];
    int[] worldPlayers = new int[4];

    // the busiest loaded chunks (chunk-scan: one chunk per collection step, within the per-tick budget)
    final TopChunks byEntities = new TopChunks();
    final TopChunks byTiles = new TopChunks();
    int chunksScanned;

    int players;
    java.util.UUID[] playerUuid = new java.util.UUID[8];
    String[] playerName = new String[8];
    String[] playerBrand = new String[8];
    java.util.Locale[] playerLocale = new java.util.Locale[8];
    String[] playerWorld = new String[8];
    int[] ping = new int[8];
    int[] clientView = new int[8];
    int[] sendView = new int[8];
    int[] chunkX = new int[8];
    int[] chunkZ = new int[8];
    int[] nearEntities = new int[8];
    boolean[] gliding = new boolean[8];
    boolean[] tips = new boolean[8];
    long[] joined = new long[8];
    int[] fps = new int[8]; // v1.2: the Smallworld client mod's latest report (mc-brand's player metadata), -1 none

    void reset() {
        tickBank = -1;
        selfBank = -1;
        spikes = 0;
        worlds = 0;
        players = 0;
        skippedWindows = 0;
        budgetSkips = 0;
        byEntities.reset();
        byTiles.reset();
        chunksScanned = 0;
    }

    /** Grows the per-world arrays (rare: only when more worlds exist than ever before). */
    void ensureWorlds(int need) {
        if (need <= worldName.length) {
            return;
        }
        int cap = Math.max(need, worldName.length * 2);
        worldName = java.util.Arrays.copyOf(worldName, cap);
        worldChunks = java.util.Arrays.copyOf(worldChunks, cap);
        worldEntities = java.util.Arrays.copyOf(worldEntities, cap);
        worldTiles = java.util.Arrays.copyOf(worldTiles, cap);
        worldTicking = java.util.Arrays.copyOf(worldTicking, cap);
        worldPlayers = java.util.Arrays.copyOf(worldPlayers, cap);
    }

    /** Grows the per-player arrays (rare: only when more players are online than ever before). */
    void ensurePlayers(int need) {
        if (need <= playerUuid.length) {
            return;
        }
        int cap = Math.max(need, playerUuid.length * 2);
        playerUuid = java.util.Arrays.copyOf(playerUuid, cap);
        playerName = java.util.Arrays.copyOf(playerName, cap);
        playerBrand = java.util.Arrays.copyOf(playerBrand, cap);
        playerLocale = java.util.Arrays.copyOf(playerLocale, cap);
        playerWorld = java.util.Arrays.copyOf(playerWorld, cap);
        ping = java.util.Arrays.copyOf(ping, cap);
        clientView = java.util.Arrays.copyOf(clientView, cap);
        sendView = java.util.Arrays.copyOf(sendView, cap);
        chunkX = java.util.Arrays.copyOf(chunkX, cap);
        chunkZ = java.util.Arrays.copyOf(chunkZ, cap);
        nearEntities = java.util.Arrays.copyOf(nearEntities, cap);
        gliding = java.util.Arrays.copyOf(gliding, cap);
        tips = java.util.Arrays.copyOf(tips, cap);
        joined = java.util.Arrays.copyOf(joined, cap);
        fps = java.util.Arrays.copyOf(fps, cap);
    }
}
