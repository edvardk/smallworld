// Watchdog: a Cloudflare Worker that watches the Minecraft server from outside (design §5, §5a, §8 P6).
//
// - Cron (every 5 min): a Minecraft status ping (handshake + status request, JSON parsed: the server answers,
//   not just the kernel) to each target, IPv4 and IPv6 separately, through connect() from cloudflare:sockets.
//   Up to `probe.attempts` tries, `probe.retry_seconds` apart, inside one run; a family alerts after
//   `probe.alert_after_runs` failed runs in a row, and its recovery is announced. A family is armed by its
//   first successful ping (no alerts while a fresh server is still being deployed).
// - Heartbeats: POST /hb/{health|backup}[/start|/fail] with `Authorization: Bearer <HEARTBEAT_TOKEN>`
//   (compared in constant time). A check is LATE when no run has *finished* within period + grace: measured
//   from the last success (`last_ok`; for checks whose sender alerts on its own failures, also the last
//   /fail), never from a /start. /start only arms a "running too long" timer (`max_run_minutes`). /fail
//   alerts if `alert_on_fail`. Recoveries are announced. Armed by its first heartbeat. A JSON body
//   {"message", "admin_stopped"} lets health.py report the admin-stopped marker (design §5a "Stop"): while it
//   is set, the probes don't alert (the server is down on purpose); a late health heartbeat still does.
// - POST /maintenance {"minutes": 0-1440, "reason": "..."}: no alerts until it ends (0 ends it now).
// - GET /status: freshness and states only: no addresses, no messages. Heartbeat replies carry
//   `last_run_age_s`, so the server notices a cron that stopped running. v1.2: `client` (boolean) says whether
//   the client page's bindings are in place (CLIENT, CLIENT_KEY: `mcctl apply`), for `mcctl doctor`.
// - State in D1 (binding DB, one key/value table `watchdog`). Every read-modify-write is a compare-and-set on
//   the row's previous value, so a heartbeat arriving during a cron run is never overwritten. An alert is
//   only marked as sent once Discord accepted it (otherwise the next run tries again). Config in the
//   plain-text binding TARGET (JSON, rendered by terraform/watchdog.tf); optional secret DISCORD_WEBHOOK.
// - A cron run that throws is reported on Discord (at most hourly) and rethrown, so it shows as failed.
// - Monitoring (design §5a, P8; only with the MONITORING, CONTROL and LOGS bindings, i.e. panel.mode
//   cloudflare): mc-agent pushes with `Authorization: Bearer <AGENT_TOKEN>` (a separate secret):
//   POST /ingest/metrics {samples: [...]} -> D1 day tables samples_YYYYMMDD (one row per sample);
//   POST /ingest/logs {rows: [...]} -> D1 day tables logs_YYYYMMDD, capped at logs.max_rows_per_day per UTC
//   day (counter table log_days); POST /ingest/archive?batch=<hex> (gzipped NDJSON) -> R2 binding LOGS
//   (journal/YYYY/MM/DD/<batch>.ndjson.gz; the bucket's lifecycle rule deletes it after logs.r2_days).
//   Retention runs in the cron once a day: sample days older than 7 days become 5-minute rollups
//   (samples_5m, kept retention_days) and are dropped whole, log days older than logs.d1_days are dropped
//   whole (DROP TABLE writes no rows, unlike DELETE). The DOWN alert uses the agent's last sample (< 3 min:
//   the host is up, so it's the network) when there is one.
// - Commands (P8 transport; the panel that issues them is P9/P10): the Durable Object `Control` (SQLite
//   storage, WebSocket hibernation API) holds the agent's socket (GET /agent/ws) and a queue for the poll
//   fallback (GET /agent/commands, POST /agent/results). A command's payload {id, action, args, login, iat,
//   nonce} is signed with HMAC-SHA256 (COMMAND_HMAC_KEY, shared only with the agent's env file); the agent
//   checks signature, age, nonce, its allowlist and the console allowlist. Results: first one wins.
// - Player experience (P13; only with the TELEMETRY binding): POST /ingest/experience (mc-agent's minute rows ->
//   D1 database TELEMETRY, verdicts back in the reply, in-game messages as signed `player_message` commands),
//   PUT /ingest/segment (raw segments -> R2 telemetry/), rollups/retention/remedy checks in the cron, the panel's
//   /api/experience*. See the "player experience" section below.
// - Discord slash commands (P14; only with the plain-text binding DISCORD): POST /discord/interactions (Ed25519-
//   signed by Discord, allowlisted guild/channel/user ids, each interaction once): read commands from D1, and only
//   `/backup now` and `/whitelist add` as signed agent commands. See the "Discord" section below.
//
// No dependencies, no build step: this file is uploaded as is (ES module). The tests (watchdog/test/, node
// --test) drive the same code through `_internals` with a fake socket, D1 and fetch.

import { connect } from "cloudflare:sockets";

// --- monitoring storage (P8) ----------------------------------------------------------------------------
const DAY_TABLE = /^(samples|logs)_(\d{8})$/;
const MONITORING_DEFAULTS = {
  retention_days: 90,
  rollup_after_days: 7,
  logs: { d1_days: 14, max_rows_per_day: 30000 },
  metrics_interval_s: 60,
};
const MAX_METRICS_BODY = 524288;
const MAX_LOGS_BODY = 4194304;
const MAX_ARCHIVE_BODY = 16777216;
const MAX_SAMPLES = 500;
const MAX_ROWS = 20000;
const SAMPLE_PAST_MS = 25 * 3600000; // the agent spools up to 24 h
const SAMPLE_FUTURE_MS = 300000;
const AGENT_FRESH_MS = 180000; // design §5a: no sample for 3 min = the host may be down
const RETENTION_EVERY_MS = 86400000;
const ROLLUP_MS = 300000;
const D1_BATCH = 50;
const COMMAND_TTL_MS = 60000; // = the agent's MAX_AGE_S
const CONSOLE_KEEP = 500; // live console lines kept in the Control object's memory
const CONSOLE_TAIL_S = 120; // one signed `tail` streams this long...
const CONSOLE_RENEW_MS = 30000; // ...and is renewed when less than this is left
const COMMAND_ACTIONS = [
  "status", "console", "start", "stop", "restart", "backup", "tail",
  // P10 (the agent's allowlist; mc_panel.py does the work)
  "cancel", "whitelist", "rejected_dismiss", "moderate", "plugin", "restore",
  // P13: an in-game message the verdict engine decided on (the agent rate-limits it again)
  "player_message",
  // v1.2 world library (panel only, confirmation host; scripts/worlds.py does the work as the ops user)
  "world_create", "world_switch", "world_delete",
  // v1.3: the Panel -> Git self-test (panel, Health tab) and the name check's Mojang lookup from the box (GET
  // /api/mojang asks the agent first: Mojang refuses Cloudflare's egress; never through POST /api/action)
  "selftest", "lookup",
];

const SCHEMA =
  "CREATE TABLE IF NOT EXISTS watchdog (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)";
const UPSERT =
  "INSERT INTO watchdog (key, value, updated_at) VALUES (?1, ?2, ?3) " +
  "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at";
// Compare-and-set: insert only if the key is new; update only if the row still holds the value we read.
const INSERT_NEW =
  "INSERT INTO watchdog (key, value, updated_at) VALUES (?1, ?2, ?3) ON CONFLICT(key) DO NOTHING";
const CAS = "UPDATE watchdog SET value = ?2, updated_at = ?3 WHERE key = ?1 AND value = ?4";
const SELECT = "SELECT key, value FROM watchdog";
const CHECKS = ["health", "backup"];
const MAX_MAINTENANCE_MINUTES = 1440;
const MAX_BODY = 4096;
const MAX_STATUS_BYTES = 262144; // a status response with a big favicon is still far below this
const HEARTBEAT_RETRIES = 3;
const CRON_ERROR_ALERT_MS = 3600000; // a failing cron is reported at most this often

const DEFAULTS = {
  server_host: "",
  targets: [],
  checks: {
    health: { period_minutes: 5, grace_minutes: 10, alert_on_fail: false, max_run_minutes: null },
    backup: { period_minutes: 60, grace_minutes: 30, alert_on_fail: true, max_run_minutes: 60 },
  },
  probe: { attempts: 3, retry_seconds: 20, timeout_seconds: 10, alert_after_runs: 2 },
};

// --- config, state ----------------------------------------------------------------------------------

function config(env) {
  let raw = {};
  try {
    raw = JSON.parse(env.TARGET || "{}");
  } catch {
    raw = {};
  }
  const checks = {};
  for (const name of CHECKS) {
    checks[name] = { ...DEFAULTS.checks[name], ...((raw.checks || {})[name] || {}) };
  }
  const targets = Array.isArray(raw.targets)
    ? raw.targets.filter((t) => t && /^v[46]$/.test(t.family) && typeof t.address === "string")
    : [];
  return {
    server_host: typeof raw.server_host === "string" ? raw.server_host : "",
    targets,
    checks,
    probe: { ...DEFAULTS.probe, ...(raw.probe || {}) },
  };
}

const schemaReady = new WeakSet(); // per database binding, once per isolate

// { state: key -> parsed value, raw: key -> the stored JSON text (the compare-and-set token) }
async function loadState(db) {
  if (!schemaReady.has(db)) {
    await db.prepare(SCHEMA).run();
    schemaReady.add(db);
  }
  const { results } = await db.prepare(SELECT).all();
  const state = {};
  const raw = {};
  for (const row of results || []) {
    raw[row.key] = row.value;
    try {
      state[row.key] = JSON.parse(row.value);
    } catch {
      // a corrupt row is treated as missing (and replaced by the next compare-and-set)
    }
  }
  return { state, raw };
}

// writes: [{ key, old (raw text or undefined for a new key), value }]. Returns the keys whose row changed
// under us (another request won the race); those writes are not applied.
async function commit(db, writes, now) {
  if (!writes.length) return new Set();
  const stmts = writes.map((w) =>
    w.old === undefined
      ? db.prepare(INSERT_NEW).bind(w.key, JSON.stringify(w.value), now)
      : db.prepare(CAS).bind(w.key, JSON.stringify(w.value), now, w.old),
  );
  const results = await db.batch(stmts);
  const lost = new Set();
  writes.forEach((w, i) => {
    const r = results[i];
    if (!r || !r.meta || r.meta.changes !== 1) lost.add(w.key);
  });
  return lost;
}

async function upsert(db, key, value, now) {
  await db.prepare(UPSERT).bind(key, JSON.stringify(value), now).run();
}

function maintenanceActive(state, now) {
  const m = state.maintenance;
  return m && typeof m.until === "number" && m.until > now ? m : null;
}

function adminStopped(state) {
  const h = state["check:health"];
  return Boolean(h && h.admin_stopped === true);
}

// --- auth ---------------------------------------------------------------------------------------------

async function tokenOk(header, secret) {
  if (!secret || typeof header !== "string" || !header.startsWith("Bearer ")) return false;
  const enc = new TextEncoder();
  // Compare digests: equal length, and the loop never exits early (constant time in the token's content).
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(header.slice(7))),
    crypto.subtle.digest("SHA-256", enc.encode(secret)),
  ]);
  const x = new Uint8Array(a);
  const y = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

// --- Minecraft status ping (wiki.vg "Server List Ping") ----------------------------------------------

function varint(value) {
  const out = [];
  let v = value >>> 0; // two's complement for negatives (protocol -1 -> 5 bytes)
  do {
    let byte = v & 0x7f;
    v >>>= 7;
    if (v) byte |= 0x80;
    out.push(byte);
  } while (v);
  return out;
}

function readVarint(buf, offset) {
  let value = 0;
  for (let i = 0; i < 5; i++) {
    if (offset + i >= buf.length) return null; // need more bytes
    const byte = buf[offset + i];
    value |= (byte & 0x7f) << (7 * i);
    if (!(byte & 0x80)) return { value: value >>> 0, next: offset + i + 1 };
  }
  throw new Error("bad varint");
}

function packet(id, payload) {
  const body = [...varint(id), ...payload];
  return [...varint(body.length), ...body];
}

function statusRequest(host, port) {
  const name = [...new TextEncoder().encode(host)];
  const handshake = packet(0x00, [
    ...varint(-1), // protocol version: -1 = "status only", accepted by vanilla/Paper
    ...varint(name.length),
    ...name,
    (port >> 8) & 0xff,
    port & 0xff,
    ...varint(1), // next state: status
  ]);
  return new Uint8Array([...handshake, ...packet(0x00, [])]);
}

// The status JSON from the bytes received so far, or null if more are needed.
function parseStatus(buf) {
  const len = readVarint(buf, 0);
  if (!len) return null;
  if (len.value > MAX_STATUS_BYTES) throw new Error("status response too large");
  if (buf.length < len.next + len.value) return null;
  const id = readVarint(buf, len.next);
  if (!id || id.value !== 0x00) throw new Error("unexpected packet");
  const slen = readVarint(buf, id.next);
  if (!slen || slen.next + slen.value > len.next + len.value) throw new Error("bad status string");
  const json = JSON.parse(new TextDecoder().decode(buf.subarray(slen.next, slen.next + slen.value)));
  if (!json || typeof json !== "object" || !json.version) throw new Error("not a status response");
  return json;
}

function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

function withTimeout(promise, ms, what) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} timed out`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function portOf(address) {
  const m = /:(\d+)$/.exec(address);
  return m ? Number(m[1]) : 25565;
}

async function ping(rt, address, serverHost, timeoutMs) {
  const socket = rt.connect(address);
  try {
    return await withTimeout(
      (async () => {
        await socket.opened;
        const writer = socket.writable.getWriter();
        await writer.write(statusRequest(serverHost || "localhost", portOf(address)));
        writer.releaseLock();
        const reader = socket.readable.getReader();
        let buf = new Uint8Array(0);
        for (;;) {
          const { value, done } = await reader.read();
          if (value) buf = concat(buf, value);
          const status = parseStatus(buf);
          if (status) return status;
          if (done) throw new Error("connection closed before a status response");
        }
      })(),
      timeoutMs,
      "status ping",
    );
  } finally {
    try {
      socket.close();
    } catch {
      // already closed
    }
  }
}

// An error summary safe for /status and Discord: never an address. The configured targets are cut out first
// (with and without the port: a probe name like v4.<fqdn> too), then any IPv4 or IPv6 literal that is left
// (e.g. one the runtime resolved a name to).
const IPV4 = /\b\d{1,3}(?:\.\d{1,3}){3}\b/g;
const IPV6_CANDIDATE = /\[?[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:%[0-9a-z]+)?\]?/gi;

function redactIPv6(text) {
  return text.replace(IPV6_CANDIDATE, (m) => {
    const colons = (m.match(/:/g) || []).length;
    // "12:30:45" (a time) is not an address: an IPv6 literal has "::", all 7 colons, or a hex letter.
    return m.includes("::") || colons >= 7 || /[a-f]/i.test(m.replace(/%.*/, "")) ? "<address>" : m;
  });
}

function describe(err, addresses) {
  let text = `${(err && err.name) || "Error"}: ${(err && err.message) || err}`;
  for (const a of addresses) {
    if (!a) continue;
    const host = a.replace(/:\d+$/, "").replace(/^\[(.*)\]$/, "$1");
    for (const s of [a, host]) if (s) text = text.split(s).join("<address>");
  }
  return redactIPv6(text.replace(IPV4, "<address>")).slice(0, 200);
}

async function probe(rt, target, cfg) {
  const attempts = Math.max(1, Math.min(5, cfg.probe.attempts | 0));
  const addresses = cfg.targets.map((t) => t.address);
  let error = "";
  for (let i = 0; i < attempts; i++) {
    try {
      const st = await ping(rt, target.address, cfg.server_host, cfg.probe.timeout_seconds * 1000);
      const players = st.players || {};
      return {
        ok: true,
        version: String((st.version && st.version.name) || "").slice(0, 64),
        online: Number.isFinite(players.online) ? players.online : null,
      };
    } catch (err) {
      error = describe(err, addresses);
      if (i < attempts - 1) await rt.sleep(cfg.probe.retry_seconds * 1000);
    }
  }
  return { ok: false, error };
}

// --- alerts -----------------------------------------------------------------------------------------

// One template for every alert (the same as the box's scripts/alerts.py; docs/operations.md "Alerts"):
// <emoji> **headline** / Impact / Likely cause / What to do / Details. Plain English, specific causes only.
const SEVERITY = { info: "\u2139\ufe0f", warning: "\u26a0\ufe0f", action: "\u{1f534}", recovered: "\u2705" };
const NO_ACTION = "No action needed - informational.";

function alertText({ severity, headline, impact = "", cause = "", todo = NO_ACTION, details = "" }) {
  const lines = [`${SEVERITY[severity]} **${headline}**`];
  if (impact) lines.push(`**Impact:** ${impact}`);
  if (cause) lines.push(`**Likely cause:** ${cause}`);
  if (todo) lines.push(`**What to do:** ${todo}`);
  if (details) lines.push(`**Details:** ${details}`);
  const text = redactIPv6(lines.join("\n").replace(IPV4, "<address>"));
  return text.length <= 1800 ? text : `${text.slice(0, 1799)}\u2026`;
}

function durationText(ms) {
  const m = Math.max(1, Math.round(ms / 60000));
  if (m < 90) return `${m} min`;
  const h = m / 60;
  return h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} days`;
}

const CHECK_WHAT = {
  health: { noun: "health check", impact: "Self-heal and the server's own alerts may not be running; players may not be affected.", todo: "`mcctl doctor`; on the box: `systemctl status mc-health.timer` and `journalctl -u mc-health -n 50`.", back: "Self-heal and the server's own alerts run again; players were not affected by the check itself." },
  backup: { noun: "backup", impact: "Players are not affected, but recent progress is not backed up: a crash or mistake now could lose it.", todo: "`mcctl backup now` to retry; on the box: `journalctl -u mc-backup -n 100`; check the R2 bucket and its credentials (`mcctl doctor`).", back: "Backups run again: players' progress is protected again." },
};
const checkWhat = (name) => CHECK_WHAT[name] || { noun: `${name} check`, impact: "Players are probably not affected.", todo: "`mcctl doctor`.", back: "It reports OK again." };
const WORKER_LOGS = "the Worker's logs (https://dash.cloudflare.com: Workers & Pages, the watchdog Worker, Logs)";

// Every alert the Worker sends, one builder each, all in the template above (snapshots:
// tests/test_alert_texts.py; the table: docs/operations.md "Alerts").
const WORKER_ALERTS = {
  probeBack: ({ label, version, downForMs = null, online = null }) => alertText({ severity: "recovered", headline: `The server can be reached over ${label} again`, impact: `Players can connect over ${label} again.`, details: `${version}${downForMs !== null ? `; unreachable for ${durationText(downForMs)}` : ""}${Number.isFinite(online) ? `; ${online} online` : ""}` }),
  probeDown: ({ label, other, hostUp, agent, runs, error }) => alertText({
    severity: "action",
    headline: `Players can't reach the server over ${label}`,
    impact: other ? `Players on ${label} can't connect; the other address family still works.` : "Nobody can connect to the server from outside.",
    cause: hostUp
      ? agent
        ? "The server itself still reports in (the agent's samples arrive), so the host is up: a network, firewall or DNS problem between players and the server."
        : "The server's health job still reports in, so the host is up: a network, firewall or DNS problem between players and the server."
      : agent
        ? "No agent sample for over 3 minutes either: the host itself may be down."
        : "No health heartbeat either: the host itself may be down.",
    todo: hostUp
      ? "Check the DNS records and the firewall (`mcctl doctor`), and the hosting provider's status page."
      : "Check the box in the hosting provider's console (is it running?); `mcctl doctor`.",
    details: `no Minecraft status reply over ${label} for ${runs} runs (${error})`,
  }),
  checkLate: ({ name, lateMs, hostUp, periodMinutes }) => alertText({
    severity: "action",
    headline: `The ${checkWhat(name).noun} hasn't run for ${minutes(lateMs)} min`,
    impact: checkWhat(name).impact,
    cause: hostUp ? `The server is up, but its ${checkWhat(name).noun} stopped reporting (stuck, failing before it reports, or its timer is off).` : "The server doesn't report in at all: the host may be down.",
    todo: checkWhat(name).todo,
    details: `no finished ${name} run for ${minutes(lateMs)} min (expected every ${periodMinutes} min)`,
  }),
  checkTooLong: ({ name, runningMs, maxMinutes }) => alertText({
    severity: "warning",
    headline: `The ${checkWhat(name).noun} is taking much longer than usual`,
    impact: checkWhat(name).impact,
    cause: "It is stuck or unusually slow (a slow network to the backup storage, a very large change, a hung process).",
    todo: checkWhat(name).todo,
    details: `the ${name} run started ${minutes(runningMs)} min ago hasn't finished (limit ${maxMinutes} min)`,
  }),
  checkFailed: ({ name, detail }) => alertText({
    severity: "action",
    headline: `The ${checkWhat(name).noun} failed`,
    impact: checkWhat(name).impact,
    cause: detail ? `It reported: ${detail}` : "It reported a failure without details.",
    todo: checkWhat(name).todo,
    details: `${name} /fail heartbeat`,
  }),
  checkBack: ({ name, lastOkAgoMs = null }) => alertText({ severity: "recovered", headline: `The ${checkWhat(name).noun} works again`, impact: checkWhat(name).back, details: `${name}${lastOkAgoMs !== null ? `; last OK before this ${durationText(lastOkAgoMs)} ago` : ""}` }),
  cronFailing: ({ error }) => alertText({ severity: "action", headline: "The outside watchdog itself is failing", impact: "Players are not affected, but outages, late backups and lag would go unreported while it lasts.", cause: `Its scheduled run failed: ${error}.`, todo: `Usually a passing Cloudflare problem (https://www.cloudflarestatus.com). If it lasts: ${WORKER_LOGS}.`, details: "watchdog cron run failed" }),
  cronBack: ({ since }) => alertText({ severity: "recovered", headline: "The outside watchdog runs cleanly again", impact: "Outages, late backups and lag are reported again.", details: `failing since ${new Date(since).toISOString().slice(0, 16).replace("T", " ")} UTC` }),
  housekeepingFailed: ({ what, error }) => alertText({ severity: "warning", headline: `The panel's ${what} housekeeping failed`, impact: "Players are not affected and the outside checks ran normally; panel history may lag behind.", cause: `${error}.`, todo: `Usually nothing: it retries on the next run. If it repeats: ${WORKER_LOGS}.`, details: `${what} maintenance failed` }),
  crowdedChunk: ({ c, online, limit }) => alertText({
    severity: "warning",
    headline: `A crowded chunk is slowing the server: ${c.ent} entities in one spot`,
    impact: `Every tick has to move and collide them all; with more it turns into lag for everyone. ${online ? `${online} player(s) online.` : "Nobody is online right now."}`,
    cause: `In ${chunkWhere(c)} there are ${c.ent} entities (${c.vil} villagers, ${c.items} dropped items) and ${c.tiles} tile entities: usually an animal pen, a mob farm's collection area or items piling up.`,
    todo: `Spread the pen or farm over more chunks or add floor space; collect dropped items quickly (hoppers next to the kill spot). Vanilla cramming kills mobs above ${CRAMMING} per block. The panel's Experience tab lists the busiest chunks.`,
    details: `alert at ${limit} entities per chunk (monitoring.alerts.chunk_entities); again after 6 h if still crowded`,
  }),
};

// "sent" (Discord accepted it), "off" (no webhook: nothing to deliver, the alert counts as handled) or
// "failed" (try again on the next run).
async function notify(rt, env, cfg, lines) {
  if (!lines.length) return "sent";
  if (!env.DISCORD_WEBHOOK || !env.DISCORD_WEBHOOK.startsWith("https://")) return "off";
  const who = cfg.server_host ? `[${cfg.server_host}] ` : "";
  const content = lines.map((l) => who + l).join("\n\n").slice(0, 1900);
  try {
    const resp = await rt.fetch(env.DISCORD_WEBHOOK, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
    });
    return resp.ok ? "sent" : "failed";
  } catch {
    return "failed";
  }
}

// Write `updates` (compare-and-set against what was read), send the alerts of the writes that landed, and
// undo their "alerted" marks if Discord didn't take them, so the next run (or heartbeat) alerts again.
//   updates: key -> new value;  alerts: [{ key, text, revert(value) }]
async function persistAndAlert(rt, env, cfg, raw, updates, alerts, now) {
  const writes = Object.entries(updates).map(([key, value]) => ({ key, old: raw[key], value }));
  const lost = await commit(env.DB, writes, now);
  const live = alerts.filter((a) => !lost.has(a.key));
  const outcome = await notify(rt, env, cfg, live.map((a) => a.text));
  if (outcome === "failed" && live.length) {
    const undo = {};
    for (const a of live) {
      const value = undo[a.key] || structuredClone(updates[a.key]);
      a.revert(value);
      undo[a.key] = value;
    }
    const again = Object.entries(undo).map(([key, value]) => ({
      key,
      old: JSON.stringify(updates[key]),
      value,
    }));
    await commit(env.DB, again, now); // lost again = a newer write already replaced it: nothing to undo
  }
  return { lines: live.map((a) => a.text), lost, outcome };
}

function minutes(ms) {
  return Math.round(ms / 60000);
}

// When a check last *finished*: the last success, plus the last /fail for checks whose sender alerts on
// failures itself (health.py). A /start never counts. Before the first finish: when it was armed.
function finishedAt(c, spec) {
  const ends = [c.last_ok];
  if (!spec.alert_on_fail) ends.push(c.last_fail);
  const known = ends.filter((t) => typeof t === "number");
  if (known.length) return Math.max(...known);
  if (typeof c.armed_at === "number") return c.armed_at;
  return typeof c.last_ping === "number" ? c.last_ping : null;
}

function lateBy(c, spec, now) {
  const since = finishedAt(c, spec);
  if (since === null) return null;
  const age = now - since;
  return age > (spec.period_minutes + spec.grace_minutes) * 60000 ? age : null;
}

function runningTooLong(c, spec, now) {
  if (!spec.max_run_minutes || typeof c.started_at !== "number") return null;
  const age = now - c.started_at;
  return age > spec.max_run_minutes * 60000 ? age : null;
}

// --- the cron run -----------------------------------------------------------------------------------

async function runChecks(env, now, rt) {
  const cfg = config(env);
  // Probes first (they take up to a minute), then read the state: the window in which a heartbeat could
  // slip in between our read and our write is milliseconds, and the compare-and-set closes it.
  const results = await Promise.all(cfg.targets.map((t) => probe(rt, t, cfg)));
  const { state, raw } = await loadState(env.DB);
  const maint = maintenanceActive(state, now);
  const stopped = adminStopped(state);
  const updates = {};
  const alerts = [];

  const health = state["check:health"] || {};
  const healthSpec = cfg.checks.health;
  const healthFresh =
    typeof health.last_ping === "number" &&
    now - health.last_ping <= (healthSpec.period_minutes + healthSpec.grace_minutes) * 60000;
  // The agent's samples (every minute) are the sharper signal when monitoring is on (design §5a).
  const sampleAt = env.MONITORING ? await lastSample(env, now) : null;
  const hostUp = sampleAt !== null ? now - sampleAt <= AGENT_FRESH_MS : healthFresh;

  cfg.targets.forEach((t, i) => {
    const r = results[i];
    const key = `probe:${t.family}`;
    const p = { failed_runs: 0, alerted: false, last_ok: null, ...(state[key] || {}) };
    const label = t.family === "v4" ? "IPv4" : "IPv6";
    p.last_run = now;
    if (r.ok) {
      if (p.alerted) {
        alerts.push({
          key,
          text: WORKER_ALERTS.probeBack({ label, version: r.version, downForMs: typeof p.down_since === "number" ? now - p.down_since : null, online: r.online }),
          revert: (v) => (v.alerted = true),
        });
      }
      Object.assign(p, { ok: true, last_ok: now, failed_runs: 0, alerted: false, error: "", down_since: null });
      p.version = r.version;
      p.online = r.online;
    } else {
      p.ok = false;
      p.error = r.error;
      p.failed_runs += 1;
      if (p.failed_runs === 1) p.down_since = now;
      const armed = p.last_ok !== null;
      if (armed && !maint && !stopped && !p.alerted && p.failed_runs >= cfg.probe.alert_after_runs) {
        p.alerted = true;
        const other = cfg.targets.length > 1 && results.some((x, k) => k !== i && x.ok);
        alerts.push({
          key,
          text: WORKER_ALERTS.probeDown({ label, other, hostUp, agent: sampleAt !== null, runs: p.failed_runs, error: r.error }),
          revert: (v) => (v.alerted = false),
        });
      }
    }
    updates[key] = p;
  });

  for (const name of CHECKS) {
    const key = `check:${name}`;
    const c = state[key];
    if (!c || typeof c.last_ping !== "number") continue; // not armed yet
    if (maint) continue;
    const spec = cfg.checks[name];
    const next = { ...c };
    const late = lateBy(c, spec, now);
    if (late !== null && !c.alerted) {
      next.alerted = "late";
      alerts.push({
        key,
        text: WORKER_ALERTS.checkLate({ name, lateMs: late, hostUp, periodMinutes: spec.period_minutes }),
        revert: (v) => (v.alerted = c.alerted || false),
      });
    }
    const running = runningTooLong(c, spec, now);
    if (running !== null && c.run_alerted !== c.started_at) {
      next.run_alerted = c.started_at;
      alerts.push({
        key,
        text: WORKER_ALERTS.checkTooLong({ name, runningMs: running, maxMinutes: spec.max_run_minutes }),
        revert: (v) => (v.run_alerted = c.run_alerted === undefined ? null : c.run_alerted),
      });
    }
    if (next.alerted !== c.alerted || next.run_alerted !== c.run_alerted) updates[key] = next;
  }

  const prevErr = state.cron_error;
  if (prevErr && prevErr.active) {
    updates.cron_error = { active: false, since: prevErr.since, alerted_at: prevErr.alerted_at || null };
    if (prevErr.alerted_at) {
      alerts.push({
        key: "cron_error",
        text: WORKER_ALERTS.cronBack({ since: prevErr.since || now }),
        revert: (v) => (v.active = true),
      });
    }
  }

  updates.run = { last_run: now, targets: cfg.targets.length };
  // v1.3: this Worker's version (mcctl render's stamp) in the key/value table, written only when it changes, for
  // `mcctl doctor` (which reads D1, not the panel)
  const stamp = await workerStamp(env);
  if (stamp && JSON.stringify(state.worker || null) !== JSON.stringify(stamp)) updates.worker = stamp;
  const { lines } = await persistAndAlert(rt, env, cfg, raw, updates, alerts, now);
  return lines;
}

let lastCronErrorAlert = 0; // per isolate: the fallback when D1 itself is what fails

async function reportCronError(env, now, rt, text) {
  const cfg = config(env);
  let loaded = null;
  try {
    loaded = await loadState(env.DB);
  } catch {
    loaded = null;
  }
  const prev = loaded ? loaded.state.cron_error || {} : {};
  const lastAlert = Math.max(lastCronErrorAlert, prev.active && prev.alerted_at ? prev.alerted_at : 0);
  const due = now - lastAlert >= CRON_ERROR_ALERT_MS;
  const outcome = due
    ? await notify(rt, env, cfg, [WORKER_ALERTS.cronFailing({ error: text })])
    : "skipped";
  if (outcome === "sent" || outcome === "off") lastCronErrorAlert = now;
  if (!loaded) return outcome;
  const value = {
    active: true,
    since: prev.active ? prev.since : now,
    error: text,
    alerted_at: outcome === "sent" ? now : prev.active ? prev.alerted_at || null : null,
  };
  try {
    await commit(env.DB, [{ key: "cron_error", old: loaded.raw.cron_error, value }], now);
  } catch {
    // D1 is failing too: the isolate-level timestamp above still limits the alerts
  }
  return outcome;
}

