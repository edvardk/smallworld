// Unit tests for watchdog/worker.js: node --import ./watchdog/test/register.mjs --test watchdog/test/
// No network: a fake connect() (web streams), a fake D1 and a fake fetch for Discord.
import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../worker.js";

const W = worker._internals;
const TOKEN = "t".repeat(48);
const MIN = 60000;
const T0 = Date.UTC(2026, 9, 31, 12, 0, 0);
const TARGET = {
  server_host: "mc.tests.mcctl.internal",
  targets: [
    { family: "v4", address: "203.0.113.10:25565" },
    { family: "v6", address: "[2001:db8::1]:25565" },
  ],
  checks: {
    health: { period_minutes: 5, grace_minutes: 10, alert_on_fail: false },
    backup: { period_minutes: 60, grace_minutes: 30, alert_on_fail: true },
  },
  probe: { attempts: 3, retry_seconds: 20, timeout_seconds: 0.05, alert_after_runs: 2 },
};

// --- fakes --------------------------------------------------------------------------------------------

class FakeD1 {
  constructor() {
    this.rows = new Map();
    this.created = false;
    this.writes = 0;
    this.onBatch = null; // a hook that runs before the next batch (simulates a concurrent request)
    this.failNext = 0; // make the next N SELECTs throw (D1 outage)
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
    if (this.onBatch) {
      const hook = this.onBatch;
      this.onBatch = null;
      await hook();
    }
    const out = [];
    for (const s of stmts) out.push(await s.run());
    return out;
  }
  exec(sql, args) {
    if (sql.startsWith("CREATE TABLE IF NOT EXISTS watchdog")) {
      this.created = true;
      return { results: [] };
    }
    assert.ok(this.created, "the table must be created before it is used");
    const changed = (n) => ({ results: [], meta: { changes: n } });
    if (sql === "SELECT key, value FROM watchdog") {
      if (this.failNext > 0) {
        this.failNext -= 1;
        throw new Error("D1_ERROR: database unavailable");
      }
      return { results: [...this.rows].map(([key, value]) => ({ key, value })) };
    }
    if (sql.includes("ON CONFLICT(key) DO NOTHING")) {
      assert.equal(args.length, 3);
      if (this.rows.has(args[0])) return changed(0);
      this.rows.set(args[0], args[1]);
      this.writes += 1;
      return changed(1);
    }
    if (sql.startsWith("UPDATE watchdog SET value = ?2")) {
      assert.equal(args.length, 4);
      if (this.rows.get(args[0]) !== args[3]) return changed(0);
      this.rows.set(args[0], args[1]);
      this.writes += 1;
      return changed(1);
    }
    if (sql.startsWith("INSERT INTO watchdog") && sql.includes("DO UPDATE")) {
      assert.equal(args.length, 3);
      this.rows.set(args[0], args[1]);
      this.writes += 1;
      return changed(1);
    }
    throw new Error(`unexpected SQL: ${sql}`);
  }
  get(key) {
    return this.rows.has(key) ? JSON.parse(this.rows.get(key)) : undefined;
  }
}

function statusPacket(obj) {
  const json = [...new TextEncoder().encode(JSON.stringify(obj))];
  const body = [...W.varint(0), ...W.varint(json.length), ...json];
  return new Uint8Array([...W.varint(body.length), ...body]);
}

const STATUS = { version: { name: "Purpur 26.3", protocol: 1 }, players: { online: 2, max: 20 } };

// behaviour(address) -> "ok" | "refuse" | "hang" | "garbage" | Error
function fakeRuntime(behaviour) {
  const rt = { connects: [], sleeps: [], posts: [], sent: [] };
  rt.connect = (address) => {
    rt.connects.push(address);
    const mode = behaviour(address);
    if (mode instanceof Error) throw mode;
    let controller;
    const readable = new ReadableStream({
      start(c) {
        controller = c;
      },
    });
    const writable = new WritableStream({
      write(chunk) {
        rt.sent.push(chunk);
        if (mode === "ok") {
          const bytes = statusPacket(STATUS);
          controller.enqueue(bytes.subarray(0, 3)); // arrives in two chunks: reassembled
          controller.enqueue(bytes.subarray(3));
        } else if (mode === "garbage") {
          controller.enqueue(new Uint8Array([3, 1, 2, 3]));
          controller.close();
        }
      },
    });
    const opened = mode === "refuse" ? Promise.reject(new Error(`connect to ${address} refused`)) : Promise.resolve({});
    opened.catch(() => {});
    return { opened, readable, writable, close() {} };
  };
  rt.sleep = async (ms) => {
    rt.sleeps.push(ms);
  };
  rt.discordDown = false;
  rt.fetch = async (url, init) => {
    rt.posts.push({ url, body: JSON.parse(init.body) });
    if (rt.discordDown) throw new Error("discord unreachable");
    return { ok: true };
  };
  return rt;
}

