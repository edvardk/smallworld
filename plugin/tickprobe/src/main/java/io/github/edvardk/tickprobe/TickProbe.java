package io.github.edvardk.tickprobe;

import com.destroystokyo.paper.event.server.ServerTickEndEvent;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Bench-only (scripts/bench_telemetry.py): every tick's duration into a preallocated array, the same in both arms
 * of the A/B, so the difference between the arms is mc-telemetry's. `tickprobe reset` starts a measurement,
 * `tickprobe dump FILE` writes one line per tick: duration (microseconds) and end time (epoch ms).
 */
public final class TickProbe extends JavaPlugin implements Listener {
    private final long[] micros = new long[400_000];
    private final long[] at = new long[400_000];
    private int n;

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onTick(ServerTickEndEvent e) {
        if (n < micros.length) {
            at[n] = System.currentTimeMillis();
            micros[n++] = (long) (e.getTickDuration() * 1000.0);
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length == 1 && args[0].equals("reset")) {
            n = 0;
            sender.sendMessage("tickprobe: reset");
            return true;
        }
        if (args.length == 2 && args[0].equals("dump")) {
            StringBuilder sb = new StringBuilder(n * 6);
            for (int i = 0; i < n; i++) {
                sb.append(micros[i]).append(' ').append(at[i]).append('\n');
            }
            try {
                Files.writeString(Path.of(args[1]), sb, StandardCharsets.UTF_8);
            } catch (IOException ex) {
                sender.sendMessage("tickprobe: " + ex);
                return true;
            }
            sender.sendMessage("tickprobe: " + n + " ticks");
            return true;
        }
        return false;
    }
}