const lastMaintAlert = new Map(); // per isolate: what -> last alert (ms)

// P13 review L5: a failure in the storage/experience maintenance is not "external checks are not running" (they
// ran, and persisted, before it): its own alert, at most hourly per isolate, and the cron run still succeeds.
async function reportMaintenanceError(env, now, rt, what, text) {
  console.error(`watchdog ${what} maintenance: ${text}`);
  if (now - (lastMaintAlert.get(what) || 0) < CRON_ERROR_ALERT_MS) return "skipped";
  const outcome = await notify(rt, env, config(env), [WORKER_ALERTS.housekeepingFailed({ what, error: text })]);
  if (outcome === "sent" || outcome === "off") lastMaintAlert.set(what, now);
  return outcome;
}

async function scheduledRun(env, now, rt) {
  let lines;
  try {
    lines = await runChecks(env, now, rt);
  } catch (err) {
    const text = describe(err, config(env).targets.map((t) => t.address));
    console.error(`watchdog cron run failed: ${text}`); // no addresses, no secrets
    await reportCronError(env, now, rt, text);
    throw err;
  }
  const hide = config(env).targets.map((t) => t.address);
  if (env.MONITORING) {
    try {
      await maintainStorage(env, now);
    } catch (err) {
      await reportMaintenanceError(env, now, rt, "storage", describe(err, hide));
    }
  }
  if (env.CLIENT) {
    try {
      await maintainClient(env, now);
    } catch (err) {
      await reportMaintenanceError(env, now, rt, "client", describe(err, hide));
    }
  }
  if (env.TELEMETRY) {
    try {
      const done = await maintainExperience(env, now, rt);
      if (done.skipped) await reportMaintenanceError(env, now, rt, "experience", `skipped ${done.skipped} after ${MAINT_MAX_TRIES} interrupted runs (the Worker's CPU limit?)`);
    } catch (err) {
      await reportMaintenanceError(env, now, rt, "experience", describe(err, hide));
    }
  }
  return lines;
}


// --- monitoring: ingest, retention (P8) ---------------------------------------------------------------

function monitoringConfig(env) {
  let raw = {};
  try {
    raw = JSON.parse(env.MONITORING || "{}");
  } catch {
    raw = {};
  }
  return {
    ...MONITORING_DEFAULTS,
    ...raw,
    logs: { ...MONITORING_DEFAULTS.logs, ...(raw.logs || {}) },
  };
}

function dayOf(ms) {
  return new Date(ms).toISOString().slice(0, 10).replace(/-/g, "");
}

function dayStart(day) {
  return Date.UTC(Number(day.slice(0, 4)), Number(day.slice(4, 6)) - 1, Number(day.slice(6, 8)));
}

const createdTables = new WeakMap(); // per database binding: day tables known to exist in this isolate

async function ensureTable(db, name) {
  let known = createdTables.get(db);
  if (!known) {
    known = new Set();
    createdTables.set(db, known);
  }
  if (known.has(name)) return;
  const m = DAY_TABLE.exec(name);
  let sql;
  if (name === "samples_5m" || (m && m[1] === "samples")) {
    sql = `CREATE TABLE IF NOT EXISTS ${name} (ts INTEGER PRIMARY KEY, data TEXT NOT NULL)`;
  } else if (m && m[1] === "logs") {
    sql =
      `CREATE TABLE IF NOT EXISTS ${name} (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, unit TEXT NOT NULL, ` +
      "prio INTEGER NOT NULL, audit INTEGER NOT NULL, msg TEXT NOT NULL)";
  } else if (name === "log_days") {
    sql = "CREATE TABLE IF NOT EXISTS log_days (day TEXT PRIMARY KEY, rows INTEGER NOT NULL)";
  } else {
    throw new Error("unknown table");
  }
  await db.prepare(sql).run();
  known.add(name);
}

async function runBatches(db, stmts) {
  const out = [];
  for (let i = 0; i < stmts.length; i += D1_BATCH) out.push(...(await db.batch(stmts.slice(i, i + D1_BATCH))));
  return out;
}

// P8 review: the samples a day may write (twice the configured rate, so a spool replay after an outage fits), so
// a leaked agent token can't spend the D1 write budget with made-up timestamps. The count costs no writes: each
// isolate counts a day's rows once (COUNT, at most the cap in rows read) and then adds what it inserts. Isolates
// running at the same time each enforce the cap on their own view, so the bound is per isolate.
function maxSamplesPerDay(mcfg) {
  return Math.ceil(86400 / Math.max(30, Number(mcfg.metrics_interval_s) || 60)) * 2;
}

const sampleCounts = new WeakMap(); // database binding -> Map(day -> rows known in that day table)

async function samplesStored(db, table, day) {
  let counts = sampleCounts.get(db);
  if (!counts) {
    counts = new Map();
    sampleCounts.set(db, counts);
  }
  if (!counts.has(day)) {
    for (const old of counts.keys()) if (old < day) counts.delete(old);
    const { results } = await db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).all();
    counts.set(day, results && results.length ? Number(results[0].n) || 0 : 0);
  }
  return counts;
}

async function readLimited(request, max) {
  const len = Number(request.headers.get("Content-Length") || "0");
  if (len > max) return null;
  const buf = new Uint8Array(await request.arrayBuffer());
  return buf.length > max ? null : buf;
}

async function readJson(request, max) {
  const buf = await readLimited(request, max);
  if (!buf) return { error: json({ error: "body too large" }, 413) };
  try {
    return { data: JSON.parse(new TextDecoder().decode(buf)) };
  } catch {
    return { error: json({ error: "body must be JSON" }, 400) };
  }
}

function finiteOrNull(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : typeof v === "boolean" ? v : null;
}

// A sample keeps only flat numbers/booleans/null under short keys: nothing the agent didn't measure.
function cleanSample(s) {
  const out = {};
  for (const [k, v] of Object.entries(s)) {
    if (/^[a-z][a-z0-9_]{0,31}$/.test(k) && k !== "ts") out[k] = finiteOrNull(v);
  }
  // P9: who is online (the panel's avatars); only Minecraft names, at most 100.
  if (Array.isArray(s.players_online)) {
    out.players_online = s.players_online.filter((n) => typeof n === "string" && PLAYER_NAME.test(n)).slice(0, 100);
  }
  return out;
}

const PLAYER_NAME = /^[A-Za-z0-9_]{1,16}$/;
const PLUGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/; // plugins.lock.yml names (mcctl schema), no leading dot
const INVENTORY_LISTS = {
  whitelist: ["name", "uuid", "unmanaged"], // v1.3: unmanaged = kept by the last deploy, not in players.yml
  ops: ["name", "uuid", "level"],
  bans: ["name", "uuid", "created", "source", "expires", "reason"],
  ip_bans: ["ip", "created", "source", "expires", "reason"],
  plugins: ["file", "name", "enabled"],
  // P10
  pending: ["uuid", "name", "op", "ts", "login", "pr"],
  rejected: ["name", "uuid", "count", "first", "last", "ip"],
  plugins_lock: ["name", "filename", "version"],
  snapshots: ["id", "short_id", "time", "tags", "host", "size", "added"],
};

function str(v, max = 200) {
  return typeof v === "string" ? v.slice(0, max) : "";
}

function numOrNull(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// P10: the agent's job (restart/stop/plugin/restore countdowns and steps), for the panel's banner.
const JOB_STATES = ["countdown", "running", "done", "failed", "cancelled"];
function cleanJob(j) {
  if (!j || typeof j !== "object") return null;
  return {
    id: str(j.id, 64),
    action: COMMAND_ACTIONS.includes(j.action) ? j.action : "",
    label: str(j.label, 40),
    login: str(j.login),
    state: JOB_STATES.includes(j.state) ? j.state : "",
    step: str(j.step, 300),
    countdown: numOrNull(j.countdown),
    deadline: numOrNull(j.deadline),
    started: numOrNull(j.started),
    finished: numOrNull(j.finished),
    players: numOrNull(j.players),
  };
}

function cleanSync(w) {
  if (!w || typeof w !== "object") return null;
  const pr = w.pr && typeof w.pr === "object" && Number.isInteger(w.pr.number)
    ? { number: w.pr.number, url: /^https:\/\/github\.com\//.test(str(w.pr.url)) ? str(w.pr.url) : "", branch: str(w.pr.branch, 80), created: numOrNull(w.pr.created) }
    : null;
  return {
    configured: w.configured === true,
    pr,
    error: str(w.error, 300),
    expired: (Array.isArray(w.expired) ? w.expired : []).slice(-10).filter((e) => e && typeof e === "object").map((e) => ({ name: str(e.name, 16), op: e.op === "remove" ? "remove" : "add", ts: numOrNull(e.ts) })),
  };
}

// P9: the agent's snapshot of whitelist/ops/bans/plugins (every 10 min) -> one key/value row.
function cleanInventory(inv) {
  if (!inv || typeof inv !== "object") return null;
  const out = { ts: Number.isInteger(inv.ts) ? inv.ts : null };
  for (const [list, keys] of Object.entries(INVENTORY_LISTS)) {
    const items = Array.isArray(inv[list]) ? inv[list].slice(0, 1000) : [];
    out[list] = items
      .filter((e) => e && typeof e === "object")
      .map((e) => {
        const o = {};
        for (const k of keys) {
          const v = e[k];
          if (typeof v === "string") o[k] = v.slice(0, 200);
          else if (typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v))) o[k] = v;
        }
        // P9 review: flags are booleans, whatever the agent sent (the UI puts them in attributes).
        if (list === "plugins") o.enabled = e.enabled === true;
        if (list === "whitelist") o.unmanaged = e.unmanaged === true;
        if (list === "pending") o.op = e.op === "remove" ? "remove" : "add";
        return o;
      });
  }
  out.plugins_disabled = (Array.isArray(inv.plugins_disabled) ? inv.plugins_disabled : []).filter((n) => typeof n === "string" && PLUGIN_NAME.test(n)).slice(0, 100);
  out.whitelist_sync = cleanSync(inv.whitelist_sync);
  out.job = cleanJob(inv.job);
  out.worlds = cleanWorlds(inv.worlds);
  out.product = cleanProduct(inv.product);
  out.selftest = cleanSelftest(inv.selftest);
  return out;
}

// v1.3 "versions everywhere": what the last deploy installed on the box (its version file) and the version the agent
// process runs (a difference: it wasn't restarted after the deploy).
const VERSION_TEXT = /^[0-9A-Za-z.+-]{1,40}$/;
const COMMIT = /^[0-9a-f]{40}$/;
const FINGERPRINT = /^[0-9a-f]{64}$/; // v1.5.2: the Worker fingerprint (mcctl/fingerprint.py, sha256 hex)
function cleanProduct(p) {
  if (!p || typeof p !== "object" || !VERSION_TEXT.test(str(p.version))) return null;
  const out = {
    version: p.version,
    commit: COMMIT.test(str(p.commit)) ? p.commit : "",
    running: VERSION_TEXT.test(str(p.running)) ? p.running : "",
    running_commit: COMMIT.test(str(p.running_commit)) ? p.running_commit : "",
  };
  // v1.5.2: the Worker fingerprint the installed release expects (an agent from before v1.5.2 sends none)
  if (FINGERPRINT.test(str(p.worker_fingerprint))) out.worker_fingerprint = p.worker_fingerprint;
  return out;
}

// v1.3: the agent's last Panel -> Git self-test (the steps it passed, the one that failed; never a token).
const SELFTEST_STEPS = ["configuration", "token", "permissions", "repository", "main", "pulls"];
function cleanSelftest(t) {
  if (!t || typeof t !== "object") return null;
  return {
    ok: t.ok === true,
    ts: numOrNull(t.ts),
    configured: t.configured !== false,
    error: str(t.error, 300),
    trigger: t.trigger === "panel" ? "panel" : "daily",
    failures: Number.isInteger(t.failures) && t.failures >= 0 ? t.failures : 0,
    steps: (Array.isArray(t.steps) ? t.steps : []).slice(0, 10).filter((x) => x && SELFTEST_STEPS.includes(x.step)).map((x) => ({ step: x.step, ok: x.ok === true, detail: str(x.detail, 200) })),
  };
}

// v1.2: the agent's world library (worlds.json + the library + the pending change), for the Worlds tab.
const WORLD_NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const WORLD_SEED = /^(-?[0-9]{1,19})?$/;
const WORLD_TYPES = ["default", "large_biomes", "amplified", "single_biome", "flat_classic", "flat_void", "flat_redstone"];
const WORLD_PRESETS = ["survival_smp", "creative_build", "hardcore", "farm_test", "peaceful"];
const WORLD_DIMS = { overworld: "minecraft:overworld", nether: "minecraft:the_nether", end: "minecraft:the_end" };
const BORDER_MAX = 29999984;

function cleanWorldEntry(e) {
  if (!e || typeof e !== "object" || !WORLD_NAME.test(e.name)) return null;
  const border = e.border_radius && typeof e.border_radius === "object" ? {} : null;
  if (border) for (const id of Object.values(WORLD_DIMS)) border[id] = Number.isInteger(e.border_radius[id]) ? e.border_radius[id] : null;
  return {
    name: e.name,
    seed: typeof e.seed === "string" && WORLD_SEED.test(e.seed) ? e.seed : "",
    type: WORLD_TYPES.includes(e.type) ? e.type : "default",
    preset: WORLD_PRESETS.includes(e.preset) ? e.preset : null,
    border_radius: border,
    pregen: typeof e.pregen === "boolean" ? e.pregen : null,
  };
}

function cleanWorlds(w) {
  if (!w || typeof w !== "object" || !WORLD_NAME.test(w.active)) return null;
  const archives = (Array.isArray(w.archives) ? w.archives : []).slice(0, 100).filter((a) => a && WORLD_NAME.test(a.name)).map((a) => ({
    name: a.name,
    short_id: /^[0-9a-f]{8,64}$/.test(a.short_id) ? a.short_id : "",
    time: str(a.time, 40),
    size: numOrNull(a.size),
    data_version: Number.isInteger(a.data_version) ? a.data_version : null,
    count: numOrNull(a.count),
  }));
  const p = w.pending && typeof w.pending === "object" ? w.pending : null;
  return {
    active: w.active,
    since: str(w.since, 40),
    data_version: Number.isInteger(w.data_version) ? w.data_version : null,
    archives,
    library: (Array.isArray(w.library) ? w.library : []).slice(0, 100).map(cleanWorldEntry).filter(Boolean),
    deleted: (Array.isArray(w.deleted) ? w.deleted : []).filter((n) => WORLD_NAME.test(n)).slice(0, 100),
    archive_bytes: numOrNull(w.archive_bytes),
    storage_note: w.storage_note === true,
    pending: p ? { what: str(p.what, 200), error: str(p.error, 300), pr: Number.isInteger(p.pr) ? p.pr : null, url: /^https:\/\/github\.com\//.test(str(p.url)) ? str(p.url) : "" } : null,
  };
}

async function ingestMetrics(env, mcfg, now, data) {
  const samples = data && Array.isArray(data.samples) ? data.samples : null;
  if (!samples || samples.length > MAX_SAMPLES) return json({ error: `samples: 1-${MAX_SAMPLES}` }, 400);
  const byDay = new Map();
  let rejected = 0;
  for (const s of samples) {
    const ts = s && s.ts;
    if (!Number.isInteger(ts) || ts < now - SAMPLE_PAST_MS || ts > now + SAMPLE_FUTURE_MS) {
      rejected += 1;
      continue;
    }
    const day = dayOf(ts);
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push([ts, JSON.stringify(cleanSample(s))]);
  }
  const cap = maxSamplesPerDay(mcfg);
  let stored = 0;
  let capped = 0;
  for (const [day, rows] of byDay) {
    const table = `samples_${day}`;
    await ensureTable(env.DB, table);
    const counts = await samplesStored(env.DB, table, day);
    const take = rows.slice(0, Math.max(0, cap - counts.get(day)));
    capped += rows.length - take.length;
    if (!take.length) continue;
    const stmts = take.map(([ts, body]) =>
      env.DB.prepare(`INSERT OR IGNORE INTO ${table} (ts, data) VALUES (?1, ?2)`).bind(ts, body),
    );
    const res = await runBatches(env.DB, stmts);
    const n = res.reduce((acc, r) => acc + ((r && r.meta && r.meta.changes) || 0), 0);
    counts.set(day, counts.get(day) + n);
    stored += n;
  }
  if (capped) console.error(`ingest: ${capped} sample(s) over the daily cap (${cap}) dropped`);
  return json({ ok: true, stored, rejected, capped });
}

async function ingestLogs(env, mcfg, now, data) {
  const rows = data && Array.isArray(data.rows) ? data.rows : null;
  if (!rows || rows.length > MAX_ROWS) return json({ error: `rows: 1-${MAX_ROWS}` }, 400);
  await ensureTable(env.DB, "log_days");
  const byDay = new Map();
  for (const r of rows) {
    if (!r || !Number.isInteger(r.ts) || r.ts < now - SAMPLE_PAST_MS * 7 || r.ts > now + SAMPLE_FUTURE_MS) continue;
    const day = dayOf(r.ts);
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(r);
  }
  let stored = 0;
  let dropped = rows.length;
  for (const [day, dayRows] of byDay) {
    const { results } = await env.DB.prepare("SELECT rows FROM log_days WHERE day = ?1").bind(day).all();
    const used = results && results.length ? Number(results[0].rows) || 0 : 0;
    const take = dayRows.slice(0, Math.max(0, mcfg.logs.max_rows_per_day - used));
    if (!take.length) continue;
    const table = `logs_${day}`;
    await ensureTable(env.DB, table);
    const stmts = take.map((r) =>
      env.DB.prepare(`INSERT INTO ${table} (ts, unit, prio, audit, msg) VALUES (?1, ?2, ?3, ?4, ?5)`).bind(
        r.ts,
        String(r.unit || "").slice(0, 100),
        Number.isInteger(r.prio) ? r.prio : 6,
        r.audit === 1 ? 1 : 0,
        String(r.msg || "").slice(0, 2000),
      ),
    );
    stmts.push(
      env.DB.prepare(
        "INSERT INTO log_days (day, rows) VALUES (?1, ?2) ON CONFLICT(day) DO UPDATE SET rows = rows + excluded.rows",
      ).bind(day, take.length),
    );
    await runBatches(env.DB, stmts);
    stored += take.length;
  }
  dropped -= stored;
  return json({ ok: true, stored, dropped });
}

async function ingestArchive(env, now, request, url) {
  const batch = url.searchParams.get("batch") || "";
  if (!/^[0-9a-f]{16,64}$/.test(batch)) return json({ error: "batch must be hex" }, 400);
  const body = await readLimited(request, MAX_ARCHIVE_BODY);
  if (!body) return json({ error: "body too large" }, 413);
  if (body.length < 2 || body[0] !== 0x1f || body[1] !== 0x8b) return json({ error: "body must be gzip" }, 400);
  const d = new Date(now).toISOString();
  const key = `journal/${d.slice(0, 4)}/${d.slice(5, 7)}/${d.slice(8, 10)}/${batch}.ndjson.gz`;
  await env.LOGS.put(key, body, { httpMetadata: { contentType: "application/gzip" } });
  return json({ ok: true, key });
}

// Mean of each numeric field per 5-minute bucket; booleans keep the bucket's last value.
function rollup(rows) {
  const buckets = new Map();
  for (const { ts, data } of rows) {
    let s;
    try {
      s = JSON.parse(data);
    } catch {
      continue;
    }
    const b = Math.floor(ts / ROLLUP_MS) * ROLLUP_MS;
    if (!buckets.has(b)) buckets.set(b, { sums: {}, counts: {}, last: {} });
    const acc = buckets.get(b);
    for (const [k, v] of Object.entries(s)) {
      if (typeof v === "number") {
        acc.sums[k] = (acc.sums[k] || 0) + v;
        acc.counts[k] = (acc.counts[k] || 0) + 1;
      } else if (typeof v === "boolean") {
        acc.last[k] = v;
      }
    }
  }
  return [...buckets].map(([ts, acc]) => {
    const out = { ...acc.last };
    for (const k of Object.keys(acc.sums)) out[k] = Math.round((acc.sums[k] / acc.counts[k]) * 100) / 100;
    return { ts, data: JSON.stringify(out) };
  });
}

// Once a day from the cron. At most 3 sample days are rolled up per run (CPU), the rest on the next days.
async function maintainStorage(env, now) {
  const mcfg = monitoringConfig(env);
  const { state, raw } = await loadState(env.DB);
  const last = state.retention && state.retention.last_run;
  if (typeof last === "number" && now - last < RETENTION_EVERY_MS) return null;
  const lost = await commit(env.DB, [{ key: "retention", old: raw.retention, value: { last_run: now } }], now);
  if (lost.has("retention")) return null; // another run is doing it
  const { results } = await env.DB.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND (name LIKE 'samples_%' OR name LIKE 'logs_%')",
  ).all();
  const today = dayStart(dayOf(now));
  const done = { rolled: [], dropped: [] };
  await ensureTable(env.DB, "samples_5m");
  for (const { name } of (results || []).sort((a, b) => (a.name < b.name ? -1 : 1))) {
    const m = DAY_TABLE.exec(name);
    if (!m) continue;
    const ageDays = (today - dayStart(m[2])) / 86400000;
    if (m[1] === "samples" && ageDays >= mcfg.rollup_after_days && done.rolled.length < 3) {
      const { results: rows } = await env.DB.prepare(`SELECT ts, data FROM ${name}`).all();
      const stmts = rollup(rows || []).map((r) =>
        env.DB.prepare("INSERT OR REPLACE INTO samples_5m (ts, data) VALUES (?1, ?2)").bind(r.ts, r.data),
      );
      await runBatches(env.DB, stmts);
      await env.DB.prepare(`DROP TABLE IF EXISTS ${name}`).run();
      done.rolled.push(name);
    } else if (m[1] === "logs" && ageDays >= mcfg.logs.d1_days) {
      await env.DB.prepare(`DROP TABLE IF EXISTS ${name}`).run();
      done.dropped.push(name);
    }
  }
  await env.DB.prepare("DELETE FROM samples_5m WHERE ts < ?1").bind(now - mcfg.retention_days * 86400000).run();
  await ensureTable(env.DB, "log_days");
  await env.DB.prepare("DELETE FROM log_days WHERE day < ?1").bind(dayOf(now - mcfg.logs.d1_days * 86400000)).run();
  return done;
}

// When the agent last delivered a sample (null: no agent, or no sample today/yesterday).
async function lastSample(env, now) {
  let latest = null;
  for (const day of [dayOf(now), dayOf(now - 86400000)]) {
    const table = `samples_${day}`;
    await ensureTable(env.DB, table);
    const { results } = await env.DB.prepare(`SELECT MAX(ts) AS ts FROM ${table}`).all();
    const ts = results && results.length ? results[0].ts : null;
    if (typeof ts === "number") latest = Math.max(latest || 0, ts);
    if (latest !== null) break;
  }
  return latest;
}

// --- commands: the Control Durable Object (P8) --------------------------------------------------------