function env(db = new FakeD1(), extra = {}) {
  return {
    DB: db,
    HEARTBEAT_TOKEN: TOKEN,
    TARGET: JSON.stringify(TARGET),
    DISCORD_WEBHOOK: "https://discord.com/api/webhooks/1/x",
    ...extra,
  };
}

function req(path, { method = "POST", token = TOKEN, body = "", type = null } = {}) {
  const headers = token === null ? {} : { Authorization: `Bearer ${token}` };
  if (type) headers["Content-Type"] = type;
  return new Request(`https://hb.mc.tests.mcctl.internal${path}`, {
    method,
    headers,
    body: method === "GET" ? undefined : body,
  });
}

const up = () => "ok";
const v6down = (a) => (a.startsWith("[") ? "refuse" : "ok");

// --- the status ping protocol -------------------------------------------------------------------------

test("varint encoding (protocol -1 is five bytes)", () => {
  assert.deepEqual(W.varint(0), [0]);
  assert.deepEqual(W.varint(300), [0xac, 0x02]);
  assert.deepEqual(W.varint(-1), [0xff, 0xff, 0xff, 0xff, 0x0f]);
  assert.deepEqual(W.readVarint(new Uint8Array([0xac, 0x02]), 0), { value: 300, next: 2 });
  assert.equal(W.readVarint(new Uint8Array([0xac]), 0), null); // incomplete
  assert.throws(() => W.readVarint(new Uint8Array([0xff, 0xff, 0xff, 0xff, 0xff]), 0), /bad varint/);
});

test("handshake + status request bytes", () => {
  const b = W.statusRequest("mc.x.dev", 25565);
  const len = W.readVarint(b, 0);
  const id = W.readVarint(b, len.next);
  assert.equal(id.value, 0);
  const proto = W.readVarint(b, id.next);
  assert.equal(proto.value, 0xffffffff); // -1
  const hlen = W.readVarint(b, proto.next);
  const host = new TextDecoder().decode(b.subarray(hlen.next, hlen.next + hlen.value));
  assert.equal(host, "mc.x.dev");
  const at = hlen.next + hlen.value;
  assert.equal((b[at] << 8) | b[at + 1], 25565);
  assert.equal(b[at + 2], 1); // next state: status
  assert.equal(len.next + len.value, at + 3);
  assert.deepEqual([...b.subarray(at + 3)], [1, 0]); // status request
});

test("parseStatus needs the whole packet and a real status", () => {
  const full = statusPacket(STATUS);
  assert.equal(W.parseStatus(full.subarray(0, 5)), null);
  assert.equal(W.parseStatus(full).version.name, "Purpur 26.3");
  assert.throws(() => W.parseStatus(statusPacket({ hello: 1 })), /not a status response/);
  assert.throws(() => W.parseStatus(new Uint8Array([...W.varint(1 << 20), 0])), /too large/);
});

// --- cron: external probes ----------------------------------------------------------------------------

test("probes IPv4 and IPv6 separately; IPv6 literal bracketed", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const lines = await W.runChecks(env(db), T0, rt);
  assert.deepEqual(lines, []);
  assert.deepEqual(rt.connects.sort(), ["203.0.113.10:25565", "[2001:db8::1]:25565"]);
  assert.equal(db.get("probe:v4").ok, true);
  assert.equal(db.get("probe:v6").version, "Purpur 26.3");
  assert.equal(db.get("run").last_run, T0);
  assert.equal(rt.posts.length, 0);
});

