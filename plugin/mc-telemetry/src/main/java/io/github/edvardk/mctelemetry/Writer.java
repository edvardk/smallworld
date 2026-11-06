package io.github.edvardk.mctelemetry;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.logging.Logger;

/**
 * The plugin's only background thread: serializes snapshots to JSON and writes them to the tmpfs hand-off
 * directory (write a temporary file, then an atomic rename, so mc-agent never reads half a file), deletes old
 * ones, reads the agent's verdict and message files, and persists the tips opt-out list. All file IO of the
 * plugin happens here, never on the main thread.
 */
final class Writer implements Runnable {
    private final Logger log;
    private final Path snapDir;
    private final Path verdictDir;
    private final Path optOutFile;
    private final int keep;
    private final TickHistogram ticks;
    private final TickHistogram self;
    final ArrayBlockingQueue<Snapshot> full = new ArrayBlockingQueue<>(1);
    final ArrayBlockingQueue<Snapshot> free = new ArrayBlockingQueue<>(2);
    private final StringBuilder sb = new StringBuilder(16384);
    private final double[] summary = new double[7];
    private final Consumer<Verdicts.Message> deliver;
    private final Set<String> delivered = new HashSet<>();
    private volatile boolean running = true;
    volatile Verdicts verdicts = Verdicts.EMPTY;
    volatile boolean optOutDirty;
    private final Set<UUID> optOut;
    private long verdictMtime = -1;
    private long messageMtime = -1;
    private long written;

    Writer(Logger log, Path snapDir, Path verdictDir, Path optOutFile, int keep, TickHistogram ticks,
            TickHistogram self, Set<UUID> optOut, Consumer<Verdicts.Message> deliver) {
        this.log = log;
        this.snapDir = snapDir;
        this.verdictDir = verdictDir;
        this.optOutFile = optOutFile;
        this.keep = keep;
        this.ticks = ticks;
        this.self = self;
        this.optOut = optOut;
        this.deliver = deliver;
        free.add(new Snapshot());
        free.add(new Snapshot());
    }

    void stop() {
        running = false;
    }