async function hmacHex(key, text) {
  const enc = new TextEncoder();
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(text)));
  return [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomId(bytes) {
  return [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class Control {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    // The agent's keepalive: a text "ping" is answered "pong" by the runtime without waking this object (P8
    // review: the agent reconnects when nothing comes back for 2 minutes).
    if (typeof WebSocketRequestResponsePair === "function" && ctx.setWebSocketAutoResponse) {
      ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
    }
  }

  async fetch(request) {
    const url = new URL(request.url);
    const now = Date.now();
    if (url.pathname === "/ws") return this.accept();
    if (url.pathname === "/commands") return json({ commands: await this.pending(now) });
    if (url.pathname === "/results") {
      const { data, error } = await readJson(request, MAX_METRICS_BODY);
      if (error) return error;
      const stored = await this.storeResults(data && Array.isArray(data.results) ? data.results : [], now);
      return json({ ok: true, stored });
    }
    if (url.pathname === "/enqueue") {
      const { data, error } = await readJson(request, 16384);
      if (error) return error;
      return this.enqueue(data, now);
    }
    if (url.pathname === "/result") {
      const id = url.searchParams.get("id") || "";
      return json({ result: (await this.ctx.storage.get(`result:${id}`)) || null });
    }
    if (url.pathname === "/console") return json(this.consoleSince(Number(url.searchParams.get("after")) || 0, now));
    if (url.pathname === "/live") {
      const { data, error } = await readJson(request, 4096);
      if (error) return error;
      return this.live(data, now);
    }
    if (url.pathname === "/state") return json({ agent_connected: this.ctx.getWebSockets("agent").length > 0 });
    if (url.pathname === "/ask") {
      const { data, error } = await readJson(request, 4096);
      if (error) return error;
      return this.ask(data, now);
    }
    // P10: the agent's current job (countdown, steps, result), kept in this object's storage (no D1 rows).
    if (url.pathname === "/job" && request.method === "POST") {
      const { data, error } = await readJson(request, 16384);
      if (error) return error;
      const job = cleanJob(data && data.job);
      if (!job) return json({ error: "job must be an object" }, 400);
      await this.ctx.storage.put("job", { ...job, received: now });
      return json({ ok: true });
    }
    if (url.pathname === "/job") return json({ job: (await this.ctx.storage.get("job")) || null });
    return json({ error: "not found" }, 404);
  }

  // P9 live console. Lines the agent streams (after a signed `tail`) are kept in memory only: they exist while
  // someone watches (the viewer's polls keep this object awake); after hibernation the panel shows D1's logs.
  consoleSince(after, now) {
    const buf = this.console || [];
    const lines = buf.filter((l) => l.seq > after).slice(-CONSOLE_KEEP);
    return {
      lines,
      next: buf.length ? buf[buf.length - 1].seq : after,
      live: (this.liveUntil || 0) > now && this.ctx.getWebSockets("agent").length > 0,
      agent_connected: this.ctx.getWebSockets("agent").length > 0,
    };
  }

  addConsole(lines, now) {
    if (!this.console) this.console = [];
    for (const l of lines.slice(0, 500)) {
      if (!l || typeof l.msg !== "string") continue;
      this.seq = (this.seq || 0) + 1;
      this.console.push({
        seq: this.seq,
        ts: Number.isFinite(l.ts) ? l.ts : now,
        prio: Number.isInteger(l.prio) ? l.prio : 6,
        msg: l.msg.slice(0, 2000),
      });
    }
    if (this.console.length > CONSOLE_KEEP) this.console.splice(0, this.console.length - CONSOLE_KEEP);
  }

  // A viewer opened the console: ask the agent to stream for a while (one signed `tail` per renewal window).
  async live(data, now) {
    const login = String((data && data.login) || "").slice(0, 200);
    if (!login) return json({ error: "login required" }, 400);
    if (!this.ctx.getWebSockets("agent").length) return json({ ok: false, live: false, reason: "agent not connected" });
    if ((this.liveUntil || 0) - now > CONSOLE_RENEW_MS) return json({ ok: true, live: true });
    const r = await this.enqueue({ action: "tail", args: { seconds: CONSOLE_TAIL_S }, login }, now);
    if (r.status !== 200) return r;
    this.liveUntil = now + CONSOLE_TAIL_S * 1000;
    return json({ ok: true, live: true });
  }

  // v1.3: a command whose answer the caller waits for (the name check's `lookup`): only while the agent holds its
  // WebSocket (a polling agent answers within 15 s: too slow, the caller falls back), at most wait_ms (<= 5 s).
  async ask(data, now) {
    if (!this.ctx.getWebSockets("agent").length) return json({ ok: false, reason: "agent not connected" });
    const wait = Math.min(5000, Math.max(50, Number(data && data.wait_ms) || 4000));
    const r = await this.enqueue(data, now);
    if (r.status !== 200) return r;
    const { id } = await r.json();
    if (!this.waiters) this.waiters = new Map();
    let timer;
    const result = await new Promise((resolve) => {
      this.waiters.set(id, resolve);
      timer = setTimeout(() => resolve(null), wait);
    });
    clearTimeout(timer);
    this.waiters.delete(id);
    return json(result ? { ok: true, id, result } : { ok: false, id, reason: `no answer within ${wait} ms` });
  }

  accept() {
    for (const old of this.ctx.getWebSockets("agent")) {
      try {
        old.close(1000, "replaced by a newer connection");
      } catch {
        // already closed
      }
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server, ["agent"]); // hibernation API: idle sockets cost nothing
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    let msg;
    try {
      msg = JSON.parse(typeof message === "string" ? message : new TextDecoder().decode(message));
    } catch {
      return;
    }
    if (msg && msg.type === "hello") {
      for (const c of await this.pending(Date.now())) ws.send(JSON.stringify({ type: "command", ...c }));
    } else if (msg && msg.type === "result") {
      await this.storeResults([msg], Date.now());
    } else if (msg && msg.type === "console" && Array.isArray(msg.lines)) {
      this.addConsole(msg.lines, Date.now());
    }
  }

  async webSocketClose(ws, code) {
    try {
      ws.close(code === 1005 ? 1000 : code);
    } catch {
      // already closed
    }
  }

  // The panel (P9/P10) calls this through the Worker after its own checks; P8 has no public route to it.
  async enqueue(data, now) {
    if (!data || !COMMAND_ACTIONS.includes(data.action)) return json({ error: "unknown action" }, 400);
    const args = data.args && typeof data.args === "object" && !Array.isArray(data.args) ? data.args : {};
    const login = String(data.login || "").slice(0, 200);
    if (!login) return json({ error: "login required" }, 400);
    if (!this.env.COMMAND_HMAC_KEY) return json({ error: "no command key" }, 500);
    const id = randomId(12);
    const payload = JSON.stringify({ id, action: data.action, args, login, iat: now, nonce: randomId(16) });
    const command = { payload, sig: await hmacHex(this.env.COMMAND_HMAC_KEY, payload) };
    await this.ctx.storage.put(`cmd:${id}`, { ...command, created: now });
    for (const ws of this.ctx.getWebSockets("agent")) {
      try {
        ws.send(JSON.stringify({ type: "command", ...command }));
      } catch {
        // the poll fallback picks it up
      }
    }
    return json({ ok: true, id });
  }

  async pending(now) {
    const out = [];
    const stale = [];
    for (const [key, c] of await this.ctx.storage.list({ prefix: "cmd:" })) {
      if (now - c.created > COMMAND_TTL_MS) stale.push(key);
      else out.push({ payload: c.payload, sig: c.sig });
    }
    if (stale.length) await this.ctx.storage.delete(stale);
    return out;
  }

  async storeResults(results, now) {
    let stored = 0;
    for (const r of results.slice(0, 50)) {
      const id = r && typeof r.id === "string" ? r.id : "";
      if (!/^[0-9a-f]{24}$/.test(id) || !(await this.ctx.storage.get(`cmd:${id}`))) continue;
      if (await this.ctx.storage.get(`result:${id}`)) continue; // first result wins (a replay can't overwrite)
      const stored_ = { ok: r.ok === true, output: String(r.output || "").slice(0, 4000), error: String(r.error || "").slice(0, 500), at: now };
      await this.ctx.storage.put(`result:${id}`, stored_);
      await this.ctx.storage.delete(`cmd:${id}`);
      const waiter = this.waiters && this.waiters.get(id);
      if (waiter) waiter(stored_);
      stored += 1;
    }
    return stored;
  }
}

function controlStub(env) {
  return env.CONTROL.get(env.CONTROL.idFromName("control"));
}

// --- P10 review: the panel's GitHub App ------------------------------------------------------------------------
//
// mc-agent opens the whitelist pull requests with a short-lived installation token that this Worker mints: the
// App's private key (secret binding GITHUB_APP_KEY, PEM as GitHub issued it: PKCS#1 "RSA PRIVATE KEY" or
// PKCS#8) never reaches the server. JWT RS256 (iat now - 60 s, exp now + 9 min, iss = the App id: GitHub REST
// "Authenticating as a GitHub App") -> GET /repos/{repo}/installation -> POST /app/installations/{id}/access_tokens
// narrowed to that one repository and to contents + pull_requests write (the token can't do more than that even
// if the App were given more). Tokens (1 h) are cached per isolate until 10 minutes before they expire.

const GITHUB_API = "https://api.github.com";
let githubCache = { key: "", token: "", expires: 0, installation: null };

function pemDer(pem) {
  const body = String(pem).replace(/-----(BEGIN|END) [A-Z ]+-----/g, "").replace(/\s+/g, "");
  const bin = atob(body);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function derLength(n) {
  if (n < 0x80) return [n];
  const bytes = [];
  for (let v = n; v > 0; v >>= 8) bytes.unshift(v & 0xff);
  return [0x80 | bytes.length, ...bytes];
}

// PKCS#1 RSAPrivateKey -> PKCS#8 PrivateKeyInfo (WebCrypto imports pkcs8 only): SEQUENCE { INTEGER 0,
// SEQUENCE { OID 1.2.840.113549.1.1.1, NULL }, OCTET STRING { pkcs1 } }.
function pkcs1To8(pkcs1) {
  const algo = [0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00];
  const octet = [0x04, ...derLength(pkcs1.length)];
  const inner = 3 + algo.length + octet.length + pkcs1.length;
  const out = new Uint8Array([0x30, ...derLength(inner), 0x02, 0x01, 0x00, ...algo, ...octet, ...pkcs1]);
  return out;
}

function b64url(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function githubAppJwt(pem, appId, now) {
  const der = pemDer(pem);
  const pkcs8 = /BEGIN RSA PRIVATE KEY/.test(pem) ? pkcs1To8(der) : der;
  const key = await crypto.subtle.importKey("pkcs8", pkcs8, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const t = Math.floor(now / 1000);
  const enc = (o) => b64url(new TextEncoder().encode(JSON.stringify(o)));
  const input = `${enc({ alg: "RS256", typ: "JWT" })}.${enc({ iat: t - 60, exp: t + 540, iss: String(appId) })}`;
  const sig = new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(input)));
  return `${input}.${b64url(sig)}`;
}

function githubAppConfig(env) {
  let raw;
  try {
    raw = JSON.parse(env.GITHUB_APP || "null");
  } catch {
    raw = null;
  }
  const appId = raw && String(raw.app_id || "");
  const repository = raw && String(raw.repository || "");
  if (!/^[0-9]{1,12}$/.test(appId || "") || !/^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(repository || "") || !env.GITHUB_APP_KEY) return null;
  return { appId, repository };
}

async function githubCall(rt, method, path, auth, body) {
  const resp = await rt.fetch(`${GITHUB_API}${path}`, {
    method,
    headers: {
      Authorization: auth,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "smallworld watchdog",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) throw new Error(`${method} ${path.replace(/\/[0-9]+\//, "/{id}/")}: HTTP ${resp.status}`);
  return resp.json();
}

async function githubToken(env, now, rt) {
  const cfg = githubAppConfig(env);
  if (!cfg) return json({ error: "no GitHub App for the panel (panel_github_app_id / _private_key)" }, 404);
  const cacheKey = `${cfg.appId}/${cfg.repository}`;
  if (githubCache.key === cacheKey && githubCache.token && githubCache.expires - now > 600000) {
    return json({ token: githubCache.token, expires_at: new Date(githubCache.expires).toISOString(), permissions: githubCache.permissions || null });
  }
  try {
    const jwt = `Bearer ${await githubAppJwt(env.GITHUB_APP_KEY, cfg.appId, now)}`;
    let installation = githubCache.key === cacheKey ? githubCache.installation : null;
    if (!installation) installation = (await githubCall(rt, "GET", `/repos/${cfg.repository}/installation`, jwt)).id;
    if (!Number.isInteger(installation)) throw new Error("no installation of the App on the config repo");
    const name = cfg.repository.split("/")[1];
    const minted = await githubCall(rt, "POST", `/app/installations/${installation}/access_tokens`, jwt, {
      repositories: [name],
      permissions: { contents: "write", pull_requests: "write" },
    });
    const expires = Date.parse(minted.expires_at);
    if (typeof minted.token !== "string" || !Number.isFinite(expires)) throw new Error("unexpected token reply");
    // v1.3: the granted permissions go along (the agent's Panel -> Git self-test checks them; no secret)
    const permissions = minted.permissions && typeof minted.permissions === "object" ? Object.fromEntries(Object.entries(minted.permissions).filter(([k, v]) => /^[a-z_]{1,40}$/.test(k) && typeof v === "string").map(([k, v]) => [k, v.slice(0, 10)])) : null;
    githubCache = { key: cacheKey, token: minted.token, expires, installation, permissions };
    return json({ token: minted.token, expires_at: new Date(expires).toISOString(), permissions });
  } catch (err) {
    githubCache = { key: "", token: "", expires: 0, installation: null };
    // never the key or a token: the request line and status only
    return json({ error: `GitHub App: ${String((err && err.message) || err).slice(0, 160)}` }, 502);
  }
}

async function handleAgent(request, env, now, url, rt) {
  if (!env.MONITORING || !env.CONTROL || !env.LOGS || !env.AGENT_TOKEN) {
    return json({ error: "monitoring is off (panel.mode isn't cloudflare)" }, 404);
  }
  if (!(await tokenOk(request.headers.get("Authorization"), env.AGENT_TOKEN))) {
    return json({ error: "unauthorized" }, 401);
  }
  const mcfg = monitoringConfig(env);
  const route = `${request.method} ${url.pathname}`;
  if (route === "POST /ingest/metrics") {
    const { data, error } = await readJson(request, MAX_METRICS_BODY);
    return error || ingestMetrics(env, mcfg, now, data);
  }
  if (route === "POST /ingest/logs") {
    const { data, error } = await readJson(request, MAX_LOGS_BODY);
    return error || ingestLogs(env, mcfg, now, data);
  }
  if (route === "POST /ingest/archive") return ingestArchive(env, now, request, url);
  if (route === "POST /ingest/inventory") {
    const { data, error } = await readJson(request, MAX_METRICS_BODY);
    if (error) return error;
    const inv = cleanInventory(data && data.inventory);
    if (!inv) return json({ error: "inventory must be an object" }, 400);
    await loadState(env.DB); // creates the key/value table on a fresh database
    await upsert(env.DB, "inventory", { ...inv, received: now }, now);
    return json({ ok: true });
  }
  if (route === "GET /agent/ws") {
    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return json({ error: "expected a WebSocket upgrade" }, 426);
    }
    return controlStub(env).fetch(new Request("https://control/ws", request));
  }
  if (route === "GET /agent/commands") return controlStub(env).fetch(new Request("https://control/commands"));
  if (route === "POST /agent/github-token") return githubToken(env, now, rt);
  if (route === "POST /agent/client-code") {
    const ccfg = clientConfig(env); // v1.1: /modpack's one-time code
    if (!ccfg) return json({ error: "the client modpack is off" }, 404);
    const { data, error } = await readJson(request, 1024);
    return error || mintCode(env, ccfg, now, data);
  }
  if (route === "POST /ingest/job") {
    const body = await readLimited(request, 16384);
    if (!body) return json({ error: "body too large" }, 413);
    return controlStub(env).fetch(new Request("https://control/job", { method: "POST", body }));
  }
  if (route === "POST /ingest/experience") {
    if (!env.TELEMETRY) return json({ error: "experience telemetry is off" }, 404);
    const { data, error } = await readJson(request, MAX_EXP_BODY);
    return error || ingestExperience(env, now, data, rt);
  }
  if (route === "PUT /ingest/segment") return ingestSegment(env, request, url);
  if (route === "POST /agent/results") {
    const body = await readLimited(request, MAX_METRICS_BODY);
    if (!body) return json({ error: "body too large" }, 413);
    return controlStub(env).fetch(new Request("https://control/results", { method: "POST", body }));
  }
  return json({ error: "not found" }, 404);
}

// --- player experience (P13): ingest, verdicts, messages, rollups, retention, remedies ----------------------
//
// mc-agent sends one *minute row* per minute (POST /ingest/experience, the mc-telemetry plugin's 10 s snapshots
// pre-aggregated on the box: tick-time distribution, per-player ping/view/brand/nearby entities, join/leave
// events, lag spikes with context, product overhead, backlog) and every 5 minutes the raw *segment* of those rows
// (PUT /ingest/segment, gzip NDJSON -> R2 `telemetry/YYYY/MM/DD/<segment>`, kept forever: DuckDB reads
// read_ndjson('telemetry/*/*/*/*.ndjson.gz')). Storage is the TELEMETRY D1 database (its own 500 MB on Free):
//   exp_raw_YYYYMMDD  one row per minute, the whole row JSON (raw 10 s samples): dropped whole after raw_days
//   exp_1m_YYYYMM     one summary row per minute (numbers for charts): dropped whole after minute_days
//   exp_1h            hourly server rollup (kept hour_days), exp_player_1h hourly per player
//   exp_players       one row per player (name, verdict, stats, last message), exp_sessions (join/leave)
//   exp_events        verdict changes, lag spikes, messages sent, annotations (deploys, restarts, panel actions)
//   exp_dirty         hours a backfill touched (re-rolled up by the cron), exp_state key/value
// Every write is an upsert on the row's event time (the minute) or natural key, so a replayed or backfilled row
// never duplicates; day/month tables are dropped whole (DROP writes no rows; D1 counts DELETEd rows as written).
// The verdict engine (`judge`) is the single source of truth for the rules: the plugin only shows what it says.

const EXP_RAW_TABLE = /^exp_raw_(\d{8})$/;
const EXP_MIN_TABLE = /^exp_1m_(\d{6})$/;
const EXP_DEFAULTS = {
  raw_days: 14,
  minute_days: 180,
  hour_days: 730,
  minecraft_version: "",
  messages: { enabled: true, per_player_minutes: 30, broadcast_minutes: 30, join_summary: true },
  chunk_entities: 300, // monitoring.alerts.chunk_entities: a loaded chunk with this many entities alerts (0 = off)
};
const CHUNK_ALERT_REPEAT_MS = 6 * 3600000; // the same chunk alerts again after this long (if still heavy)
const CRAMMING = 24; // vanilla's maxEntityCramming gamerule: the 25th mob in one block space takes damage
const MAX_EXP_BODY = 2097152;
const MAX_EXP_ROWS = 60;
const MAX_SEGMENT_BODY = 8388608;
const EXP_PAST_MS = 31 * 86400000; // the box keeps 30 days of segments
const EXP_LIVE_MS = 180000; // a row this fresh may trigger in-game messages
const MAX_LAG_EVENTS_PER_DAY = 500;
const MAX_VERDICT_EVENTS_PER_DAY = 500;
const MAINT_MAX_TRIES = 3; // interrupted cron runs on one maintenance unit before it is skipped (and reported)
const REMEDY_CHECK_MS = 86400000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SEGMENT_RE = /^(\d{4})(\d{2})(\d{2})T\d{4}-[0-9a-f]{8}-\d+-\d+\.ndjson\.gz$/;

// Thresholds: one place. Ticks are 50 ms at 20 TPS; a tick over 50 ms is a late tick, players notice a 100 ms+
// spike as a stutter. Ping: < 150 ms fine for Minecraft, 250 ms+ makes combat and building feel delayed.
const RULES = {
  ping_fair: 150,
  ping_poor: 250,
  ping_spike: 300,
  tick_p99_fair: 50,
  tick_p99_poor: 100,
  tick_spike_ms: 500,
  tps_fair: 19,
  tps_poor: 17,
  entities_near: 400,
  world_entities: 3000,
  client_view_low: 4,
  client_view_over_send: 6,
  heap_after_gc_pct: 90,
  high_ping_share: 0.5,
  overhead_cpu_pct: 1,
  overhead_mem_mb: 100,
  hysteresis: 0.1,
  // v1.2: the Smallworld client mod's FPS reports (minute average): below 30 stutters, below 20 is choppy
  fps_fair: 30,
  fps_poor: 20,
};
const VERDICT_RANK = { good: 0, fair: 1, poor: 2 };

// Message catalogue (in-game, private unless noted): the only place these texts live.
const MESSAGES = {
  lag_entities: (c) => ({ text: `That stutter was the server catching up: lots of mobs near you (${c.ent} entities). We're on it.`, hover: `Ticks up to ${c.max} ms. Fewer mobs in one place (farms, pens) helps everyone.` }),
  lag_server: (c) => ({ text: "That stutter was the server catching up, not your connection. We're on it.", hover: `Slowest tick ${c.max} ms (50 ms is the budget).` }),
  ping: (c) => ({ text: `Your connection spiked to ${c.ping} ms. Wi-Fi or another download? A wired connection helps.`, hover: "The server itself is running fine." }),
  view_high: (c) => ({ text: `Your render distance is ${c.view} but the server sends ${c.send}: lowering it saves FPS. Sodium helps a lot.`, hover: "Video Settings > Render Distance" }),
  chunks_flying: () => ({ text: "Flying fast into new land makes the server generate chunks: it can stutter for a moment.", hover: "Areas inside the world border are pre-generated and load smoothly." }),
  broadcast: (c) => ({ text: `The server is lagging for everyone right now (ticks up to ${c.max} ms). We're on it: nothing wrong on your side.`, hover: "Type /lag for details." }),
  // v1.0.1: the elimination answer (/lag tips): the server is smooth and the connection fine; = mc-brand Msg.FPS_TIP
  // and mc-telemetry FPS_TIP (tests/test_brand.py)
  fps: () => ({ text: "The server and your connection look fine, so stutter is likely your PC's frame rate. Press F3 to see your FPS (top-left). Lowering render distance or installing Sodium usually helps.", hover: "F3 shows your FPS at the top left (Fn+F3 on some laptops)." }),
  // v1.2: the diagnosis "server fine / your PC" with the client mod's measured FPS
  client_fps: (c) => ({ text: `The server and your connection are fine: your PC is drawing ${c.fps} FPS. Lower render distance or shaders (or close other programs) to smooth it out.`, hover: "Measured by the server's client mod (the FPS in the top HUD bar)." }),
  join_summary: (c) => ({ text: `Welcome back! Last time you had some lag (${c.why}). Type /lag anytime to see why.`, hover: "Type /lag tips off to stop these messages." }),
};

// Remedies: cause -> suggestions (config = a server.yml field, plugin/client_mod = a Modrinth project checked for
// the pinned Minecraft version before it is recommended, host = a note). Recommendations only: nothing installs.
const REMEDIES = {
  server_lag: [
    { kind: "action", text: "Profile it: run `spark profiler start --timeout 120 --save-to-file` in the console (spark is bundled with Paper), then compare plugins and entities." },
    { kind: "config", field: "performance.simulation_distance", text: "Lower the simulation distance (fewer ticking chunks around each player)." },
    { kind: "config", field: "performance.view_distance", text: "Lower the view distance (less chunk sending and loading)." },
  ],
  // No mechanic changes (the vanilla profile, server.yml performance.profile): only how players build.
  entity_hotspot: [
    { kind: "action", text: "Find the crowded spot in Busiest chunks below: big pens and farm collection areas are the usual cause." },
    { kind: "action", text: "Breed and keep fewer animals per pen; farms that kill their output promptly keep the entity count low." },
  ],
  heavy_chunk: [
    { kind: "action", text: "Spread the pen or farm over more chunks or add floor space: every entity in one chunk is ticked and collided together." },
    { kind: "action", text: `Mobs packed into one block space push each other; vanilla cramming kills them above ${CRAMMING} per block.` },
    { kind: "action", text: "Collect dropped items quickly (hoppers or a kill chamber next to the collection) so they don't pile up." },
  ],
  chunk_gen: [
    { kind: "config", field: "world.border_radius", text: "Keep players inside the pre-generated border (Chunky pre-generates every bordered dimension automatically)." },
  ],
  memory: [
    { kind: "config", field: "jvm.heap_mb", text: "The heap is nearly full after garbage collection: give the JVM more memory or move to a bigger host." },
  ],
  high_ping_cluster: [
    { kind: "host", text: "Most players have a high ping: the host may be far from them (a region closer to the players helps)." },
  ],
  client_fps: [
    { kind: "client_mod", project: "sodium", loader: "fabric", text: "Sodium (client mod): much better FPS at the same render distance." },
    { kind: "client_mod", project: "lithium", loader: "fabric", text: "Lithium (client mod): general game-logic optimisations." },
  ],
  overhead: [
    { kind: "host", text: "One of the product's own services uses more than 1% CPU or 100 MB: evidence for porting it (design §8 P13)." },
  ],
};

function experienceConfig(env) {
  let raw = {};
  try {
    raw = JSON.parse(env.EXPERIENCE || "{}");
  } catch {
    raw = {};
  }
  return { ...EXP_DEFAULTS, ...raw, messages: { ...EXP_DEFAULTS.messages, ...(raw.messages || {}) } };
}

const monthOf = (ms) => new Date(ms).toISOString().slice(0, 7).replace("-", "");
const hourOf = (ms) => Math.floor(ms / 3600000) * 3600000;
const n = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const round = (v, d = 1) => (n(v) === null ? null : Math.round(v * 10 ** d) / 10 ** d);
const worse = (a, b) => (VERDICT_RANK[b] > VERDICT_RANK[a] ? b : a);

const EXP_SCHEMA = [
  "CREATE TABLE IF NOT EXISTS exp_state (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
  "CREATE TABLE IF NOT EXISTS exp_1h (ts INTEGER PRIMARY KEY, minutes INTEGER, players_avg REAL, players_max INTEGER, tps_min REAL, mean REAL, p50 REAL, p99 REAL, p999 REAL, max REAL, spikes INTEGER, entities_max INTEGER, ping_avg REAL, poor_minutes INTEGER, fair_minutes INTEGER, self_p99_us REAL, ovh TEXT)",
  "CREATE TABLE IF NOT EXISTS exp_player_1h (uuid TEXT NOT NULL, ts INTEGER NOT NULL, name TEXT, minutes INTEGER, ping_avg REAL, ping_max INTEGER, poor INTEGER, fair INTEGER, PRIMARY KEY (uuid, ts)) WITHOUT ROWID",
  "CREATE TABLE IF NOT EXISTS exp_players (uuid TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen INTEGER, last_seen INTEGER, stats TEXT, verdict TEXT, headline TEXT, reasons TEXT, since INTEGER, last_msg INTEGER, last_poor TEXT, brand TEXT) WITHOUT ROWID",
  "CREATE TABLE IF NOT EXISTS exp_sessions (uuid TEXT NOT NULL, join_ts INTEGER NOT NULL, leave_ts INTEGER, name TEXT, worst TEXT, PRIMARY KEY (uuid, join_ts)) WITHOUT ROWID",
  "CREATE TABLE IF NOT EXISTS exp_events (ts INTEGER NOT NULL, kind TEXT NOT NULL, uuid TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL, PRIMARY KEY (ts, kind, uuid)) WITHOUT ROWID",
  "CREATE TABLE IF NOT EXISTS exp_dirty (ts INTEGER PRIMARY KEY)",
];
const EXP_SCHEMA_VERSION = 1; // bump + append to EXP_MIGRATIONS for any change to the tables above
const EXP_MIGRATIONS = []; // [version, [sql...]]: applied in order by expReady, recorded in exp_state.schema
const expReady = new WeakSet();
const expTables = new WeakMap();

async function expSchema(db) {
  if (expReady.has(db)) return;
  await db.batch(EXP_SCHEMA.map((sql) => db.prepare(sql)));
  const row = await db.prepare("SELECT value FROM exp_state WHERE key = 'schema'").first();
  let have = row ? Number(row.value) : 0;
  for (const [version, stmts] of EXP_MIGRATIONS) {
    if (version > have) {
      await db.batch(stmts.map((sql) => db.prepare(sql)));
      have = version;
    }
  }
  if (have < EXP_SCHEMA_VERSION) have = EXP_SCHEMA_VERSION;
  if (!row || Number(row.value) !== have) {
    await db.prepare("INSERT OR REPLACE INTO exp_state (key, value) VALUES ('schema', ?1)").bind(String(have)).run();
  }
  expReady.add(db);
}

async function expTable(db, name) {
  let known = expTables.get(db);
  if (!known) {
    known = new Set();
    expTables.set(db, known);
  }
  if (known.has(name)) return;
  let sql;
  if (EXP_RAW_TABLE.test(name)) sql = `CREATE TABLE IF NOT EXISTS ${name} (ts INTEGER PRIMARY KEY, id TEXT NOT NULL, data TEXT NOT NULL)`;
  else if (EXP_MIN_TABLE.test(name)) {
    sql = `CREATE TABLE IF NOT EXISTS ${name} (ts INTEGER PRIMARY KEY, players INTEGER, tps REAL, mean REAL, p50 REAL, p95 REAL, p99 REAL, p999 REAL, max REAL, spikes INTEGER, chunks INTEGER, entities INTEGER, ping_p50 REAL, ping_max REAL, poor INTEGER, fair INTEGER, self_p99_us REAL, heap_pct REAL, verdict TEXT, ovh TEXT, backlog TEXT)`;
  } else throw new Error("unknown table");
  await db.prepare(sql).run();
  known.add(name);
}

async function expState(db, key) {
  const row = await db.prepare("SELECT value FROM exp_state WHERE key = ?1").bind(key).first();
  if (!row) return null;
  try {
    return JSON.parse(row.value);
  } catch {
    return null;
  }
}

function expStateStmt(db, key, value) {
  return db.prepare("INSERT OR REPLACE INTO exp_state (key, value) VALUES (?1, ?2)").bind(key, JSON.stringify(value));
}

// --- clean input (the agent's row: numbers, short strings, known keys only) ---

function cleanPlayer(p) {
  if (!p || typeof p !== "object" || !UUID_RE.test(String(p.u)) || !PLAYER_NAME.test(String(p.n))) return null;
  return {
    u: p.u, n: p.n, ping: n(p.ping), ping_max: n(p.ping_max), view: n(p.view), send: n(p.send),
    brand: str(p.brand, 32), loc: str(p.loc, 16), w: str(p.w, 64), cx: n(p.cx), cz: n(p.cz), ent: n(p.ent),
    glide: p.glide === true, tips: p.tips !== false, join: n(p.join), samples: n(p.samples), fps: n(p.fps), fps_min: n(p.fps_min),
    pings: Array.isArray(p.pings) ? p.pings.filter((x) => n(x) !== null).slice(0, 12) : [],
  };
}

function cleanRow(r, now) {
  if (!r || typeof r !== "object" || !Number.isInteger(r.ts) || r.ts % 60000 !== 0) return null;
  if (r.ts < now - EXP_PAST_MS || r.ts > now + SAMPLE_FUTURE_MS) return null;
  if (typeof r.id !== "string" || !/^[0-9a-f]{8}:\d{1,12}$/.test(r.id)) return null;
  const s = r.srv && typeof r.srv === "object" ? r.srv : {};
  const srv = {};
  for (const k of ["ticks", "tps", "mean", "p50", "p95", "p99", "p999", "max", "spikes", "chunks", "entities", "tiles", "heap_used_mb", "heap_max_mb", "self_p99_us", "self_max_us", "budget_skips", "lost_windows", "samples", "gc_heap_after_mb", "gc_max_pause_ms"]) srv[k] = n(s[k]);
  srv.hot = (Array.isArray(s.hot) ? s.hot : []).slice(0, 10).filter((c) => c && typeof c.w === "string" && Number.isInteger(c.cx) && Number.isInteger(c.cz)).map((c) => ({ w: str(c.w, 64), cx: c.cx, cz: c.cz, ent: n(c.ent) || 0, tiles: n(c.tiles) || 0, vil: n(c.vil) || 0, items: n(c.items) || 0 }));
  srv.worlds = (Array.isArray(s.worlds) ? s.worlds : []).slice(0, 16).filter((w) => w && typeof w.n === "string").map((w) => ({ n: str(w.n, 64), chunks: n(w.chunks), entities: n(w.entities), ticking: n(w.ticking), players: n(w.players) }));
  const pl = (Array.isArray(r.pl) ? r.pl : []).slice(0, 500).map(cleanPlayer).filter(Boolean);
  const ev = (Array.isArray(r.ev) ? r.ev : []).slice(0, 200).filter((e) => e && (e.k === "join" || e.k === "leave") && UUID_RE.test(String(e.u)) && PLAYER_NAME.test(String(e.n)) && Number.isInteger(e.t) && Number.isInteger(e.join));
  const lag = (Array.isArray(r.lag) ? r.lag : []).slice(0, 20).filter((e) => e && Number.isInteger(e.t) && n(e.ms) !== null).map((e) => {
    const text = e.ctx && typeof e.ctx === "object" ? JSON.stringify(e.ctx) : "{}";
    return { t: e.t, ms: e.ms, ctx: text.length <= 4000 ? JSON.parse(text) : {} };
  });
  const ovh = {};
  for (const [unit, v] of Object.entries(r.ovh && typeof r.ovh === "object" ? r.ovh : {}).slice(0, 20)) {
    if (!/^(minecraft|mc-[a-z0-9@._-]+)\.service$/.test(unit) || !v || typeof v !== "object") continue;
    ovh[unit] = {};
    for (const k of ["cpu_pct", "mem_mb", "mem_peak_mb", "io_read_kb", "io_write_kb", "cpu_ms", "runs"]) if (n(v[k]) !== null) ovh[unit][k] = v[k];
  }
  const b = r.backlog && typeof r.backlog === "object" ? r.backlog : {};
  const backlog = { segments: n(b.segments), rows: n(b.rows), bytes: n(b.bytes), oldest: str(b.oldest, 13) || null, stored_segments: n(b.stored_segments), stored_bytes: n(b.stored_bytes) };
  const stats = {};
  for (const [u, st] of Object.entries(r.stats && typeof r.stats === "object" ? r.stats : {}).slice(0, 100)) {
    if (!UUID_RE.test(u) || !st || typeof st !== "object") continue;
    stats[u] = {};
    for (const [k, v] of Object.entries(st).slice(0, 16)) if (/^[a-z_]{1,16}$/.test(k) && Number.isInteger(v)) stats[u][k] = v;
  }
  const running = (Array.isArray(r.running) ? r.running : []).filter((x) => ["backup", "restore", "prune"].includes(x));
  const raw = (Array.isArray(r.raw) ? r.raw : []).slice(0, 12);
  return { id: r.id, ts: r.ts, srv, pl, ev, lag, ovh, backlog, stats, running, raw, backfill: r.backfill === true };
}

// --- the verdict engine (pure: tested on its own in watchdog/test/experience.test.mjs) ---

// Block coordinates of a chunk (what players see on F3 / a map), for reasons and alerts.
function chunkWhere(c) {
  return `${c.w} chunk ${c.cx},${c.cz} (blocks ${c.cx * 16}..${c.cx * 16 + 15}, ${c.cz * 16}..${c.cz * 16 + 15})`;
}

function heavyChunks(srv, limit) {
  return limit > 0 ? (srv.hot || []).filter((c) => c.ent >= limit) : [];
}

function judgeServer(srv, pl, chunkLimit = EXP_DEFAULTS.chunk_entities) {
  const reasons = [];
  const causes = [];
  let verdict = "good";
  const p99 = n(srv.p99);
  const tps = n(srv.tps);
  if (p99 !== null && p99 >= RULES.tick_p99_poor) {
    verdict = "poor";
    reasons.push(`Lag spikes: the slowest 1% of ticks took ${Math.round(p99)} ms (budget 50 ms)`);
    causes.push("server_lag");
  } else if (p99 !== null && p99 >= RULES.tick_p99_fair) {
    verdict = "fair";
    reasons.push(`Some late ticks: the slowest 1% took ${Math.round(p99)} ms (budget 50 ms)`);
    causes.push("server_lag");
  }
  if (tps !== null && tps < RULES.tps_poor) {
    verdict = "poor";
    reasons.push(`The server runs slow: ${tps.toFixed(1)} TPS (20 is full speed)`);
    if (!causes.includes("server_lag")) causes.push("server_lag");
  } else if (tps !== null && tps < RULES.tps_fair) {
    verdict = worse(verdict, "fair");
    reasons.push(`Slightly behind: ${tps.toFixed(1)} TPS`);
  }
  const hot = (srv.worlds || []).filter((w) => n(w.entities) !== null && w.entities >= RULES.world_entities);
  for (const w of hot) {
    reasons.push(`${w.entities} entities loaded in ${w.n}`);
    causes.push("entity_hotspot");
  }
  for (const c of heavyChunks(srv, chunkLimit).slice(0, 3)) {
    reasons.push(`${c.ent} entities in one chunk: ${chunkWhere(c)}`);
    if (!causes.includes("heavy_chunk")) causes.push("heavy_chunk");
  }
  const heapPct = n(srv.gc_heap_after_mb) !== null && n(srv.heap_max_mb) ? (100 * srv.gc_heap_after_mb) / srv.heap_max_mb : null;
  if (heapPct !== null && heapPct >= RULES.heap_after_gc_pct) {
    verdict = worse(verdict, "fair");
    reasons.push(`Memory pressure: ${Math.round(heapPct)}% of the heap still used after garbage collection`);
    causes.push("memory");
  }
  const pinged = pl.filter((p) => n(p.ping) !== null);
  if (pinged.length >= 2 && pinged.filter((p) => p.ping >= RULES.ping_fair).length / pinged.length >= RULES.high_ping_share) {
    reasons.push("Most players have a high ping (the host may be far from them)");
    causes.push("high_ping_cluster");
  }
  const headline = verdict === "good" ? "Running smoothly" : verdict === "fair" ? "Minor lag" : "Lagging";
  return { verdict, headline, reasons, tips: [], causes };
}

function judgePlayer(p, srv, serverVerdict, prev = null) {
  const reasons = [];
  const tips = [];
  const causes = [];
  let verdict = "good";
  // Hysteresis: a player stays at a ping verdict until the ping is clearly (RULES.hysteresis) below its threshold,
  // so a ping hovering at a threshold doesn't flip the verdict (and its history) every minute.
  const h = 1 - RULES.hysteresis;
  const pingPoor = prev === "poor" ? RULES.ping_poor * h : RULES.ping_poor;
  const pingFair = prev === "poor" || prev === "fair" ? RULES.ping_fair * h : RULES.ping_fair;
  const ping = n(p.ping);
  const pingMax = n(p.ping_max);
  if (ping !== null && ping >= pingPoor) {
    verdict = "poor";
    reasons.push(`High ping (${ping} ms): your connection, not the server`);
    causes.push("ping");
  } else if (ping !== null && ping >= pingFair) {
    verdict = "fair";
    reasons.push(`Ping is a bit high (${ping} ms): your connection, not the server`);
    causes.push("ping");
  } else if (pingMax !== null && pingMax >= RULES.ping_spike) {
    verdict = "fair";
    reasons.push(`Your connection spiked to ${pingMax} ms`);
    causes.push("ping_spike");
    tips.push("Wi-Fi or another download? A wired connection helps.");
  }
  if (serverVerdict.verdict !== "good") {
    verdict = worse(verdict, serverVerdict.verdict);
    if (n(p.ent) !== null && p.ent >= RULES.entities_near) {
      reasons.push(`Server lag spikes (ticks up to ${Math.round(n(srv.max) || n(srv.p99) || 0)} ms) while you were near ${p.ent} entities at ${p.w} ${p.cx * 16},${p.cz * 16}`);
      causes.push("entities_near");
    } else if (p.glide && n(srv.p99) !== null && srv.p99 >= RULES.tick_p99_fair) {
      reasons.push("Chunk loading is slow while flying fast into new land");
      causes.push("chunk_gen");
    } else {
      reasons.push(`Server lag spikes (ticks up to ${Math.round(n(srv.max) || n(srv.p99) || 0)} ms), not your connection`);
      causes.push("server_lag");
    }
  }
  if (n(p.view) !== null && p.view > 0 && p.view <= RULES.client_view_low) {
    verdict = worse(verdict, "fair");
    reasons.push(`Low render distance on your side (${p.view})`);
  } else if (n(p.view) !== null && n(p.send) !== null && p.send > 0 && p.view - p.send >= RULES.client_view_over_send) {
    tips.push(`Your render distance is ${p.view} but the server sends ${p.send}: lowering it saves FPS. Sodium helps a lot.`);
    causes.push("view_high");
  }
  // v1.2: a measured frame rate (the client mod's reports) answers "server or my PC?" instead of the F3 hint
  const fps = n(p.fps);
  const serverFine = serverVerdict.verdict === "good" && !causes.some((c) => c === "ping" || c === "ping_spike");
  if (fps !== null && fps < RULES.fps_fair) {
    verdict = worse(verdict, fps < RULES.fps_poor ? "poor" : "fair");
    const low = n(p.fps_min) !== null && p.fps_min < fps ? ` (lowest ${p.fps_min})` : "";
    reasons.push(serverFine ? `Server fine, your PC: ${fps} FPS${low}` : `Your PC is also slow: ${fps} FPS${low}`);
    causes.push("client_fps");
    if (serverFine) tips.push("Lower render distance or shaders, or close other programs: the server isn't the bottleneck.");
  } else if (verdict === "good" && !causes.length && fps === null) tips.push(MESSAGES.fps().text); // nothing on our side: the client's FPS
  const headline = verdict === "good" ? "Smooth" : causes.includes("ping") || causes.includes("ping_spike") ? "Your connection" : serverFine && causes.includes("client_fps") ? "Your PC" : "Server lag";
  return { verdict, headline, reasons, tips, causes };
}

function judge(row, prev = {}, chunkLimit = EXP_DEFAULTS.chunk_entities) {
  const server = judgeServer(row.srv, row.pl, chunkLimit);
  const players = {};
  for (const p of row.pl) players[p.u] = judgePlayer(p, row.srv, server, prev[p.u] || null);
  return { server, players };
}

// Which in-game messages this minute calls for (pure; rate limits are applied by the caller and again by the agent).
function messagesFor(row, verdicts, prev, cfg, now) {
  const out = [];
  if (!cfg.messages.enabled) return out;
  const realSpike = n(row.srv.max) !== null && row.srv.max >= RULES.tick_spike_ms;
  const poorPlayers = row.pl.filter((p) => verdicts.players[p.u].verdict === "poor");
  if (verdicts.server.verdict === "poor" && row.pl.length >= 2 && poorPlayers.filter((p) => verdicts.players[p.u].causes.some((c) => c !== "ping")).length >= 2) {
    out.push({ to: "*", kind: "server", ...MESSAGES.broadcast({ max: Math.round(row.srv.max || row.srv.p99 || 0) }) });
    return out; // one broadcast, no individual "server lag" messages on top
  }
  for (const p of row.pl) {
    const v = verdicts.players[p.u];
    if (!p.tips || (v.verdict !== "poor" && !(realSpike && v.verdict !== "good")) || (p.join && now - p.join < 60000)) continue;
    let m;
    if (v.causes.includes("ping")) m = MESSAGES.ping({ ping: p.ping_max || p.ping });
    else if (v.causes.includes("entities_near")) m = MESSAGES.lag_entities({ ent: p.ent, max: Math.round(row.srv.max || 0) });
    else if (v.causes.includes("chunk_gen")) m = MESSAGES.chunks_flying({});
    else if (v.causes.includes("client_fps") && verdicts.server.verdict === "good") m = MESSAGES.client_fps({ fps: p.fps });
    else m = MESSAGES.lag_server({ max: Math.round(row.srv.max || 0) });
    out.push({ to: p.u, kind: "lag", ...m });
  }
  for (const e of row.ev) {
    if (e.k !== "join" || !cfg.messages.join_summary) continue;
    const last = prev.lastPoor[e.u];
    const p = row.pl.find((x) => x.u === e.u);
    if (last && p && p.tips) out.push({ to: e.u, kind: "join", ...MESSAGES.join_summary({ why: last }) });
  }
  return out;
}

// --- ingest ---

async function enqueueMessage(env, now, m) {
  if (!env.CONTROL || !env.COMMAND_HMAC_KEY) return null;
  const id = `${m.kind === "join" ? "join" : "lag"}-${randomId(8)}`;
  const args = { to: m.to, kind: m.kind, text: m.text, hover: m.hover, id };
  const resp = await controlStub(env).fetch(
    new Request("https://control/enqueue", { method: "POST", body: JSON.stringify({ action: "player_message", args, login: "experience@worker" }) }),
  );
  return resp.status === 200 ? id : null;
}

// A Discord alert when a loaded chunk holds at least chunk_entities entities (live rows only): once per chunk, then
// again only after CHUNK_ALERT_REPEAT_MS if it is still heavy. Returns the statements that record what was sent.
async function chunkAlerts(env, now, rt, cfg, row) {
  const db = env.TELEMETRY;
  const heavy = heavyChunks(row.srv, cfg.chunk_entities);
  const st = (await expState(db, "chunk_alerts")) || {};
  for (const k of Object.keys(st)) if (now - st[k] > CHUNK_ALERT_REPEAT_MS) delete st[k];
  const fresh = heavy.filter((c) => !st[`${c.w}:${c.cx}:${c.cz}`]).slice(0, 3);
  if (!fresh.length || !rt) return [];
  const lines = fresh.map((c) => WORKER_ALERTS.crowdedChunk({ c, online: row.pl.length, limit: cfg.chunk_entities }));
  const outcome = await notify(rt, env, config(env), lines);
  if (outcome === "failed") return [];
  for (const c of fresh) st[`${c.w}:${c.cx}:${c.cz}`] = now;
  return [expStateStmt(db, "chunk_alerts", st)];
}

async function ingestExperience(env, now, data, rt = null) {
  const db = env.TELEMETRY;
  const rowsIn = data && Array.isArray(data.rows) ? data.rows : null;
  if (!rowsIn || !rowsIn.length || rowsIn.length > MAX_EXP_ROWS) return json({ error: `rows: 1-${MAX_EXP_ROWS}` }, 400);
  const cfg = experienceConfig(env);
  await expSchema(db);
  const rows = rowsIn.map((r) => cleanRow(r, now)).filter(Boolean).sort((a, b) => a.ts - b.ts);
  const live = data.live === true;
  const players = new Map();
  const uuids = [...new Set(rows.flatMap((r) => [...r.pl.map((p) => p.u), ...r.ev.map((e) => e.u)]))];
  for (let i = 0; i < uuids.length; i += 90) {
    const chunk = uuids.slice(i, i + 90);
    const { results } = await db.prepare(`SELECT uuid, verdict, since, last_msg, last_poor FROM exp_players WHERE uuid IN (${chunk.map((_, k) => `?${k + 1}`).join(",")})`).bind(...chunk).all();
    for (const r of results || []) players.set(r.uuid, r);
  }
  const counters = (await expState(db, "counters")) || {};
  const today = dayOf(now);
  if (counters.day !== today) Object.assign(counters, { day: today, lag: 0, verdicts: 0 });
  const countersBefore = JSON.stringify(counters);
  const stmts = [];
  let last = null;
  const sent = [];
  const currentHour = hourOf(now);
  const dirty = new Set();
  for (const row of rows) {
    const v = judge(row, Object.fromEntries([...players].map(([u, x]) => [u, x.verdict])), cfg.chunk_entities);
    last = { row, v };
    const raw = `exp_raw_${dayOf(row.ts)}`;
    const min = `exp_1m_${monthOf(row.ts)}`;
    await expTable(db, raw);
    await expTable(db, min);
    const pings = row.pl.map((p) => p.ping).filter((x) => x !== null).sort((a, b) => a - b);
    const verdictCount = (k) => Object.values(v.players).filter((x) => x.verdict === k).length;
    stmts.push(db.prepare(`INSERT OR REPLACE INTO ${raw} (ts, id, data) VALUES (?1, ?2, ?3)`).bind(row.ts, row.id, JSON.stringify({ ...row, verdicts: v })));
    stmts.push(
      db.prepare(`INSERT OR REPLACE INTO ${min} (ts, players, tps, mean, p50, p95, p99, p999, max, spikes, chunks, entities, ping_p50, ping_max, poor, fair, self_p99_us, heap_pct, verdict, ovh, backlog) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21)`).bind(
        row.ts, row.pl.length, row.srv.tps, row.srv.mean, row.srv.p50, row.srv.p95, row.srv.p99, row.srv.p999, row.srv.max, row.srv.spikes, row.srv.chunks, row.srv.entities,
        pings.length ? pings[Math.floor(pings.length / 2)] : null, pings.length ? pings[pings.length - 1] : null, verdictCount("poor"), verdictCount("fair"),
        row.srv.self_p99_us, n(row.srv.gc_heap_after_mb) !== null && row.srv.heap_max_mb ? round((100 * row.srv.gc_heap_after_mb) / row.srv.heap_max_mb) : null,
        v.server.verdict, JSON.stringify(row.ovh), JSON.stringify(row.backlog),
      ),
    );
    if (hourOf(row.ts) < currentHour) dirty.add(hourOf(row.ts));
    // sessions + the player dimension (written on join/leave/verdict change only: not every minute)
    for (const e of row.ev) {
      if (e.k === "join") {
        // sessions and playtime are counted from exp_sessions (keyed by the plugin's join time), so a replayed
        // row or an agent restart (everyone "joins" again) never counts twice
        // P13 review L6: an older session of this player still open (its leave never arrived: the agent was
        // down) ends at the player's last-seen time, never after this join: no session spans a down period
        stmts.push(db.prepare("UPDATE exp_sessions SET leave_ts = MIN(?2, MAX(join_ts, COALESCE((SELECT last_seen FROM exp_players WHERE uuid = ?1), join_ts))) WHERE uuid = ?1 AND join_ts < ?2 AND leave_ts IS NULL").bind(e.u, e.join));
        stmts.push(db.prepare("INSERT INTO exp_sessions (uuid, join_ts, name) VALUES (?1, ?2, ?3) ON CONFLICT(uuid, join_ts) DO NOTHING").bind(e.u, e.join, e.n));
        stmts.push(db.prepare("INSERT INTO exp_players (uuid, name, first_seen, last_seen) VALUES (?1, ?2, ?3, ?3) ON CONFLICT(uuid) DO UPDATE SET name = excluded.name, first_seen = MIN(COALESCE(first_seen, excluded.first_seen), excluded.first_seen), last_seen = MAX(COALESCE(last_seen, 0), excluded.last_seen)").bind(e.u, e.n, e.join));
      } else {
        stmts.push(db.prepare("INSERT INTO exp_sessions (uuid, join_ts, name, leave_ts) VALUES (?1, ?2, ?3, ?4) ON CONFLICT(uuid, join_ts) DO UPDATE SET leave_ts = MIN(COALESCE(leave_ts, excluded.leave_ts), excluded.leave_ts)").bind(e.u, e.join, e.n, e.t));
        stmts.push(db.prepare("INSERT INTO exp_players (uuid, name, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4) ON CONFLICT(uuid) DO UPDATE SET name = excluded.name, last_seen = MAX(COALESCE(last_seen, 0), excluded.last_seen)").bind(e.u, e.n, e.join, e.t));
      }
    }
    for (const p of row.pl) {
      const pv = v.players[p.u];
      const prev = players.get(p.u) || { verdict: null, since: null, last_msg: null, last_poor: null };
      const lastPoor = pv.verdict === "poor" ? pv.reasons[0] || "lag" : prev.last_poor;
      if (prev.verdict !== pv.verdict || lastPoor !== prev.last_poor) {
        stmts.push(
          db.prepare("INSERT INTO exp_players (uuid, name, first_seen, last_seen, verdict, headline, reasons, since, last_poor, brand) VALUES (?1, ?2, ?3, ?3, ?4, ?5, ?6, ?3, ?7, ?8) ON CONFLICT(uuid) DO UPDATE SET name = excluded.name, last_seen = MAX(COALESCE(last_seen, 0), excluded.last_seen), verdict = excluded.verdict, headline = excluded.headline, reasons = excluded.reasons, since = excluded.since, last_poor = excluded.last_poor, brand = excluded.brand")
            .bind(p.u, p.n, row.ts, pv.verdict, pv.headline, JSON.stringify(pv.reasons), String(lastPoor || "").slice(0, 200) || null, p.brand),
        );
        if (prev.verdict && counters.verdicts < MAX_VERDICT_EVENTS_PER_DAY) {
          counters.verdicts += 1;
          stmts.push(db.prepare("INSERT OR REPLACE INTO exp_events (ts, kind, uuid, detail) VALUES (?1, 'verdict', ?2, ?3)").bind(row.ts, p.u, JSON.stringify({ name: p.n, from: prev.verdict, to: pv.verdict, reasons: pv.reasons })));
        }
        players.set(p.u, { ...prev, verdict: pv.verdict, since: row.ts, last_poor: lastPoor });
      }
    }
    for (const e of row.lag) {
      if (counters.lag >= MAX_LAG_EVENTS_PER_DAY) break;
      counters.lag += 1;
      stmts.push(db.prepare("INSERT OR REPLACE INTO exp_events (ts, kind, uuid, detail) VALUES (?1, 'lag', '', ?2)").bind(e.t, JSON.stringify({ ms: e.ms, ...e.ctx })));
    }
    for (const [u, st] of Object.entries(row.stats)) {
      stmts.push(db.prepare("UPDATE exp_players SET stats = ?2 WHERE uuid = ?1").bind(u, JSON.stringify(st)));
    }
    // in-game messages: live rows only, rate-limited here (the agent checks again with the live state)
    if (live && !row.backfill && now - row.ts <= EXP_LIVE_MS) {
      stmts.push(...(await chunkAlerts(env, now, rt, cfg, row)));
      const lastPoor = {};
      for (const e of row.ev) if (e.k === "join") lastPoor[e.u] = (players.get(e.u) || {}).last_poor || null;
      const wanted = messagesFor(row, v, { lastPoor }, cfg, now);
      const st = (await expState(db, "messages")) || { broadcast: 0 };
      for (const m of wanted) {
        if (m.to === "*") {
          if (now - (st.broadcast || 0) < cfg.messages.broadcast_minutes * 60000) continue;
          st.broadcast = now;
        } else {
          const prev = players.get(m.to) || {};
          if (m.kind !== "join" && now - (prev.last_msg || 0) < cfg.messages.per_player_minutes * 60000) continue;
          players.set(m.to, { ...prev, last_msg: now });
          stmts.push(db.prepare("UPDATE exp_players SET last_msg = ?2 WHERE uuid = ?1").bind(m.to, now));
        }
        const id = await enqueueMessage(env, now, m);
        if (!id) continue;
        sent.push({ id, to: m.to, kind: m.kind });
        const name = m.to === "*" ? "everyone" : (row.pl.find((p) => p.u === m.to) || {}).n || "";
        stmts.push(db.prepare("INSERT OR REPLACE INTO exp_events (ts, kind, uuid, detail) VALUES (?1, 'message', ?2, ?3)").bind(now + sent.length, m.to === "*" ? "" : m.to, JSON.stringify({ id, name, kind: m.kind, text: m.text })));
      }
      if (wanted.some((m) => m.to === "*")) stmts.push(expStateStmt(db, "messages", st));
    }
  }
  for (const h of dirty) stmts.push(db.prepare("INSERT OR IGNORE INTO exp_dirty (ts) VALUES (?1)").bind(h));
  if (JSON.stringify(counters) !== countersBefore) stmts.push(expStateStmt(db, "counters", counters)); // written only when it changed: a steady minute costs 2 rows
  await runBatches(db, stmts);
  const verdicts = last && live
    ? { server: last.v.server, players: Object.fromEntries(Object.entries(last.v.players).map(([u, x]) => [u, { verdict: x.verdict, headline: x.headline, reasons: x.reasons, tips: x.tips }])) }
    : null;
  return json({ ok: true, stored: rows.length, rejected: rowsIn.length - rows.length, verdicts, messages: sent });
}

async function ingestSegment(env, request, url) {
  const name = url.searchParams.get("name") || "";
  const m = SEGMENT_RE.exec(name);
  if (!m) return json({ error: "bad segment name" }, 400);
  const body = await readLimited(request, MAX_SEGMENT_BODY);
  if (!body) return json({ error: "body too large" }, 413);
  if (body.length < 2 || body[0] !== 0x1f || body[1] !== 0x8b) return json({ error: "body must be gzip" }, 400);
  const key = `telemetry/${m[1]}/${m[2]}/${m[3]}/${name}`;
  await env.LOGS.put(key, body, { httpMetadata: { contentType: "application/gzip" } });
  return json({ ok: true, key });
}

// --- rollups + retention (cron) ---

async function rollupHour(db, hour, now) {
  const min = `exp_1m_${monthOf(hour)}`;
  const raw = `exp_raw_${dayOf(hour)}`;
  await expTable(db, min);
  await expTable(db, raw);
  const end = hour + 3600000;
  const agg = await db.prepare(`SELECT COUNT(*) AS minutes, AVG(players) AS players_avg, MAX(players) AS players_max, MIN(tps) AS tps_min, AVG(mean) AS mean, AVG(p50) AS p50, MAX(p99) AS p99, MAX(p999) AS p999, MAX(max) AS max, SUM(spikes) AS spikes, MAX(entities) AS entities_max, AVG(ping_p50) AS ping_avg, SUM(poor > 0) AS poor_minutes, SUM(fair > 0) AS fair_minutes, MAX(self_p99_us) AS self_p99_us FROM ${min} WHERE ts >= ?1 AND ts < ?2`).bind(hour, end).first();
  if (!agg || !agg.minutes) return 0;
  // P13 review M2: the per-player sums run in D1 (json_each over the raw rows: D1's time, not the Worker's 10 ms of
  // CPU); the Worker only parses the 60 small overhead objects. Bare columns with MAX(r.ts) take the latest name.
  const { results: perRows } = await db.prepare(
    `SELECT json_extract(p.value, '$.u') AS u, MAX(r.ts) AS last_ts, json_extract(p.value, '$.n') AS name, COUNT(*) AS minutes,
       SUM(json_extract(p.value, '$.ping')) AS ping_sum, COUNT(json_extract(p.value, '$.ping')) AS ping_n,
       MAX(COALESCE(json_extract(p.value, '$.ping_max'), 0)) AS ping_max,
       SUM(json_extract(r.data, '$.verdicts.players."' || json_extract(p.value, '$.u') || '".verdict') = 'poor') AS poor,
       SUM(json_extract(r.data, '$.verdicts.players."' || json_extract(p.value, '$.u') || '".verdict') = 'fair') AS fair
     FROM ${raw} AS r, json_each(r.data, '$.pl') AS p WHERE r.ts >= ?1 AND r.ts < ?2 AND json_valid(r.data) GROUP BY u`,
  ).bind(hour, end).all();
  const per = new Map();
  for (const x of perRows || []) {
    if (typeof x.u !== "string") continue;
    per.set(x.u, { name: x.name, minutes: x.minutes, ping: x.ping_sum || 0, pingN: x.ping_n || 0, ping_max: x.ping_max || 0, poor: x.poor || 0, fair: x.fair || 0 });
  }
  const { results: ovhRows } = await db.prepare(`SELECT ovh FROM ${min} WHERE ts >= ?1 AND ts < ?2`).bind(hour, end).all();
  const ovh = {};
  for (const { ovh: text } of ovhRows || []) {
    let o0;
    try {
      o0 = JSON.parse(text || "{}");
    } catch {
      continue;
    }
    for (const [unit, o] of Object.entries(o0 || {})) {
      const acc = ovh[unit] || { cpu_pct: 0, cpu_n: 0, mem_mb: 0, cpu_ms: 0, runs: 0 };
      if (n(o.cpu_pct) !== null) {
        acc.cpu_pct += o.cpu_pct;
        acc.cpu_n += 1;
      }
      acc.mem_mb = Math.max(acc.mem_mb, n(o.mem_mb) || 0, n(o.mem_peak_mb) || 0);
      acc.cpu_ms += n(o.cpu_ms) || 0;
      acc.runs += n(o.runs) || 0;
      ovh[unit] = acc;
    }
  }
  const ovhOut = {};
  for (const [unit, a] of Object.entries(ovh)) {
    // oneshot units report CPU ms per run; spread over the hour that is a CPU share too
    const cpu = a.cpu_n ? a.cpu_pct / a.cpu_n : a.cpu_ms ? (100 * a.cpu_ms) / 3600000 : null;
    ovhOut[unit] = { cpu_pct: round(cpu, 3), mem_mb: round(a.mem_mb), runs: a.runs || undefined };
  }
  const stmts = [
    db.prepare("INSERT OR REPLACE INTO exp_1h (ts, minutes, players_avg, players_max, tps_min, mean, p50, p99, p999, max, spikes, entities_max, ping_avg, poor_minutes, fair_minutes, self_p99_us, ovh) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17)").bind(
      hour, agg.minutes, round(agg.players_avg, 2), agg.players_max, agg.tps_min, round(agg.mean, 2), round(agg.p50, 2), agg.p99, agg.p999, agg.max, agg.spikes, agg.entities_max, round(agg.ping_avg), agg.poor_minutes, agg.fair_minutes, agg.self_p99_us, JSON.stringify(ovhOut),
    ),
  ];
  for (const [u, a] of per) {
    stmts.push(db.prepare("INSERT OR REPLACE INTO exp_player_1h (uuid, ts, name, minutes, ping_avg, ping_max, poor, fair) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)").bind(u, hour, a.name, a.minutes, a.pingN ? Math.round(a.ping / a.pingN) : null, a.ping_max, a.poor, a.fair));
    stmts.push(db.prepare("UPDATE exp_players SET last_seen = MAX(COALESCE(last_seen, 0), ?2) WHERE uuid = ?1").bind(u, Math.min(end, now)));
  }
  await runBatches(db, stmts);
  return stmts.length;
}

async function checkRemedies(env, cfg, now, rt) {
  const projects = [...new Map(Object.values(REMEDIES).flat().filter((r) => r.project).map((r) => [`${r.project}:${r.loader}`, r])).values()];
  const out = {};
  for (const r of projects) {
    const q = `loaders=${encodeURIComponent(JSON.stringify([r.loader]))}&game_versions=${encodeURIComponent(JSON.stringify([cfg.minecraft_version]))}`;
    try {
      const resp = await withTimeout(rt.fetch(`https://api.modrinth.com/v2/project/${r.project}/version?${q}`, { headers: { "User-Agent": "edvardk/smallworld (watchdog remedies check)" } }), 10000, "modrinth");
      const body = resp.ok ? await resp.json() : null;
      out[`${r.project}:${r.loader}`] = { available: Array.isArray(body) && body.length > 0, version: Array.isArray(body) && body.length ? str(body[0].version_number, 40) : null, checked: now, status: resp.status };
    } catch (err) {
      out[`${r.project}:${r.loader}`] = { available: null, checked: now, error: String((err && err.message) || err).slice(0, 80) };
    }
  }
  return out;
}

// From the cron (every 5 min), P13 review M2: ONE unit of work per run, so a run stays far inside the Free plan's
// 10 ms of CPU: the next hour to roll up (the last complete hour; after a gap up to 6 hours back, oldest first),
// else the oldest backfilled ("dirty") hour, else the daily retention (drop expired day/month tables whole, delete
// expired hourly rows and events), else the daily remedy check. The unit is recorded in exp_state.maint *before*
// it runs: a run killed for CPU (no catch, no report) leaves it there, the next run retries it, and after
// MAINT_MAX_TRIES interrupted runs it is skipped (advanced past) and reported, instead of blocking the queue.
function nextMaintUnit(st, lastHour, dirtyTs, cfg, now) {
  if ((st.rolled || 0) < lastHour) return { kind: "rollup", ts: Math.max(st.rolled ? st.rolled + 3600000 : lastHour, lastHour - 6 * 3600000) };
  if (dirtyTs !== null) return { kind: "dirty", ts: dirtyTs };
  if (!st.retention || now - st.retention >= RETENTION_EVERY_MS) return { kind: "retention", ts: 0 };
  if (cfg.minecraft_version && (!st.remedies || now - st.remedies >= REMEDY_CHECK_MS)) return { kind: "remedies", ts: 0 };
  return null;
}

async function maintainExperience(env, now, rt) {
  const db = env.TELEMETRY;
  const cfg = experienceConfig(env);
  await expSchema(db);
  const st = (await expState(db, "maint")) || {};
  const done = { hours: [], dropped: [], remedies: false, unit: null, skipped: null };
  const lastHour = hourOf(now) - 3600000;
  const dirtyRow = await db.prepare("SELECT ts FROM exp_dirty ORDER BY ts LIMIT 1").first();
  const unit = nextMaintUnit(st, lastHour, dirtyRow ? dirtyRow.ts : null, cfg, now);
  if (!unit) return done;
  const key = `${unit.kind}:${unit.ts}`;
  done.unit = key;
  const advance = async () => {
    if (unit.kind === "rollup") st.rolled = unit.ts;
    if (unit.kind === "dirty") await db.prepare("DELETE FROM exp_dirty WHERE ts = ?1").bind(unit.ts).run();
    if (unit.kind === "retention") st.retention = now;
    if (unit.kind === "remedies") st.remedies = now;
  };
  const tries = st.inflight && st.inflight.key === key ? st.inflight.tries + 1 : 1;
  if (tries > MAINT_MAX_TRIES) {
    await advance();
    delete st.inflight;
    await expStateStmt(db, "maint", st).run();
    done.skipped = key;
    return done;
  }
  st.inflight = { key, tries, since: tries === 1 ? now : st.inflight.since };
  await expStateStmt(db, "maint", st).run(); // progress first: a run killed below leaves this behind
  if (unit.kind === "rollup" || unit.kind === "dirty") {
    if (unit.ts <= lastHour) await rollupHour(db, unit.ts, now);
    done.hours.push(unit.ts);
  } else if (unit.kind === "retention") {
    const { results } = await db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'exp_%'").all();
    const today = dayStart(dayOf(now));
    for (const { name } of results || []) {
      let drop = false;
      const r = EXP_RAW_TABLE.exec(name);
      const m = EXP_MIN_TABLE.exec(name);
      if (r) drop = (today - dayStart(r[1])) / 86400000 >= cfg.raw_days;
      if (m) {
        const monthEnd = Date.UTC(Number(m[1].slice(0, 4)), Number(m[1].slice(4, 6)), 1);
        drop = now - monthEnd >= cfg.minute_days * 86400000;
      }
      if (drop) {
        await db.prepare(`DROP TABLE IF EXISTS ${name}`).run();
        const known = expTables.get(db);
        if (known) known.delete(name);
        done.dropped.push(name);
      }
    }
    await db.batch([
      db.prepare("DELETE FROM exp_1h WHERE ts < ?1").bind(now - cfg.hour_days * 86400000),
      db.prepare("DELETE FROM exp_player_1h WHERE ts < ?1").bind(now - cfg.hour_days * 86400000),
      db.prepare("DELETE FROM exp_events WHERE ts < ?1").bind(now - cfg.minute_days * 86400000),
      db.prepare("DELETE FROM exp_sessions WHERE join_ts < ?1").bind(now - cfg.hour_days * 86400000),
    ]);
  } else {
    await expStateStmt(db, "remedies", await checkRemedies(env, cfg, now, rt)).run();
    done.remedies = true;
  }
  await advance();
  delete st.inflight;
  await expStateStmt(db, "maint", st).run();
  return done;
}

// An annotation on the history charts (deploys, restarts, restores, panel actions): best effort, never fatal.
async function annotate(env, ts, kind, text) {
  if (!env.TELEMETRY) return;
  try {
    await expSchema(env.TELEMETRY);
    await env.TELEMETRY.prepare("INSERT OR REPLACE INTO exp_events (ts, kind, uuid, detail) VALUES (?1, 'note', ?2, ?3)").bind(ts, kind, JSON.stringify({ text: String(text).slice(0, 200) })).run();
  } catch {
    // the annotation is a nice-to-have
  }
}

// --- panel API (read) ---

const EXP_RANGES = {
  "24h": { ms: 86400000, source: "1m", step: 0 },
  "7d": { ms: 7 * 86400000, source: "1m", step: 900000 },
  "30d": { ms: 30 * 86400000, source: "1h", step: 0 },
  "180d": { ms: 180 * 86400000, source: "1h", step: 6 * 3600000 },
  "2y": { ms: 730 * 86400000, source: "1h", step: 86400000 },
};

async function minuteRows(db, from, to, cols) {
  const months = new Set();
  for (let t = from; t <= to + 31 * 86400000; t += 28 * 86400000) {
    const m = monthOf(Math.min(t, to));
    months.add(m);
  }
  months.add(monthOf(to));
  const { results: tables } = await db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'exp_1m_%'").all();
  const have = new Set((tables || []).map((t) => t.name));
  const out = [];
  for (const m of [...months].sort()) {
    const t = `exp_1m_${m}`;
    if (!have.has(t)) continue;
    const { results } = await db.prepare(`SELECT ${cols} FROM ${t} WHERE ts >= ?1 AND ts <= ?2 ORDER BY ts`).bind(from, to).all();
    out.push(...(results || []));
  }
  return out;
}

function bucketRows(rows, step, keysMax) {
  if (!step) return rows;
  const out = new Map();
  for (const r of rows) {
    const b = Math.floor(r.ts / step) * step;
    const acc = out.get(b) || { ts: b, _n: 0 };
    acc._n += 1;
    for (const [k, v] of Object.entries(r)) {
      if (k === "ts" || typeof v !== "number") continue;
      if (keysMax.includes(k)) acc[k] = Math.max(acc[k] ?? -Infinity, v);
      else acc[k] = (acc[k] || 0) + v;
    }
    out.set(b, acc);
  }
  return [...out.values()].map((a) => {
    const o = { ts: a.ts };
    for (const [k, v] of Object.entries(a)) {
      if (k === "ts" || k === "_n") continue;
      o[k] = keysMax.includes(k) ? v : Math.round((v / a._n) * 100) / 100;
    }
    return o;
  });
}

const HISTORY_MAX = ["p99", "p999", "max", "players_max", "spikes", "entities", "entities_max", "ping_max", "poor", "fair", "poor_minutes"];

async function experienceHistory(env, now, range) {
  const db = env.TELEMETRY;
  const r = EXP_RANGES[range] || EXP_RANGES["24h"];
  await expSchema(db);
  const from = now - r.ms;
  let points;
  if (r.source === "1m") {
    points = await minuteRows(db, from, now, "ts, players, tps, mean, p50, p99, p999, max, spikes, entities, ping_p50, ping_max, poor, fair, self_p99_us");
  } else {
    const { results } = await db.prepare("SELECT ts, minutes, players_avg AS players, players_max, tps_min AS tps, mean, p50, p99, p999, max, spikes, entities_max AS entities, ping_avg AS ping_p50, poor_minutes AS poor, fair_minutes AS fair, self_p99_us FROM exp_1h WHERE ts >= ?1 ORDER BY ts").bind(from).all();
    points = results || [];
  }
  points = bucketRows(points, r.step, HISTORY_MAX);
  const { results: notes } = await db.prepare("SELECT ts, uuid AS kind, detail FROM exp_events WHERE kind = 'note' AND ts >= ?1 ORDER BY ts LIMIT 200").bind(from).all();
  // data gaps: stretches over 10 minutes (24h/7d) or 2 hours (longer) without a row
  const gapMs = r.source === "1m" ? 600000 : 2 * 3600000;
  const gaps = [];
  let prev = from;
  for (const p of points) {
    if (p.ts - prev > gapMs + (r.step || 0)) gaps.push({ from: prev, to: p.ts });
    prev = p.ts;
  }
  if (now - prev > gapMs + (r.step || 0)) gaps.push({ from: prev, to: now });
  return {
    range, from, to: now, source: r.source, step_ms: r.step || (r.source === "1m" ? 60000 : 3600000), points,
    annotations: (notes || []).map((x) => ({ ts: x.ts, kind: x.kind, text: (safeJson(x.detail) || {}).text || "" })),
    gaps: gaps.slice(-50),
  };
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function remediesFor(causes, availability) {
  const out = [];
  for (const cause of [...new Set(causes)]) {
    for (const r of REMEDIES[cause] || []) {
      const avail = r.project ? availability[`${r.project}:${r.loader}`] || null : null;
      out.push({ cause, ...r, available: avail ? avail.available : r.project ? null : undefined, version: avail ? avail.version || null : null });
    }
  }
  return out;
}

// The newest minute row (whole JSON, verdicts included) of today or yesterday: {row, ts}, or nulls.
async function latestExperienceRow(db, now) {
  for (const day of [dayOf(now), dayOf(now - 86400000)]) {
    const t = `exp_raw_${day}`;
    await expTable(db, t);
    const r = await db.prepare(`SELECT ts, data FROM ${t} WHERE ts <= ?1 ORDER BY ts DESC LIMIT 1`).bind(now).first();
    if (r) return { row: safeJson(r.data), ts: r.ts };
  }
  return { row: null, ts: null };
}

async function experienceOverview(env, now) {
  const db = env.TELEMETRY;
  await expSchema(db);
  const availability = (await expState(db, "remedies")) || {};
  const cfg = experienceConfig(env);
  const { row, ts: latest } = await latestExperienceRow(db, now);
  const live = latest === null ? null : { ts: latest };
  const fresh = latest !== null && now - latest < 180000 + 60000; // the minute row arrives just after its minute
  const players = row ? row.pl.map((p) => ({ uuid: p.u, name: p.n, ping: p.ping, ping_max: p.ping_max, view: p.view, send: p.send, brand: p.brand, fps: p.fps ?? null, fps_min: p.fps_min ?? null, world: p.w, ent: p.ent, join: p.join, ...((row.verdicts && row.verdicts.players[p.u]) || {}), tips_on: p.tips })) : [];
  const causes = [...((row && row.verdicts && row.verdicts.server.causes) || []), ...players.flatMap((p) => p.causes || []).map((c) => (c === "view_high" ? "client_fps" : c === "entities_near" ? "entity_hotspot" : c))];
  // (v1.2: a measured low FPS is already "client_fps": the same remedies, Sodium & co.)
  const { results: msgs } = await db.prepare("SELECT ts, uuid, detail FROM exp_events WHERE kind = 'message' ORDER BY ts DESC LIMIT 30").all();
  const { results: lags } = await db.prepare("SELECT ts, detail FROM exp_events WHERE kind = 'lag' ORDER BY ts DESC LIMIT 20").all();
  const { results: hourly } = await db.prepare("SELECT ts, ovh FROM exp_1h WHERE ts >= ?1 ORDER BY ts").bind(now - 86400000).all();
  // product overhead: last 24 h of hourly rollups (+ the current minute row)
  const ovh = {};
  for (const h of hourly || []) {
    for (const [unit, o] of Object.entries(safeJson(h.ovh) || {})) {
      const a = ovh[unit] || { cpu: 0, cpuN: 0, mem: 0 };
      if (n(o.cpu_pct) !== null) {
        a.cpu += o.cpu_pct;
        a.cpuN += 1;
      }
      a.mem = Math.max(a.mem, n(o.mem_mb) || 0);
      ovh[unit] = a;
    }
  }
  for (const [unit, o] of Object.entries((row && row.ovh) || {})) {
    const a = ovh[unit] || { cpu: 0, cpuN: 0, mem: 0 };
    if (!a.cpuN && n(o.cpu_pct) !== null) {
      a.cpu = o.cpu_pct;
      a.cpuN = 1;
    }
    a.mem = Math.max(a.mem, n(o.mem_mb) || 0);
    ovh[unit] = a;
  }
  const overhead = Object.entries(ovh).map(([unit, a]) => {
    const cpu = a.cpuN ? round(a.cpu / a.cpuN, 3) : null;
    const ours = unit !== "minecraft.service";
    return { unit, cpu_pct: cpu, mem_mb: round(a.mem), flag: ours && ((cpu || 0) > RULES.overhead_cpu_pct || a.mem > RULES.overhead_mem_mb) };
  }).sort((a, b) => (a.unit === "minecraft.service" ? -1 : b.unit === "minecraft.service" ? 1 : a.unit < b.unit ? -1 : 1));
  if (overhead.some((o) => o.flag)) causes.push("overhead");
  return {
    now,
    fresh: Boolean(fresh),
    ts: live ? live.ts : null,
    server: (row && row.verdicts && row.verdicts.server) || null,
    srv: row ? row.srv : null,
    busiest: row ? (row.srv.hot || []).map((c) => ({ ...c, where: chunkWhere(c), heavy: cfg.chunk_entities > 0 && c.ent >= cfg.chunk_entities })) : [],
    chunk_limit: cfg.chunk_entities,
    players,
    remedies: remediesFor(causes, availability),
    availability,
    messages: (msgs || []).map((m) => ({ ts: m.ts, uuid: m.uuid, ...(safeJson(m.detail) || {}) })),
    lag: (lags || []).map((l) => ({ ts: l.ts, ...(safeJson(l.detail) || {}) })),
    overhead,
    backlog: (row && row.backlog) || null,
    plugin: row ? { self_p99_us: row.srv.self_p99_us, self_max_us: row.srv.self_max_us, budget_skips: row.srv.budget_skips, lost_windows: row.srv.lost_windows } : null,
    rules: RULES,
  };
}

async function experiencePlayer(env, now, uuid) {
  const db = env.TELEMETRY;
  await expSchema(db);
  const p = await db.prepare("SELECT uuid, name, first_seen, last_seen, (SELECT COUNT(*) FROM exp_sessions s WHERE s.uuid = exp_players.uuid) AS sessions, (SELECT COALESCE(SUM(leave_ts - join_ts), 0) FROM exp_sessions s WHERE s.uuid = exp_players.uuid AND leave_ts IS NOT NULL) AS play_ms, stats, verdict, headline, reasons, since, last_msg, last_poor, brand FROM exp_players WHERE uuid = ?1").bind(uuid).first();
  if (!p) return null;
  const { results: hours } = await db.prepare("SELECT ts, minutes, ping_avg, ping_max, poor, fair FROM exp_player_1h WHERE uuid = ?1 AND ts >= ?2 ORDER BY ts").bind(uuid, now - 30 * 86400000).all();
  const { results: sessions } = await db.prepare("SELECT join_ts, leave_ts, name FROM exp_sessions WHERE uuid = ?1 ORDER BY join_ts DESC LIMIT 30").bind(uuid).all();
  const { results: events } = await db.prepare("SELECT ts, kind, detail FROM exp_events WHERE uuid = ?1 AND kind IN ('verdict', 'message') ORDER BY ts DESC LIMIT 30").bind(uuid).all();
  // the last 24 h of 1-minute ping (the raw rows carry it per player)
  const pings = [];
  for (const day of [dayOf(now - 86400000), dayOf(now)]) {
    const t = `exp_raw_${day}`;
    await expTable(db, t);
    const { results } = await db.prepare(`SELECT ts, json_extract(data, '$.pl') AS pl FROM ${t} WHERE ts >= ?1 AND instr(data, ?2) > 0 ORDER BY ts`).bind(now - 86400000, uuid).all();
    for (const r of results || []) {
      const me = (safeJson(r.pl) || []).find((x) => x.u === uuid);
      if (me) pings.push({ ts: r.ts, ping: me.ping, ping_max: me.ping_max });
    }
  }
  return {
    ...p,
    stats: safeJson(p.stats),
    reasons: safeJson(p.reasons) || [],
    hours: hours || [],
    session_list: sessions || [],
    events: (events || []).map((e) => ({ ...(safeJson(e.detail) || {}), ts: e.ts, kind: e.kind })),
    pings,
  };
}

async function experienceLeaderboard(env) {
  const db = env.TELEMETRY;
  await expSchema(db);
  const { results } = await db.prepare("SELECT p.uuid, p.name, p.last_seen, p.verdict, p.stats, COUNT(s.join_ts) AS sessions, COALESCE(SUM(s.leave_ts - s.join_ts), 0) AS play_ms FROM exp_players p LEFT JOIN exp_sessions s ON s.uuid = p.uuid GROUP BY p.uuid ORDER BY play_ms DESC LIMIT 50").all();
  return (results || []).map((r) => ({ ...r, stats: safeJson(r.stats) }));
}

// --- panel (P9): Cloudflare Access-protected UI + read API on panel.<zone> ----------------------------------
//
// Requests whose host is PANEL.host never reach the watchdog or agent routes. Each one needs a valid Cloudflare
// Access JWT (Cf-Access-Jwt-Assertion: RS256, signed by a key of https://<team>/cdn-cgi/access/certs, issuer
// https://<team>, audience = the Access application's AUD tag, unexpired, email on panel.access.emails). The
// header is never trusted on its own: without a verifiable token the answer is 403, even if the Access
// application were deleted or misconfigured (design §5a). Static files come from the ASSETS binding (watchdog/
// panel/, run_worker_first) with a strict CSP; the API reads D1 (samples, logs, audit, watchdog state, the
// agent's inventory), R2 (archive listing) and the Control object (live console). P9 is read-only except the
// console: allowlisted console commands (the agent decides) and the live-console subscription.

const PANEL_SECURITY_HEADERS = {
  "Content-Security-Policy":
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; " +
    "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Strict-Transport-Security": "max-age=31536000",
};
const JWKS_TTL_MS = 3600000;
const JWKS_RETRY_MS = 60000;
const JWT_LEEWAY_S = 60;
const SERIES_KEYS = [
  "tps", "mspt", "players", "heap_used_mb", "heap_max_mb", "cpu_pct", "steal_pct", "mem_used_mb",
  "mem_total_mb", "jvm_mem_mb", "disk_pct", "net_rx_bps", "net_tx_bps", "gc_max_pause_ms", "load1",
];
const RANGES = {
  "2h": { ms: 2 * 3600000, step: 0 },
  "24h": { ms: 86400000, step: 300000 },
  "7d": { ms: 7 * 86400000, step: 1800000 },
  "30d": { ms: 30 * 86400000, step: 7200000 },
  "90d": { ms: 90 * 86400000, step: 21600000 },
};
const AUDIT_SCHEMA =
  "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, login TEXT NOT NULL, " +
  "action TEXT NOT NULL, args TEXT NOT NULL, cmd_id TEXT, result TEXT NOT NULL, ok INTEGER)";

function panelConfig(env) {
  let raw;
  try {
    raw = JSON.parse(env.PANEL || "null");
  } catch {
    raw = null;
  }
  if (!raw || typeof raw !== "object") return null;
  const emails = Array.isArray(raw.emails) ? raw.emails.map((e) => String(e).toLowerCase()).filter(Boolean) : [];
  const cfg = {
    host: String(raw.host || "").toLowerCase(),
    team_domain: String(raw.team_domain || "").toLowerCase(),
    aud: String(raw.aud || ""),
    // P10 review: destructive actions only on the confirmation host (its own Access app and AUD)
    confirm_host: String(raw.confirm_host || "").toLowerCase(),
    confirm_aud: String(raw.confirm_aud || ""),
    emails,
    reauth_minutes: Number.isInteger(raw.reauth_minutes) ? raw.reauth_minutes : 10,
    title: String(raw.title || "Minecraft server").slice(0, 80),
    server_host: String(raw.server_host || ""),
    flavour: String(raw.flavour || ""),
    minecraft_version: String(raw.minecraft_version || ""),
    backup_schedule: String(raw.backup_schedule || ""),
    max_players: Number.isInteger(raw.max_players) ? raw.max_players : null,
  };
  cfg.ok = Boolean(cfg.host && /^[a-z0-9.-]+$/.test(cfg.team_domain) && cfg.aud && emails.length);
  cfg.confirm_ok = Boolean(cfg.ok && cfg.confirm_host && cfg.confirm_host !== cfg.host && cfg.confirm_aud && cfg.confirm_aud !== cfg.aud);
  return cfg;
}

function b64urlBytes(text) {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function b64urlJson(text) {
  return JSON.parse(new TextDecoder().decode(b64urlBytes(text)));
}

// Per isolate. P9 review: a failing certs URL is asked again at most once a minute (negative cache), and the
// last good keys stay usable for JWKS_GRACE_MS past their TTL while it fails (a short Cloudflare blip must not
// lock the admin out); with no good keys at all every request fails closed.
const JWKS_GRACE_MS = 15 * 60000;
let jwksCache = { team: "", keys: new Map(), fetched: 0, failed: 0 };

async function accessKey(cfg, kid, now, rt) {
  const same = jwksCache.team === cfg.team_domain;
  const age = now - jwksCache.fetched;
  if (same && age < JWKS_TTL_MS && jwksCache.keys.has(kid)) return jwksCache.keys.get(kid);
  const usable = same && age < JWKS_TTL_MS + JWKS_GRACE_MS ? jwksCache.keys : new Map();
  // An unknown kid (or an expired cache) refetches at most once a minute, and not within a minute of a failure.
  if (same && (age < JWKS_RETRY_MS || now - jwksCache.failed < JWKS_RETRY_MS)) return usable.get(kid) || null;
  let keys;
  try {
    const resp = await rt.fetch(`https://${cfg.team_domain}/cdn-cgi/access/certs`, { headers: { Accept: "application/json" } });
    if (!resp.ok) throw new Error(`certs: HTTP ${resp.status}`);
    const body = await resp.json();
    keys = new Map();
    for (const jwk of (body && Array.isArray(body.keys) ? body.keys : []).slice(0, 20)) {
      if (!jwk || jwk.kty !== "RSA" || typeof jwk.kid !== "string") continue;
      const pub = { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true };
      keys.set(jwk.kid, await crypto.subtle.importKey("jwk", pub, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]));
    }
    if (!keys.size) throw new Error("certs: no RSA keys");
  } catch {
    jwksCache = same ? { ...jwksCache, failed: now } : { team: cfg.team_domain, keys: new Map(), fetched: 0, failed: now };
    return usable.get(kid) || null;
  }
  jwksCache = { team: cfg.team_domain, keys, fetched: now, failed: 0 };
  return keys.get(kid) || null;
}

// The verified identity {email, iat, exp} or null. Never throws.
async function verifyAccess(request, cfg, now, rt) {
  const token = request.headers.get("Cf-Access-Jwt-Assertion") || "";
  const parts = token.split(".");
  if (parts.length !== 3 || token.length > 8192) return null;
  try {
    const header = b64urlJson(parts[0]);
    const claims = b64urlJson(parts[1]);
    if (!header || header.alg !== "RS256" || typeof header.kid !== "string") return null;
    const key = await accessKey(cfg, header.kid, now, rt);
    if (!key) return null;
    const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
    if (!(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlBytes(parts[2]), signed))) return null;
    const t = now / 1000;
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (claims.iss !== `https://${cfg.team_domain}` || !aud.includes(cfg.aud)) return null;
    if (!Number.isFinite(claims.exp) || claims.exp < t - JWT_LEEWAY_S) return null;
    if (Number.isFinite(claims.nbf) && claims.nbf > t + JWT_LEEWAY_S) return null;
    if (!Number.isFinite(claims.iat) || claims.iat > t + JWT_LEEWAY_S) return null;
    const email = typeof claims.email === "string" ? claims.email.toLowerCase() : "";
    if (!email || !cfg.emails.includes(email)) return null;
    return { email, iat: claims.iat, exp: claims.exp };
  } catch {
    return null; // malformed token, certs unreachable: fail closed
  }
}

function withPanelHeaders(resp, extra = {}) {
  const headers = new Headers(resp.headers);
  for (const [k, v] of Object.entries({ ...PANEL_SECURITY_HEADERS, ...extra })) headers.set(k, v);
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers });
}

function denied(request) {
  const wantsHtml = (request.headers.get("Accept") || "").includes("text/html");
  const body = wantsHtml
    ? "<!doctype html><meta charset=utf-8><title>Forbidden</title><p>403: sign in through Cloudflare Access.</p>"
    : JSON.stringify({ error: "forbidden: a valid Cloudflare Access login is required" });
  return withPanelHeaders(
    new Response(body, {
      status: 403,
      headers: { "Content-Type": wantsHtml ? "text/html; charset=utf-8" : "application/json", "Cache-Control": "no-store" },
    }),
  );
}

// Day tables that exist, newest first (`samples` or `logs`).
async function dayTables(db, kind) {
  const { results } = await db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?1")
    .bind(`${kind}_%`)
    .all();
  return (results || [])
    .map((r) => r.name)
    .filter((n) => DAY_TABLE.test(n) && n.startsWith(`${kind}_`))
    .sort()
    .reverse();
}

function parseRow(row) {
  try {
    return { ts: row.ts, ...JSON.parse(row.data) };
  } catch {
    return null;
  }
}

async function latestSample(env) {
  for (const table of (await dayTables(env.DB, "samples")).slice(0, 2)) {
    const { results } = await env.DB.prepare(`SELECT ts, data FROM ${table} ORDER BY ts DESC LIMIT 1`).all();
    if (results && results.length) return parseRow(results[0]);
  }
  return null;
}

function bucketed(points, step) {
  if (!step) return points;
  const buckets = new Map();
  for (const p of points) {
    const b = Math.floor(p.ts / step) * step;
    if (!buckets.has(b)) buckets.set(b, { sums: {}, counts: {} });
    const acc = buckets.get(b);
    for (const k of SERIES_KEYS) {
      if (typeof p[k] === "number") {
        acc.sums[k] = (acc.sums[k] || 0) + p[k];
        acc.counts[k] = (acc.counts[k] || 0) + 1;
      }
    }
  }
  return [...buckets]
    .sort((a, b) => a[0] - b[0])
    .map(([ts, acc]) => {
      const o = { ts };
      for (const k of Object.keys(acc.sums)) o[k] = Math.round((acc.sums[k] / acc.counts[k]) * 100) / 100;
      return o;
    });
}

async function series(env, now, range) {
  const r = RANGES[range] || RANGES["2h"];
  const from = now - r.ms;
  const points = [];
  for (const table of await dayTables(env.DB, "samples")) {
    if (dayStart(DAY_TABLE.exec(table)[2]) + 86400000 <= from) break;
    const { results } = await env.DB.prepare(`SELECT ts, data FROM ${table} WHERE ts >= ?1 ORDER BY ts`).bind(from).all();
    for (const row of results || []) {
      const p = parseRow(row);
      if (p) points.push(p);
    }
  }
  if (r.ms > 7 * 86400000 - 1 || !points.length || points[0].ts > from + r.ms / 2) {
    await ensureTable(env.DB, "samples_5m");
    const { results } = await env.DB.prepare("SELECT ts, data FROM samples_5m WHERE ts >= ?1 ORDER BY ts").bind(from).all();
    for (const row of results || []) {
      const p = parseRow(row);
      if (p) points.push(p);
    }
  }
  points.sort((a, b) => a.ts - b.ts);
  const slim = points.map((p) => {
    const o = { ts: p.ts };
    for (const k of SERIES_KEYS) if (typeof p[k] === "number") o[k] = p[k];
    return o;
  });
  return { range: RANGES[range] ? range : "2h", step_ms: r.step || null, points: bucketed(slim, r.step) };
}

async function kvState(env) {
  const { state } = await loadState(env.DB);
  return state;
}

async function ensureAudit(db) {
  let known = createdTables.get(db);
  if (!known) {
    known = new Set();
    createdTables.set(db, known);
  }
  if (known.has("audit")) return;
  await db.prepare(AUDIT_SCHEMA).run();
  known.add("audit");
}

async function panelAudit(env, limit) {
  await ensureAudit(env.DB);
  const { results } = await env.DB.prepare(
    "SELECT id, ts, login, action, args, result, ok FROM audit ORDER BY id DESC LIMIT ?1",
  )
    .bind(limit)
    .all();
  return (results || []).map((r) => ({ source: "panel", ...r }));
}

async function serverAudit(env, limit, days = 3) {
  const out = [];
  for (const table of (await dayTables(env.DB, "logs")).slice(0, days)) {
    const { results } = await env.DB.prepare(
      `SELECT id, ts, unit, prio, msg FROM ${table} WHERE audit = 1 ORDER BY id DESC LIMIT ?1`,
    )
      .bind(limit - out.length)
      .all();
    out.push(...(results || []).map((r) => ({ source: "server", ...r })));
    if (out.length >= limit) break;
  }
  return out;
}

async function auditFeed(env, limit) {
  const [a, b] = await Promise.all([panelAudit(env, limit), serverAudit(env, limit)]);
  return [...a, ...b].sort((x, y) => y.ts - x.ts).slice(0, limit);
}

function escapeLike(text) {
  return text.replace(/[\\%_]/g, (c) => `\\${c}`);
}

const LEVELS = { all: 7, info: 6, notice: 5, warn: 4, error: 3 };

async function searchLogs(env, url) {
  const days = await dayTables(env.DB, "logs");
  const wanted = url.searchParams.get("day") || "";
  const table = /^\d{8}$/.test(wanted) ? `logs_${wanted}` : days[0];
  const q = (url.searchParams.get("q") || "").slice(0, 100);
  const unit = (url.searchParams.get("unit") || "").slice(0, 100);
  const prio = LEVELS[url.searchParams.get("level") || "all"] ?? 7;
  const audit = url.searchParams.get("audit") === "1" ? 1 : 0;
  const before = Number.parseInt(url.searchParams.get("before") || "0", 10) || 0;
  const limit = Math.min(500, Math.max(1, Number.parseInt(url.searchParams.get("limit") || "200", 10) || 200));
  const dayList = days.map((t) => t.slice(5));
  if (!table || !days.includes(table)) return { day: wanted || null, days: dayList, rows: [], next_before: null };
  const { results } = await env.DB.prepare(
    `SELECT id, ts, unit, prio, audit, msg FROM ${table} WHERE (?1 = '' OR msg LIKE ?1 ESCAPE '\\') ` +
      "AND (?2 = '' OR unit = ?2) AND prio <= ?3 AND audit >= ?4 AND (?5 = 0 OR id < ?5) ORDER BY id DESC LIMIT ?6",
  )
    .bind(q ? `%${escapeLike(q)}%` : "", unit, prio, audit, before, limit)
    .all();
  const rows = results || [];
  return {
    day: table.slice(5),
    days: dayList,
    rows,
    next_before: rows.length === limit ? rows[rows.length - 1].id : null,
  };
}

const SNAPSHOT_SAVED = /snapshot ([0-9a-f]{8}) saved/;
const ADDED = /Added to the repository: ([0-9.]+ [KMGT]?i?B)/;

// Backups as the journal saw them (restic's own lines from mc-backup.service), newest first, plus the
// watchdog's backup heartbeat. Snapshot lists from restic itself need the backup credentials: P10.
async function backups(env, now) {
  const snapshots = [];
  const failures = [];
  for (const table of (await dayTables(env.DB, "logs")).slice(0, 3)) {
    const { results } = await env.DB.prepare(
      `SELECT ts, prio, msg FROM ${table} WHERE unit = 'mc-backup.service' ORDER BY id DESC LIMIT 2000`,
    ).all();
    for (const r of results || []) {
      // newest first: a run's "snapshot … saved" line comes before (above) its "Added to the repository" line
      const a = ADDED.exec(r.msg);
      const last = snapshots[snapshots.length - 1];
      if (a && last && last.added === null) last.added = a[1];
      const m = SNAPSHOT_SAVED.exec(r.msg);
      if (m) {
        snapshots.push({ id: m[1], ts: r.ts, added: null });
      } else if (r.prio <= 3) {
        failures.push({ ts: r.ts, msg: r.msg.slice(0, 300) });
      }
    }
    if (snapshots.length >= 50) break;
  }
  const state = await kvState(env);
  const c = state["check:backup"] || {};
  // P10: restic's own list (the agent sends it after every backup/restore) wins over the log lines.
  const listed = state.inventory && Array.isArray(state.inventory.snapshots) ? state.inventory.snapshots : [];
  return {
    source: listed.length ? "restic" : "log",
    restic: listed.slice(0, 200).map((x) => ({ ...x, ts: Date.parse(x.time) || null })),
    snapshots: snapshots.slice(0, 50),
    failures: failures.slice(0, 10),
    last_ok: iso(c.last_ok),
    last_fail: iso(c.last_fail),
    running_since: iso(c.started_at),
    age_s: typeof c.last_ok === "number" ? Math.round((now - c.last_ok) / 1000) : null,
  };
}

async function consoleBacklog(env, limit = 200) {
  const out = [];
  for (const table of (await dayTables(env.DB, "logs")).slice(0, 2)) {
    const { results } = await env.DB.prepare(
      `SELECT id, ts, prio, msg FROM ${table} WHERE unit = 'minecraft.service' ORDER BY id DESC LIMIT ?1`,
    )
      .bind(limit - out.length)
      .all();
    out.push(...(results || []));
    if (out.length >= limit) break;
  }
  return out.reverse();
}

function warnings(sample, history, watchdog, now, inv = null) {
  const out = [];
  // P10: the panel's own state (whitelist -> Git, plugin drift, the GitHub token)
  if (inv) {
    const sync = inv.whitelist_sync || {};
    const pend = inv.pending || [];
    if (pend.length) {
      const pr = sync.pr && sync.pr.number ? ` (pull request #${sync.pr.number})` : "";
      out.push({ level: "info", text: `${pend.length} whitelist change(s) made here are live but not in Git yet${pr}; deploys keep them until they are.` });
    }
    if (sync.error) out.push({ level: "warn", text: `Whitelist -> Git: ${sync.error}` });
    for (const e of sync.expired || []) {
      if (typeof e.ts === "number" && now - e.ts < 7 * 86400000) out.push({ level: "warn", text: `The whitelist ${e.op} of ${e.name} never reached Git within 24 h and was dropped; the next deploy follows players.yml.`, ts: e.ts });
    }
    if ((inv.plugins_disabled || []).length) out.push({ level: "warn", text: `Server differs from Git: ${inv.plugins_disabled.join(", ")} switched off in the panel (plugins/disabled/); deploys keep it off.` });
  }
  if (!sample || now - sample.ts > AGENT_FRESH_MS) {
    out.push({ level: "bad", text: "No sample from the server's agent for over 3 minutes: the host or its link may be down." });
  }
  if (watchdog.admin_stopped) out.push({ level: "info", text: "The server is admin-stopped: self-heal and the watchdog leave it down until it is started." });
  if (watchdog.maintenance) out.push({ level: "info", text: `Maintenance window until ${watchdog.maintenance.until}: ${watchdog.maintenance.reason}` });
  for (const [fam, p] of Object.entries(watchdog.probes || {})) {
    if (p.ok === false) out.push({ level: "bad", text: `The watchdog can't reach the server over ${fam === "v4" ? "IPv4" : "IPv6"} (${p.failed_runs} runs).` });
  }
  for (const [name, c] of Object.entries(watchdog.checks || {})) {
    if (c.state === "late" || c.state === "failing") out.push({ level: "warn", text: `The ${name} check is ${c.state}.` });
  }
  if (watchdog.cron_error) out.push({ level: "warn", text: "The watchdog's own cron run is failing." });
  const spikes = history.filter((p) => typeof p.mspt === "number" && p.mspt > 50);
  if (spikes.length) {
    const worst = spikes.reduce((a, b) => (b.mspt > a.mspt ? b : a));
    out.push({ level: "warn", text: `MSPT went over 50 ms in ${spikes.length} sample(s) in the last 2 hours (worst ${worst.mspt} ms).`, ts: worst.ts });
  }
  if (sample && sample.steal_pct > 5) out.push({ level: "warn", text: `CPU steal is ${sample.steal_pct}%: the host is oversubscribed.` });
  if (sample && sample.disk_pct > 85) out.push({ level: "warn", text: `The disk is ${sample.disk_pct}% full.` });
  return out;
}

// --- v1.3: versions everywhere ------------------------------------------------------------------------------------
//
// `mcctl render` stamps the product version (and the config repo's pin) into the Worker's static assets as
// version.json, which `mcctl apply` uploads with worker.js: what this Worker runs. The box's version comes from the
// agent's inventory (each deploy writes it). Read through the ASSETS binding only (never served: ASSET_PATHS),
// cached per isolate (a new upload is a new isolate). v1.5.2: version.json also carries the Worker fingerprint
// (mcctl/fingerprint.py: a sha256 over worker.js, the panel's files and terraform/, never the version label), and
// each deploy records the fingerprint its release expects next to the installed version.
let workerStampCache = null;

async function workerStamp(env) {
  if (workerStampCache) return workerStampCache.stamp;
  let stamp = null;
  try {
    if (env.ASSETS) {
      const resp = await env.ASSETS.fetch(new Request("https://assets.invalid/version.json"));
      const d = resp.ok ? await resp.json() : null;
      if (d && VERSION_TEXT.test(str(d.product))) {
        const pin = d.pin && typeof d.pin === "object" ? d.pin : {};
        stamp = {
          version: d.product,
          fingerprint: FINGERPRINT.test(str(d.fingerprint)) ? d.fingerprint : "",
          pin_sha: COMMIT.test(str(pin.sha)) ? pin.sha : "",
          pin_version: /^v?[0-9A-Za-z.+-]{1,40}$/.test(str(pin.version)) ? pin.version : "",
          repository: /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(str(d.repository)) ? d.repository : "",
        };
      }
    }
  } catch {
    stamp = null; // an older upload without version.json, or no assets: "unknown"
  }
  workerStampCache = { stamp };
  return stamp;
}

// "1.2.10" vs "v1.2.9": -1, 0, 1 (numeric parts; anything after them compares as text), null if either is empty.
function cmpVersion(a, b) {
  if (!a || !b) return null;
  const parts = (v) => String(v).replace(/^v/, "").split(/[.+-]/);
  const x = parts(a);
  const y = parts(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const p = x[i] ?? "";
    const q = y[i] ?? "";
    if (p === q) continue;
    if (/^\d+$/.test(p) && /^\d+$/.test(q)) return Number(p) < Number(q) ? -1 : 1;
    return p < q ? -1 : 1;
  }
  return 0;
}

// The panel's Versions row: server (deployed), Worker (applied), the pin the Worker was applied with; each mismatch
// with its fix. v1.5.2: when both sides carry a Worker fingerprint (`fingerprint`: "same" | "differs"), the Worker
// is out of date only when the fingerprints differ: a release that changed nothing the Worker runs leaves it on
// its older version label, shown neutrally. Without one on either side (a Worker applied or an agent/deploy from
// before v1.5.2: `fingerprint` null) the versions are compared, as before.
function versions(product, stamp) {
  const out = { server: product || null, worker: stamp || null, fingerprint: null, problems: [] };
  const sv = product && product.version;
  const wv = stamp && stamp.version;
  const c = cmpVersion(wv, sv);
  const expected = product && FINGERPRINT.test(str(product.worker_fingerprint)) ? product.worker_fingerprint : "";
  const running = stamp && FINGERPRINT.test(str(stamp.fingerprint)) ? stamp.fingerprint : "";
  if (expected && running) {
    out.fingerprint = expected === running ? "same" : "differs";
    if (c === 1) {
      out.problems.push({ level: "warn", text: `The server is v${sv}, the Worker is v${wv}: deploy (push to the config repo's main, or \`mcctl deploy\`) to update the server.` });
    } else if (out.fingerprint === "differs") {
      out.problems.push({ level: "warn", text: `The Worker is out of date for v${sv} (its code or Terraform changed): run \`mcctl rollout\` or \`mcctl render\` + \`mcctl apply\`.` });
    } else if (c === -1) {
      out.problems.push({ level: "info", text: `Worker v${wv} (unchanged in v${sv}): nothing it runs changed, no apply needed.` });
    }
  } else {
    if (c === -1) out.problems.push({ level: "warn", text: `The Worker is v${wv}, the server is v${sv}: run \`mcctl render\` + \`mcctl apply\` to update the Worker.` });
    if (c === 1) out.problems.push({ level: "warn", text: `The server is v${sv}, the Worker is v${wv}: deploy (push to the config repo's main, or \`mcctl deploy\`) to update the server.` });
    if (c === 0 && product.commit && stamp.pin_sha && product.commit !== stamp.pin_sha) {
      out.problems.push({ level: "info", text: `The server runs commit ${product.commit.slice(0, 12)}, the Worker was applied for the pin ${stamp.pin_sha.slice(0, 12)} (same version v${sv}).` });
    }
  }
  if (product && product.running && cmpVersion(product.running, product.version) !== 0) {
    out.problems.push({ level: "warn", text: `mc-agent still runs v${product.running} code although v${product.version} is installed: restart it (a deploy does: \`mcctl deploy\`).` });
  }
  return out;
}

async function overview(env, pcfg, now, identity) {
  const cfg = config(env);
  const [sample, hist, watchdog, state, audit] = await Promise.all([
    latestSample(env),
    series(env, now, "2h"),
    statusData(env, cfg, now, true),
    kvState(env),
    auditFeed(env, 6),
  ]);
  const inv = state.inventory || null;
  const version = Object.values(watchdog.probes).map((p) => p.version).find(Boolean) || null;
  const vers = versions(inv && inv.product, await workerStamp(env));
  const warns = warnings(sample, hist.points, watchdog, now, inv);
  warns.push(...vers.problems.filter((p) => p.level === "warn"));
  const st = inv && inv.selftest;
  if (st && st.configured && !st.ok) warns.push({ level: "warn", text: `Panel -> Git self-test failed at ${st.error || "an unknown step"}: panel changes can't reach the config repo (Health tab).`, ts: st.ts || undefined });
  return {
    server: {
      title: pcfg.title,
      host: pcfg.server_host,
      flavour: pcfg.flavour,
      minecraft_version: pcfg.minecraft_version,
      version,
      max_players: pcfg.max_players,
      backup_schedule: pcfg.backup_schedule,
      product: (vers.server && vers.server.version) || (vers.worker && vers.worker.version) || null,
    },
    versions: vers,
    selftest: st || null,
    me: identity,
    now,
    sample,
    sample_age_s: sample ? Math.round((now - sample.ts) / 1000) : null,
    history: hist.points,
    watchdog,
    inventory: inv
      ? {
          whitelist: inv.whitelist.length,
          ops: inv.ops.length,
          op_names: inv.ops.map((o) => o.name).filter(Boolean).slice(0, 100),
          bans: inv.bans.length + inv.ip_bans.length,
          plugins: inv.plugins.length,
          disabled_plugins: inv.plugins.filter((p) => p.enabled === false).length,
          received: inv.received || null,
          pending: (inv.pending || []).length,
          rejected: (inv.rejected || []).length,
        }
      : null,
    audit,
    job: (inv && inv.job) || null,
    warnings: warns,
    experience: env.TELEMETRY ? await experienceSummary(env, now).catch(() => null) : null,
  };
}

// P13: the overview's card: the latest minute's verdicts (one row read from the 1-minute table).
async function experienceSummary(env, now) {
  const db = env.TELEMETRY;
  await expSchema(db);
  for (const ms of [now, now - 32 * 86400000]) {
    const t = `exp_1m_${monthOf(ms)}`;
    await expTable(db, t);
    const r = await db.prepare(`SELECT ts, players, p99, max, poor, fair, verdict, backlog FROM ${t} ORDER BY ts DESC LIMIT 1`).first();
    if (!r) continue;
    const backlog = safeJson(r.backlog) || {};
    const headline = r.verdict === "good" ? "Running smoothly" : r.verdict === "fair" ? "Minor lag" : "Lagging";
    return { ts: r.ts, fresh: now - r.ts < 240000, verdict: r.verdict, headline, p99: r.p99, max: r.max, counts: { good: Math.max(0, r.players - r.poor - r.fair), fair: r.fair, poor: r.poor }, backlog_rows: backlog.rows ?? null };
  }
  return null;
}

async function recordAudit(env, now, login, action, args, cmdId, result, ok) {
  await ensureAudit(env.DB);
  await env.DB.prepare(
    "INSERT INTO audit (ts, login, action, args, cmd_id, result, ok) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
  )
    .bind(now, login, action, JSON.stringify(args).slice(0, 500), cmdId, result, ok)
    .run();
}

// --- P10: panel actions ---------------------------------------------------------------------------------------
//
// POST /api/action {action, args}: checked here (shape, destructive actions only on the confirmation host, typed confirmation for a
// restore), audited with the Access identity, then signed for the agent, which checks everything again against its
// own allowlist and runs it through the fixed polkit-allowed units. GET /api/job: the agent's current job.
// GET /api/mojang?name=: Java name -> UUID (the whitelist form checks names before anything changes).

const COUNTDOWN_CHOICES = [0, 10, 60, 300];
const JAVA_NAME = /^[A-Za-z0-9_]{3,16}$/;
const SNAPSHOT = /^[0-9a-f]{8,64}$/;
const IP_TARGET = /^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]+)$/;
// Design §5a + P10 review: restore, plugin switches and stop run only on the confirmation host (handleConfirm);
// v1.2: so does every world library action (never from Discord or in-game: DISCORD_ACTIONS doesn't list them).
const DESTRUCTIVE = new Set(["stop", "plugin", "restore", "world_create", "world_switch", "world_delete"]);

