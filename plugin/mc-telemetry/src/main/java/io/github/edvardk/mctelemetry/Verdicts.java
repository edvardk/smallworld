package io.github.edvardk.mctelemetry;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * The verdicts mc-agent last received from the Worker (where the verdict engine lives: one source of truth for the
 * rules), read from {@code <verdict-dir>/verdicts.tsv}, and the in-game messages it queued
 * ({@code messages.tsv}). Both files are written by the agent (as the ops user) into a directory this process may
 * only read. Tab-separated lines; a field never contains a tab, a newline or U+001F (the agent strips them), and
 * lists inside a field are joined with U+001F:
 *
 * <pre>
 * mc-verdicts 1 &lt;ts-ms&gt;
 * S &lt;verdict&gt; &lt;headline&gt; &lt;reasons&gt; &lt;tips&gt;
 * P &lt;uuid&gt; &lt;verdict&gt; &lt;headline&gt; &lt;reasons&gt; &lt;tips&gt;
 *
 * mc-messages 1
 * M &lt;id&gt; &lt;uuid or *&gt; &lt;expires-ms&gt; &lt;text&gt; &lt;hover&gt;
 * </pre>
 *
 * Parsed on the writer thread; the result is immutable and published through a volatile field.
 */
final class Verdicts {
    record Verdict(String verdict, String headline, List<String> reasons, List<String> tips) {}

    record Message(String id, String to, long expires, String text, String hover) {}

    static final Verdicts EMPTY = new Verdicts(0, null, Map.of());

    final long ts;
    final Verdict server;
    final Map<UUID, Verdict> players;

    Verdicts(long ts, Verdict server, Map<UUID, Verdict> players) {
        this.ts = ts;
        this.server = server;
        this.players = players;
    }

    private static List<String> list(String field) {
        if (field.isEmpty()) {
            return List.of();
        }
        List<String> out = new ArrayList<>();
        for (String s : field.split("\u001f", 16)) {
            if (!s.isEmpty()) {
                out.add(s);
            }
        }
        return Collections.unmodifiableList(out);
    }

    static Verdicts parse(List<String> lines) {
        if (lines.isEmpty()) {
            return EMPTY;
        }
        String[] head = lines.get(0).split(" ");
        if (head.length != 3 || !head[0].equals("mc-verdicts") || !head[1].equals("1")) {
            return EMPTY;
        }
        long ts;
        try {
            ts = Long.parseLong(head[2]);
        } catch (NumberFormatException e) {
            return EMPTY;
        }
        Verdict server = null;
        Map<UUID, Verdict> players = new HashMap<>();
        for (int i = 1; i < lines.size() && i < 1000; i++) {
            String[] f = lines.get(i).split("\t", -1);
            try {
                if (f.length == 5 && f[0].equals("S")) {
                    server = new Verdict(f[1], f[2], list(f[3]), list(f[4]));
                } else if (f.length == 6 && f[0].equals("P")) {
                    players.put(UUID.fromString(f[1]), new Verdict(f[2], f[3], list(f[4]), list(f[5])));
                }
            } catch (IllegalArgumentException e) {
                // a malformed line is skipped, the rest still counts
            }
        }
        return new Verdicts(ts, server, Collections.unmodifiableMap(players));
    }

    static List<Message> parseMessages(List<String> lines) {
        if (lines.isEmpty() || !lines.get(0).equals("mc-messages 1")) {
            return List.of();
        }
        List<Message> out = new ArrayList<>();
        for (int i = 1; i < lines.size() && i < 200; i++) {
            String[] f = lines.get(i).split("\t", -1);
            if (f.length != 6 || !f[0].equals("M") || f[1].isEmpty() || f[4].isEmpty()) {
                continue;
            }
            try {
                if (!f[2].equals("*")) {
                    UUID.fromString(f[2]);
                }
                out.add(new Message(f[1], f[2], Long.parseLong(f[3]), f[4], f[5]));
            } catch (IllegalArgumentException e) {
                // skipped
            }
        }
        return out;
    }
}