test("a family alerts after 2 failed runs (3 tries each, 20 s apart), then recovers", async () => {
  const db = new FakeD1();
  await W.runChecks(env(db), T0, fakeRuntime(up)); // armed
  let rt = fakeRuntime(v6down);
  assert.deepEqual(await W.runChecks(env(db), T0 + 5 * MIN, rt), []);
  assert.equal(rt.connects.filter((a) => a.startsWith("[")).length, 3);
  assert.deepEqual(rt.sleeps, [20000, 20000]);
  rt = fakeRuntime(v6down);
  const lines = await W.runChecks(env(db), T0 + 10 * MIN, rt);
  assert.equal(lines.length, 1);
  assert.match(lines[0], /^\u{1f534} \*\*Players can't reach the server over IPv6\*\*\n\*\*Impact:\*\* Players on IPv6 can't connect; the other address family still works\./u);
  assert.match(lines[0], /\*\*Details:\*\* no Minecraft status reply over IPv6 for 2 runs/);
  assert.ok(!lines[0].includes("2001:db8"), "no address in alerts");
  assert.equal(rt.posts.length, 1);
  assert.match(rt.posts[0].body.content, /^\[mc\.tests\.mcctl\.internal\] \u{1f534} \*\*Players can't reach/u);
  assert.deepEqual(rt.posts[0].body.allowed_mentions, { parse: [] });
  rt = fakeRuntime(v6down);
  assert.deepEqual(await W.runChecks(env(db), T0 + 15 * MIN, rt), []); // alerted once only
  rt = fakeRuntime(up);
  const rec = await W.runChecks(env(db), T0 + 20 * MIN, rt);
  assert.equal(rec.length, 1);
  assert.match(rec[0], /^\u2705 \*\*The server can be reached over IPv6 again\*\*/);
  assert.match(rec[0], /\*\*Details:\*\* Purpur 26\.3; unreachable for \d+ min/);
});

test("no alert before the first successful ping (fresh server)", async () => {
  const db = new FakeD1();
  for (let i = 0; i < 4; i++) {
    assert.deepEqual(await W.runChecks(env(db), T0 + i * 5 * MIN, fakeRuntime(() => "refuse")), []);
  }
});

test("diagnosis: health heartbeat fresh = network problem, stale = host down", async () => {
  const db = new FakeD1();
  await W.runChecks(env(db), T0, fakeRuntime(up));
  await W.handle(req("/hb/health"), env(db), T0 + 9 * MIN, fakeRuntime(up));
  await W.runChecks(env(db), T0 + 5 * MIN, fakeRuntime(() => "refuse"));
  const lines = await W.runChecks(env(db), T0 + 10 * MIN, fakeRuntime(() => "refuse"));
  assert.equal(lines.length, 2);
  assert.ok(lines.every((l) => l.includes("network, firewall or DNS")));

  const db2 = new FakeD1();
  await W.runChecks(env(db2), T0, fakeRuntime(up));
  await W.runChecks(env(db2), T0 + 5 * MIN, fakeRuntime(() => "hang"));
  const lines2 = await W.runChecks(env(db2), T0 + 10 * MIN, fakeRuntime(() => "hang"));
  assert.ok(lines2.every((l) => l.includes("the host itself may be down") && l.includes("timed out")));
});

test("errors never carry the address (thrown, garbage, refused)", async () => {
  const db = new FakeD1();
  await W.runChecks(env(db), T0, fakeRuntime(up));
  const boom = (a) => (a.startsWith("[") ? new TypeError(`bad address ${a}`) : "garbage");
  await W.runChecks(env(db), T0 + 5 * MIN, fakeRuntime(boom));
  const resp = await W.handle(req("/status", { method: "GET", token: null }), env(db), T0 + 6 * MIN, fakeRuntime(up));
  const text = await resp.text();
  assert.ok(!text.includes("203.0.113.10") && !text.includes("2001:db8"), text);
  const st = JSON.parse(text);
  assert.match(st.probes.v6.error, /^TypeError: bad address <address>/);
  assert.equal(st.probes.v4.ok, false);
});

test("probes by name (the default) report per family and never leak the name or the resolved address", async () => {
  // First real setup: Cloudflare refused connect() to the IPv4 literal with exactly this error.
  const db = new FakeD1();
  const byName = {
    ...TARGET,
    targets: [
      { family: "v4", address: "v4.mc.example.com:25565" },
      { family: "v6", address: "v6.mc.example.com:25565" },
    ],
  };
  const e = env(db, { TARGET: JSON.stringify(byName) });
  await W.runChecks(e, T0, fakeRuntime(up));
  const refused = (a) =>
    a.startsWith("v4.") ? new Error(`proxy request failed, cannot connect to the specified address ${a} (v4.mc.example.com = 198.51.100.7)`) : "ok";
  const rt = fakeRuntime(refused);
  await W.runChecks(e, T0 + 5 * MIN, rt);
  assert.ok(rt.connects.includes("v4.mc.example.com:25565") && rt.connects.includes("v6.mc.example.com:25565"));
  const resp = await W.handle(req("/status", { method: "GET", token: null }), e, T0 + 6 * MIN, fakeRuntime(up));
  const text = await resp.text();
  assert.ok(!text.includes("v4.mc.example.com") && !text.includes("198.51.100.7"), text);
  const st = JSON.parse(text);
  assert.equal(st.probes.v6.ok, true);
  assert.equal(st.probes.v4.ok, false);
  assert.match(st.probes.v4.error, /^Error: proxy request failed, cannot connect to the specified address <address> \(<address> = <address>\)/);
});

test("maintenance silences alerts until it ends", async () => {
  const db = new FakeD1();
  await W.runChecks(env(db), T0, fakeRuntime(up));
  const r = await W.handle(
    req("/maintenance", { body: JSON.stringify({ minutes: 20, reason: "deploy" }) }),
    env(db),
    T0 + MIN,
    fakeRuntime(up),
  );
  assert.equal(r.status, 200);
  assert.deepEqual(await W.runChecks(env(db), T0 + 5 * MIN, fakeRuntime(() => "refuse")), []);
  assert.deepEqual(await W.runChecks(env(db), T0 + 10 * MIN, fakeRuntime(() => "refuse")), []);
  const st = await (await W.handle(req("/status", { method: "GET" }), env(db), T0 + 11 * MIN, fakeRuntime(up))).json();
  assert.equal(st.maintenance.reason, "deploy");
  const after = await W.runChecks(env(db), T0 + 25 * MIN, fakeRuntime(() => "refuse"));
  assert.equal(after.length, 2); // still down after the window: alert now
});

test("maintenance input validation and early end", async () => {
  const db = new FakeD1();
  const e = env(db);
  const rt = fakeRuntime(up);
  assert.equal((await W.handle(req("/maintenance", { body: "nope" }), e, T0, rt)).status, 400);
  assert.equal((await W.handle(req("/maintenance", { body: '{"minutes": 2000}' }), e, T0, rt)).status, 400);
  assert.equal((await W.handle(req("/maintenance", { body: '{"minutes": 1.5}' }), e, T0, rt)).status, 400);
  await W.handle(req("/maintenance", { body: '{"minutes": 30, "reason": "restore"}' }), e, T0, rt);
  await W.handle(req("/maintenance", { body: '{"minutes": 0}' }), e, T0 + MIN, rt);
  const st = await (await W.handle(req("/status", { method: "GET" }), e, T0 + 2 * MIN, rt)).json();
  assert.equal(st.maintenance, null);
});

// --- heartbeats ---------------------------------------------------------------------------------------

test("heartbeat auth: bearer token, constant-time compare, methods and paths", async () => {
  const e = env();
  const rt = fakeRuntime(up);
  assert.equal((await W.handle(req("/hb/health", { token: null }), e, T0, rt)).status, 401);
  assert.equal((await W.handle(req("/hb/health", { token: "short" }), e, T0, rt)).status, 401);
  assert.equal((await W.handle(req("/hb/health", { token: `${TOKEN}x` }), e, T0, rt)).status, 401);
  assert.equal((await W.handle(req("/hb/health", { method: "GET" }), e, T0, rt)).status, 405);
  assert.equal((await W.handle(req("/hb/other"), e, T0, rt)).status, 404);
  assert.equal((await W.handle(req("/"), e, T0, rt)).status, 404);
  assert.equal((await W.handle(req("/hb/health"), e, T0, rt)).status, 200);
  assert.equal((await W.handle(req("/hb/health"), env(new FakeD1(), { HEARTBEAT_TOKEN: "" }), T0, rt)).status, 401);
  assert.equal(await W.tokenOk(`Bearer ${TOKEN}`, TOKEN), true);
  assert.equal(await W.tokenOk(TOKEN, TOKEN), false); // scheme required
});

test("backup failure alerts once (health's doesn't: health.py alerts itself), success recovers", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  await W.handle(req("/hb/backup/start"), env(db), T0, rt);
  await W.handle(req("/hb/backup/fail", { body: "backup failed: restic exit 1\nmore" }), env(db), T0 + MIN, rt);
  await W.handle(req("/hb/backup/fail", { body: "again" }), env(db), T0 + 61 * MIN, rt);
  await W.handle(req("/hb/health/fail", { body: "DOWN" }), env(db), T0 + MIN, rt);
  const contents = rt.posts.map((p) => p.body.content);
  assert.equal(contents.length, 1);
  assert.match(contents[0], /^\[mc\.tests\.mcctl\.internal\] \u{1f534} \*\*The backup failed\*\*[\s\S]*It reported: backup failed: restic exit 1[\s\S]*`mcctl backup now`/u);
  await W.handle(req("/hb/backup"), env(db), T0 + 121 * MIN, rt);
  assert.match(rt.posts.at(-1).body.content, /^\[mc\.tests\.mcctl\.internal\] \u2705 \*\*The backup works again\*\*/);
  const c = db.get("check:backup");
  assert.equal(c.last_ok, T0 + 121 * MIN);
  assert.equal(c.alerted, false);
});