class ActionInputError extends Error {}

function need(cond, message) {
  if (!cond) throw new ActionInputError(message);
}

function countdownArg(v) {
  const c = v === undefined ? 60 : v;
  need(COUNTDOWN_CHOICES.includes(c), `countdown must be one of ${COUNTDOWN_CHOICES.join(", ")} seconds`);
  return c;
}

function oneLine(v, max) {
  return String(v ?? "").replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);
}

// {action, args} as the agent will get them, or an ActionInputError.
function actionArgs(action, a) {
  a = a && typeof a === "object" && !Array.isArray(a) ? a : {};
  switch (action) {
    case "restart":
      return { countdown: countdownArg(a.countdown), backup: a.backup === true };
    case "stop":
      return { countdown: countdownArg(a.countdown), backup: a.backup === true, reason: oneLine(a.reason || "stopped from the panel", 200) };
    case "start":
    case "cancel":
    case "backup":
    case "selftest": // v1.3: read-only (mints a token, reads the config repo); the agent limits it to one a minute
      return {};
    case "lookup": // v1.3: the name check's (GET /api/mojang), refused by postAction
      need(typeof a.name === "string" && JAVA_NAME.test(a.name), "a Java name is 3-16 letters, digits or _");
      return { name: a.name };
    case "whitelist":
      need(a.op === "add" || a.op === "remove", "op must be add or remove");
      need(typeof a.name === "string" && JAVA_NAME.test(a.name), "a Java name is 3-16 letters, digits or _");
      return { op: a.op, name: a.name };
    case "rejected_dismiss":
      need(typeof a.name === "string" && JAVA_NAME.test(a.name), "a Java name is 3-16 letters, digits or _");
      return { name: a.name };
    case "moderate": {
      need(["kick", "ban", "pardon", "ban-ip", "pardon-ip"].includes(a.verb), "verb must be kick, ban, pardon, ban-ip or pardon-ip");
      const ip = a.verb.endsWith("-ip");
      const target = typeof a.target === "string" ? a.target.trim() : "";
      need(ip ? IP_TARGET.test(target) || (a.verb === "ban-ip" && JAVA_NAME.test(target)) : JAVA_NAME.test(target), ip ? "an IP address (or, to ban-ip, a player name)" : "a Java name is 3-16 letters, digits or _");
      const out = { verb: a.verb, target };
      if (["kick", "ban", "ban-ip"].includes(a.verb) && a.reason) out.reason = oneLine(a.reason, 100);
      return out;
    }
    case "plugin":
      need(typeof a.name === "string" && PLUGIN_NAME.test(a.name), "not a plugin name");
      need(typeof a.enabled === "boolean", "enabled must be true or false");
      return { name: a.name, enabled: a.enabled, countdown: countdownArg(a.countdown) };
    case "restore": {
      need(typeof a.snapshot === "string" && SNAPSHOT.test(a.snapshot), "snapshot must be a restic snapshot id");
      // the typed confirmation: the dialog asks for exactly this text
      need(a.confirm === `restore ${a.snapshot.slice(0, 8)}`, `type "restore ${a.snapshot.slice(0, 8)}" to confirm`);
      return { snapshot: a.snapshot, countdown: countdownArg(a.countdown) };
    }
    // v1.2 world library: each typed on the confirmation page ("create <name>", "switch to <name>", "delete <name>")
    case "world_create": {
      need(typeof a.name === "string" && WORLD_NAME.test(a.name), "a world name is 1-32 lowercase letters, digits, _ or -");
      const seed = typeof a.seed === "string" ? a.seed.trim() : "";
      need(WORLD_SEED.test(seed), "the seed must be a whole number (or empty for a random one)");
      need(WORLD_TYPES.includes(a.type), `type must be one of ${WORLD_TYPES.join(", ")}`);
      need(WORLD_PRESETS.includes(a.preset), `preset must be one of ${WORLD_PRESETS.join(", ")}`);
      let border = null;
      if (a.border_radius !== undefined && a.border_radius !== null) {
        need(typeof a.border_radius === "object" && !Array.isArray(a.border_radius), "border_radius maps overworld/nether/end to a radius");
        need(Object.keys(a.border_radius).every((k) => k in WORLD_DIMS), "border_radius has only overworld, nether and end");
        border = {};
        for (const [k, id] of Object.entries(WORLD_DIMS)) {
          const v = a.border_radius[k] ?? null;
          need(v === null || (Number.isInteger(v) && v >= 0 && v <= BORDER_MAX), `a border radius is 0-${BORDER_MAX} blocks or empty`);
          border[id] = v;
        }
      }
      need(a.pregen === undefined || a.pregen === null || typeof a.pregen === "boolean", "pregen must be true or false");
      need(a.confirm === `create ${a.name}`, `type "create ${a.name}" to confirm`);
      return { name: a.name, seed, type: a.type, preset: a.preset, border_radius: border, pregen: typeof a.pregen === "boolean" ? a.pregen : null, countdown: countdownArg(a.countdown) };
    }
    case "world_switch":
      need(typeof a.name === "string" && WORLD_NAME.test(a.name), "a world name is 1-32 lowercase letters, digits, _ or -");
      need(a.confirm === `switch to ${a.name}`, `type "switch to ${a.name}" to confirm`);
      return { name: a.name, countdown: countdownArg(a.countdown) };
    case "world_delete":
      need(typeof a.name === "string" && WORLD_NAME.test(a.name), "a world name is 1-32 lowercase letters, digits, _ or -");
      need(a.confirm === `delete ${a.name}`, `type "delete ${a.name}" to confirm`);
      return { name: a.name };
    default:
      throw new ActionInputError("unknown action");
  }
}

