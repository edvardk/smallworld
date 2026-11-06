// Shared by panel.test.mjs (Worker API + Access JWT tests) and ui.smoke.mjs (Playwright): a test Access team
// (RSA key pair, JWKS served by a fake fetch), a panel env (real SQL D1 via node:sqlite, assets from
// watchdog/panel/, the real Control object on in-memory storage) and realistic seed data. No network.
import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";

import { Control } from "../worker.js";
import { SqliteD1 } from "./d1-sqlite.mjs";

const subtle = webcrypto.subtle;
export const TEAM = "lh-test.cloudflareaccess.com";
export const AUD = "a".repeat(64);
export const PANEL_HOST = "panel.example.com";
export const CONFIRM_HOST = "panel-confirm.example.com";
export const CONFIRM_AUD = "c".repeat(64);
export const EMAIL = "sam@example.com";
const ASSET_DIR = new URL("../panel/", import.meta.url);
const TYPES = { html: "text/html; charset=utf-8", css: "text/css; charset=utf-8", js: "text/javascript; charset=utf-8", woff2: "font/woff2", svg: "image/svg+xml", json: "application/json" };

const b64url = (bytes) => Buffer.from(bytes).toString("base64url");

export async function makeTeam(kid = "test-key-1") {
  const pair = await subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"],
  );
  const jwk = { ...(await subtle.exportKey("jwk", pair.publicKey)), kid, alg: "RS256", use: "sig" };
  return { kid, privateKey: pair.privateKey, jwk, fetches: 0 };
}

