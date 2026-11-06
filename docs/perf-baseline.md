# Performance baseline

Targets (PLAN §5): stable **20 TPS**, **MSPT < 35 ms** with all farms loaded, **no GC pause > 100 ms**.

| Date | Players | Farms loaded | TPS | MSPT avg / p95 | Max GC pause | Heap after GC | spark report | Notes |
|---|---|---|---|---|---|---|---|---|
| _pending first run_ | | | | | | | | |

## How to capture

```bash
mc spark profiler start --timeout 600
journalctl -u minecraft -f | grep -i spark     # report URL
mc spark health                                 # TPS / MSPT / memory snapshot
journalctl -u mc-health --since -1h             # 5-minute health-check history
```

## Tuning log

Knobs live in the config repo's `server.yml` (`performance.*`; defaults in the product schema). Change one at a time, push, and
re-profile. Escalate to a netcup RS (dedicated cores) or larger server if MSPT stays > 40 ms without
an obvious cause, or CPU steal stays > 10% (health.py posts both recommendations to Discord).

| Date | Change | Before | After |
|---|---|---|---|
| | | | |