const mojangCache = new Map(); // per isolate: lower-case name -> {at, result}
const MOJANG_TTL_MS = 600000;

// Name -> profile endpoints, tried in order. [V, secondary] api.mojang.com/users/profiles/minecraft/<name>: 200
// {id, name}; 204/404 = no such account (the endpoint mcctl init uses, mcctl/providers/mojang.py). It answers
// Cloudflare Workers' shared egress with 403 (v1.2.2: seen live), so the newer
// api.minecraftservices.com/minecraft/profile/lookup/name/<name> (same answers, 404 = no such account) is the
// fallback. Unauthenticated; only the name is sent.
const MOJANG_LOOKUPS = [
  "https://api.mojang.com/users/profiles/minecraft/",
  "https://api.minecraftservices.com/minecraft/profile/lookup/name/",
];

// v1.3: `lookup {name}` through the agent (signed like every command, answered over its WebSocket within
// AGENT_LOOKUP_WAIT_MS). A definite answer (found / not found) is cached like the Worker's own; anything else
// (agent offline, polling, slow, Mojang unreachable from the box too) returns null and the caller falls back.
const AGENT_LOOKUP_WAIT_MS = 4000;

async function agentLookup(env, name, login, now, waitMs = AGENT_LOOKUP_WAIT_MS) {
  const key = name.toLowerCase();
  const hit = mojangCache.get(key);
  if (hit && now - hit.at < MOJANG_TTL_MS) return hit.result;
  try {
    const resp = await controlStub(env).fetch(new Request("https://control/ask", { method: "POST", body: JSON.stringify({ action: "lookup", args: { name }, login, wait_ms: waitMs }) }));
    const r = await resp.json();
    if (!r || !r.ok || !r.result || !r.result.ok) return null;
    const d = JSON.parse(r.result.output || "null");
    let result = null;
    if (d && d.found === false) result = { found: false, via: "server" };
    if (d && d.found === true && typeof d.uuid === "string" && UUID_RE.test(d.uuid)) {
      result = { found: true, name: typeof d.name === "string" && JAVA_NAME.test(d.name) ? d.name : name, uuid: d.uuid, via: "server" };
    }
    if (!result) return null;
    if (mojangCache.size > 200) mojangCache.delete(mojangCache.keys().next().value);
    mojangCache.set(key, { at: now, result });
    return result;
  } catch {
    return null;
  }
}