export async function signJwt(team, claims, { kid = team.kid, alg = "RS256", key = team.privateKey } = {}) {
  const head = b64url(JSON.stringify({ alg, kid, typ: "JWT" }));
  const body = b64url(JSON.stringify(claims));
  const sig = new Uint8Array(await subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${head}.${body}`)));
  return `${head}.${body}.${b64url(sig)}`;
}

export function claimsFor(now, extra = {}) {
  const t = Math.floor(now / 1000);
  return { aud: [AUD], email: EMAIL, exp: t + 3600, iat: t - 30, nbf: t - 30, iss: `https://${TEAM}`, type: "app", sub: "u-1", ...extra };
}

export function runtimeFor(team, extraFetch = null) {
  return {
    connect: () => {
      throw new Error("no sockets in panel tests");
    },
    sleep: async () => {},
    fetch: async (url, init) => {
      if (url === `https://${TEAM}/cdn-cgi/access/certs`) {
        team.fetches += 1;
        return new Response(JSON.stringify({ keys: [team.jwk], public_cert: { kid: team.kid } }), { headers: { "Content-Type": "application/json" } });
      }
      if (extraFetch) return extraFetch(url, init);
      throw new Error(`unexpected fetch ${url}`);
    },
  };
}

class MemStorage {
  constructor() {
    this.map = new Map();
  }
  async get(k) {
    return this.map.get(k);
  }
  async put(k, v) {
    this.map.set(k, structuredClone(v));
  }
  async delete(k) {
    for (const key of Array.isArray(k) ? k : [k]) this.map.delete(key);
  }
  async list({ prefix }) {
    return new Map([...this.map].filter(([k]) => k.startsWith(prefix)));
  }
}

export class AgentSocket {
  constructor() {
    this.sent = [];
  }
  send(text) {
    this.sent.push(JSON.parse(text));
  }
  close() {}
}

export function makePanelEnv(extra = {}) {
  const sockets = [];
  const ctx = {
    storage: new MemStorage(),
    acceptWebSocket: (ws, tags) => sockets.push({ ws, tags }),
    getWebSockets: (tag) => sockets.filter((s) => s.tags.includes(tag)).map((s) => s.ws),
  };
  const env = {
    DB: new SqliteD1(),
    AGENT_TOKEN: "a".repeat(48),
    HEARTBEAT_TOKEN: "h".repeat(48),
    COMMAND_HMAC_KEY: "k".repeat(64),
    MONITORING: JSON.stringify({ retention_days: 90, rollup_after_days: 7, metrics_interval_s: 60, logs: { d1_days: 14, max_rows_per_day: 30000 } }),
    TARGET: JSON.stringify({ server_host: "mc.example.com", targets: [{ family: "v4", address: "192.0.2.1:25565" }, { family: "v6", address: "[2001:db8::1]:25565" }] }),
    PANEL: JSON.stringify({
      host: PANEL_HOST,
      team_domain: TEAM,
      aud: AUD,
      confirm_host: CONFIRM_HOST,
      confirm_aud: CONFIRM_AUD,
      emails: [EMAIL, "friend@example.com"],
      reauth_minutes: 10,
      title: "Lighthouse",
      server_host: "mc.example.com",
      flavour: "purpur",
      minecraft_version: "26.3",
      backup_schedule: "hourly",
      max_players: 20,
    }),
    ASSETS: {
      fetch: async (req) => {
        const path = new URL(req.url).pathname.slice(1);
        try {
          const body = await readFile(new URL(path, ASSET_DIR));
          return new Response(body, { headers: { "Content-Type": TYPES[path.split(".").pop()] || "application/octet-stream" } });
        } catch {
          return new Response("not found", { status: 404 });
        }
      },
    },
    archived: [],
    ...extra,
  };
  env.LOGS = {
    put: async (key) => env.archived.push({ key, size: 1234, uploaded: new Date() }),
    list: async ({ prefix }) => ({ objects: env.archived.filter((o) => o.key.startsWith(prefix)) }),
  };
  env.ctx = ctx;
  env.control = new Control(ctx, env);
  env.CONTROL = { idFromName: (n) => n, get: () => ({ fetch: (r) => env.control.fetch(r) }) };
  return env;
}

export function connectAgent(env) {
  const ws = new AgentSocket();
  env.ctx.acceptWebSocket(ws, ["agent"]);
  return ws;
}

const day = (ms) => new Date(ms).toISOString().slice(0, 10).replace(/-/g, "");

// Two hours of minute samples, a day of logs, an inventory, watchdog state and some audit history.
export async function seed(env, now, { players = ["Em_K", "MiaBuilds", "jonas_r"] } = {}) {
  const db = env.DB;
  const tables = new Set();
  const ensureSamples = async (t) => {
    if (!tables.has(t)) {
      await db.prepare(`CREATE TABLE IF NOT EXISTS ${t} (ts INTEGER PRIMARY KEY, data TEXT NOT NULL)`).run();
      tables.add(t);
    }
  };
  const ensureLogs = async (t) => {
    if (!tables.has(t)) {
      await db.prepare(`CREATE TABLE IF NOT EXISTS ${t} (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, unit TEXT NOT NULL, prio INTEGER NOT NULL, audit INTEGER NOT NULL, msg TEXT NOT NULL)`).run();
      tables.add(t);
    }
  };
  let heap = 2600;
  for (let i = 120; i >= 0; i--) {
    const ts = now - i * 60000 - 7000;
    heap = heap > 4300 ? 2500 : heap + 40 + (i % 7) * 9;
    const mspt = i === 47 ? 1891.3 : 11 + ((i * 7) % 9) + (i % 13 === 0 ? 14 : 0);
    const cores = [34, 29, 41, 27].map((c, k) => Math.max(3, Math.min(97, c + ((i * (k + 3)) % 17) - 8)));
    const sample = {
      up: true, tps: mspt > 50 ? Math.round((20 * 50 / mspt) * 100) / 100 : 20, tps5: 19.99, tps15: 19.98, mspt,
      players: players.length, max_players: 20, players_online: players, heap_used_mb: heap, heap_max_mb: 5120,
      gc_max_pause_ms: 12.3, jvm_mem_mb: 5734, uptime_s: 3 * 86400 + 14 * 3600 + (120 - i) * 60,
      cpu_pct: cores.reduce((a, b) => a + b, 0) / 4, steal_pct: 0.4 + (i % 5) / 10, cpu_cores: 4,
      cpu0_pct: cores[0], cpu1_pct: cores[1], cpu2_pct: cores[2], cpu3_pct: cores[3], load1: 1.36, load5: 1.3, load15: 1.25,
      mem_pct: 74.2, mem_used_mb: 6080, mem_total_mb: 8192, disk_pct: 37.7, disk_used_gb: 29.4, disk_total_gb: 78,
      net_rx_bps: 160000 + (i % 9) * 9000, net_tx_bps: 590000 + (i % 11) * 21000, admin_stopped: false, ws: true,
    };
    const t = `samples_${day(ts)}`;
    await ensureSamples(t);
    await db.prepare(`INSERT OR IGNORE INTO ${t} (ts, data) VALUES (?1, ?2)`).bind(ts, JSON.stringify(sample)).run();
  }
  const logs = [
    [-3600, "minecraft.service", 6, 0, "[13:58:02 INFO]: Em_K[/100.101.7.22:51034] logged in with entity id 412 at ([world]412.5, 71.0, -1180.3)"],
    [-3590, "minecraft.service", 6, 0, "[13:58:02 INFO]: Em_K joined the game"],
    [-3500, "minecraft.service", 6, 0, "[13:59:40 INFO]: <MiaBuilds> the new bridge is done, come look"],
    [-3000, "mc-backup.service", 6, 0, "Added to the repository: 212.345 MiB (61.2 MiB stored)"],
    [-2999, "mc-backup.service", 6, 0, "snapshot 8c41d2e7 saved"],
    [-2900, "minecraft.service", 4, 0, "[14:00:21 WARN]: Can't keep up! Is the server overloaded? Running 1840ms or 36 ticks behind"],
    [-2500, "mc-health.service", 6, 0, "status=ok tps=20.0 mspt=14.2 heap=62% disk=38%"],
    [-2000, "mc-agent.service", 5, 1, "agent action=console ok=True login=sam@example.com"],
    [-1500, "minecraft.service", 6, 0, "[14:05:47 INFO]: jonas_r joined the game"],
    [-1400, "minecraft.service", 6, 0, "[14:06:02 INFO]: Disconnecting xXcreeperXx (/203.0.113.9:5121): You are not white-listed on this server!"],
    [-1300, "minecraft.service", 3, 0, "[14:09:15 ERROR]: Could not pass event EntityDamageEvent to Chunky v1.4.40"],
    [-600, "mc-backup.service", 6, 0, "Added to the repository: 188.004 MiB (52.0 MiB stored)"],
    [-599, "mc-backup.service", 6, 0, "snapshot f0a93b11 saved"],
    [-300, "mc-health.service", 5, 1, "AUDIT self-heal action=none reason=healthy"],
    [-60, "minecraft.service", 6, 0, "[14:12:30 INFO]: <jonas_r> anyone got spare blaze rods"],
  ];
  for (const [dt, unit, prio, audit, msg] of logs) {
    const ts = now + dt * 1000;
    const t = `logs_${day(ts)}`;
    await ensureLogs(t);
    await db.prepare(`INSERT INTO ${t} (ts, unit, prio, audit, msg) VALUES (?1, ?2, ?3, ?4, ?5)`).bind(ts, unit, prio, audit, msg).run();
  }
  const kv = async (key, value) => {
    await db.prepare("CREATE TABLE IF NOT EXISTS watchdog (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)").run();
    await db.prepare("INSERT OR REPLACE INTO watchdog (key, value, updated_at) VALUES (?1, ?2, ?3)").bind(key, JSON.stringify(value), now).run();
  };
  await kv("probe:v4", { ok: true, last_ok: now - 120000, last_run: now - 120000, failed_runs: 0, version: "Purpur 26.3", online: players.length });
  await kv("probe:v6", { ok: true, last_ok: now - 120000, last_run: now - 120000, failed_runs: 0, version: "Purpur 26.3", online: players.length });
  await kv("check:health", { last_ping: now - 140000, last_ok: now - 140000, armed_at: now - 86400000, last_status: "ok", alerted: false, admin_stopped: false });
  await kv("check:backup", { last_ping: now - 600000, last_ok: now - 600000, armed_at: now - 86400000, last_status: "ok", alerted: false });
  await kv("run", { last_run: now - 120000, targets: 2 });
  await kv("inventory", {
    ts: now - 300000,
    received: now - 300000,
    whitelist: ["Em_K", "MiaBuilds", "jonas_r", "Lukas_P", "saraplays", "TomTheMiner", "nora_k"].map((name, i) => ({ name, uuid: `${(i + 3).toString(16)}f2a0000-0000-4000-8000-00000000${i}c41` })),
    ops: [{ name: "Em_K", uuid: "3f2a0000-0000-4000-8000-000000000c41", level: 4 }],
    bans: [{ name: "griefer_99", uuid: "9", created: "2026-10-03 18:22:10 +0100", source: "Server", expires: "forever", reason: "Stole from shared chests" }],
    ip_bans: [{ ip: "203.0.113.47", created: "2026-10-12 19:10:00 +0100", source: "Server", expires: "forever", reason: "Repeated login spam" }],
    plugins: [{ file: "Chunky-Bukkit-1.4.40.jar", name: "Chunky-Bukkit-1.4.40", enabled: true }],
    // P10
    pending: [{ uuid: "8f2a0000-0000-4000-8000-000000006c41", name: "nora_k", op: "add", ts: now - 900000, login: EMAIL, pr: 12 }],
    whitelist_sync: { configured: true, pr: { number: 12, url: "https://github.com/octocat/mc-config/pull/12", branch: "panel/20261031-135800", created: now - 840000 }, error: "", expired: [], token_expires: "" },
    rejected: [
      { name: "xXcreeperXx", uuid: "5c090000-0000-4000-8000-00000000e212", count: 3, first: now - 86400000, last: now - 1400000, ip: "203.0.113.x" },
      { name: "Felix_2011", count: 1, first: now - 10800000, last: now - 10800000, ip: "198.51.100.x" },
    ],
    plugins_lock: [{ name: "chunky", filename: "Chunky-Bukkit-1.4.40.jar", version: "1.4.40" }],
    plugins_disabled: [],
    snapshots: [
      { id: `f0a93b11${"0".repeat(56)}`, short_id: "f0a93b11", time: new Date(now - 600000).toISOString(), tags: "auto", host: "mc-server", size: 1820000000, added: 197137000 },
      { id: `8c41d2e7${"1".repeat(56)}`, short_id: "8c41d2e7", time: new Date(now - 3000000).toISOString(), tags: "auto", host: "mc-server", size: 1810000000, added: 222656000 },
      { id: `3b07aa90${"2".repeat(56)}`, short_id: "3b07aa90", time: new Date(now - 90000000).toISOString(), tags: "pre-restore", host: "mc-server", size: 1790000000, added: 1200000 },
    ],
    job: null,
    // v1.2 world library
    worlds: {
      active: "main",
      since: new Date(now - 86400000).toISOString(),
      data_version: 4671,
      archives: [
        { name: "creative", short_id: "c4a1e000", time: new Date(now - 3 * 86400000).toISOString(), size: 2400000000, data_version: 4671, count: 1 },
        { name: "old-smp", short_id: "0d5e1000", time: new Date(now - 40 * 86400000).toISOString(), size: 9100000000, data_version: 4554, count: 2 },
      ],
      library: [
        { name: "main", seed: "", type: "default", preset: "survival_smp", border_radius: null, pregen: null },
        { name: "creative", seed: "-4172144997902289642", type: "flat_classic", preset: "creative_build", border_radius: { "minecraft:overworld": 1000, "minecraft:the_nether": null, "minecraft:the_end": null }, pregen: false },
      ],
      deleted: [],
      archive_bytes: 11500000000,
      storage_note: true,
      pending: { what: "switch to main", error: "", pr: 13, url: "https://github.com/octocat/mc-config/pull/13" },
    },
  });
  await db.prepare("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, login TEXT NOT NULL, action TEXT NOT NULL, args TEXT NOT NULL, cmd_id TEXT, result TEXT NOT NULL, ok INTEGER)").run();
  await db.prepare("INSERT INTO audit (ts, login, action, args, cmd_id, result, ok) VALUES (?1, ?2, 'console', ?3, NULL, 'ok', 1)")
    .bind(now - 1800000, EMAIL, JSON.stringify({ command: "say Restart at 18:00 for the 26.3.1 update" }))
    .run();
  env.archived.push({ key: `journal/${new Date(now).toISOString().slice(0, 10).replace(/-/g, "/")}/${"ab".repeat(16)}.ndjson.gz`, size: 48213, uploaded: new Date(now - 900000) });
}

// P13: two hours of experience minute rows through the real ingest (verdicts, a lag spike, a poor player, a
// maintenance annotation, a gap), for the Experience tab in the UI smoke test.
export async function seedExperience(env, now, handle, rt) {
  const players = [["3f2a0000-0000-4000-8000-000000000c41", "Em_K"], ["4f2a0000-0000-4000-8000-000000001c41", "MiaBuilds"], ["5f2a0000-0000-4000-8000-000000002c41", "jonas_r"]];
  const base = Math.floor(now / 60000) * 60000;
  for (let i = 120; i >= 1; i -= 1) {
    if (i > 60 && i < 66) continue; // a short gap
    const ts = base - i * 60000;
    const spike = i === 10;
    const pl = players.map(([u, n], k) => ({ u, n, ping: k === 2 ? 190 + (i % 7) * 10 : 35 + k * 10, ping_max: k === 2 ? 320 : 60, pings: [], view: k === 1 ? 32 : 10, send: 8, brand: k === 1 ? "fabric" : "vanilla", loc: "en_gb", w: "world", cx: 25 + k, cz: -73, ent: spike && k === 0 ? 820 : 20, glide: false, tips: k !== 1, join: now - 3 * 3600000, samples: 6 }));
    const row = {
      v: 1, id: `abcd0123:${200 - i}`, src: "abcd0123", seq: 200 - i, ts,
      srv: { ticks: 1200, tps: spike ? 18.6 : 20, mean: 11 + (i % 5), p50: 10 + (i % 4), p95: 18, p99: spike ? 72 : 22 + (i % 9), p999: spike ? 140 : 30 + (i % 7), max: spike ? 160 : 33 + (i % 11), spikes: spike ? 1 : 0, chunks: 1800, entities: 900 + (i % 50), tiles: 40, heap_used_mb: 2600, heap_max_mb: 5120, gc_heap_after_mb: 2100, gc_max_pause_ms: 9, self_p99_us: 7 + (i % 3), self_max_us: 48, budget_skips: 0, lost_windows: 0, samples: 6, worlds: [{ n: "world", chunks: 1500, entities: 820, ticking: 30, players: 3 }] },
      pl, ev: [], lag: spike ? [{ t: ts + 12000, ms: 160, ctx: { near: [{ n: "Em_K", ent: 820, w: "world", cx: 25, cz: -73 }], running: ["backup"], worlds: [{ n: "world", entities: 820, chunks: 1500 }] } }] : [],
      ovh: { "minecraft.service": { cpu_pct: 58.4, mem_mb: 5810 }, "mc-agent.service": { cpu_pct: 0.31, mem_mb: 26 } },
      backlog: { segments: 0, rows: 0, bytes: 0, oldest: null, stored_segments: 24, stored_bytes: 312000 }, stats: {}, raw: [], running: [],
    };
    await handle(new Request("https://hb.example.com/ingest/experience", { method: "POST", headers: { Authorization: `Bearer ${"a".repeat(48)}` }, body: JSON.stringify({ live: true, rows: [row] }) }), env, ts + 20000, rt);
  }
  await handle(new Request("https://hb.example.com/maintenance", { method: "POST", headers: { Authorization: `Bearer ${"h".repeat(48)}` }, body: JSON.stringify({ minutes: 20, reason: "deploy (mc-config@abc1234)" }) }), env, now - 45 * 60000, rt);
}
