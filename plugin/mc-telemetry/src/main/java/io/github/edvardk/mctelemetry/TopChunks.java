package io.github.edvardk.mctelemetry;

/**
 * The K busiest chunks of one window by one score (entities or tile entities), kept sorted by insertion into
 * preallocated arrays: the main thread never allocates for it. World is an index into the snapshot's worlds.
 */
final class TopChunks {
    static final int K = 5;

    final int[] world = new int[K];
    final int[] x = new int[K];
    final int[] z = new int[K];
    final int[] entities = new int[K];
    final int[] tiles = new int[K];
    final int[] villagers = new int[K];
    final int[] items = new int[K];
    final int[] score = new int[K];
    int n;

    void reset() {
        n = 0;
    }

    /** Offers one chunk; kept when its score is among the K highest so far (ties: the earlier one stays). */
    void offer(int sc, int w, int cx, int cz, int ent, int til, int vil, int itm) {
        if (sc <= 0 || (n == K && sc <= score[K - 1])) {
            return;
        }
        int i = n < K ? n++ : K - 1;
        while (i > 0 && score[i - 1] < sc) {
            world[i] = world[i - 1];
            x[i] = x[i - 1];
            z[i] = z[i - 1];
            entities[i] = entities[i - 1];
            tiles[i] = tiles[i - 1];
            villagers[i] = villagers[i - 1];
            items[i] = items[i - 1];
            score[i] = score[i - 1];
            i--;
        }
        world[i] = w;
        x[i] = cx;
        z[i] = cz;
        entities[i] = ent;
        tiles[i] = til;
        villagers[i] = vil;
        items[i] = itm;
        score[i] = sc;
    }
}