async function mojangLookup(name, now, rt) {
  const key = name.toLowerCase();
  const hit = mojangCache.get(key);
  if (hit && now - hit.at < MOJANG_TTL_MS) return hit.result;
  let result = null;
  const failures = [];
  for (const base of MOJANG_LOOKUPS) {
    let resp;
    try {
      resp = await rt.fetch(`${base}${encodeURIComponent(name)}`, {
        headers: { Accept: "application/json", "User-Agent": "smallworld panel" },
      });
    } catch (err) {
      failures.push(`${new URL(base).host}: ${String((err && err.message) || err).slice(0, 40)}`);
      continue;
    }
    if (resp.status === 204 || resp.status === 404) {
      result = { found: false };
      break;
    }
    if (resp.ok) {
      const data = await resp.json().catch(() => null);
      const id = data && typeof data.id === "string" ? data.id.toLowerCase() : "";
      if (/^[0-9a-f]{32}$/.test(id)) {
        const uuid = `${id.slice(0, 8)}-${id.slice(8, 12)}-${id.slice(12, 16)}-${id.slice(16, 20)}-${id.slice(20)}`;
        result = { found: true, name: typeof data.name === "string" && JAVA_NAME.test(data.name) ? data.name : name, uuid };
        break;
      }
      failures.push(`${new URL(base).host}: unexpected answer`);
      continue;
    }
    failures.push(`${new URL(base).host}: HTTP ${resp.status}`);
  }
  // Neither answered: not an error for the panel. The whitelist add still works (the server resolves the name
  // itself, as for Discord's /whitelist add), so the form offers to add it unverified. Not cached.
  if (!result) return { found: null, unverified: true, detail: failures.join("; ").slice(0, 160) };
  if (mojangCache.size > 200) mojangCache.delete(mojangCache.keys().next().value);
  mojangCache.set(key, { at: now, result });
  return result;
}

function sameOrigin(request, host) {
  const origin = request.headers.get("Origin");
  const site = request.headers.get("Sec-Fetch-Site");
  if (origin !== `https://${host}`) return false;
  if (site && site !== "same-origin") return false;
  return (request.headers.get("Content-Type") || "").startsWith("application/json");
}

async function panelApi(request, env, now, url, pcfg, identity, rt) {
  const route = `${request.method} ${url.pathname}`;
  const hasControl = Boolean(env.CONTROL && env.MONITORING);
  if (request.method === "POST" && !sameOrigin(request, pcfg.host)) return json({ error: "cross-site request refused" }, 403);
  if (route === "GET /api/me") {
    return json({
      ...identity,
      reauth_minutes: pcfg.reauth_minutes,
      login_age_s: Math.round(now / 1000 - identity.iat),
      confirm_url: pcfg.confirm_ok ? `https://${pcfg.confirm_host}/` : null,
    });
  }
  if (route === "GET /api/overview") return json(await overview(env, pcfg, now, identity));
  if (route === "GET /api/metrics") return json(await series(env, now, url.searchParams.get("range") || "2h"));
  if (route === "GET /api/logs") return json(await searchLogs(env, url));
  if (route === "GET /api/health") return json(await statusData(env, config(env), now, true));
  if (route === "GET /api/inventory") {
    const inv = (await kvState(env)).inventory || null;
    const sample = await latestSample(env);
    return json({ inventory: inv, online: (sample && sample.players_online) || [], sample_ts: sample ? sample.ts : null });
  }
  if (route === "GET /api/backups") return json(await backups(env, now));
  if (url.pathname.startsWith("/api/experience")) {
    if (!env.TELEMETRY) return json({ error: "experience telemetry is off (server.yml experience.enabled)" }, 404);
    if (route === "GET /api/experience") return json(await experienceOverview(env, now));
    if (route === "GET /api/experience/history") return json(await experienceHistory(env, now, url.searchParams.get("range") || "24h"));
    if (route === "GET /api/experience/leaderboard") return json({ players: await experienceLeaderboard(env) });
    const pm = /^GET \/api\/experience\/player\/([0-9a-f-]{36})$/.exec(route);
    if (pm && UUID_RE.test(pm[1])) {
      const p = await experiencePlayer(env, now, pm[1]);
      return p ? json(p) : json({ error: "unknown player" }, 404);
    }
    return json({ error: "not found" }, 404);
  }
  if (route === "GET /api/audit") {
    const limit = Math.min(500, Math.max(1, Number.parseInt(url.searchParams.get("limit") || "200", 10) || 200));
    return json({ entries: await auditFeed(env, limit) });
  }
  if (route === "GET /api/archives") {
    const day = url.searchParams.get("day") || dayOf(now);
    if (!/^\d{8}$/.test(day) || !env.LOGS || !env.LOGS.list) return json({ day, objects: [] });
    const listed = await env.LOGS.list({ prefix: `journal/${day.slice(0, 4)}/${day.slice(4, 6)}/${day.slice(6, 8)}/`, limit: 500 });
    const objects = (listed.objects || []).map((o) => ({ key: o.key, size: o.size, uploaded: o.uploaded ? new Date(o.uploaded).toISOString() : null }));
    return json({ day, objects });
  }
  if (route === "GET /api/mojang") {
    const name = url.searchParams.get("name") || "";
    if (!JAVA_NAME.test(name)) return json({ error: "a Java name is 3-16 letters, digits or _" }, 400);
    // v1.3: the box asks Mojang first (Mojang refuses Cloudflare's shared egress); this Worker's own lookup when
    // the agent is offline, slow or can't tell; "unverified" stays the last resort.
    const viaAgent = hasControl ? await agentLookup(env, name, identity.email, now) : null;
    if (viaAgent) return json(viaAgent);
    try {
      return json(await mojangLookup(name, now, rt));
    } catch (err) {
      return json({ error: `Mojang lookup failed (${String((err && err.message) || err).slice(0, 80)})` }, 502);
    }
  }
  if (!hasControl) return json({ error: "the live console needs panel.mode cloudflare" }, 404);
  const stub = controlStub(env);
  if (route === "GET /api/console") {
    const after = Number(url.searchParams.get("after")) || 0;
    const live = await (await stub.fetch(new Request(`https://control/console?after=${after}`))).json();
    if (!after) live.backlog = await consoleBacklog(env);
    return json(live);
  }
  if (route === "POST /api/console/live") {
    return stub.fetch(new Request("https://control/live", { method: "POST", body: JSON.stringify({ login: identity.email }) }));
  }
  if (route === "POST /api/console/command") {
    const { data, error } = await readJson(request, 4096);
    if (error) return error;
    const command = data && typeof data.command === "string" ? data.command.slice(0, 300) : "";
    if (!command.trim()) return json({ error: "command required" }, 400);
    const args = { command };
    const resp = await stub.fetch(
      new Request("https://control/enqueue", { method: "POST", body: JSON.stringify({ action: "console", args, login: identity.email }) }),
    );
    const body = await resp.json();
    await recordAudit(env, now, identity.email, "console", args, body.id || null, body.id ? "queued" : String(body.error || "refused"), body.id ? null : 0);
    return json(body, resp.status);
  }
  if (route === "GET /api/job") {
    const { job } = await (await stub.fetch(new Request("https://control/job"))).json();
    return json({ job, now });
  }
  if (route === "POST /api/action") return postAction(request, env, now, identity, pcfg, false);
  const cmd = /^GET \/api\/commands\/([0-9a-f]{24})$/.exec(route);
  if (cmd) return commandResult(env, stub, cmd[1]);
  return json({ error: "not found" }, 404);
}

async function commandResult(env, stub, id) {
  const { result } = await (await stub.fetch(new Request(`https://control/result?id=${id}`))).json();
  if (result) {
    await ensureAudit(env.DB);
    await env.DB.prepare("UPDATE audit SET result = ?1, ok = ?2 WHERE cmd_id = ?3 AND result IN ('queued', 'queued (confirmed)', 'queued (discord)')")
      .bind(result.ok ? "ok" : (result.error || "failed").slice(0, 300), result.ok ? 1 : 0, id)
      .run();
  }
  return json({ result });
}

// One action: shape (actionArgs), host rules (P10 review: destructive ones only on the confirmation host, with a
// token of that host's Access app no older than reauth_minutes; never on the panel host), audit, signed command.
async function postAction(request, env, now, identity, pcfg, onConfirmHost) {
  const { data, error } = await readJson(request, 4096);
  if (error) return error;
  const action = data && typeof data.action === "string" ? data.action : "";
  let args;
  try {
    args = actionArgs(action, data && data.args);
  } catch (err) {
    if (!(err instanceof ActionInputError)) throw err;
    return json({ error: err.message }, 400);
  }
  if (action === "lookup") return json({ error: "lookup is the name check's (GET /api/mojang), not an action" }, 400);
  if (DESTRUCTIVE.has(action) && !onConfirmHost) {
    await recordAudit(env, now, identity.email, action, args, null, "refused: destructive actions are confirmed on the confirmation host", 0);
    return json(
      {
        error: `${action} is confirmed in a separate sign-in window (a short session with a one-time PIN)`,
        confirm: true,
        confirm_url: pcfg.confirm_ok ? `https://${pcfg.confirm_host}/` : null,
      },
      403,
    );
  }
  if (onConfirmHost && !DESTRUCTIVE.has(action)) return json({ error: "only stop, plugin switches, restores and world changes are confirmed here" }, 400);
  const age = Math.round(now / 1000 - identity.iat);
  if (onConfirmHost && age > pcfg.reauth_minutes * 60) {
    await recordAudit(env, now, identity.email, action, args, null, `refused: confirmation login ${Math.round(age / 60)} min old (limit ${pcfg.reauth_minutes} min)`, 0);
    return json({ error: `${action} needs a fresh sign-in (your confirmation login is ${Math.round(age / 60)} min old; the limit is ${pcfg.reauth_minutes} min)`, reauth: true, reauth_minutes: pcfg.reauth_minutes }, 401);
  }
  const stub = controlStub(env);
  const resp = await stub.fetch(
    new Request("https://control/enqueue", { method: "POST", body: JSON.stringify({ action, args, login: identity.email }) }),
  );
  const body = await resp.json();
  await recordAudit(env, now, identity.email, action, args, body.id || null, body.id ? (onConfirmHost ? "queued (confirmed)" : "queued") : String(body.error || "refused"), body.id ? null : 0);
  if (body.id && ["restart", "stop", "start", "plugin", "restore", "world_create", "world_switch", "world_delete"].includes(action)) {
    await annotate(env, now, action, `${action}${args.name ? ` ${args.name}` : ""} by ${identity.email}`);
  }
  return json(body, resp.status);
}

// v1.2: seeds.json = the world library's seed catalogue, verified on the pinned build (scripts/seed_check.py)
const ASSET_PATHS = /^\/(?:index\.html|panel\.css|panel\.js|seeds\.json|favicon\.svg|fonts\/[a-z0-9-]+\.woff2)$/;

async function handlePanel(request, env, now, rt, url, pcfg) {
  if (!pcfg.ok) return denied(request); // misconfigured: fail closed
  const identity = await verifyAccess(request, pcfg, now, rt);
  if (!identity) return denied(request);
  if (url.pathname.startsWith("/api/")) {
    const resp = await panelApi(request, env, now, url, pcfg, identity, rt);
    return withPanelHeaders(resp, { "Cache-Control": "no-store" });
  }
  if (request.method !== "GET" && request.method !== "HEAD") return withPanelHeaders(json({ error: "method not allowed" }, 405));
  const path = url.pathname === "/" ? "/index.html" : url.pathname;
  if (!ASSET_PATHS.test(path) || !env.ASSETS) return withPanelHeaders(json({ error: "not found" }, 404));
  const asset = await env.ASSETS.fetch(new Request(`https://${pcfg.host}${path}`, { method: request.method }));
  const cache = path.startsWith("/fonts/") ? "private, max-age=86400" : "no-cache";
  return withPanelHeaders(asset, { "Cache-Control": cache });
}

// P10 review: <panel>-confirm.<zone>. Its own Access application (session = reauth_minutes, one-time PIN only)
// and AUD; only the confirmation page (confirm.html/confirm.js + the panel's CSS and fonts), POST /api/action for
// destructive actions, the command result and the current job. The panel host opens it in a new window.
const CONFIRM_ASSETS = /^\/(?:confirm\.html|confirm\.js|panel\.css|favicon\.svg|fonts\/[a-z0-9-]+\.woff2)$/;

async function handleConfirm(request, env, now, rt, url, pcfg) {
  if (!pcfg.confirm_ok) return denied(request); // no confirmation app configured: destructive actions can't run
  const identity = await verifyAccess(request, { ...pcfg, aud: pcfg.confirm_aud }, now, rt);
  if (!identity) return denied(request);
  if (url.pathname.startsWith("/api/")) {
    const route = `${request.method} ${url.pathname}`;
    let resp;
    if (request.method === "POST" && !sameOrigin(request, pcfg.confirm_host)) {
      resp = json({ error: "cross-site request refused" }, 403);
    } else if (route === "GET /api/me") {
      resp = json({ ...identity, reauth_minutes: pcfg.reauth_minutes, login_age_s: Math.round(now / 1000 - identity.iat), panel_url: `https://${pcfg.host}/` });
    } else if (!env.CONTROL || !env.MONITORING) {
      resp = json({ error: "panel actions need panel.mode cloudflare" }, 404);
    } else if (route === "POST /api/action") {
      resp = await postAction(request, env, now, identity, pcfg, true);
    } else if (route === "GET /api/job") {
      const { job } = await (await controlStub(env).fetch(new Request("https://control/job"))).json();
      resp = json({ job, now });
    } else {
      const cmd = /^GET \/api\/commands\/([0-9a-f]{24})$/.exec(route);
      resp = cmd ? await commandResult(env, controlStub(env), cmd[1]) : json({ error: "not found" }, 404);
    }
    return withPanelHeaders(resp, { "Cache-Control": "no-store" });
  }
  if (request.method !== "GET" && request.method !== "HEAD") return withPanelHeaders(json({ error: "method not allowed" }, 405));
  const path = url.pathname === "/" ? "/confirm.html" : url.pathname;
  if (!CONFIRM_ASSETS.test(path) || !env.ASSETS) return withPanelHeaders(json({ error: "not found" }, 404));
  const asset = await env.ASSETS.fetch(new Request(`https://${pcfg.host}${path}`, { method: request.method }));
  const cache = path.startsWith("/fonts/") ? "private, max-age=86400" : "no-cache";
  return withPanelHeaders(asset, { "Cache-Control": cache });
}

// --- P14: Discord slash commands (HTTP interactions) ------------------------------------------------------------
//
// POST /discord/interactions on the ingest host (hb.<host>.<zone>; not behind Access: Discord can't sign in there).
// Discord's docs (Interactions: Overview, Receiving and Responding; checked 2026-11-02): every request carries
// X-Signature-Ed25519 (hex) over X-Signature-Timestamp + the raw body, made with the application's key whose public
// half (hex) is in the plain-text binding DISCORD; a request that fails verification must get 401 (Discord sends bad
// signatures on purpose and removes the endpoint if one is accepted). PING (type 1) is answered PONG (type 1). A
// command (type 2) is answered within 3 s with CHANNEL_MESSAGE_WITH_SOURCE (4; flags 64 = only the caller sees it);
// the interaction token (valid 15 min) edits that reply later: PATCH /webhooks/{app}/{token}/messages/@original (no
// bot token: the Worker holds none).
//
// Checks, in this order, each one failing closed: the config (server.yml discord.*: ids and the public key, nothing
// secret; incomplete = 404 for everything), the body size, the signature (WebCrypto Ed25519), the timestamp (within
// DISCORD_SKEW_S of the Worker's clock), PING, the application id, then the allowlists (guild, channel, user: an admin
// in discord.user_ids, or a helper by id or by the role discord.helper_role_id in the signed payload's member.roles,
// limited to DISCORD_HELPER_COMMANDS and audited as "discord:<id> (helper)") and the
// replay guard (each interaction id once: D1 table discord_seen) and the per-user rate limits. Read commands answer
// from D1 (the agent's samples and inventory, the watchdog state, P13's experience tables): no server round trip.
// Actions: only `/backup now` and `/whitelist add <name>`, i.e. the agent's existing `backup` and `whitelist` actions,
// shaped by actionArgs like the panel's, signed by the Control object, audited (login discord:<user id>); the agent
// checks them again. Never stop/restart/restore/plugins/console from Discord (DISCORD_ACTIONS is the whole list).
// The agent's result is edited into the reply (waitUntil, polling the Control object for <= DISCORD_POLL.max_ms).

const DISCORD_PATH = "/discord/interactions";
const DISCORD_API = "https://discord.com/api/v10";
const DISCORD_MAX_BODY = 32768;
const DISCORD_SKEW_S = 300; // a signed request older (or newer) than this is refused, so a replay window is bounded
const DISCORD_SEEN_KEEP_MS = 3600000; // > the skew: an interaction id is remembered for longer than it could be valid
const DISCORD_RATE = { per_minute: 10, actions_per_10_minutes: 3 };
// the follow-up polls for at most max_ms in total (12 x 2 s), well inside waitUntil's 30 s with the D1/DO calls and
// the PATCH that follow
const DISCORD_POLL = { every_ms: 2000, max_ms: 24000 };
// refusals: each user's first few per minute are audited one by one, the rest only counted (per isolate) and written
// as one aggregate row, so a flood of refused (validly signed) interactions can't spend the D1 write budget
const DISCORD_REFUSAL_AUDIT = { per_user_minute: 5, max_users: 1000 };
const SNOWFLAKE = /^\d{17,20}$/;
// The commands this Worker answers: the same names as mcctl/discordcmds.py COMMANDS (what setup registers);
// tests/test_discord.py checks that the two lists agree.
const DISCORD_COMMANDS = ["status", "players", "lag", "perf", "backups", "backup", "whitelist", "rejected"];
// The only actions Discord may start: "<command> <subcommand>" -> the agent's action (COMMAND_ACTIONS).
const DISCORD_ACTIONS = { "backup now": "backup", "whitelist add": "whitelist" };
// v1.0.1 helper tier (server.yml discord.helper_user_ids / helper_role_id): what a helper may run, as "<command>[
// <subcommand>]". Everything else (today: /backup now) stays with the admins (discord.user_ids).
const DISCORD_HELPER_COMMANDS = ["status", "players", "lag", "perf", "backups", "rejected", "whitelist list", "whitelist add"];
const DISCORD_SEEN_SCHEMA =
  "CREATE TABLE IF NOT EXISTS discord_seen (id TEXT PRIMARY KEY, ts INTEGER NOT NULL, user TEXT NOT NULL) WITHOUT ROWID";