test("a missing heartbeat alerts after period + grace, once; the next one recovers", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const e = env(db, { TARGET: JSON.stringify({ ...TARGET, targets: [] }) });
  await W.handle(req("/hb/health"), e, T0, rt);
  assert.deepEqual(await W.runChecks(e, T0 + 15 * MIN, rt), []); // exactly period + grace: not late yet
  const lines = await W.runChecks(e, T0 + 16 * MIN, rt);
  assert.equal(lines.length, 1);
  assert.match(lines[0], /^\u{1f534} \*\*The health check hasn't run for 16 min\*\*[\s\S]*\*\*Details:\*\* no finished health run for 16 min \(expected every 5 min\)$/u);
  assert.deepEqual(await W.runChecks(e, T0 + 21 * MIN, rt), []);
  const st = await (await W.handle(req("/status", { method: "GET" }), e, T0 + 21 * MIN, rt)).json();
  assert.equal(st.checks.health.state, "late");
  assert.equal(st.ok, false);
  await W.handle(req("/hb/health"), e, T0 + 22 * MIN, rt);
  assert.match(rt.posts.at(-1).body.content, /^\[mc\.tests\.mcctl\.internal\] \u2705 \*\*The health check works again\*\*/);
});

test("a backup that starts but never finishes: too long after max_run, late after period + grace", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const e = env(db, { TARGET: JSON.stringify({ ...TARGET, targets: [] }) });
  await W.handle(req("/hb/backup/start"), e, T0, rt);
  assert.deepEqual(await W.runChecks(e, T0 + 60 * MIN, rt), []);
  const long = await W.runChecks(e, T0 + 65 * MIN, rt);
  assert.equal(long.length, 1);
  assert.match(long[0], /The backup is taking much longer than usual[\s\S]*the backup run started 65 min ago hasn't finished \(limit 60 min\)/);
  const late = await W.runChecks(e, T0 + 95 * MIN, rt);
  assert.equal(late.length, 1);
  assert.match(late[0], /The backup hasn't run for 95 min[\s\S]*no finished backup run for 95 min \(expected every 60 min\)/);
  assert.deepEqual(await W.runChecks(e, T0 + 100 * MIN, rt), []); // each alert once
  await W.handle(req("/hb/backup"), e, T0 + 101 * MIN, rt);
  assert.match(rt.posts.at(-1).body.content, /\u2705 \*\*The backup works again\*\*/);
});

test("P6 review repro: 24 hourly /start heartbeats with no finish alert (lateness is from last_ok)", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const e = env(db, { TARGET: JSON.stringify({ ...TARGET, targets: [] }) });
  await W.handle(req("/hb/backup"), e, T0, rt); // one good backup, then every run hangs
  const lines = [];
  for (let h = 1; h <= 24; h++) {
    await W.handle(req("/hb/backup/start"), e, T0 + h * 60 * MIN, rt);
    for (let m = 5; m <= 55; m += 5) lines.push(...(await W.runChecks(e, T0 + h * 60 * MIN + m * MIN, rt)));
  }
  const late = lines.filter((l) => l.includes("no finished backup run"));
  assert.equal(late.length, 1, lines.join("\n"));
  const st = await (await W.handle(req("/status", { method: "GET" }), e, T0 + 25 * 60 * MIN, rt)).json();
  assert.equal(st.checks.backup.state, "late");
  assert.equal(st.checks.backup.last_ok, new Date(T0).toISOString());
});

