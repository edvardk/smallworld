// P8 tests for watchdog/worker.js: agent ingest (metrics, logs, archive), the Control Durable Object (signed
// commands, WebSocket delivery, poll fallback, results), D1 retention and the agent-aware DOWN alert.
// node --import ./watchdog/test/register.mjs --test watchdog/test/*.test.mjs  (no network, no npm packages)
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { gzipSync } from "node:zlib";
import { test } from "node:test";

import worker, { Control } from "../worker.js";

const W = worker._internals;
const AGENT = "a".repeat(48);
const KEY = "k".repeat(64);
const T0 = Date.UTC(2026, 9, 31, 12, 0, 0);
const DAY = 86400000;
const MONITORING = JSON.stringify({ retention_days: 90, rollup_after_days: 7, logs: { d1_days: 14, max_rows_per_day: 3 } });

// --- fakes --------------------------------------------------------------------------------------------

// Just enough SQL for the Worker's statements: watchdog key/value rows plus the monitoring tables.
class FakeD1 {
  constructor() {
    this.tables = new Map(); // name -> array of row objects
    this.kv = new Map();
    this.written = 0;
  }
  prepare(sql) {
    const db = this;
    const stmt = (args) => ({
      bind: (...a) => stmt(a),
      run: async () => db.exec(sql, args),
      all: async () => db.exec(sql, args),
    });
    return stmt([]);
  }
  async batch(stmts) {
    const out = [];
    for (const s of stmts) out.push(await s.run());
    return out;
  }
  table(name) {
    assert.ok(this.tables.has(name), `table ${name} used before it was created`);
    return this.tables.get(name);
  }
  exec(sql, a) {
    const changed = (n) => {
      this.written += n;
      return { results: [], meta: { changes: n } };
    };
    let m;
    if ((m = /^CREATE TABLE IF NOT EXISTS (\w+) /.exec(sql))) {
      if (m[1] !== "watchdog" && !this.tables.has(m[1])) this.tables.set(m[1], []);
      return { results: [] };
    }
    if (sql === "SELECT key, value FROM watchdog") {
      return { results: [...this.kv].map(([key, value]) => ({ key, value })) };
    }
    if (sql.includes("INTO watchdog") && sql.includes("DO NOTHING")) {
      if (this.kv.has(a[0])) return changed(0);
      this.kv.set(a[0], a[1]);
      return changed(1);
    }
    if (sql.startsWith("UPDATE watchdog")) {
      if (this.kv.get(a[0]) !== a[3]) return changed(0);
      this.kv.set(a[0], a[1]);
      return changed(1);
    }
    if (sql.startsWith("INSERT INTO watchdog") && sql.includes("DO UPDATE")) {
      this.kv.set(a[0], a[1]);
      return changed(1);
    }
    if ((m = /^INSERT OR (IGNORE|REPLACE) INTO (\w+) \(ts, data\)/.exec(sql))) {
      const rows = this.table(m[2]);
      const i = rows.findIndex((r) => r.ts === a[0]);
      if (i >= 0 && m[1] === "IGNORE") return changed(0);
      if (i >= 0) rows.splice(i, 1);
      rows.push({ ts: a[0], data: a[1] });
      return changed(1);
    }
    if ((m = /^SELECT rows FROM (log_days|sample_days) WHERE day = \?1$/.exec(sql))) {
      return { results: this.table(m[1]).filter((r) => r.day === a[0]) };
    }
    if ((m = /^INSERT INTO (log_days|sample_days) /.exec(sql))) {
      const rows = this.table(m[1]);
      const r = rows.find((x) => x.day === a[0]);
      if (r) r.rows += a[1];
      else rows.push({ day: a[0], rows: a[1] });
      return changed(1);
    }
    if ((m = /^INSERT INTO (logs_\d{8}) \(ts, unit, prio, audit, msg\)/.exec(sql))) {
      this.table(m[1]).push({ ts: a[0], unit: a[1], prio: a[2], audit: a[3], msg: a[4] });
      return changed(1);
    }
    if (sql.startsWith("SELECT name FROM sqlite_master")) {
      return { results: [...this.tables.keys()].filter((n) => /^(samples|logs)_/.test(n)).map((name) => ({ name })) };
    }
    if ((m = /^SELECT ts, data FROM (samples_\d{8})$/.exec(sql))) return { results: [...this.table(m[1])] };
    if ((m = /^SELECT COUNT\(\*\) AS n FROM (samples_\d{8})$/.exec(sql))) return { results: [{ n: this.table(m[1]).length }] };
    if ((m = /^SELECT MAX\(ts\) AS ts FROM (samples_\d{8})$/.exec(sql))) {
      const rows = this.table(m[1]);
      return { results: [{ ts: rows.length ? Math.max(...rows.map((r) => r.ts)) : null }] };
    }
    if ((m = /^DROP TABLE IF EXISTS (\w+)$/.exec(sql))) {
      this.tables.delete(m[1]);
      return changed(0); // a whole-table drop writes no rows
    }
    if (sql === "DELETE FROM samples_5m WHERE ts < ?1") {
      const rows = this.table("samples_5m");
      const keep = rows.filter((r) => r.ts >= a[0]);
      this.tables.set("samples_5m", keep);
      return changed(rows.length - keep.length);
    }
    if ((m = /^DELETE FROM (log_days|sample_days) WHERE day < \?1$/.exec(sql))) {
      const rows = this.table(m[1]);
      const keep = rows.filter((r) => r.day >= a[0]);
      this.tables.set(m[1], keep);
      return changed(rows.length - keep.length);
    }
    throw new Error(`unexpected SQL: ${sql}`);
  }
}