const PERF_RANGES = ["24h", "7d", "30d"];

function discordConfig(env) {
  let raw;
  try {
    raw = JSON.parse(env.DISCORD || "null");
  } catch {
    raw = null;
  }
  if (!raw || typeof raw !== "object") return null;
  const ids = (v) => (Array.isArray(v) ? v.map(String).filter((x) => SNOWFLAKE.test(x)) : []);
  const cfg = {
    app_id: String(raw.app_id || ""),
    public_key: String(raw.public_key || "").toLowerCase(),
    guild_id: String(raw.guild_id || ""),
    channel_ids: ids(raw.channel_ids),
    user_ids: ids(raw.user_ids),
    helper_user_ids: ids(raw.helper_user_ids),
    helper_role_id: SNOWFLAKE.test(String(raw.helper_role_id || "")) ? String(raw.helper_role_id) : "",
    ephemeral: raw.ephemeral !== false,
    title: String(raw.title || "Minecraft server").slice(0, 80),
  };
  cfg.ok = SNOWFLAKE.test(cfg.app_id) && /^[0-9a-f]{64}$/.test(cfg.public_key) && SNOWFLAKE.test(cfg.guild_id) &&
    cfg.channel_ids.length > 0 && cfg.user_ids.length > 0;
  return cfg;
}

function hexBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = Number.parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

let discordKey = { hex: "", key: null }; // per isolate: the imported public key
let discordRefusals = new Map(); // per isolate: login -> { minute, n, dropped } (DISCORD_REFUSAL_AUDIT)

// The signed message: the timestamp header's bytes (ASCII digits, checked) followed by the body's raw bytes exactly
// as received. No text decoding/encoding in between: a BOM or invalid UTF-8 would be changed by a round trip, and
// the signature must cover what is then parsed.
function discordMessage(timestamp, body) {
  const out = new Uint8Array(timestamp.length + body.byteLength);
  for (let k = 0; k < timestamp.length; k++) out[k] = timestamp.charCodeAt(k);
  out.set(body, timestamp.length);
  return out;
}

async function verifyDiscord(publicKeyHex, signature, timestamp, body) {
  if (!/^[0-9a-fA-F]{128}$/.test(signature || "") || !/^\d{1,12}$/.test(timestamp || "")) return false;
  if (!(body instanceof Uint8Array)) return false; // raw bytes only (never a decoded string)
  try {
    if (discordKey.hex !== publicKeyHex) {
      // Ed25519 (Secure Curves API; Workers and Node >= 22 both have it): the raw 32-byte public key
      const key = await crypto.subtle.importKey("raw", hexBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]);
      discordKey = { hex: publicKeyHex, key };
    }
    return await crypto.subtle.verify("Ed25519", discordKey.key, hexBytes(signature), discordMessage(timestamp, body));
  } catch {
    return false;
  }
}

// A refusal's audit row, capped per user and minute (DISCORD_REFUSAL_AUDIT): only allowlisted users pass the checks
// before the replay guard, so anyone else in the guild (or a guild admin adding the app elsewhere) can make Discord
// deliver validly signed requests that are refused. The rest of a minute's refusals become one aggregate row.
async function auditRefusal(env, now, login, action, args, result) {
  const minute = Math.floor(now / 60000);
  let r = discordRefusals.get(login);
  if (!r || r.minute !== minute) {
    if (r && r.dropped > 0) {
      await recordAudit(env, now, login, "discord (refusals)", { minute: r.minute * 60000, dropped: r.dropped }, null,
        `refused: ${r.dropped} more in that minute (not audited one by one)`, 0);
    }
    if (!r && discordRefusals.size >= DISCORD_REFUSAL_AUDIT.max_users) discordRefusals = new Map();
    r = { minute, n: 0, dropped: 0 };
    discordRefusals.set(login, r);
  }
  r.n += 1;
  if (r.n <= DISCORD_REFUSAL_AUDIT.per_user_minute) return recordAudit(env, now, login, action, args, null, result, 0);
  r.dropped += 1;
}

// Discord markdown: names like Felix_2011 would turn italic; mentions are disabled separately (allowed_mentions).
function md(text) {
  return String(text ?? "").replace(/[\\*_~`|>#[\]()-]/g, (c) => `\\${c}`);
}

function ago(ms) {
  if (typeof ms !== "number" || ms < 0) return "never";
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function discordReply(cfg, content, extra = {}) {
  return json({
    type: 4,
    data: { content: String(content).slice(0, 1900), flags: cfg.ephemeral ? 64 : 0, allowed_mentions: { parse: [] }, ...extra },
  });
}

function discordOptions(data) {
  // {sub, opts}: a subcommand (type 1) and the options under it, or the command's own options
  const top = Array.isArray(data && data.options) ? data.options : [];
  const sub = top.find((o) => o && o.type === 1);
  const list = sub ? (Array.isArray(sub.options) ? sub.options : []) : top;
  const opts = {};
  for (const o of list) if (o && typeof o.name === "string" && ["string", "number", "boolean"].includes(typeof o.value)) opts[o.name] = o.value;
  return { sub: sub ? String(sub.name) : "", opts };
}

async function discordSeen(env, now, id, user) {
  const db = env.DB;
  let known = createdTables.get(db);
  if (!known) {
    known = new Set();
    createdTables.set(db, known);
  }
  if (!known.has("discord_seen")) {
    await db.prepare(DISCORD_SEEN_SCHEMA).run();
    known.add("discord_seen");
  }
  const r = await db.prepare("INSERT INTO discord_seen (id, ts, user) VALUES (?1, ?2, ?3) ON CONFLICT(id) DO NOTHING").bind(id, now, user).run();
  if (!(r && r.meta && r.meta.changes)) return { replay: true };
  await db.prepare("DELETE FROM discord_seen WHERE ts < ?1").bind(now - DISCORD_SEEN_KEEP_MS).run();
  const c = await db.prepare("SELECT COUNT(*) AS n FROM discord_seen WHERE user = ?1 AND ts > ?2").bind(user, now - 60000).first();
  return { replay: false, recent: Number((c && c.n) || 0) };
}

async function handleDiscord(request, env, now, rt) {
  const cfg = discordConfig(env);
  if (!cfg || !cfg.ok) return json({ error: "not found" }, 404);
  if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
  const buf = await readLimited(request, DISCORD_MAX_BODY);
  if (!buf) return json({ error: "body too large" }, 413);
  const timestamp = request.headers.get("X-Signature-Timestamp") || "";
  const signature = request.headers.get("X-Signature-Ed25519") || "";
  if (!(await verifyDiscord(cfg.public_key, signature, timestamp, buf))) return json({ error: "invalid request signature" }, 401);
  if (Math.abs(now / 1000 - Number(timestamp)) > DISCORD_SKEW_S) return json({ error: "stale request" }, 401);
  let i;
  try {
    // the verified bytes, strictly: invalid UTF-8 throws, a BOM stays a character (and JSON.parse refuses it)
    i = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(buf));
  } catch {
    return json({ error: "body must be JSON" }, 400);
  }
  if (!i || typeof i !== "object") return json({ error: "body must be JSON" }, 400);
  if (i.type === 1) return json({ type: 1 }); // PING -> PONG
  if (i.type !== 2) return json({ error: "unsupported interaction type" }, 400);
  if (String(i.application_id || "") !== cfg.app_id) return json({ error: "wrong application" }, 401);
  const user = String((i.member && i.member.user && i.member.user.id) || (i.user && i.user.id) || "");
  const channel = String(i.channel_id || (i.channel && i.channel.id) || "");
  const guild = String(i.guild_id || "");
  const name = String((i.data && i.data.name) || "");
  const { sub, opts } = discordOptions(i.data);
  const what = `${name}${sub ? ` ${sub}` : ""}`.slice(0, 60);
  const tier = discordTier(cfg, user, i.member);
  const login = `discord:${SNOWFLAKE.test(user) ? user : "?"}${tier === "helper" ? " (helper)" : ""}`;
  let refusal = null;
  if (!guild) refusal = "direct messages aren't allowed";
  else if (guild !== cfg.guild_id) refusal = "not allowed in this server";
  else if (!cfg.channel_ids.includes(channel)) refusal = "not allowed in this channel";
  else if (!tier) refusal = "you are not on this server's Discord allowlist";
  else if (tier === "helper" && DISCORD_COMMANDS.includes(name) && !DISCORD_HELPER_COMMANDS.includes(what)) refusal = `/${what} is for the server's admins only`;
  if (refusal) {
    // audited, capped per user and minute (auditRefusal): Discord delivers whatever anyone in a guild sends
    await auditRefusal(env, now, login, `discord ${what}`, { guild, channel }, `refused: ${refusal}`);
    return discordReply({ ...cfg, ephemeral: true }, `Not allowed: ${refusal}.`);
  }
  if (!/^\d{17,20}$/.test(String(i.id || ""))) return json({ error: "bad interaction id" }, 400);
  const seen = await discordSeen(env, now, String(i.id), user);
  if (seen.replay) return json({ error: "replayed interaction" }, 409);
  if (seen.recent > DISCORD_RATE.per_minute) {
    await auditRefusal(env, now, login, `discord ${what}`, opts, "refused: rate limit");
    return discordReply({ ...cfg, ephemeral: true }, `Slow down: at most ${DISCORD_RATE.per_minute} commands a minute.`);
  }
  if (!DISCORD_COMMANDS.includes(name)) return discordReply({ ...cfg, ephemeral: true }, "Unknown command (run `mcctl discord sync` to update them).");
  const action = DISCORD_ACTIONS[what];
  if (action) return discordAction(env, now, rt, cfg, i, login, action, opts, what);
  try {
    return discordReply(cfg, await discordRead(env, now, cfg, name, sub, opts));
  } catch (err) {
    console.error("discord command failed", name, String((err && err.message) || err).slice(0, 200));
    return discordReply({ ...cfg, ephemeral: true }, "That didn't work (the Worker's storage refused); try again in a minute.");
  }
}

// "admin" (discord.user_ids), "helper" (discord.helper_user_ids, or the member carries discord.helper_role_id: the
// roles come from the signed payload, member.roles = the guild member's role ids) or null. Only in a guild: a DM
// has no member (and is refused before this matters).
function discordTier(cfg, user, member) {
  if (!SNOWFLAKE.test(user)) return null;
  if (cfg.user_ids.includes(user)) return "admin";
  if (cfg.helper_user_ids.includes(user)) return "helper";
  const roles = member && Array.isArray(member.roles) ? member.roles.map(String) : [];
  if (cfg.helper_role_id && roles.includes(cfg.helper_role_id)) return "helper";
  return null;
}

async function discordAction(env, now, rt, cfg, i, login, action, opts, what) {
  let args;
  try {
    args = actionArgs(action, action === "whitelist" ? { op: "add", name: typeof opts.name === "string" ? opts.name.trim() : "" } : {});
  } catch (err) {
    if (!(err instanceof ActionInputError)) throw err;
    return discordReply({ ...cfg, ephemeral: true }, `Refused: ${err.message}.`);
  }
  if (!env.CONTROL || !env.MONITORING) return discordReply({ ...cfg, ephemeral: true }, "Actions need panel.mode cloudflare.");
  await ensureAudit(env.DB);
  // the rate check and the slot are one statement (atomic in D1/SQLite): the audit row is inserted only while fewer
  // than the limit of Discord actions (rows with a cmd_id) exist in the last 10 minutes, so two concurrent requests
  // can't both pass a count taken before either wrote. The row gets the real command id (or the refusal) below.
  const slot = await env.DB.prepare(
    "INSERT INTO audit (ts, login, action, args, cmd_id, result, ok) SELECT ?1, ?2, ?3, ?4, 'reserved', 'queued (discord)', NULL " +
      "WHERE (SELECT COUNT(*) FROM audit WHERE login LIKE 'discord:%' AND cmd_id IS NOT NULL AND ts > ?5) < ?6 RETURNING id",
  )
    .bind(now, login, action, JSON.stringify(args).slice(0, 500), now - 600000, DISCORD_RATE.actions_per_10_minutes)
    .first();
  if (!slot) {
    await auditRefusal(env, now, login, action, args, "refused: rate limit (Discord actions)");
    return discordReply({ ...cfg, ephemeral: true }, `Slow down: at most ${DISCORD_RATE.actions_per_10_minutes} actions in 10 minutes from Discord.`);
  }
  const settle = (cmdId, result, ok) =>
    env.DB.prepare("UPDATE audit SET cmd_id = ?1, result = ?2, ok = ?3 WHERE id = ?4").bind(cmdId, result, ok, slot.id).run();
  let out;
  try {
    const stub = controlStub(env);
    const resp = await stub.fetch(new Request("https://control/enqueue", { method: "POST", body: JSON.stringify({ action, args, login }) }));
    out = await resp.json();
  } catch (err) {
    await settle(null, "refused: the command queue failed", 0); // frees the slot
    throw err;
  }
  await settle(out.id || null, out.id ? "queued (discord)" : String(out.error || "refused").slice(0, 300), out.id ? null : 0);
  if (!out.id) return discordReply({ ...cfg, ephemeral: true }, `Refused: ${md(out.error || "the command queue said no")}.`);
  const label = action === "backup" ? "Backup" : `Whitelist add ${md(args.name)}`;
  const token = typeof i.token === "string" && /^[A-Za-z0-9._-]{20,500}$/.test(i.token) ? i.token : "";
  if (token && rt.waitUntil) rt.waitUntil(discordFollowUp(env, rt, cfg, token, out.id, label).catch(() => {}));
  return discordReply(cfg, `${label}: queued for the server (command ${out.id.slice(0, 8)}); the result follows here.`);
}

// After the reply: wait for the agent's result and edit it into the reply (first result wins, like the panel's).
async function discordFollowUp(env, rt, cfg, token, id, label) {
  const stub = controlStub(env);
  let result = null;
  for (let waited = 0; waited < DISCORD_POLL.max_ms && !result; waited += DISCORD_POLL.every_ms) {
    await rt.sleep(DISCORD_POLL.every_ms);
    result = (await (await commandResult(env, stub, id)).json()).result;
  }
  const text = !result
    ? `${label}: queued (command ${id.slice(0, 8)}); no result within ${DISCORD_POLL.max_ms / 1000} s: see the panel's audit.`
    : result.ok
      ? `${label}: done. ${md(String(result.output || "").slice(0, 600))}`
      : `${label}: failed: ${md(String(result.error || "").slice(0, 600))}`;
  await rt.fetch(`${DISCORD_API}/webhooks/${cfg.app_id}/${token}/messages/@original`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: text.slice(0, 1900), allowed_mentions: { parse: [] } }),
  });
}

async function discordRead(env, now, cfg, name, sub, opts) {
  switch (name) {
    case "status":
      return discordStatus(env, now, cfg);
    case "players": {
      const s = await latestSample(env);
      if (!s || now - s.ts > AGENT_FRESH_MS) return s ? `No fresh sample from the server (last ${ago(now - s.ts)}).` : "No sample from the server yet.";
      const names = s.players_online || [];
      const max = typeof s.max_players === "number" ? `/${s.max_players}` : "";
      return names.length ? `**${names.length}${max} online:** ${names.map(md).join(", ")}` : `Nobody online (0${max}).`;
    }
    case "lag":
      return discordLag(env, now, typeof opts.player === "string" ? opts.player.trim() : "");
    case "perf":
      return discordPerf(env, now, PERF_RANGES.includes(opts.range) ? opts.range : "24h");
    case "backups": {
      const b = await backups(env, now);
      const list = b.source === "restic" ? b.restic.map((x) => ({ id: x.short_id || String(x.id || "").slice(0, 8), ts: x.ts, added: x.added })) : b.snapshots;
      const recent = [...list].sort((a, c) => (c.ts || 0) - (a.ts || 0)).slice(0, 5);
      const lines = [`**Backups:** last OK ${ago(b.age_s === null ? -1 : b.age_s * 1000)}${b.running_since ? " (one is running now)" : ""}`];
      for (const x of recent) lines.push(`- \`${md(x.id)}\` ${x.ts ? new Date(x.ts).toISOString().slice(0, 16).replace("T", " ") : "?"} UTC${x.added ? ` (+${md(x.added)})` : ""}`);
      if (b.failures.length) lines.push(`Last failure: ${md(b.failures[0].msg.slice(0, 200))}`);
      return lines.join("\n");
    }
    case "whitelist": {
      const inv = (await kvState(env)).inventory;
      if (!inv) return "No whitelist from the server yet.";
      const names = inv.whitelist.map((w) => w.name).filter(Boolean).sort((a, c) => a.localeCompare(c));
      const lines = [`**Whitelist (${names.length}):** ${names.slice(0, 80).map(md).join(", ")}${names.length > 80 ? ", …" : ""}`];
      if ((inv.pending || []).length) lines.push(`Not in Git yet: ${inv.pending.map((p) => `${p.op === "remove" ? "-" : "+"}${md(p.name)}`).join(", ")}`);
      return lines.join("\n");
    }
    case "rejected": {
      const inv = (await kvState(env)).inventory;
      const rej = (inv && inv.rejected) || [];
      if (!rej.length) return "No rejected logins.";
      return [`**Rejected logins (${rej.length}):**`, ...rej.slice(0, 15).map((r) => `- ${md(r.name)}: ${r.count || 1}×, last ${typeof r.last === "number" ? ago(now - r.last) : "?"}`), "Add one with `/whitelist add name`."].join("\n");
    }
    default:
      return "Unknown command.";
  }
}

async function discordStatus(env, now, cfg) {
  const [s, w] = await Promise.all([latestSample(env), statusData(env, config(env), now)]);
  const fresh = s && now - s.ts <= AGENT_FRESH_MS;
  const lines = [];
  if (w.admin_stopped) lines.push(`**${md(cfg.title)}**: stopped by the admin.`);
  else if (fresh) {
    const n = (s.players_online || []).length || s.players || 0;
    const tps = typeof s.tps === "number" ? ` · TPS ${s.tps.toFixed(1)}` : "";
    const mspt = typeof s.mspt === "number" ? ` · MSPT ${s.mspt.toFixed(1)}` : "";
    lines.push(`**${md(cfg.title)}**: up, ${n} online${tps}${mspt}`);
  } else lines.push(`**${md(cfg.title)}**: ${s ? `no sample from the server for ${ago(now - s.ts).replace(" ago", "")}` : "no sample from the server yet"}`);
  const probes = Object.entries(w.probes).map(([f, p]) => `${f} ${p.ok === null ? "?" : p.ok ? "ok" : "DOWN"}`).join(", ");
  const checks = Object.entries(w.checks).map(([k, c]) => `${k} ${c.state}`).join(", ");
  lines.push(`Watchdog: ${probes || "no probes"} · ${checks}${w.maintenance ? ` · maintenance until ${w.maintenance.until}` : ""}`);
  if (env.TELEMETRY) {
    const e = await experienceSummary(env, now).catch(() => null);
    if (e && e.fresh) lines.push(`Experience: ${e.headline} (slowest 1% of ticks ${Math.round(e.p99 ?? 0)} ms; ${e.counts.poor} lagging, ${e.counts.fair} minor)`);
  }
  return lines.join("\n");
}

async function discordLag(env, now, player) {
  if (!env.TELEMETRY) return "Experience telemetry is off (server.yml experience.enabled).";
  const { row, ts } = await latestExperienceRow(env.TELEMETRY, now);
  if (!row || now - ts > 240000) return "No analysis right now (no fresh minute from the server).";
  const v = row.verdicts || { server: {}, players: {} };
  if (player) {
    if (!/^[A-Za-z0-9_]{1,16}$/.test(player)) return "A Java name is 3-16 letters, digits or _.";
    const p = row.pl.find((x) => String(x.n).toLowerCase() === player.toLowerCase());
    if (!p) return `${md(player)} isn't online now.`;
    const pv = v.players[p.u] || {};
    const reasons = (pv.reasons || []).slice(0, 4).map((r) => `- ${md(r)}`);
    return [`**${md(p.n)}**: ${md(pv.headline || pv.verdict || "no verdict")} (ping ${p.ping ?? "?"} ms)`, ...reasons].join("\n");
  }
  const sv = v.server || {};
  const lines = [`**Server:** ${md(sv.headline || sv.verdict || "no verdict")} (slowest 1% of ticks ${Math.round(row.srv.p99 ?? 0)} ms)`];
  for (const r of (sv.reasons || []).slice(0, 3)) lines.push(`- ${md(r)}`);
  const bad = row.pl.filter((p) => (v.players[p.u] || {}).verdict && v.players[p.u].verdict !== "good");
  for (const p of bad.slice(0, 8)) lines.push(`${md(p.n)}: ${md(v.players[p.u].headline || v.players[p.u].verdict)}`);
  if (!bad.length && row.pl.length) lines.push(`All ${row.pl.length} online players are fine.`);
  return lines.join("\n");
}

async function discordPerf(env, now, range) {
  const lines = [];
  if (env.TELEMETRY) {
    const h = await experienceHistory(env, now, range);
    const pts = h.points.filter((p) => typeof p.p99 === "number");
    if (pts.length) {
      const avg = (k) => pts.reduce((a, p) => a + (p[k] || 0), 0) / pts.length;
      const worst = pts.reduce((a, p) => (p.p99 > a.p99 ? p : a), pts[0]);
      const tps = pts.filter((p) => typeof p.tps === "number").map((p) => p.tps);
      lines.push(`**Last ${range}:** tick p99 avg ${avg("p99").toFixed(1)} ms, worst ${worst.p99.toFixed(1)} ms (${new Date(worst.ts).toISOString().slice(0, 16).replace("T", " ")} UTC)` + (tps.length ? ` · TPS min ${Math.min(...tps).toFixed(1)}` : ""));
      const poor = pts.reduce((a, p) => a + (p.poor || 0), 0);
      lines.push(`Lagging player-minutes: ${poor} · data gaps: ${h.gaps.length}`);
      return lines.join("\n");
    }
  }
  const s = await series(env, now, range);
  const pts = s.points.filter((p) => typeof p.tps === "number");
  if (!pts.length) return `No data for the last ${range}.`;
  const tps = pts.map((p) => p.tps);
  const mspt = pts.filter((p) => typeof p.mspt === "number").map((p) => p.mspt);
  return `**Last ${range}:** TPS min ${Math.min(...tps).toFixed(1)}, avg ${(tps.reduce((a, b) => a + b, 0) / tps.length).toFixed(1)}` + (mspt.length ? ` · MSPT max ${Math.max(...mspt).toFixed(1)}` : "");
}

// --- HTTP ---------------------------------------------------------------------------------------------

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

async function readBody(request) {
  const text = await request.text();
  return text.slice(0, MAX_BODY);
}

// A heartbeat body is plain text (the first line is the detail of a /fail) or JSON {message, admin_stopped}.
function parseHeartbeatBody(request, body) {
  const type = request.headers.get("Content-Type") || "";
  if (type.startsWith("application/json")) {
    try {
      const data = JSON.parse(body);
      if (data && typeof data === "object") {
        return { message: String(data.message || ""), admin_stopped: data.admin_stopped === true };
      }
    } catch {
      // not JSON after all: treated as text
    }
  }
  return { message: body, admin_stopped: null };
}

function runAge(state, now) {
  const run = state.run || {};
  return typeof run.last_run === "number" ? Math.round((now - run.last_run) / 1000) : null;
}

async function heartbeat(env, cfg, rt, now, name, kind, payload) {
  const key = `check:${name}`;
  const spec = cfg.checks[name];
  for (let attempt = 0; attempt < HEARTBEAT_RETRIES; attempt++) {
    const { state, raw } = await loadState(env.DB);
    const maint = maintenanceActive(state, now);
    const prev = state[key] || {};
    const c = { alerted: false, last_ok: null, ...prev };
    const alerts = [];
    c.last_ping = now;
    c.last_status = kind || "ok";
    if (typeof c.armed_at !== "number") c.armed_at = now;
    if (payload.admin_stopped !== null) c.admin_stopped = payload.admin_stopped;
    if (kind === "start") {
      c.started_at = now;
    } else if (kind === "fail") {
      c.last_fail = now;
      c.started_at = null;
      if (spec.alert_on_fail && !maint && c.alerted !== "fail") {
        c.alerted = "fail";
        const detail = payload.message.split("\n")[0].slice(0, 300);
        alerts.push({
          key,
          text: WORKER_ALERTS.checkFailed({ name, detail }),
          revert: (v) => (v.alerted = prev.alerted || false),
        });
      }
    } else {
      if (c.alerted || (c.run_alerted && c.run_alerted === c.started_at)) {
        alerts.push({
          key,
          text: WORKER_ALERTS.checkBack({ name, lastOkAgoMs: typeof prev.last_ok === "number" ? now - prev.last_ok : null }),
          revert: (v) => {
            v.alerted = prev.alerted || false;
            v.run_alerted = prev.run_alerted === undefined ? null : prev.run_alerted;
            v.started_at = prev.started_at === undefined ? null : prev.started_at;
          },
        });
      }
      c.alerted = false;
      c.last_ok = now;
      c.started_at = null;
      c.run_alerted = null;
    }
    const { lost } = await persistAndAlert(rt, env, cfg, raw, { [key]: c }, alerts, now);
    if (!lost.has(key)) {
      return json({ ok: true, check: name, status: c.last_status, last_run_age_s: runAge(state, now) });
    }
  }
  return json({ error: "busy, try again" }, 503);
}

async function maintenance(env, now, body) {
  let data;
  try {
    data = JSON.parse(body);
  } catch {
    return json({ error: "body must be JSON {minutes, reason}" }, 400);
  }
  const mins = Number(data && data.minutes);
  if (!Number.isInteger(mins) || mins < 0 || mins > MAX_MAINTENANCE_MINUTES) {
    return json({ error: `minutes must be an integer 0-${MAX_MAINTENANCE_MINUTES}` }, 400);
  }
  const reason = String((data && data.reason) || "maintenance").replace(/\s+/g, " ").slice(0, 200);
  const { state } = await loadState(env.DB); // creates the table on a fresh database
  const value = mins ? { until: now + mins * 60000, reason, since: now } : { until: 0, reason, since: now };
  await upsert(env.DB, "maintenance", value, now);
  if (mins) await annotate(env, now, "maintenance", reason);
  return json({
    ok: true,
    until: mins ? new Date(value.until).toISOString() : null,
    last_run_age_s: runAge(state, now),
  });
}

function iso(ms) {
  return typeof ms === "number" && ms > 0 ? new Date(ms).toISOString() : null;
}

async function status(env, cfg, now) {
  return json(await statusData(env, cfg, now));
}

// The watchdog's view (GET /status; the panel adds `extended`: last failure times, players seen by the probe).
async function statusData(env, cfg, now, extended = false) {
  const { state } = await loadState(env.DB);
  const maint = maintenanceActive(state, now);
  const run = state.run || {};
  const checks = {};
  for (const name of CHECKS) {
    const c = state[`check:${name}`] || {};
    const spec = cfg.checks[name];
    const armed = typeof c.last_ping === "number";
    const late = armed && lateBy(c, spec, now) !== null;
    const running = armed && runningTooLong(c, spec, now) !== null;
    checks[name] = {
      state: !armed
        ? "unknown"
        : late
          ? "late"
          : c.last_status === "fail"
            ? "failing"
            : running
              ? "running-long"
              : "ok",
      last_ping: iso(c.last_ping),
      last_ok: iso(c.last_ok),
      age_s: armed ? Math.round((now - c.last_ping) / 1000) : null,
    };
    if (extended) checks[name].last_fail = iso(c.last_fail);
  }
  const probes = {};
  for (const t of cfg.targets) {
    const p = state[`probe:${t.family}`] || {};
    probes[t.family] = {
      ok: typeof p.ok === "boolean" ? p.ok : null,
      last_ok: iso(p.last_ok),
      failed_runs: p.failed_runs || 0,
      error: p.ok === false ? p.error || "" : "",
      version: p.version || null,
    };
    if (extended) probes[t.family].online = Number.isFinite(p.online) ? p.online : null;
  }
  const stopped = adminStopped(state);
  const cronError = Boolean(state.cron_error && state.cron_error.active);
  const healthy =
    !cronError &&
    Object.values(checks).every((c) => c.state === "ok" || c.state === "unknown") &&
    (stopped || Object.values(probes).every((p) => p.ok !== false));
  return {
    ok: healthy,
    last_run: iso(run.last_run),
    last_run_age_s: runAge(state, now),
    cron_error: cronError,
    maintenance: maint ? { until: iso(maint.until), reason: maint.reason } : null,
    admin_stopped: stopped,
    client: clientConfig(env) !== null, // v1.2: a flag only, never the host or a binding's value
    checks,
    probes,
  };
}

async function handle(request, env, now, rt) {
  const url = new URL(request.url);
  const pcfg = panelConfig(env);
  if (pcfg && url.hostname.toLowerCase() === pcfg.host) return handlePanel(request, env, now, rt, url, pcfg);
  if (pcfg && pcfg.confirm_host && url.hostname.toLowerCase() === pcfg.confirm_host) {
    return handleConfirm(request, env, now, rt, url, pcfg);
  }
  const ccfg = clientConfig(env); // v1.1
  if (ccfg && url.hostname.toLowerCase() === ccfg.host) return handleClient(request, env, now, url, ccfg);
  const cfg = config(env);
  if (PACK_PATH.test(url.pathname)) return servePack(request, env, url); // P17
  if (url.pathname.startsWith("/publish/") || url.pathname.startsWith("/artifacts/")) {
    return handleArtifacts(request, env, url); // v1.1
  }
  if (url.pathname === DISCORD_PATH) return handleDiscord(request, env, now, rt); // P14
  if (url.pathname.startsWith("/ingest/") || url.pathname.startsWith("/agent/")) {
    return handleAgent(request, env, now, url, rt);
  }
  if (url.pathname === "/status") {
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);
    return status(env, cfg, now);
  }
  const hb = /^\/hb\/(health|backup)(?:\/(start|fail))?$/.exec(url.pathname);
  if (!hb && url.pathname !== "/maintenance") return json({ error: "not found" }, 404);
  if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
  if (!(await tokenOk(request.headers.get("Authorization"), env.HEARTBEAT_TOKEN))) {
    return json({ error: "unauthorized" }, 401);
  }
  const body = await readBody(request);
  if (hb) return heartbeat(env, cfg, rt, now, hb[1], hb[2] || "", parseHeartbeatBody(request, body));
  return maintenance(env, now, body);
}

// P17: the branding plugin's optional resource pack (the drink's model and the glyph), built by `mcctl render`
// from the config repo's art (mcctl/brandpack.py) into rendered/watchdog/pack/<sha1>.zip and uploaded with the
// Worker's static assets (terraform/watchdog.tf var.watchdog_assets_dir). Public on the
// watchdog host only: Minecraft clients download it without credentials when server.properties offers it. The
// name is the file's sha1 (the client checks it), so the URL is immutable and cacheable for good. Clients send
// X-Minecraft-Username/-UUID headers (26.3 client, DownloadedPackSource); the Worker never reads or logs them.
const PACK_PATH = /^\/pack\/[0-9a-f]{40}\.zip$/;