test("a check armed by /start alone goes late from when it was armed", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const e = env(db, { TARGET: JSON.stringify({ ...TARGET, targets: [] }) });
  for (let h = 0; h < 3; h++) await W.handle(req("/hb/backup/start"), e, T0 + h * 60 * MIN, rt);
  const lines = await W.runChecks(e, T0 + 125 * MIN, rt);
  assert.ok(lines.some((l) => l.includes("no finished backup run for 125 min")), lines.join("\n"));
});

test("health /fail heartbeats keep the health check fresh (health.py alerts on its own)", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const e = env(db, { TARGET: JSON.stringify({ ...TARGET, targets: [] }) });
  await W.handle(req("/hb/health"), e, T0, rt);
  for (let i = 1; i <= 12; i++) {
    await W.handle(req("/hb/health/fail", { body: "DOWN" }), e, T0 + i * 5 * MIN, rt);
    assert.deepEqual(await W.runChecks(e, T0 + i * 5 * MIN + MIN, rt), []);
  }
  assert.equal(rt.posts.length, 0);
});

test("an alert Discord didn't accept is not marked sent: the next run retries it", async () => {
  const db = new FakeD1();
  await W.runChecks(env(db), T0, fakeRuntime(up));
  await W.runChecks(env(db), T0 + 5 * MIN, fakeRuntime(v6down));
  let rt = fakeRuntime(v6down);
  rt.discordDown = true;
  assert.equal((await W.runChecks(env(db), T0 + 10 * MIN, rt)).length, 1);
  assert.equal(rt.posts.length, 1); // tried
  assert.equal(db.get("probe:v6").alerted, false); // ... but not recorded as sent
  rt = fakeRuntime(v6down);
  const lines = await W.runChecks(env(db), T0 + 15 * MIN, rt);
  assert.match(lines[0], /no Minecraft status reply over IPv6 for 3 runs/);
  assert.equal(db.get("probe:v6").alerted, true);
  // the same for a heartbeat /fail alert
  const hb = fakeRuntime(up);
  hb.discordDown = true;
  await W.handle(req("/hb/backup/fail", { body: "restic exit 1" }), env(db), T0 + 16 * MIN, hb);
  assert.equal(db.get("check:backup").alerted, false);
  const hb2 = fakeRuntime(up);
  await W.handle(req("/hb/backup/fail", { body: "restic exit 1" }), env(db), T0 + 76 * MIN, hb2);
  assert.equal(hb2.posts.length, 1);
  assert.equal(db.get("check:backup").alerted, "fail");
});