    @Override
    public void run() {
        while (running) {
            Snapshot s;
            try {
                s = full.poll(1, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                break;
            }
            if (s != null) {
                try {
                    write(s);
                } catch (IOException | RuntimeException e) {
                    log.warning("mc-telemetry: snapshot not written: " + e);
                } finally {
                    if (s.tickBank >= 0) {
                        ticks.release();
                    }
                    if (s.selfBank >= 0) {
                        self.release();
                    }
                    s.reset();
                    free.offer(s);
                }
            }
            readAgentFiles();
            if (optOutDirty) {
                optOutDirty = false;
                saveOptOut();
            }
        }
        if (optOutDirty) {
            saveOptOut();
        }
    }

    // --- snapshot -> JSON ---------------------------------------------------------------------------------

    private void str(String v, int max) {
        sb.append('"');
        if (v != null) {
            int n = Math.min(v.length(), max);
            for (int i = 0; i < n; i++) {
                char c = v.charAt(i);
                if (c == '"' || c == '\\') {
                    sb.append('\\').append(c);
                } else if (c < 0x20 || c == 0x7f || (c >= 0xd800 && c <= 0xdfff)) {
                    sb.append('?'); // client-controlled strings (brand): no control characters, no lone surrogates
                } else {
                    sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    private void num(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            sb.append("null");
        } else {
            sb.append(Math.round(v * 100.0) / 100.0);
        }
    }

    void serialize(Snapshot s) {
        sb.setLength(0);
        sb.append("{\"v\":1,\"seq\":").append(s.seq).append(",\"ts\":").append(s.ts);
        sb.append(",\"win_ms\":").append(s.windowMs);
        sb.append(",\"srv\":{\"tps\":");
        num(s.tps);
        if (s.tickBank >= 0) {
            ticks.summarize(s.tickBank, summary);
            sb.append(",\"ticks\":").append((long) summary[0]).append(",\"mean\":");
            num(summary[1]);
            sb.append(",\"p50\":");
            num(summary[2]);
            sb.append(",\"p95\":");
            num(summary[3]);
            sb.append(",\"p99\":");
            num(summary[4]);
            sb.append(",\"p999\":");
            num(summary[5]);
            sb.append(",\"max\":");
            num(summary[6]);
        }
        if (s.selfBank >= 0) {
            self.summarize(s.selfBank, summary); // recorded in units of 10 us
            sb.append(",\"self_p99_us\":");
            num(summary[4] * 10.0);
            sb.append(",\"self_max_us\":");
            num(summary[6] * 10.0);
        }
        sb.append(",\"budget_skips\":").append(s.budgetSkips);
        sb.append(",\"lost_windows\":").append(s.skippedWindows);
        sb.append(",\"heap_used_mb\":").append(s.heapUsed >> 20).append(",\"heap_max_mb\":").append(s.heapMax >> 20);
        sb.append(",\"spikes\":[");
        for (int i = 0; i < s.spikes; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append('[').append(s.spikeTs[i]).append(',');
            num(s.spikeMs[i]);
            sb.append(']');
        }
        sb.append("],\"worlds\":[");
        for (int i = 0; i < s.worlds; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append("{\"n\":");
            str(s.worldName[i], 64);
            sb.append(",\"chunks\":").append(s.worldChunks[i]).append(",\"entities\":").append(s.worldEntities[i]);
            sb.append(",\"tiles\":").append(s.worldTiles[i]).append(",\"ticking\":").append(s.worldTicking[i]);
            sb.append(",\"players\":").append(s.worldPlayers[i]).append('}');
        }
        sb.append("],\"scanned\":").append(s.chunksScanned).append(",\"hot\":[");
        int out = 0;
        out = hot(s, s.byEntities, null, out);
        hot(s, s.byTiles, s.byEntities, out);
        sb.append("]},\"pl\":[");
        for (int i = 0; i < s.players; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append("{\"u\":");
            str(s.playerUuid[i] == null ? "" : s.playerUuid[i].toString(), 36);
            sb.append(",\"n\":");
            str(s.playerName[i], 16);
            sb.append(",\"ping\":").append(s.ping[i]).append(",\"brand\":");
            str(s.playerBrand[i], 32);
            sb.append(",\"view\":").append(s.clientView[i]).append(",\"send\":").append(s.sendView[i]);
            sb.append(",\"loc\":");
            str(s.playerLocale[i] == null ? "" : s.playerLocale[i].toString(), 16);
            sb.append(",\"w\":");
            str(s.playerWorld[i], 64);
            sb.append(",\"cx\":").append(s.chunkX[i]).append(",\"cz\":").append(s.chunkZ[i]);
            sb.append(",\"ent\":").append(s.nearEntities[i]);
            sb.append(",\"glide\":").append(s.gliding[i]).append(",\"tips\":").append(s.tips[i]);
            sb.append(",\"join\":").append(s.joined[i]);
            if (s.fps[i] >= 0) {
                sb.append(",\"fps\":").append(s.fps[i]); // v1.2: only for players with the client mod
            }
            sb.append('}');
        }
        sb.append("]}\n");
    }

    /** The chunks of `top` (skipping those already written from `seen`) as {w,cx,cz,ent,tiles,vil,items}. */
    private int hot(Snapshot s, TopChunks top, TopChunks seen, int out) {
        for (int i = 0; i < top.n; i++) {
            if (seen != null && contains(seen, top.world[i], top.x[i], top.z[i])) {
                continue;
            }
            if (out++ > 0) {
                sb.append(',');
            }
            sb.append("{\"w\":");
            str(s.worldName[top.world[i]], 64);
            sb.append(",\"cx\":").append(top.x[i]).append(",\"cz\":").append(top.z[i]);
            sb.append(",\"ent\":").append(top.entities[i]).append(",\"tiles\":").append(top.tiles[i]);
            sb.append(",\"vil\":").append(top.villagers[i]).append(",\"items\":").append(top.items[i]).append('}');
        }
        return out;
    }

    private static boolean contains(TopChunks t, int w, int x, int z) {
        for (int i = 0; i < t.n; i++) {
            if (t.world[i] == w && t.x[i] == x && t.z[i] == z) {
                return true;
            }
        }
        return false;
    }

    String json() {
        return sb.toString();
    }

    private void write(Snapshot s) throws IOException {
        serialize(s);
        byte[] data = sb.toString().getBytes(StandardCharsets.UTF_8);
        Path tmp = snapDir.resolve(".snap.tmp");
        Path dst = snapDir.resolve("snap-" + s.seq + ".json");
        Files.write(tmp, data);
        try {
            Files.move(tmp, dst, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(tmp, dst, StandardCopyOption.REPLACE_EXISTING);
        }
        written++;
        long old = s.seq - keep;
        if (old >= 0) {
            Files.deleteIfExists(snapDir.resolve("snap-" + old + ".json"));
        }
    }

    /** Deletes snapshots a previous run left behind (their sequence numbers restart at 0). */
    void clean() {
        try (var files = Files.list(snapDir)) {
            files.filter(p -> p.getFileName().toString().matches("snap-\\d+\\.json|\\.snap\\.tmp"))
                    .forEach(p -> {
                        try {
                            Files.deleteIfExists(p);
                        } catch (IOException e) {
                            // best effort
                        }
                    });
        } catch (IOException e) {
            log.warning("mc-telemetry: can't list " + snapDir + ": " + e);
        }
    }

    // --- the agent's files ------------------------------------------------------------------------------

    private List<String> readIfChanged(Path p, boolean messages) {
        try {
            long m = Files.getLastModifiedTime(p).toMillis() ^ Files.size(p);
            if (m == (messages ? messageMtime : verdictMtime)) {
                return null;
            }
            List<String> lines = Files.readAllLines(p, StandardCharsets.UTF_8);
            if (messages) {
                messageMtime = m;
            } else {
                verdictMtime = m;
            }
            return lines;
        } catch (NoSuchFileException e) {
            return null;
        } catch (IOException | RuntimeException e) {
            return null;
        }
    }

    private void readAgentFiles() {
        List<String> v = readIfChanged(verdictDir.resolve("verdicts.tsv"), false);
        if (v != null) {
            verdicts = Verdicts.parse(v);
        }
        List<String> m = readIfChanged(verdictDir.resolve("messages.tsv"), true);
        if (m != null) {
            List<Verdicts.Message> msgs = Verdicts.parseMessages(m);
            Set<String> ids = new HashSet<>();
            long now = System.currentTimeMillis();
            for (Verdicts.Message msg : msgs) {
                ids.add(msg.id());
                if (msg.expires() > now && delivered.add(msg.id())) {
                    deliver.accept(msg);
                }
            }
            delivered.retainAll(ids); // the agent drops old messages from the file; so does this set
        }
    }

    // --- tips opt-out ------------------------------------------------------------------------------------

    static Set<UUID> loadOptOut(Path file, Logger log) {
        Set<UUID> out = java.util.concurrent.ConcurrentHashMap.newKeySet();
        try {
            for (String line : Files.readAllLines(file, StandardCharsets.UTF_8)) {
                try {
                    out.add(UUID.fromString(line.trim()));
                } catch (IllegalArgumentException e) {
                    // skipped
                }
            }
        } catch (NoSuchFileException e) {
            // nobody opted out yet
        } catch (IOException e) {
            log.warning("mc-telemetry: can't read " + file + ": " + e);
        }
        return out;
    }

    private void saveOptOut() {
        List<String> lines = new ArrayList<>();
        for (UUID u : optOut) {
            lines.add(u.toString());
        }
        lines.sort(null);
        try {
            Files.createDirectories(optOutFile.getParent());
            Path tmp = optOutFile.resolveSibling(optOutFile.getFileName() + ".tmp");
            Files.write(tmp, lines, StandardCharsets.UTF_8);
            Files.move(tmp, optOutFile, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            log.warning("mc-telemetry: can't save " + optOutFile + ": " + e);
        }
    }

    long written() {
        return written;
    }
}