async function servePack(request, env, url) {
  if (request.method !== "GET" && request.method !== "HEAD") return json({ error: "method not allowed" }, 405);
  if (!env.ASSETS) return json({ error: "not found" }, 404);
  const asset = await env.ASSETS.fetch(new Request(`https://assets${url.pathname}`, { method: request.method }));
  if (!asset.ok) return json({ error: "not found" }, 404);
  return new Response(request.method === "HEAD" ? null : asset.body, {
    status: 200,
    headers: {
      "Content-Type": "application/zip",
      "Cache-Control": "public, max-age=31536000, immutable",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

// --- v1.1: the client modpack (setup page, one-time codes, packs, the pinned artifact mirror) ---------------------
//
// Only with the CLIENT binding (plain text JSON {host, title}; server.yml client.enabled, terraform/client.tf), the
// CLIENT_KEY secret (signs sessions, links and code hashes), PUBLISH_TOKEN (the deploy job's uploads) and the R2
// bucket ARTIFACTS. The Worker never sees a Minecraft credential; codes reach players only through mc-agent:
//
//   POST /agent/client-code {uuid, name}  (bearer AGENT_TOKEN: mc-agent, for mc-brand's /modpack)
//        -> a 6-digit code bound to the UUID, valid CLIENT_DEFAULTS.code_minutes, single use; stored as an HMAC
//           (D1 client_codes), at most codes_per_player_hour per player.
//   client host (<client.hostname>.<zone>, public, the setup page; the static files are watchdog/panel/client/):
//     POST /api/code {code}   -> the session cookie (HttpOnly, Secure, SameSite=Lax, session_minutes) for that
//                               player; rate-limited per IP (keyed by an HMAC, never the address) and globally
//     GET  /api/session       -> the player's name and the tiers (R2 client/index.json); 401 without a session
//     POST /api/link {tier[, kind: "launcher"]} -> a signed download link, valid link_minutes
//     GET  /dl/<tier>/<exp>/<sig>/<file>.zip?u=&n=  -> v1.2: the official launcher's zip, stored by the deploy
//          (R2 artifacts/<sha256>/<file>, named in client/index.json `launcher`; mcctl/clientpack.py
//          launcher_zip). Audit row: who, which tier (kind "launcher").
//     GET  /dl/<tier>/<exp>/<sig>/<file>.mrpack?u=&n=  -> the pack, written per download (zipStored: the same
//          bytes as mcctl/clientpack.py for the same links); every mirrored file's `downloads` starts with
//          /m/<exp>/<sig>/<sha256>/<filename> (valid mirror_hours), then the official URL (the launcher falls back
//          by itself and checks sha512 either way). Audit row: who, which tier.
//     GET  /m/<exp>/<sig>/<sha256>/<filename> -> R2 artifacts/<sha256>/<filename>
//   watchdog host: PUT|HEAD /publish/artifacts/<sha256>/<filename> (never overwritten: the key is the content's
//     sha256, which every consumer checks), PUT /publish/client/<index|tier>.json (bearer PUBLISH_TOKEN, the
//     deploy job's `mcctl client-publish`; v1.2: GET of index.json too, for the launcher zips' recipes);
//     GET /artifacts/<sha256>/<filename> (bearer AGENT_TOKEN: the deploy's
//     server-side downloads, upstream as the fallback, sha256-checked by Ansible).
// No player data beyond UUID and name; the code is never logged or stored in clear. Not a security boundary for
// the mods (public, integrity by the pinned hashes): it keeps the page and the server address unadvertised.

const CLIENT_DEFAULTS = {
  code_minutes: 15,
  session_minutes: 60,
  link_minutes: 10,
  mirror_hours: 6,
  codes_per_player_hour: 5,
  attempts_per_ip: 10, // per 10 minutes
  failures_per_hour: 200, // all IPs together: past this, codes are refused for the rest of the hour
  audit_days: 180,
};
const CLIENT_TIERS = ["low", "medium", "high", "maximum"];
const CLIENT_SCHEMA = [
  "CREATE TABLE IF NOT EXISTS client_codes (code_hash TEXT PRIMARY KEY, uuid TEXT NOT NULL, name TEXT NOT NULL, " +
    "created INTEGER NOT NULL, expires INTEGER NOT NULL, used INTEGER)",
  "CREATE INDEX IF NOT EXISTS client_codes_uuid ON client_codes (uuid, created)",
  "CREATE TABLE IF NOT EXISTS client_rate (key TEXT PRIMARY KEY, count INTEGER NOT NULL, until INTEGER NOT NULL)",
  "CREATE TABLE IF NOT EXISTS client_audit (ts INTEGER NOT NULL, uuid TEXT NOT NULL, name TEXT NOT NULL, " +
    "kind TEXT NOT NULL, tier TEXT)",
];
const CLIENT_ASSETS = /^\/(?:client\.css|client\.js|favicon\.svg|img\/[a-z0-9-]+\.svg|fonts\/[a-z0-9-]+\.woff2)$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const ARTIFACT_NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const MAX_ARTIFACT_BYTES = 100 * 1048576; // the Workers request body limit
const MAX_PLAN_BYTES = 1048576;
const COOKIE = "wc_session";
const CLIENT_SECURITY_HEADERS = {
  ...PANEL_SECURITY_HEADERS,
  "Content-Security-Policy":
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; " +
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
};

// The page's colours: the server's branding (server.yml branding.colors, in the CLIENT binding), hex only.
const HEX_COLOR = /^#[0-9A-Fa-f]{6}$/;
function themeCss(ccfg) {
  const c = ccfg.colors || {};
  // not `text`: the page keeps its own text colour for light and dark mode
  const vars = ["accent", "secondary", "subtle"].filter((k) => HEX_COLOR.test(String(c[k] || "")));
  return `:root { ${vars.map((k) => `--${k}: ${c[k]};`).join(" ")} }\n`;
}

function clientConfig(env) {
  if (!env.CLIENT || !env.CLIENT_KEY || !env.ARTIFACTS || !env.DB) return null;
  let raw;
  try {
    raw = JSON.parse(env.CLIENT);
  } catch {
    return null;
  }
  if (!raw || typeof raw.host !== "string" || !raw.host) return null;
  return { ...CLIENT_DEFAULTS, ...raw, host: raw.host.toLowerCase(), title: String(raw.title || "Minecraft") };
}

let clientSchemaReady = null;
async function clientReady(db) {
  if (!clientSchemaReady) {
    clientSchemaReady = db.batch(CLIENT_SCHEMA.map((s) => db.prepare(s))).catch((err) => {
      clientSchemaReady = null;
      throw err;
    });
  }
  return clientSchemaReady;
}

function clientHeaders(resp, extra = {}) {
  const headers = new Headers(resp.headers);
  for (const [k, v] of Object.entries({ ...CLIENT_SECURITY_HEADERS, ...extra })) headers.set(k, v);
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers });
}

function sig32(hex) {
  return hex.slice(0, 32);
}

// Constant-time comparison of two hex strings of equal length (signatures).
function sameHex(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function sixDigits() {
  const buf = new Uint32Array(1);
  for (;;) {
    crypto.getRandomValues(buf);
    if (buf[0] < 4294000000) return String(buf[0] % 1000000).padStart(6, "0"); // no modulo bias
  }
}

async function codeHash(key, code) {
  return hmacHex(key, `code|${code}`);
}

async function mintCode(env, ccfg, now, data) {
  const uuid = String((data && data.uuid) || "");
  const name = String((data && data.name) || "");
  if (!UUID_RE.test(uuid) || !PLAYER_NAME.test(name)) return json({ error: "uuid and name required" }, 400);
  await clientReady(env.DB);
  const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM client_codes WHERE uuid = ?1 AND created > ?2")
    .bind(uuid, now - 3600000)
    .first();
  if (recent && recent.n >= ccfg.codes_per_player_hour) return json({ error: "too many codes for this player" }, 429);
  const expires = now + ccfg.code_minutes * 60000;
  for (let i = 0; i < 5; i++) {
    const code = sixDigits();
    const hash = await codeHash(env.CLIENT_KEY, code);
    const res = await env.DB.prepare(
      "INSERT INTO client_codes (code_hash, uuid, name, created, expires, used) VALUES (?1, ?2, ?3, ?4, ?5, NULL) " +
        "ON CONFLICT(code_hash) DO NOTHING",
    )
      .bind(hash, uuid, name, now, expires)
      .run();
    if (res.meta && res.meta.changes === 1) return json({ code, expires_ms: expires });
  }
  return json({ error: "no free code, try again" }, 503);
}

// A counter that resets after `ms`: returns the count after this hit.
async function bump(db, key, now, ms) {
  const row = await db
    .prepare(
      "INSERT INTO client_rate (key, count, until) VALUES (?1, 1, ?2) ON CONFLICT(key) DO UPDATE SET " +
        "count = CASE WHEN until <= ?3 THEN 1 ELSE count + 1 END, until = CASE WHEN until <= ?3 THEN ?2 ELSE until END " +
        "RETURNING count",
    )
    .bind(key, now + ms, now)
    .first();
  return row ? row.count : 1;
}

async function peek(db, key, now) {
  const row = await db.prepare("SELECT count FROM client_rate WHERE key = ?1 AND until > ?2").bind(key, now).first();
  return row ? row.count : 0;
}

function b64urlText(text) {
  return b64url(new TextEncoder().encode(text));
}

async function sessionCookie(env, ccfg, now, uuid, name) {
  const body = b64urlText(JSON.stringify({ u: uuid, n: name, e: now + ccfg.session_minutes * 60000 }));
  const sig = sig32(await hmacHex(env.CLIENT_KEY, `session|${body}`));
  return `${COOKIE}=${body}.${sig}; Max-Age=${ccfg.session_minutes * 60}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

async function readSession(request, env, now) {
  const cookie = request.headers.get("Cookie") || "";
  const m = new RegExp(`(?:^|;\\s*)${COOKIE}=([A-Za-z0-9_-]{10,600})\\.([0-9a-f]{32})(?:;|$)`).exec(cookie);
  if (!m) return null;
  if (!sameHex(m[2], sig32(await hmacHex(env.CLIENT_KEY, `session|${m[1]}`)))) return null;
  let s;
  try {
    s = JSON.parse(new TextDecoder().decode(b64urlBytes(m[1])));
  } catch {
    return null;
  }
  if (!s || !UUID_RE.test(String(s.u)) || !PLAYER_NAME.test(String(s.n)) || !(s.e > now)) return null;
  return { uuid: s.u, name: s.n };
}

async function ipKey(env, request) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  return `ip|${sig32(await hmacHex(env.CLIENT_KEY, `ip|${ip}`))}`; // never the address itself
}

async function useCode(request, env, ccfg, now) {
  if (!sameOrigin(request, ccfg.host)) return json({ error: "forbidden" }, 403);
  const { data, error } = await readJson(request, 1024);
  if (error) return error;
  const code = String((data && data.code) || "").replace(/\s+/g, "");
  await clientReady(env.DB);
  const hour = Math.floor(now / 3600000);
  if ((await peek(env.DB, `fail|${hour}`, now)) >= ccfg.failures_per_hour) {
    return json({ error: "too many attempts right now: try again in an hour" }, 429);
  }
  if ((await bump(env.DB, await ipKey(env, request), now, 600000)) > ccfg.attempts_per_ip) {
    return json({ error: "too many attempts: wait 10 minutes" }, 429);
  }
  if (!/^[0-9]{6}$/.test(code)) return json({ error: "that isn't a 6-digit code" }, 400);
  const row = await env.DB.prepare(
    "UPDATE client_codes SET used = ?1 WHERE code_hash = ?2 AND used IS NULL AND expires > ?1 RETURNING uuid, name",
  )
    .bind(now, await codeHash(env.CLIENT_KEY, code))
    .first();
  if (!row) {
    await bump(env.DB, `fail|${hour}`, now, 3600000);
    return json({ error: "that code is wrong, used or expired: type /modpack in game for a new one" }, 401);
  }
  await clientAudit(env, now, row.uuid, row.name, "code", null);
  const resp = json({ ok: true, name: row.name });
  resp.headers.append("Set-Cookie", await sessionCookie(env, ccfg, now, row.uuid, row.name));
  return resp;
}

async function clientAudit(env, now, uuid, name, kind, tier) {
  await env.DB.prepare("INSERT INTO client_audit (ts, uuid, name, kind, tier) VALUES (?1, ?2, ?3, ?4, ?5)")
    .bind(now, uuid, name, kind, tier)
    .run();
}

async function r2Json(env, key) {
  const obj = await env.ARTIFACTS.get(key);
  if (!obj) return null;
  try {
    return JSON.parse(await obj.text());
  } catch {
    return null;
  }
}

async function linkSig(env, kind, ...parts) {
  return sig32(await hmacHex(env.CLIENT_KEY, [kind, ...parts].join("|")));
}

async function downloadLink(env, ccfg, now, session, tier, filename, kind = "dl") {
  const exp = Math.floor(now / 1000) + ccfg.link_minutes * 60;
  const sig = await linkSig(env, kind, tier, exp, session.uuid, session.name);
  const q = `u=${session.uuid}&n=${encodeURIComponent(session.name)}`;
  return `/dl/${tier}/${exp}/${sig}/${encodeURIComponent(filename)}?${q}`;
}

// The mirror link of one artifact (absolute: the launcher downloads it).
async function mirrorLink(env, ccfg, exp, sha256, filename) {
  return `https://${ccfg.host}/m/${exp}/${await linkSig(env, "m", exp, sha256, filename)}/${sha256}/${encodeURIComponent(filename)}`;
}

// --- the .mrpack writer: stored zip entries, fixed date, UTF-8 names (= mcctl/clientpack.py zip_stored) --------

let CRC_TABLE = null;
function crc32(bytes) {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      CRC_TABLE[n] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function zipStored(entries) {
  const enc = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;
  let centralSize = 0;
  for (const [name, data] of entries) {
    const raw = enc.encode(name);
    const crc = crc32(data);
    const head = new DataView(new ArrayBuffer(30));
    head.setUint32(0, 0x04034b50, true);
    head.setUint16(4, 10, true);
    head.setUint16(6, 0x0800, true);
    head.setUint16(8, 0, true);
    head.setUint16(10, 0, true);
    head.setUint16(12, 0x21, true);
    head.setUint32(14, crc, true);
    head.setUint32(18, data.length, true);
    head.setUint32(22, data.length, true);
    head.setUint16(26, raw.length, true);
    head.setUint16(28, 0, true);
    const cen = new DataView(new ArrayBuffer(46));
    cen.setUint32(0, 0x02014b50, true);
    cen.setUint16(4, 0x031e, true);
    cen.setUint16(6, 10, true);
    cen.setUint16(8, 0x0800, true);
    cen.setUint16(10, 0, true);
    cen.setUint16(12, 0, true);
    cen.setUint16(14, 0x21, true);
    cen.setUint32(16, crc, true);
    cen.setUint32(20, data.length, true);
    cen.setUint32(24, data.length, true);
    cen.setUint16(28, raw.length, true);
    cen.setUint16(30, 0, true);
    cen.setUint16(32, 0, true);
    cen.setUint16(34, 0, true);
    cen.setUint16(36, 0, true);
    cen.setUint32(38, 0o100644 * 65536, true);
    cen.setUint32(42, offset, true);
    parts.push(new Uint8Array(head.buffer), raw, data);
    central.push(new Uint8Array(cen.buffer), raw);
    offset += 30 + raw.length + data.length;
    centralSize += 46 + raw.length;
  }
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(all.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of all) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function b64decode(text) {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// The pack of one plan; `link(sha256, filename)` = the mirror URL of a mirrored file.
async function packZip(plan, link) {
  const files = [];
  for (const f of plan.files) {
    // v1.2: an in-house file (the Smallworld mod) has no upstream URL: the mirror is its only download
    const downloads = [...(f.mirror ? [await link(f.mirror, f.filename)] : []), ...(f.upstream ? [f.upstream] : [])];
    files.push({ path: f.path, hashes: f.hashes, env: f.env, downloads, fileSize: f.fileSize });
  }
  const index = {
    formatVersion: 1,
    game: "minecraft",
    versionId: plan.version_id,
    name: plan.name,
    summary: plan.summary,
    files,
    dependencies: plan.dependencies,
  };
  const entries = [["modrinth.index.json", new TextEncoder().encode(JSON.stringify(index))]];
  for (const o of plan.overrides) entries.push([o.path, b64decode(o.data)]);
  return zipStored(entries);
}

// v1.2: the official launcher's zip of one tier: the stored artifact client/index.json names (never built here).
async function serveLauncherZip(request, env, now, url) {
  const m = /^\/dl\/([a-z]+)\/(\d{9,11})\/([0-9a-f]{32})\/([A-Za-z0-9._-]{1,80}\.zip)$/.exec(url.pathname);
  const uuid = url.searchParams.get("u") || "";
  const name = url.searchParams.get("n") || "";
  if (!m || !CLIENT_TIERS.includes(m[1]) || !UUID_RE.test(uuid) || !PLAYER_NAME.test(name)) {
    return json({ error: "not found" }, 404);
  }
  const exp = Number(m[2]);
  if (exp * 1000 < now || !sameHex(m[3], await linkSig(env, "zip", m[1], exp, uuid, name))) {
    return json({ error: "this download link expired: reload the setup page" }, 410);
  }
  const index = await r2Json(env, "client/index.json");
  const tier = index && Array.isArray(index.tiers) ? index.tiers.find((t) => t.id === m[1]) : null;
  const la = tier && tier.launcher;
  if (!la || !SHA256_RE.test(String(la.sha256 || "")) || !ARTIFACT_NAME.test(String(la.filename || ""))) {
    return json({ error: "not published yet" }, 404);
  }
  const resp = await artifactResponse(env, request, `artifacts/${la.sha256}/${la.filename}`, la.filename);
  if (!resp.ok) return resp;
  await clientReady(env.DB);
  await clientAudit(env, now, uuid, name, "launcher", m[1]);
  resp.headers.set("Content-Type", "application/zip");
  resp.headers.set("Cache-Control", "no-store");
  return clientHeaders(resp);
}

async function servePackDownload(request, env, ccfg, now, url) {
  if (url.pathname.endsWith(".zip")) return serveLauncherZip(request, env, now, url);
  const m = /^\/dl\/([a-z]+)\/(\d{9,11})\/([0-9a-f]{32})\/([A-Za-z0-9._-]{1,80}\.mrpack)$/.exec(url.pathname);
  const uuid = url.searchParams.get("u") || "";
  const name = url.searchParams.get("n") || "";
  if (!m || !CLIENT_TIERS.includes(m[1]) || !UUID_RE.test(uuid) || !PLAYER_NAME.test(name)) {
    return json({ error: "not found" }, 404);
  }
  const exp = Number(m[2]);
  if (exp * 1000 < now || !sameHex(m[3], await linkSig(env, "dl", m[1], exp, uuid, name))) {
    return json({ error: "this download link expired: reload the setup page" }, 410);
  }
  const plan = await r2Json(env, `client/${m[1]}.json`);
  if (!plan || !Array.isArray(plan.files) || !Array.isArray(plan.overrides)) return json({ error: "not published yet" }, 404);
  const mirrorExp = Math.floor(now / 1000) + ccfg.mirror_hours * 3600;
  const zip = await packZip(plan, (sha, filename) => mirrorLink(env, ccfg, mirrorExp, sha, filename));
  await clientReady(env.DB);
  await clientAudit(env, now, uuid, name, "download", m[1]);
  return clientHeaders(
    new Response(zip, {
      headers: {
        "Content-Type": "application/x-modrinth-modpack+zip",
        "Content-Disposition": `attachment; filename="${String(plan.filename).replace(/[^A-Za-z0-9._-]/g, "")}"`,
        "Cache-Control": "no-store",
      },
    }),
  );
}

async function serveMirror(request, env, now, url) {
  const m = /^\/m\/(\d{9,11})\/([0-9a-f]{32})\/([0-9a-f]{64})\/([^/]{1,200})$/.exec(url.pathname);
  if (!m || (request.method !== "GET" && request.method !== "HEAD")) return json({ error: "not found" }, 404);
  const filename = decodeURIComponent(m[4]);
  if (!ARTIFACT_NAME.test(filename)) return json({ error: "not found" }, 404);
  if (Number(m[1]) * 1000 < now || !sameHex(m[2], await linkSig(env, "m", m[1], m[3], filename))) {
    return json({ error: "expired" }, 410); // the launcher moves on to the official URL
  }
  return artifactResponse(env, request, `artifacts/${m[3]}/${filename}`, filename);
}

async function artifactResponse(env, request, key, filename) {
  const obj = request.method === "HEAD" ? await env.ARTIFACTS.head(key) : await env.ARTIFACTS.get(key);
  if (!obj) return json({ error: "not found" }, 404);
  return new Response(request.method === "HEAD" ? null : obj.body, {
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Length": String(obj.size),
      "Content-Disposition": `attachment; filename="${filename.replace(/[^A-Za-z0-9._+-]/g, "")}"`,
      "Cache-Control": "private, max-age=3600",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

async function handleClient(request, env, now, url, ccfg) {
  const route = `${request.method} ${url.pathname}`;
  if (url.pathname.startsWith("/m/")) return serveMirror(request, env, now, url);
  if (url.pathname.startsWith("/dl/")) {
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);
    return servePackDownload(request, env, ccfg, now, url);
  }
  if (route === "POST /api/code") return clientHeaders(await useCode(request, env, ccfg, now));
  if (route === "GET /api/session" || route === "POST /api/link") {
    const session = await readSession(request, env, now);
    if (!session) return clientHeaders(json({ error: "no session" }, 401));
    const index = await r2Json(env, "client/index.json");
    if (!index || !Array.isArray(index.tiers)) return clientHeaders(json({ error: "not published yet" }, 503));
    if (route === "GET /api/session") {
      return clientHeaders(json({ name: session.name, ...index, title: ccfg.title }));
    }
    if (!sameOrigin(request, ccfg.host)) return clientHeaders(json({ error: "forbidden" }, 403));
    const { data, error } = await readJson(request, 1024);
    if (error) return clientHeaders(error);
    const tier = index.tiers.find((t) => t.id === (data && data.tier));
    if (!tier) return clientHeaders(json({ error: "unknown tier" }, 400));
    if (data.kind === "launcher") {
      // v1.2: the official launcher's zip, once the deploy has stored it
      const la = tier.launcher;
      if (!la || !SHA256_RE.test(String(la.sha256 || "")) || !ARTIFACT_NAME.test(String(la.filename || ""))) {
        return clientHeaders(json({ error: "This download isn't ready yet. Use the .mrpack file below, or try later." }, 404));
      }
      return clientHeaders(json({ url: await downloadLink(env, ccfg, now, session, tier.id, la.filename, "zip") }));
    }
    return clientHeaders(json({ url: await downloadLink(env, ccfg, now, session, tier.id, tier.filename) }));
  }
  if (route === "POST /api/logout") {
    const resp = json({ ok: true });
    resp.headers.append("Set-Cookie", `${COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`);
    return clientHeaders(resp);
  }
  if (request.method !== "GET" && request.method !== "HEAD") return clientHeaders(json({ error: "method not allowed" }, 405));
  if (url.pathname === "/theme.css") return clientHeaders(new Response(themeCss(ccfg), { headers: { "Content-Type": "text/css; charset=utf-8", "Cache-Control": "public, max-age=300" } }));
  const path = url.pathname === "/" ? "/index.html" : url.pathname;
  if ((path !== "/index.html" && !CLIENT_ASSETS.test(path)) || !env.ASSETS) {
    return clientHeaders(json({ error: "not found" }, 404));
  }
  const shared = path.startsWith("/fonts/") || path === "/favicon.svg";
  const asset = await env.ASSETS.fetch(
    new Request(`https://${ccfg.host}${shared ? path : `/client${path}`}`, { method: request.method }),
  );
  if (!asset.ok) return clientHeaders(json({ error: "not found" }, 404));
  return clientHeaders(asset, { "Cache-Control": path === "/index.html" ? "no-store" : "public, max-age=3600" });
}

// The deploy job's uploads (bearer PUBLISH_TOKEN) and the server's downloads (bearer AGENT_TOKEN).
// v1.2: every answer that didn't consume the request body cancels it, so a client still writing an upload
// gets the answer (401, 413, existed, ...) instead of blocking until its write times out.
async function handleArtifacts(request, env, url) {
  let resp;
  try {
    resp = await artifactRoute(request, env, url);
  } finally {
    await dropBody(request);
  }
  return resp;
}

async function dropBody(request) {
  const body = request.body;
  if (!body || request.bodyUsed || body.locked) return;
  try {
    await body.cancel();
  } catch {
    // already closed or errored: nothing left to drop
  }
}

function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// PUT /publish/artifacts/<sha256>/<filename>: streamed into R2 through FixedLengthStream (R2 needs the length
// of a streamed body) and, where the runtime has it, crypto.DigestStream; a body whose sha256 isn't the one in
// the path is never kept. R2 checks the same sha256 itself (the put's `sha256` option).
async function putArtifact(request, env, key, sha) {
  const raw = request.headers.get("Content-Length");
  const length = raw && /^[0-9]{1,12}$/.test(raw) ? Number(raw) : 0;
  if (!(length > 0)) return json({ error: "Content-Length required" }, 411);
  if (length > MAX_ARTIFACT_BYTES) return json({ error: "Content-Length over 100 MB" }, 413);
  if (!request.body) return json({ error: "Content-Length required" }, 411);
  let body = request.body;
  let digest = null;
  let got = null;
  if (typeof crypto !== "undefined" && typeof crypto.DigestStream === "function") {
    const hasher = new crypto.DigestStream("SHA-256");
    const writer = hasher.getWriter();
    digest = hasher.digest.then((d) => (got = hex(d))).catch(() => null);
    body = body.pipeThrough(
      new TransformStream({
        async transform(chunk, ctl) {
          await writer.write(chunk);
          ctl.enqueue(chunk);
        },
        async flush() {
          await writer.close();
        },
      }),
    );
  }
  if (typeof FixedLengthStream === "function") body = body.pipeThrough(new FixedLengthStream(length));
  try {
    await env.ARTIFACTS.put(key, body, { httpMetadata: { contentType: "application/octet-stream" }, sha256: sha });
  } catch (err) {
    if (got !== null && got !== sha) return json({ error: "sha256 mismatch: not stored" }, 422);
    const reason = String((err && err.message) || err).slice(0, 160);
    console.error(`artifact upload failed: ${reason}`); // the key is a public content hash; no token
    return json({ error: `storing failed: ${reason}` }, 502);
  }
  if (digest) {
    await digest;
    if (got !== sha) {
      await env.ARTIFACTS.delete(key);
      return json({ error: "sha256 mismatch: not stored" }, 422);
    }
  }
  return json({ ok: true, existed: false });
}

async function artifactRoute(request, env, url) {
  const ccfgOk = env.ARTIFACTS && env.PUBLISH_TOKEN;
  if (!ccfgOk) return json({ error: "the artifact mirror is off" }, 404);
  const art = /^\/(publish\/)?artifacts\/([0-9a-f]{64})\/([^/]{1,200})$/.exec(url.pathname);
  const plan = /^\/publish\/client\/(index|low|medium|high|maximum)\.json$/.exec(url.pathname);
  if (art && !art[1]) {
    if (request.method !== "GET" && request.method !== "HEAD") return json({ error: "method not allowed" }, 405);
    if (!(await tokenOk(request.headers.get("Authorization"), env.AGENT_TOKEN))) return json({ error: "unauthorized" }, 401);
    const filename = decodeURIComponent(art[3]);
    if (!ARTIFACT_NAME.test(filename)) return json({ error: "not found" }, 404);
    return artifactResponse(env, request, `artifacts/${art[2]}/${filename}`, filename);
  }
  if (!art && !plan) return json({ error: "not found" }, 404);
  if (!(await tokenOk(request.headers.get("Authorization"), env.PUBLISH_TOKEN))) return json({ error: "unauthorized" }, 401);
  if (art) {
    const filename = decodeURIComponent(art[3]);
    if (!ARTIFACT_NAME.test(filename)) return json({ error: "bad filename" }, 400);
    const key = `artifacts/${art[2]}/${filename}`;
    const have = await env.ARTIFACTS.head(key);
    if (request.method === "HEAD") return new Response(null, { status: have ? 200 : 404 });
    if (request.method !== "PUT") return json({ error: "method not allowed" }, 405);
    if (have) return json({ ok: true, existed: true }); // immutable: never overwritten
    return putArtifact(request, env, key, art[2]);
  }
  if (request.method === "GET" && plan[1] === "index") {
    // v1.2: client-publish reads the launcher zips' recipes back (bearer PUBLISH_TOKEN, checked above)
    const obj = await env.ARTIFACTS.get("client/index.json");
    if (!obj) return json({ error: "not found" }, 404);
    return new Response(await obj.text(), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
  }
  if (request.method !== "PUT") return json({ error: "method not allowed" }, 405);
  const body = await readLimited(request, MAX_PLAN_BYTES);
  if (!body) return json({ error: "body too large" }, 413);
  const text = new TextDecoder().decode(body);
  try {
    JSON.parse(text);
  } catch {
    return json({ error: "not JSON" }, 400);
  }
  await env.ARTIFACTS.put(`client/${plan[1]}.json`, text, { httpMetadata: { contentType: "application/json" } });
  return json({ ok: true });
}

// Daily (the cron's storage maintenance): spent and expired codes, old rate counters, audit past audit_days.
async function maintainClient(env, now) {
  const ccfg = clientConfig(env);
  if (!ccfg || Math.floor(now / 300000) % 288 !== 0) return; // the first cron run of each UTC day
  await clientReady(env.DB);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM client_codes WHERE expires < ?1").bind(now - 86400000),
    env.DB.prepare("DELETE FROM client_rate WHERE until < ?1").bind(now),
    env.DB.prepare("DELETE FROM client_audit WHERE ts < ?1").bind(now - ccfg.audit_days * 86400000),
  ]);
}

function runtime(ctx = null) {
  return {
    connect: (address) => connect(address),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    fetch: (url, init) => fetch(url, init),
    // P14: work after the response (the Discord reply's follow-up edit)
    waitUntil: ctx && ctx.waitUntil ? (p) => ctx.waitUntil(p) : null,
  };
}

export default {
  async fetch(request, env, ctx) {
    return handle(request, env, Date.now(), runtime(ctx));
  },
  async scheduled(controller, env) {
    await scheduledRun(env, Date.now(), runtime());
  },
  // For the tests only (watchdog/test/); the runtime ignores properties that aren't handlers.
  _internals: {
    handle,
    runChecks,
    alertText,
    WORKER_ALERTS,
    scheduledRun,
    statusRequest,
    parseStatus,
    varint,
    readVarint,
    tokenOk,
    config,
    monitoringConfig,
    maintainStorage,
    lastSample,
    rollup,
    hmacHex,
    dayOf,
    describe,
    verifyAccess,
    panelConfig,
    series,
    // P13
    judge,
    judgeServer,
    judgePlayer,
    messagesFor,
    cleanRow,
    ingestExperience,
    maintainExperience,
    nextMaintUnit,
    MAINT_MAX_TRIES,
    experienceHistory,
    experienceOverview,
    remediesFor,
    RULES,
    MESSAGES,
    // P14
    discordConfig,
    verifyDiscord,
    DISCORD_COMMANDS,
    DISCORD_ACTIONS,
    DISCORD_HELPER_COMMANDS,
    discordTier,
    DISCORD_RATE,
    DISCORD_POLL,
    DISCORD_REFUSAL_AUDIT,
    // v1.1
    clientConfig,
    zipStored,
    packZip,
    crc32,
    maintainClient,
    CLIENT_DEFAULTS,
    // v1.3
    versions,
    cmpVersion,
    workerStamp,
    agentLookup,
    resetIsolate: () => {
      clientSchemaReady = null;
      discordKey = { hex: "", key: null };
      discordRefusals = new Map();
      lastCronErrorAlert = 0;
      lastMaintAlert.clear();
      githubCache = { key: "", token: "", expires: 0, installation: null };
      mojangCache.clear();
      workerStampCache = null;
      jwksCache = { team: "", keys: new Map(), fetched: 0, failed: 0 };
    },
  },
};