test("a heartbeat that lands during a cron run is never overwritten (compare-and-set)", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  const e = env(db, { TARGET: JSON.stringify({ ...TARGET, targets: [] }) });
  await W.handle(req("/hb/backup"), e, T0, rt);
  // The cron reads the state (backup 100 min old = late), then the backup reports in before the cron writes.
  db.onBatch = () => W.handle(req("/hb/backup"), e, T0 + 100 * MIN, fakeRuntime(up));
  const lines = await W.runChecks(e, T0 + 100 * MIN, rt);
  assert.deepEqual(lines, []); // the LATE alert belonged to the write that lost: dropped
  const c = db.get("check:backup");
  assert.equal(c.last_ok, T0 + 100 * MIN);
  assert.equal(c.alerted, false);
  assert.equal(db.get("run").last_run, T0 + 100 * MIN); // the cron's own rows still landed
});

test("concurrent heartbeats retry their compare-and-set instead of losing an update", async () => {
  const db = new FakeD1();
  const e = env(db);
  await W.handle(req("/hb/backup/start"), e, T0, fakeRuntime(up));
  db.onBatch = () => W.handle(req("/hb/backup/fail", { body: "x" }), e, T0 + MIN, fakeRuntime(up));
  const r = await W.handle(req("/hb/backup"), e, T0 + 2 * MIN, fakeRuntime(up));
  assert.equal(r.status, 200);
  const c = db.get("check:backup");
  assert.equal(c.last_fail, T0 + MIN); // the concurrent /fail survived
  assert.equal(c.last_ok, T0 + 2 * MIN);
});