class FakeStorage {
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

class FakeSocket {
  constructor() {
    this.sent = [];
    this.closed = null;
  }
  send(text) {
    this.sent.push(JSON.parse(text));
  }
  close(code) {
    this.closed = code;
  }
}

function fakeCtx() {
  const sockets = [];
  return {
    storage: new FakeStorage(),
    sockets,
    acceptWebSocket: (ws, tags) => sockets.push({ ws, tags }),
    getWebSockets: (tag) => sockets.filter((s) => s.tags.includes(tag)).map((s) => s.ws),
  };
}

function makeEnv(extra = {}) {
  const env = {
    DB: new FakeD1(),
    AGENT_TOKEN: AGENT,
    HEARTBEAT_TOKEN: "h".repeat(48),
    COMMAND_HMAC_KEY: KEY,
    MONITORING,
    puts: [],
    ...extra,
  };
  env.LOGS = { put: async (key, body, opts) => env.puts.push({ key, body, opts }) };
  env.control = new Control(fakeCtx(), env);
  env.CONTROL = {
    idFromName: (n) => n,
    get: () => ({ fetch: (req) => env.control.fetch(req) }),
  };
  return env;
}

function req(path, { method = "POST", token = AGENT, body, headers = {} } = {}) {
  const h = { ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  const init = { method, headers: h };
  if (body !== undefined) init.body = typeof body === "string" || body instanceof Uint8Array ? body : JSON.stringify(body);
  return new Request(`https://hb.mc.tests.mcctl.internal${path}`, init);
}

const rt = { connect: () => { throw new Error("no sockets here"); }, sleep: async () => {}, fetch: async () => ({ ok: true }) };

// --- ingest --------------------------------------------------------------------------------------------

test("ingest needs the agent token, not the heartbeat token, and is off without the bindings", async () => {
  const env = makeEnv();
  const body = { samples: [{ ts: T0 }] };
  assert.equal((await W.handle(req("/ingest/metrics", { body }), env, T0, rt)).status, 200);
  assert.equal((await W.handle(req("/ingest/metrics", { body, token: env.HEARTBEAT_TOKEN }), env, T0, rt)).status, 401);
  assert.equal((await W.handle(req("/ingest/metrics", { body, token: null }), env, T0, rt)).status, 401);
  const off = makeEnv({ MONITORING: undefined });
  assert.equal((await W.handle(req("/ingest/metrics", { body }), off, T0, rt)).status, 404);
  assert.equal((await W.handle(req("/ingest/metrics", { method: "GET" }), env, T0, rt)).status, 404);
});

test("metrics land in day tables, one row per sample, cleaned and deduplicated", async () => {
  const env = makeEnv();
  const samples = [
    { ts: T0 - DAY, tps: 20, players: 2 },
    { ts: T0, tps: 19.5, up: true, nested: { x: 1 }, "Bad Key": 1, msg: "text" },
    { ts: T0, tps: 1 }, // duplicate ts: ignored
    { ts: T0 - 26 * 3600000, tps: 20 }, // older than the spool keeps: rejected
    { ts: T0 + DAY, tps: 20 }, // from the future: rejected
    { tps: 20 },
  ];
  const r = await (await W.handle(req("/ingest/metrics", { body: { samples } }), env, T0, rt)).json();
  assert.deepEqual(r, { ok: true, stored: 2, rejected: 3, capped: 0 });
  assert.equal(env.DB.table("samples_20261030").length, 1);
  const today = env.DB.table("samples_20261031");
  assert.equal(today.length, 1);
  assert.deepEqual(JSON.parse(today[0].data), { tps: 19.5, up: true, nested: null, msg: null });
  const tooMany = { samples: Array.from({ length: 501 }, (_, i) => ({ ts: T0 - i })) };
  assert.equal((await W.handle(req("/ingest/metrics", { body: tooMany }), env, T0, rt)).status, 400);
  assert.equal((await W.handle(req("/ingest/metrics", { body: "{not json" }), env, T0, rt)).status, 400);
});

test("samples are capped per UTC day (twice the configured rate), counted without writing rows", async () => {
  const env = makeEnv({ MONITORING: JSON.stringify({ metrics_interval_s: 300 }) }); // cap: 288 * 2 = 576
  const batch = (start, n) => ({ samples: Array.from({ length: n }, (_, i) => ({ ts: T0 - (start + i) * 1000, tps: 20 })) });
  let r = await (await W.handle(req("/ingest/metrics", { body: batch(0, 500) }), env, T0, rt)).json();
  assert.deepEqual([r.stored, r.capped], [500, 0]);
  const before = env.DB.written;
  r = await (await W.handle(req("/ingest/metrics", { body: batch(500, 100) }), env, T0, rt)).json();
  assert.deepEqual([r.stored, r.capped], [76, 24]);
  assert.equal(env.DB.written - before, 76); // the count itself writes nothing
  r = await (await W.handle(req("/ingest/metrics", { body: batch(600, 5) }), env, T0, rt)).json();
  assert.deepEqual([r.stored, r.capped], [0, 5]);
  // a fresh isolate (new binding view) counts the table once and enforces the same cap
  const env2 = { ...env, DB: Object.assign(Object.create(Object.getPrototypeOf(env.DB)), env.DB) };
  r = await (await W.handle(req("/ingest/metrics", { body: batch(700, 5) }), env2, T0, rt)).json();
  assert.deepEqual([r.stored, r.capped], [0, 5]);
});

test("log rows are capped per UTC day across requests", async () => {
  const env = makeEnv();
  const rows = (n, ts = T0) => Array.from({ length: n }, (_, i) => ({ ts: ts + i, unit: "minecraft.service", prio: 6, audit: 0, msg: `m${i}` }));
  let r = await (await W.handle(req("/ingest/logs", { body: { rows: rows(2) } }), env, T0, rt)).json();
  assert.deepEqual(r, { ok: true, stored: 2, dropped: 0 });
  r = await (await W.handle(req("/ingest/logs", { body: { rows: [...rows(2), ...rows(1, T0 + DAY / 2 + DAY / 3)] } }), env, T0 + DAY, rt)).json();
  assert.deepEqual(r, { ok: true, stored: 2, dropped: 1 }); // 1 more today (cap 3), 1 tomorrow
  assert.equal(env.DB.table("logs_20261031").length, 3);
  assert.equal(env.DB.table("logs_20261101").length, 1);
  assert.deepEqual(env.DB.table("log_days"), [{ day: "20261031", rows: 3 }, { day: "20261101", rows: 1 }]);
});

test("archives go to R2 under the day's prefix, gzip only", async () => {
  const env = makeEnv();
  const gz = new Uint8Array(gzipSync('{"msg":"x"}\n'));
  const ok = await W.handle(req("/ingest/archive?batch=" + "ab".repeat(16), { body: gz, headers: { "Content-Type": "application/gzip" } }), env, T0, rt);
  assert.equal(ok.status, 200);
  assert.equal(env.puts.length, 1);
  assert.equal(env.puts[0].key, `journal/2026/10/31/${"ab".repeat(16)}.ndjson.gz`);
  assert.equal(env.puts[0].opts.httpMetadata.contentType, "application/gzip");
  assert.equal((await W.handle(req("/ingest/archive?batch=" + "ab".repeat(16), { body: "plain" }), env, T0, rt)).status, 400);
  assert.equal((await W.handle(req("/ingest/archive?batch=../x", { body: gz }), env, T0, rt)).status, 400);
});

// --- commands -------------------------------------------------------------------------------------------

test("the command signature matches the agent's (shared test vector with tests/test_agent.py)", async () => {
  const sig = await W.hmacHex(KEY, '{"id":"x"}');
  assert.equal(sig, "185e8cabe8ec874b9adbc8d200ab798033279c0ff095465c6cb31a0803ce3e2a");
  assert.equal(sig, createHmac("sha256", KEY).update('{"id":"x"}').digest("hex"));
});

test("enqueue signs, delivers over the socket, and the poll fallback sees it until a result arrives", async () => {
  const env = makeEnv();
  const ws = new FakeSocket();
  env.control.ctx.acceptWebSocket(ws, ["agent"]);
  const bad = await env.control.fetch(new Request("https://control/enqueue", { method: "POST", body: JSON.stringify({ action: "shell", login: "ed" }) }));
  assert.equal(bad.status, 400);
  const res = await (await env.control.fetch(new Request("https://control/enqueue", { method: "POST", body: JSON.stringify({ action: "console", args: { command: "list" }, login: "admin@x" }) }))).json();
  assert.ok(res.ok && /^[0-9a-f]{24}$/.test(res.id));
  assert.equal(ws.sent.length, 1);
  const { payload, sig } = ws.sent[0];
  assert.equal(ws.sent[0].type, "command");
  assert.equal(sig, createHmac("sha256", KEY).update(payload).digest("hex"));
  const cmd = JSON.parse(payload);
  assert.deepEqual(Object.keys(cmd).sort(), ["action", "args", "iat", "id", "login", "nonce"]);
  assert.equal(cmd.id, res.id);
  assert.equal(cmd.nonce.length, 32);

  // poll fallback through the Worker, with the agent token
  const polled = await (await W.handle(req("/agent/commands", { method: "GET" }), env, T0, rt)).json();
  assert.deepEqual(polled.commands, [{ payload, sig }]);
  // results: unknown ids ignored, first result wins (a replay's "rejected" can't overwrite it)
  const post = (results) => W.handle(req("/agent/results", { body: { results } }), env, T0, rt);
  assert.deepEqual(await (await post([{ id: "f".repeat(24), ok: true }])).json(), { ok: true, stored: 0 });
  assert.deepEqual(await (await post([{ id: res.id, ok: true, output: "There are 0 players" }])).json(), { ok: true, stored: 1 });
  assert.deepEqual(await (await post([{ id: res.id, ok: false, error: "replayed nonce" }])).json(), { ok: true, stored: 0 });
  const stored = await (await env.control.fetch(new Request(`https://control/result?id=${res.id}`))).json();
  assert.equal(stored.result.ok, true);
  assert.equal(stored.result.output, "There are 0 players");
  assert.deepEqual((await (await W.handle(req("/agent/commands", { method: "GET" }), env, T0, rt)).json()).commands, []);
  assert.equal((await W.handle(req("/agent/commands", { method: "GET", token: "x".repeat(48) }), env, T0, rt)).status, 401);
});

test("commands expire after 60 s; a reconnecting agent gets the pending ones on hello; results over the socket", async () => {
  const env = makeEnv();
  const ctl = env.control;
  await ctl.enqueue({ action: "status", login: "ed" }, T0 - 120000);
  const fresh = await (await ctl.enqueue({ action: "backup", login: "ed" }, Date.now())).json();
  const ws = new FakeSocket();
  await ctl.webSocketMessage(ws, JSON.stringify({ type: "hello", agent: "1" }));
  assert.equal(ws.sent.length, 1);
  assert.equal(JSON.parse(ws.sent[0].payload).action, "backup");
  assert.equal((await ctl.ctx.storage.list({ prefix: "cmd:" })).size, 1); // the stale one was removed
  await ctl.webSocketMessage(ws, JSON.stringify({ type: "result", id: fresh.id, ok: true, output: "backup started" }));
  assert.equal((await ctl.ctx.storage.get(`result:${fresh.id}`)).output, "backup started");
  await ctl.webSocketMessage(ws, "not json"); // ignored
});

test("a new agent socket replaces the old one (hibernation API)", async () => {
  const env = makeEnv();
  const old = new FakeSocket();
  env.control.ctx.acceptWebSocket(old, ["agent"]);
  const server = new FakeSocket();
  const realResponse = globalThis.Response;
  globalThis.WebSocketPair = class {
    constructor() {
      this[0] = { client: true };
      this[1] = server;
    }
  };
  globalThis.Response = class extends realResponse {
    constructor(body, init) {
      super(body, init && init.status === 101 ? { status: 200 } : init);
      this.upgraded = init && init.status === 101 ? init.webSocket : null;
    }
  };
  try {
    const r = await W.handle(req("/agent/ws", { method: "GET", headers: { Upgrade: "websocket" } }), env, T0, rt);
    assert.deepEqual(r.upgraded, { client: true });
  } finally {
    globalThis.Response = realResponse;
    delete globalThis.WebSocketPair;
  }
  assert.equal(old.closed, 1000);
  assert.deepEqual(env.control.ctx.getWebSockets("agent"), [old, server]);
  assert.equal((await W.handle(req("/agent/ws", { method: "GET" }), env, T0, rt)).status, 426);
});

// --- retention --------------------------------------------------------------------------------------------

test("retention: old sample days become 5-minute rollups, old log days are dropped, once a day", async () => {
  const env = makeEnv();
  const db = env.DB;
  const old = "20261020"; // 11 days before T0
  db.tables.set(`samples_${old}`, [
    { ts: Date.UTC(2026, 9, 20, 0, 0, 0), data: JSON.stringify({ tps: 20, players: 1, up: true }) },
    { ts: Date.UTC(2026, 9, 20, 0, 1, 0), data: JSON.stringify({ tps: 18, players: 3, up: false }) },
    { ts: Date.UTC(2026, 9, 20, 0, 5, 0), data: JSON.stringify({ tps: 19, players: null }) },
  ]);
  db.tables.set("samples_20261030", [{ ts: T0 - DAY, data: "{}" }]);
  db.tables.set("logs_20261001", [{ ts: 1 }]);
  db.tables.set("logs_20261030", [{ ts: 1 }]);
  db.tables.set("samples_5m", [{ ts: T0 - 100 * DAY, data: "{}" }]);
  db.tables.set("log_days", [{ day: "20261001", rows: 1 }, { day: "20261030", rows: 1 }]);
  const done = await W.maintainStorage(env, T0);
  assert.deepEqual(done, { rolled: [`samples_${old}`], dropped: ["logs_20261001"] });
  assert.ok(!db.tables.has(`samples_${old}`) && db.tables.has("samples_20261030") && db.tables.has("logs_20261030"));
  const rollups = db.table("samples_5m").map((r) => ({ ts: r.ts, ...JSON.parse(r.data) }));
  assert.deepEqual(rollups, [
    { ts: Date.UTC(2026, 9, 20, 0, 0, 0), tps: 19, players: 2, up: false },
    { ts: Date.UTC(2026, 9, 20, 0, 5, 0), tps: 19 },
  ]); // the 100-day-old rollup is gone
  assert.deepEqual(db.table("log_days"), [{ day: "20261030", rows: 1 }]);
  assert.equal(await W.maintainStorage(env, T0 + 3600000), null); // not again within a day
});

test("the cron runs retention only with monitoring on", async () => {
  const env = makeEnv({ TARGET: JSON.stringify({ targets: [] }) });
  await W.scheduledRun(env, T0, rt);
  assert.equal(JSON.parse(env.DB.kv.get("retention")).last_run, T0);
  const off = makeEnv({ TARGET: JSON.stringify({ targets: [] }), MONITORING: undefined });
  await W.scheduledRun(off, T0, rt);
  assert.equal(off.DB.kv.get("retention"), undefined);
});

// --- DOWN: host down vs network down (design §5a) ---------------------------------------------------------

async function downAlert(sampleAgeMs) {
  const posts = [];
  const env = makeEnv({
    TARGET: JSON.stringify({ targets: [{ family: "v4", address: "203.0.113.10:25565" }], probe: { attempts: 1 } }),
    DISCORD_WEBHOOK: "https://discord.example/hook",
  });
  const runtime = { ...rt, fetch: async (url, init) => posts.push(JSON.parse(init.body).content) && { ok: true } };
  env.DB.kv.set("probe:v4", JSON.stringify({ failed_runs: 1, alerted: false, last_ok: T0 - 600000 }));
  env.DB.tables.set("samples_20261031", [{ ts: T0 - sampleAgeMs, data: "{}" }]);
  await W.runChecks(env, T0, runtime);
  return posts.join("\n");
}

test("the DOWN alert tells host down from network down by the agent's samples", async () => {
  assert.match(await downAlert(60000), /the agent's samples arrive\), so the host is up/);
  assert.match(await downAlert(600000), /No agent sample for over 3 minutes either: the host itself may be down/);
});

test("D1 rows written at the defaults stay as budgeted (one per sample; drops write none)", async () => {
  const env = makeEnv();
  const samples = Array.from({ length: 60 }, (_, i) => ({ ts: T0 - i * 60000, tps: 20 }));
  const before = env.DB.written;
  await W.handle(req("/ingest/metrics", { body: { samples } }), env, T0, rt);
  assert.equal(env.DB.written - before, 60);
});

test("the Control object answers the agent's keepalive ping without waking (auto-response)", () => {
  const pairs = [];
  globalThis.WebSocketRequestResponsePair = class {
    constructor(request, response) {
      Object.assign(this, { request, response });
    }
  };
  try {
    new Control({ ...fakeCtx(), setWebSocketAutoResponse: (p) => pairs.push(p) }, {});
  } finally {
    delete globalThis.WebSocketRequestResponsePair;
  }
  assert.deepEqual(pairs.map((p) => [p.request, p.response]), [["ping", "pong"]]);
});