test("a failing cron run is reported (at most hourly), rethrown, and its recovery announced", async () => {
  W.resetIsolate();
  const db = new FakeD1();
  const e = env(db);
  await W.runChecks(e, T0, fakeRuntime(up));
  db.failNext = 1;
  let rt = fakeRuntime(up);
  await assert.rejects(W.scheduledRun(e, T0 + 5 * MIN, rt), /database unavailable/);
  assert.equal(rt.posts.length, 1);
  assert.match(rt.posts[0].body.content, /The outside watchdog itself is failing[\s\S]*Its scheduled run failed: Error: D1_ERROR/);
  db.failNext = 1;
  rt = fakeRuntime(up);
  await assert.rejects(W.scheduledRun(e, T0 + 10 * MIN, rt));
  assert.equal(rt.posts.length, 0); // not again within the hour
  const st = await (await W.handle(req("/status", { method: "GET" }), e, T0 + 11 * MIN, rt)).json();
  assert.equal(st.cron_error, true);
  assert.equal(st.ok, false);
  assert.equal(st.last_run_age_s, 11 * 60); // last_run only moves on a clean run
  rt = fakeRuntime(up);
  const rec = await W.scheduledRun(e, T0 + 15 * MIN, rt);
  assert.equal(rec.length, 1);
  assert.match(rec[0], /^\u2705 \*\*The outside watchdog runs cleanly again\*\*/);
});

test("a failing maintenance step has its own alert, and the cron run still succeeds (P13 review L5)", async () => {
  W.resetIsolate();
  const db = new FakeD1();
  const broken = { prepare: () => { throw new Error("D1_ERROR: telemetry database unavailable"); }, batch: async () => { throw new Error("x"); } };
  const e = env(db, { TELEMETRY: broken });
  let rt = fakeRuntime(up);
  const lines = await W.scheduledRun(e, T0, rt);
  assert.ok(Array.isArray(lines));
  assert.equal(rt.posts.length, 1);
  assert.match(rt.posts[0].body.content, /The panel's experience housekeeping failed[\s\S]*the outside checks ran normally[\s\S]*Error: D1_ERROR: telemetry database unavailable\./);
  assert.doesNotMatch(rt.posts[0].body.content, /external checks are not running/);
  assert.equal(db.get("run").last_run, T0); // the checks ran and persisted
  rt = fakeRuntime(up);
  await W.scheduledRun(e, T0 + 5 * MIN, rt);
  assert.equal(rt.posts.length, 0); // at most hourly
  const st = await (await W.handle(req("/status", { method: "GET" }), e, T0 + 6 * MIN, rt)).json();
  assert.equal(st.cron_error, false);
});

test("heartbeat and maintenance replies carry the cron's age", async () => {
  const db = new FakeD1();
  const e = env(db);
  const rt = fakeRuntime(up);
  const r0 = await (await W.handle(req("/hb/health"), e, T0, rt)).json();
  assert.equal(r0.last_run_age_s, null); // the cron never ran
  await W.runChecks(e, T0, rt);
  const r1 = await (await W.handle(req("/hb/health"), e, T0 + 20 * MIN, rt)).json();
  assert.equal(r1.last_run_age_s, 20 * 60);
  const m = await (await W.handle(req("/maintenance", { body: '{"minutes": 5}' }), e, T0 + 21 * MIN, rt)).json();
  assert.equal(m.last_run_age_s, 21 * 60);
});

test("admin-stopped (from health.py's JSON heartbeat) silences the probes, not a dead host", async () => {
  const db = new FakeD1();
  const e = env(db);
  await W.runChecks(e, T0, fakeRuntime(up));
  const body = JSON.stringify({ message: "status=down", admin_stopped: true });
  await W.handle(req("/hb/health", { body, type: "application/json" }), e, T0 + MIN, fakeRuntime(up));
  assert.equal(db.get("check:health").admin_stopped, true);
  for (let i = 1; i <= 3; i++) {
    const lines = await W.runChecks(e, T0 + i * 5 * MIN, fakeRuntime(() => "refuse"));
    assert.deepEqual(lines, []);
    await W.handle(req("/hb/health", { body, type: "application/json" }), e, T0 + i * 5 * MIN + MIN, fakeRuntime(up));
  }
  const st = await (await W.handle(req("/status", { method: "GET" }), e, T0 + 16 * MIN, fakeRuntime(up))).json();
  assert.equal(st.admin_stopped, true);
  assert.equal(st.ok, true);
  // The host dies while stopped: the health heartbeat goes late and that still alerts.
  const lines = await W.runChecks(e, T0 + 40 * MIN, fakeRuntime(() => "refuse"));
  assert.equal(lines.length, 1);
  assert.match(lines[0], /no finished health run for 24 min \(expected every 5 min\)/);
  // Started again: a plain-text heartbeat doesn't touch the flag, a JSON one clears it.
  await W.handle(req("/hb/health", { body: "text" }), e, T0 + 41 * MIN, fakeRuntime(up));
  assert.equal(db.get("check:health").admin_stopped, true);
  const clear = JSON.stringify({ message: "ok", admin_stopped: false });
  await W.handle(req("/hb/health", { body: clear, type: "application/json" }), e, T0 + 42 * MIN, fakeRuntime(up));
  assert.equal(db.get("check:health").admin_stopped, false);
});

test("describe() redacts any IPv4/IPv6 literal, not times", () => {
  const err = new Error("connect to [2001:db8:abcd::42]:25565 via 2001:db8:0:0:0:0:0:1 or 198.51.100.7 at 12:30:45");
  const text = W.describe(err, []);
  assert.ok(!/2001|2a01|198\.51/.test(text), text);
  assert.match(text, /at 12:30:45$/);
  assert.equal(W.describe(new Error("fe80::1%eth0 refused"), []), "Error: <address> refused");
  assert.equal(W.describe(new Error("::ffff:192.0.2.1 down"), []).includes("192.0.2"), false);
});

test("/status: freshness only, unauthenticated, no addresses", async () => {
  const db = new FakeD1();
  const rt = fakeRuntime(up);
  await W.runChecks(env(db), T0, rt);
  await W.handle(req("/hb/health"), env(db), T0, rt);
  const resp = await W.handle(req("/status", { method: "GET", token: null }), env(db), T0 + 60000, rt);
  assert.equal(resp.headers.get("Cache-Control"), "no-store");
  const st = await resp.json();
  assert.deepEqual(Object.keys(st).sort(), [
    "admin_stopped", "checks", "client", "cron_error", "last_run", "last_run_age_s", "maintenance", "ok", "probes",
  ]);
  assert.equal(st.client, false, "no CLIENT binding here");
  assert.equal(st.ok, true);
  assert.equal(st.last_run_age_s, 60);
  assert.equal(st.checks.health.state, "ok");
  assert.equal(st.checks.backup.state, "unknown");
  assert.deepEqual(Object.keys(st.probes), ["v4", "v6"]);
  assert.ok(!JSON.stringify(st).includes("203.0.113"));
  assert.equal((await W.handle(req("/status"), env(db), T0, rt)).status, 405);
});

test("no Discord webhook: alerts are still recorded, nothing is posted", async () => {
  const db = new FakeD1();
  const e = env(db, { DISCORD_WEBHOOK: undefined });
  await W.runChecks(e, T0, fakeRuntime(up));
  await W.runChecks(e, T0 + 5 * MIN, fakeRuntime(v6down));
  const rt = fakeRuntime(v6down);
  assert.equal((await W.runChecks(e, T0 + 10 * MIN, rt)).length, 1);
  assert.equal(rt.posts.length, 0);
  assert.equal(db.get("probe:v6").alerted, true);
});

test("config: defaults, bad JSON, unknown families dropped", () => {
  assert.deepEqual(W.config({ TARGET: "not json" }).targets, []);
  const c = W.config({ TARGET: JSON.stringify({ targets: [{ family: "v5", address: "x:1" }, { family: "v4", address: "a:1" }] }) });
  assert.deepEqual(c.targets, [{ family: "v4", address: "a:1" }]);
  assert.equal(c.checks.backup.grace_minutes, 30);
  assert.equal(c.probe.attempts, 3);
});

test("the scheduled and fetch handlers exist on the default export", () => {
  assert.equal(typeof worker.scheduled, "function");
  assert.equal(typeof worker.fetch, "function");
});
