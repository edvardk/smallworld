// P13 tests for watchdog/worker.js: player experience. The verdict engine (rules, explainable reasons), ingest
// (idempotent on the minute, sessions keyed by the plugin's join time, live vs backfill), in-game messages (the
// signed `player_message` command, rate limits, broadcast, join summary, tips off), rollups + backfilled hours,
// retention (day/month tables dropped whole), the remedies' availability check, R2 segments, the panel API and
// the D1 write budget per minute. Real SQL through node:sqlite; no network.
import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../worker.js";
import { PANEL_HOST, claimsFor, connectAgent, makePanelEnv, makeTeam, runtimeFor, signJwt } from "./panel-fixtures.mjs";
import { SqliteD1 } from "./d1-sqlite.mjs";

const W = worker._internals;
const NOW = Date.UTC(2026, 10, 1, 14, 30, 20);
const MIN = Math.floor(NOW / 60000) * 60000 - 60000; // the minute that just closed
const ED = "3f2a0000-0000-4000-8000-000000000c41";
const MIA = "4f2a0000-0000-4000-8000-000000001c41";
const team = await makeTeam();
const rt = runtimeFor(team);

export function player(u, n, extra = {}) {
  return { u, n, ping: 40, ping_max: 55, pings: [40, 38, 41, 39, 42, 40], view: 10, send: 8, brand: "vanilla", loc: "en_gb", w: "world", cx: 25, cz: -73, ent: 30, glide: false, tips: true, join: NOW - 3600000, samples: 6, ...extra };
}

export function row(ts = MIN, { seq = 1, srv = {}, pl = [player(ED, "Em_K")], ev = [], lag = [], backfill = false, stats = {} } = {}) {
  return {
    v: 1, id: `abcd0123:${seq}`, src: "abcd0123", seq, ts,
    srv: { ticks: 1200, tps: 20, mean: 12, p50: 11, p95: 18, p99: 24, p999: 31, max: 33, spikes: 0, chunks: 900, entities: 600, tiles: 40, heap_used_mb: 2048, heap_max_mb: 5120, gc_heap_after_mb: 1800, gc_max_pause_ms: 8, self_p99_us: 6.5, self_max_us: 40, budget_skips: 0, lost_windows: 0, samples: 6, worlds: [{ n: "world", chunks: 700, entities: 500, ticking: 30, players: pl.length }], ...srv },
    pl, ev, lag, stats, backfill,
    raw: [], running: [],
    ovh: { "minecraft.service": { cpu_pct: 61.2, mem_mb: 5800 }, "mc-agent.service": { cpu_pct: 0.3, mem_mb: 24 }, "mc-backup.service": { cpu_ms: 2100, runs: 1, mem_peak_mb: 90 } },
    backlog: { segments: 0, rows: 0, bytes: 0, oldest: null, stored_segments: 12, stored_bytes: 81234 },
  };
}

function env(extra = {}) {
  const e = makePanelEnv({ TELEMETRY: new SqliteD1(), EXPERIENCE: JSON.stringify({ raw_days: 14, minute_days: 180, hour_days: 730, minecraft_version: "26.3", messages: { enabled: true, per_player_minutes: 30, broadcast_minutes: 30, join_summary: true } }), ...extra });
  return e;
}

async function post(e, body, now = NOW) {
  const resp = await W.handle(new Request("https://hb.example.com/ingest/experience", { method: "POST", headers: { Authorization: `Bearer ${"a".repeat(48)}`, "Content-Type": "application/json" }, body: JSON.stringify(body) }), e, now, rt);
  return { status: resp.status, body: await resp.json() };
}

async function q(e, sql, ...args) {
  return (await e.TELEMETRY.prepare(sql).bind(...args).all()).results;
}

function commands(e) {
  return [...e.ctx.storage.map.entries()].filter(([k]) => k.startsWith("cmd:")).map(([, c]) => JSON.parse(c.payload));
}

// --- the verdict engine -------------------------------------------------------------------------------------

test("verdicts: a healthy minute is good with no reasons", () => {
  const v = W.judge(W.cleanRow(row(), NOW));
  assert.equal(v.server.verdict, "good");
  assert.deepEqual(v.server.reasons, []);
  assert.equal(v.players[ED].verdict, "good");
  assert.equal(v.players[ED].headline, "Smooth");
});

test("verdicts: high ping is the player's connection, not the server", () => {
  const v = W.judge(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { ping: 180, ping_max: 260 }), player(MIA, "MiaBuilds")] }), NOW));
  assert.equal(v.players[ED].verdict, "fair");
  assert.match(v.players[ED].reasons[0], /Ping is a bit high \(180 ms\): your connection, not the server/);
  assert.equal(v.players[MIA].verdict, "good");
  const poor = W.judge(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { ping: 300, ping_max: 420 })] }), NOW));
  assert.equal(poor.players[ED].verdict, "poor");
  assert.equal(poor.players[ED].headline, "Your connection");
  const spike = W.judge(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { ping: 60, ping_max: 340 })] }), NOW));
  assert.match(spike.players[ED].reasons[0], /spiked to 340 ms/);
  assert.match(spike.players[ED].tips[0], /wired/);
});

test("verdicts: server lag spikes name the entities near the player and the place", () => {
  const r = row(MIN, { srv: { p99: 70, max: 140 }, pl: [player(ED, "Em_K", { ent: 900 }), player(MIA, "MiaBuilds", { ent: 12 })] });
  const v = W.judge(W.cleanRow(r, NOW));
  assert.equal(v.server.verdict, "fair");
  assert.match(v.server.reasons[0], /slowest 1% took 70 ms/);
  assert.deepEqual(v.server.causes, ["server_lag"]);
  assert.match(v.players[ED].reasons[0], /near 900 entities at world 400,-1168/);
  assert.match(v.players[MIA].reasons[0], /Server lag spikes \(ticks up to 140 ms\), not your connection/);
  const poor = W.judge(W.cleanRow(row(MIN, { srv: { p99: 130, tps: 15.2 } }), NOW));
  assert.equal(poor.server.verdict, "poor");
  assert.match(poor.server.reasons.join(" "), /15\.2 TPS/);
});

test("verdicts: flying into new land, low/high render distance, memory, hotspots, a far-away host", () => {
  const glide = W.judge(W.cleanRow(row(MIN, { srv: { p99: 60 }, pl: [player(ED, "Em_K", { glide: true })] }), NOW));
  assert.match(glide.players[ED].reasons[0], /Chunk loading is slow while flying/);
  assert.ok(glide.players[ED].causes.includes("chunk_gen"));
  const low = W.judge(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { view: 3 })] }), NOW));
  assert.equal(low.players[ED].verdict, "fair");
  assert.match(low.players[ED].reasons[0], /Low render distance on your side \(3\)/);
  const high = W.judge(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { view: 32, send: 10 })] }), NOW));
  assert.equal(high.players[ED].verdict, "good");
  assert.match(high.players[ED].tips[0], /render distance is 32 but the server sends 10.*Sodium/);
  const mem = W.judge(W.cleanRow(row(MIN, { srv: { gc_heap_after_mb: 4800 } }), NOW));
  assert.ok(mem.server.causes.includes("memory"));
  const hot = W.judge(W.cleanRow(row(MIN, { srv: { worlds: [{ n: "world", entities: 4200, chunks: 900 }] } }), NOW));
  assert.match(hot.server.reasons[0], /4200 entities loaded in world/);
  const far = W.judge(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { ping: 190 }), player(MIA, "MiaBuilds", { ping: 210 })] }), NOW));
  assert.ok(far.server.causes.includes("high_ping_cluster"));
});

test("cleanRow refuses bad shapes and strips what the agent didn't measure", () => {
  assert.equal(W.cleanRow({ ...row(), ts: MIN + 5 }, NOW), null); // not on a minute
  assert.equal(W.cleanRow({ ...row(), id: "x" }, NOW), null);
  assert.equal(W.cleanRow(row(NOW - 40 * 86400000 - (NOW % 60000)), NOW), null); // older than the box keeps
  const r = W.cleanRow({ ...row(), pl: [player(ED, "Em_K"), { u: "nope", n: "x" }, { ...player(MIA, "Mia"), ip: "203.0.113.9" }], srv: { ...row().srv, evil: "x" } }, NOW);
  assert.equal(r.pl.length, 2);
  assert.equal(r.pl[1].ip, undefined); // never an IP
  assert.equal(r.srv.evil, undefined);
});

// --- ingest -------------------------------------------------------------------------------------------------

test("ingest stores the minute (raw + 1-min summary), returns verdicts, and a replay changes nothing", async () => {
  W.resetIsolate();
  const e = env();
  const r = await post(e, { live: true, rows: [row()] });
  assert.equal(r.status, 200);
  assert.equal(r.body.stored, 1);
  assert.equal(r.body.verdicts.server.verdict, "good");
  assert.equal(r.body.verdicts.players[ED].verdict, "good");
  const day = new Date(MIN).toISOString().slice(0, 10).replace(/-/g, "");
  const month = day.slice(0, 6);
  assert.equal((await q(e, `SELECT COUNT(*) AS n FROM exp_raw_${day}`))[0].n, 1);
  const [m] = await q(e, `SELECT * FROM exp_1m_${month}`);
  assert.equal(m.ts, MIN);
  assert.equal(m.p99, 24);
  assert.equal(m.players, 1);
  assert.equal(JSON.parse(m.ovh)["mc-agent.service"].cpu_pct, 0.3);
  // the same row again (a backfill after a lost reply): same rows, no duplicates
  await post(e, { live: false, rows: [{ ...row(), backfill: true }] });
  assert.equal((await q(e, `SELECT COUNT(*) AS n FROM exp_raw_${day}`))[0].n, 1);
  assert.equal((await q(e, `SELECT COUNT(*) AS n FROM exp_1m_${month}`))[0].n, 1);
  // no IPs anywhere in what was stored
  const raw = (await q(e, `SELECT data FROM exp_raw_${day}`))[0].data;
  assert.doesNotMatch(raw, /\b\d{1,3}(\.\d{1,3}){3}\b/);
});

test("a steady minute writes at most 3 D1 rows (budget)", async () => {
  W.resetIsolate();
  const e = env();
  await post(e, { live: true, rows: [row(MIN - 60000, { seq: 0 })] }); // schema + the first verdict rows
  const before = e.TELEMETRY.written;
  await post(e, { live: true, rows: [row(MIN, { seq: 1 })] });
  const used = e.TELEMETRY.written - before;
  assert.ok(used <= 2, `rows written: ${used}`); // raw + 1m
});

test("sessions: join/leave keyed by the plugin's join time; replays and agent restarts count once", async () => {
  W.resetIsolate();
  const e = env();
  const join = NOW - 2 * 3600000;
  const joinEv = { k: "join", t: join, u: ED, n: "Em_K", join };
  await post(e, { live: true, rows: [row(MIN - 120000, { seq: 1, ev: [joinEv], pl: [player(ED, "Em_K", { join })] })] });
  await post(e, { live: true, rows: [row(MIN - 60000, { seq: 2, ev: [joinEv], pl: [player(ED, "Em_K", { join })] })] }); // agent restart
  const leave = { k: "leave", t: MIN, u: ED, n: "Em_K", join };
  await post(e, { live: true, rows: [row(MIN, { seq: 3, ev: [leave], pl: [] })] });
  await post(e, { live: false, rows: [row(MIN, { seq: 3, ev: [leave], pl: [], backfill: true })] }); // replay
  const s = await q(e, "SELECT * FROM exp_sessions");
  assert.equal(s.length, 1);
  assert.equal(s[0].leave_ts - s[0].join_ts, MIN - join);
  const board = await W.experienceOverview(e, NOW);
  assert.equal(board.players.length, 0);
  const resp = await W.handle(new Request(`https://${PANEL_HOST}/api/experience/leaderboard`, { headers: { "Cf-Access-Jwt-Assertion": await signJwt(team, claimsFor(NOW)) } }), e, NOW, rt);
  const lb = await resp.json();
  assert.equal(lb.players[0].sessions, 1);
  assert.equal(lb.players[0].play_ms, MIN - join);
});

test("sessions: an open session whose leave never arrived ends at last-seen, never across a down period (L6)", async () => {
  W.resetIsolate();
  const e = env();
  const first = MIN - 5 * 3600000;
  await post(e, { live: false, rows: [row(first + 60000, { seq: 1, ev: [{ k: "join", t: first, u: ED, n: "Em_K", join: first }], pl: [player(ED, "Em_K", { join: first })], backfill: true })] });
  await e.TELEMETRY.prepare("UPDATE exp_players SET last_seen = ?1").bind(first + 1800000).run(); // seen for 30 min
  const second = NOW - 600000; // the agent was down while the admin left; hours later he joins again
  await post(e, { live: true, rows: [row(MIN, { seq: 2, ev: [{ k: "join", t: second, u: ED, n: "Em_K", join: second }], pl: [player(ED, "Em_K", { join: second })] })] });
  const s = await q(e, "SELECT join_ts, leave_ts FROM exp_sessions ORDER BY join_ts");
  assert.deepEqual(s.map((x) => [x.join_ts, x.leave_ts]), [[first, first + 1800000], [second, null]]);
  // the agent's precise leave (a late backfill) wins over the estimate; a replay changes nothing
  const leave = { k: "leave", t: first + 1200000, u: ED, n: "Em_K", join: first };
  for (let i = 0; i < 2; i += 1) await post(e, { live: false, rows: [row(first + 1260000, { seq: 3, ev: [leave], pl: [], backfill: true })] });
  assert.equal((await q(e, "SELECT leave_ts FROM exp_sessions WHERE join_ts = ?1", first))[0].leave_ts, first + 1200000);
});

// --- messages -----------------------------------------------------------------------------------------------

test("a poor player gets one private message (signed player_message), then the rate limit holds", async () => {
  W.resetIsolate();
  const e = env();
  const ws = connectAgent(e);
  const bad = { pl: [player(ED, "Em_K", { ping: 320, ping_max: 410 })] };
  const r = await post(e, { live: true, rows: [row(MIN, bad)] });
  assert.equal(r.body.messages.length, 1);
  const [cmd] = commands(e);
  assert.equal(cmd.action, "player_message");
  assert.equal(cmd.args.to, ED);
  assert.match(cmd.args.text, /connection spiked to 410 ms.*wired/i);
  assert.match(cmd.args.id, /^lag-[0-9a-f]{16}$/);
  assert.equal(ws.sent.length, 1); // pushed over the agent's WebSocket at once
  // the next minute: still poor, but one message per 30 minutes
  const r2 = await post(e, { live: true, rows: [row(MIN + 60000, { seq: 2, ...bad })] }, NOW + 60000);
  assert.equal(r2.body.messages.length, 0);
  const sent = await q(e, "SELECT * FROM exp_events WHERE kind = 'message'");
  assert.equal(sent.length, 1);
  assert.equal(JSON.parse(sent[0].detail).name, "Em_K");
});

test("no messages: tips off, first minute online, backfilled rows, stale rows, messages disabled", async () => {
  W.resetIsolate();
  const poor = (extra) => ({ pl: [player(ED, "Em_K", { ping: 320, ...extra })] });
  for (const [body, now, cfg] of [
    [{ live: true, rows: [row(MIN, poor({ tips: false }))] }, NOW, null],
    [{ live: true, rows: [row(MIN, poor({ join: NOW - 30000 }))] }, NOW, null],
    [{ live: false, rows: [row(MIN, { ...poor({}), backfill: true })] }, NOW, null],
    [{ live: true, rows: [row(MIN, poor({}))] }, NOW + 10 * 60000, null],
    [{ live: true, rows: [row(MIN, poor({}))] }, NOW, { messages: { enabled: false } }],
  ]) {
    const e = env(cfg ? { EXPERIENCE: JSON.stringify(cfg) } : {});
    const r = await post(e, body, now);
    assert.equal(r.status, 200);
    assert.deepEqual(r.body.messages || [], [], JSON.stringify(body).slice(0, 120));
    assert.equal(commands(e).length, 0);
  }
});

test("server-wide lag: one broadcast (no blame), at most once per 30 minutes", async () => {
  W.resetIsolate();
  const e = env();
  const lag = { srv: { p99: 160, max: 410, tps: 16 }, pl: [player(ED, "Em_K"), player(MIA, "MiaBuilds")] };
  const r = await post(e, { live: true, rows: [row(MIN, lag)] });
  assert.equal(r.body.messages.length, 1);
  assert.equal(r.body.messages[0].to, "*");
  assert.match(commands(e)[0].args.text, /lagging for everyone.*nothing wrong on your side/);
  const r2 = await post(e, { live: true, rows: [row(MIN + 60000, { seq: 2, ...lag })] }, NOW + 60000);
  assert.equal(r2.body.messages.length, 0);
});

test("join summary: a player whose last session was poor gets a friendly note on joining", async () => {
  W.resetIsolate();
  const e = env();
  await post(e, { live: true, rows: [row(MIN - 3600000, { pl: [player(ED, "Em_K", { ping: 330 })] })] }, NOW - 3600000);
  const before = commands(e).length;
  const join = NOW - 20000;
  const r = await post(e, { live: true, rows: [row(MIN, { seq: 2, ev: [{ k: "join", t: join, u: ED, n: "Em_K", join }], pl: [player(ED, "Em_K", { join })] })] });
  assert.equal(r.body.messages.length, 1);
  const c = commands(e).slice(before)[0];
  assert.equal(c.args.kind, "join");
  assert.match(c.args.text, /Welcome back! Last time you had some lag \(High ping \(330 ms\)/);
});

// --- busiest chunks (P17: performance profile) -----------------------------------------------------------------

const HOT = [
  { w: "world", cx: 12, cz: -5, ent: 412, tiles: 3, vil: 0, items: 40 },
  { w: "world", cx: -3, cz: 7, ent: 80, tiles: 260, vil: 12, items: 0 },
];

test("busiest chunks: a chunk over chunk_entities is a heavy_chunk reason with its coordinates, no mechanic remedies", () => {
  const v = W.judge(W.cleanRow(row(MIN, { srv: { hot: HOT } }), NOW));
  assert.ok(v.server.causes.includes("heavy_chunk"));
  assert.ok(v.server.reasons.some((r) => r.includes("412 entities in one chunk: world chunk 12,-5 (blocks 192..207, -80..-65)")));
  assert.ok(!W.judge(W.cleanRow(row(MIN, { srv: { hot: HOT } }), NOW), {}, 500).server.causes.includes("heavy_chunk"));
  assert.ok(!W.judge(W.cleanRow(row(MIN, { srv: { hot: HOT } }), NOW), {}, 0).server.causes.includes("heavy_chunk")); // 0 = off
  const rem = W.remediesFor(["heavy_chunk"], {});
  assert.ok(rem.some((r) => /cramming kills them above 24 per block/.test(r.text)) && rem.some((r) => /Spread the pen/.test(r.text)));
  // junk from the agent is dropped field by field
  const bad = W.cleanRow(row(MIN, { srv: { hot: [{ w: 3, cx: 1, cz: 1 }, { w: "world", cx: 1.5, cz: 1 }, { w: "world", cx: 1, cz: 2, ent: "x" }] } }), NOW);
  assert.deepEqual(bad.srv.hot, [{ w: "world", cx: 1, cz: 2, ent: 0, tiles: 0, vil: 0, items: 0 }]);
});

test("busiest chunks: one Discord alert per heavy chunk (live rows), again only after 6 h; the panel lists them", async () => {
  W.resetIsolate();
  const posts = [];
  const rtd = runtimeFor(team, async (url, init) => {
    posts.push({ url, body: JSON.parse(init.body) });
    return new Response(null, { status: 204 });
  });
  const e = env({ DISCORD_WEBHOOK: "https://discord.example/webhook" });
  const send = (ts, now, extra = {}) => W.handle(new Request("https://hb.example.com/ingest/experience", { method: "POST", headers: { Authorization: `Bearer ${"a".repeat(48)}`, "Content-Type": "application/json" }, body: JSON.stringify({ live: true, rows: [row(ts, { seq: ts / 60000, srv: { hot: HOT }, ...extra })] }) }), e, now, rtd);
  await send(MIN, NOW);
  const alerts = () => posts.filter((p) => p.url.startsWith("https://discord.example/")).map((p) => p.body.content);
  assert.equal(alerts().length, 1);
  assert.match(alerts()[0], /\*\*A crowded chunk is slowing the server: 412 entities in one spot\*\*/);
  assert.match(alerts()[0], /In world chunk 12,-5 \(blocks 192\.\.207, -80\.\.-65\) there are 412 entities \(0 villagers, 40 dropped items\) and 3 tile entities/);
  assert.match(alerts()[0], /Spread the pen or farm over more chunks or add floor space/);
  assert.ok(alerts()[0].length <= 2000);
  await send(MIN + 60000, NOW + 60000); // still heavy a minute later: no repeat
  await send(MIN - 120000, NOW + 120000, { backfill: true }); // a backfilled row never alerts
  assert.equal(alerts().length, 1);
  const later = NOW + 6 * 3600000 + 60000;
  await send(Math.floor(later / 60000) * 60000 - 60000, later);
  assert.equal(alerts().length, 2);
  const o = await W.handle(new Request(`https://${PANEL_HOST}/api/experience`, { headers: { "Cf-Access-Jwt-Assertion": await signJwt(team, claimsFor(later)) } }), e, later, rtd);
  assert.equal(o.status, 200);
  const body = await o.json();
  assert.equal(body.chunk_limit, 300);
  assert.deepEqual(body.busiest.map((c) => [c.cx, c.heavy]), [[12, true], [-3, false]]);
  assert.match(body.busiest[0].where, /world chunk 12,-5/);
});

// --- rollups, backfill, retention, remedies ------------------------------------------------------------------

test("cron: hourly rollup (server + per player), backfilled hours re-rolled, day/month tables dropped whole", async () => {
  W.resetIsolate();
  const e = env();
  const hour = Math.floor(NOW / 3600000) * 3600000 - 3600000;
  const rows = [];
  for (let i = 0; i < 60; i += 1) rows.push(row(hour + i * 60000, { seq: i, srv: { p99: i === 30 ? 90 : 20, players: 2 }, pl: [player(ED, "Em_K", { ping: 50 + i }), player(MIA, "MiaBuilds", { ping: i === 30 ? 300 : 40, ping_max: i === 30 ? 300 : 55 })] }));
  await post(e, { live: false, rows: rows.slice(0, 30) });
  await post(e, { live: false, rows: rows.slice(30) });
  // an old day + an old month that retention must drop
  await e.TELEMETRY.prepare("CREATE TABLE exp_raw_20260101 (ts INTEGER PRIMARY KEY, id TEXT NOT NULL, data TEXT NOT NULL)").run();
  await e.TELEMETRY.prepare("CREATE TABLE exp_1m_202604 (ts INTEGER PRIMARY KEY)").run();
  const fetched = [];
  const rtm = runtimeFor(team, async (url) => {
    fetched.push(url);
    const ok = url.includes("/sodium/") || url.includes("/lithium/");
    return new Response(JSON.stringify(ok ? [{ version_number: "0.8.0+mc26.3" }] : []), { headers: { "Content-Type": "application/json" } });
  });
  // one unit per cron run: the hour's rollup, the dirty hours the ingest marked, retention, remedies
  const runs = [];
  for (let i = 0; i < 10; i += 1) {
    const d = await W.maintainExperience(e, NOW + i, rtm);
    if (!d.unit) break;
    runs.push(d);
  }
  assert.equal(runs[0].unit, `rollup:${hour}`);
  assert.deepEqual(runs.map((d) => d.unit.split(":")[0]), ["rollup", "dirty", "retention", "remedies"]);
  assert.ok(runs.every((d) => d.skipped === null));
  const done = { hours: runs.flatMap((d) => d.hours), dropped: runs.flatMap((d) => d.dropped) };
  assert.ok(done.hours.includes(hour));
  assert.deepEqual(done.dropped.sort(), ["exp_1m_202604", "exp_raw_20260101"]);
  const [h] = await q(e, "SELECT * FROM exp_1h WHERE ts = ?1", hour);
  assert.equal(h.minutes, 60);
  assert.equal(h.p99, 90);
  assert.ok(JSON.parse(h.ovh)["mc-backup.service"].cpu_pct > 0);
  const ph = await q(e, "SELECT * FROM exp_player_1h WHERE ts = ?1 ORDER BY name", hour);
  assert.deepEqual(ph.map((p) => [p.name, p.minutes]), [["Em_K", 60], ["MiaBuilds", 60]]);
  assert.equal(ph[1].ping_max, 300);
  assert.ok(ph[1].poor >= 1);
  // remedies: checked on Modrinth for the pinned version, recorded with availability
  assert.ok(fetched.some((u) => u.includes("api.modrinth.com/v2/project/sodium/version") && u.includes(encodeURIComponent('["26.3"]'))));
  assert.ok(!fetched.some((u) => u.includes("rosestacker"))); // vanilla: no mechanic-changing plugin is suggested
  const avail = JSON.parse((await q(e, "SELECT value FROM exp_state WHERE key = 'remedies'"))[0].value);
  assert.equal(avail["sodium:fabric"].available, true);
  const rem = W.remediesFor(["entity_hotspot", "heavy_chunk", "client_fps"], avail);
  assert.ok(rem.filter((r) => r.cause !== "client_fps").every((r) => r.kind === "action" && !r.field && !r.project));
  assert.equal(rem.find((r) => r.project === "sodium").available, true);
  // a later backfill into that hour marks it dirty; the next run re-rolls it
  await post(e, { live: false, rows: [row(hour + 5 * 60000, { seq: 5, srv: { p99: 140 }, backfill: true })] });
  assert.equal((await q(e, "SELECT COUNT(*) AS n FROM exp_dirty"))[0].n, 1);
  assert.equal((await W.maintainExperience(e, NOW + 300000, rtm)).unit, `dirty:${hour}`);
  assert.equal((await q(e, "SELECT p99 FROM exp_1h WHERE ts = ?1", hour))[0].p99, 140);
  assert.equal((await q(e, "SELECT COUNT(*) AS n FROM exp_dirty"))[0].n, 0);
});

test("segments go to R2 under telemetry/YYYY/MM/DD/ (gzip only, names checked)", async () => {
  W.resetIsolate();
  const e = env();
  const name = "20261101T1425-abcd0123-10-14.ndjson.gz";
  const put = (n, body) => W.handle(new Request(`https://hb.example.com/ingest/segment?name=${n}`, { method: "PUT", headers: { Authorization: `Bearer ${"a".repeat(48)}` }, body }), e, NOW, rt);
  const ok = await put(name, new Uint8Array([0x1f, 0x8b, 8, 0]));
  assert.equal(ok.status, 200);
  assert.equal(e.archived.at(-1).key, `telemetry/2026/11/01/${name}`);
  assert.equal((await put("../x", new Uint8Array([0x1f, 0x8b]))).status, 400);
  assert.equal((await put(name, new Uint8Array([1, 2, 3]))).status, 400);
});

// --- panel API ----------------------------------------------------------------------------------------------

test("panel: overview with verdicts, remedies, overhead; history with annotations and gaps; player detail", async () => {
  W.resetIsolate();
  const e = env();
  const token = await signJwt(team, claimsFor(NOW));
  const get = async (path) => {
    const resp = await W.handle(new Request(`https://${PANEL_HOST}${path}`, { headers: { "Cf-Access-Jwt-Assertion": token } }), e, NOW, rt);
    return { status: resp.status, body: await resp.json() };
  };
  assert.equal((await W.handle(new Request(`https://${PANEL_HOST}/api/experience`), e, NOW, rt)).status, 403);
  for (let i = 30; i >= 0; i -= 1) {
    if (i >= 12 && i <= 20) continue; // a gap
    await post(e, { live: true, rows: [row(MIN - i * 60000, { seq: 100 - i, srv: { p99: i === 0 ? 75 : 20 }, pl: [player(ED, "Em_K", { ent: i === 0 ? 800 : 10 }), player(MIA, "MiaBuilds", { view: 32, send: 8 })] })] }, NOW - i * 60000);
  }
  await W.handle(new Request("https://hb.example.com/maintenance", { method: "POST", headers: { Authorization: `Bearer ${"h".repeat(48)}` }, body: JSON.stringify({ minutes: 20, reason: "deploy (mc-config@abc1234)" }) }), e, NOW - 25 * 60000, rt);
  const o = await get("/api/experience");
  assert.equal(o.status, 200);
  assert.equal(o.body.fresh, true);
  assert.equal(o.body.server.verdict, "fair");
  const ed = o.body.players.find((p) => p.name === "Em_K");
  assert.match(ed.reasons[0], /near 800 entities/);
  assert.ok(o.body.remedies.some((r) => r.cause === "server_lag" && /spark/.test(r.text)));
  assert.ok(o.body.remedies.some((r) => r.cause === "client_fps"));
  assert.ok(o.body.remedies.some((r) => r.cause === "entity_hotspot"));
  const agent = o.body.overhead.find((x) => x.unit === "mc-agent.service");
  assert.equal(agent.flag, false);
  assert.equal(o.body.overhead[0].unit, "minecraft.service");
  assert.equal(o.body.plugin.self_p99_us, 6.5);
  const h = await get("/api/experience/history?range=24h");
  assert.equal(h.body.points.length, 22);
  assert.equal(h.body.annotations[0].kind, "maintenance");
  assert.match(h.body.annotations[0].text, /deploy/);
  assert.ok(h.body.gaps.some((g) => g.to - g.from >= 9 * 60000 && g.to < NOW));
  const p = await get(`/api/experience/player/${ED}`);
  assert.equal(p.status, 200);
  assert.equal(p.body.name, "Em_K");
  assert.ok(p.body.pings.length >= 20);
  assert.equal((await get("/api/experience/player/00000000-0000-4000-8000-000000000000")).status, 404);
  assert.equal((await get("/api/experience/history?range=2y")).status, 200);
});

test("without the TELEMETRY binding the endpoints say it is off", async () => {
  W.resetIsolate();
  const e = makePanelEnv();
  const r = await post(e, { live: true, rows: [row()] });
  assert.equal(r.status, 404);
  const token = await signJwt(team, claimsFor(NOW));
  const resp = await W.handle(new Request(`https://${PANEL_HOST}/api/experience`, { headers: { "Cf-Access-Jwt-Assertion": token } }), e, NOW, rt);
  assert.equal(resp.status, 404);
});

test("verdicts: hysteresis keeps a ping hovering at a threshold from flapping", () => {
  const at = (ping, prev) => W.judgePlayer(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { ping })] }), NOW).pl[0], row().srv, { verdict: "good" }, prev).verdict;
  assert.equal(at(250, null), "poor");
  assert.equal(at(240, "poor"), "poor"); // within 10% of the poor line: stays poor
  assert.equal(at(220, "poor"), "fair");
  assert.equal(at(140, "fair"), "fair");
  assert.equal(at(130, "fair"), "good");
  assert.equal(at(140, null), "good");
});

// --- Free-plan CPU (P13 review M2) --------------------------------------------------------------------------
// Workers Free allows 10 ms of CPU per invocation; D1 queries are I/O and don't count. Node's performance.now()
// around the Worker's code, minus the time spent inside SQLite, is the proxy: the minimum over a few runs (robust
// to noise) must stay under CPU_BUDGET_MS, a 2.5x margin. AGENT_BACKFILL_BATCH mirrors scripts/telemetry.py's
// BACKFILL_BATCH (tests/test_telemetry.py checks they agree).
const AGENT_BACKFILL_BATCH = 3;
const CPU_BUDGET_MS = 4;
const CPU_PLAYERS = 50;

class TimedD1 extends SqliteD1 {
  constructor() {
    super();
    this.io = 0;
  }
  exec(sql, args) {
    const t = performance.now();
    try {
      return super.exec(sql, args);
    } finally {
      this.io += performance.now() - t;
    }
  }
}

function crowd(k) {
  return Array.from({ length: k }, (_, i) => player(`3f2a0000-0000-4000-8000-${String(i).padStart(12, "0")}`, `Player${i}`, { ping: 40 + i, ping_max: 60 + i }));
}

function fullRow(ts, seq, pl) {
  const r = row(ts, { seq, pl, lag: [{ t: ts + 5000, ms: 180, ctx: { world: "world", cx: 1, cz: 2, entities: 400, players: ["Player1"] } }] });
  r.raw = Array.from({ length: 6 }, (_, j) => ({ ts: ts + j * 10000, tps: 20, p50: 11, p99: 24, p999: 30, max: 33, pl: Object.fromEntries(pl.map((p) => [p.u, [p.ping, p.ent]])) }));
  return r;
}

async function cpuMs(db, fn, runs = 12) {
  let best = Infinity;
  for (let i = 0; i < runs; i += 1) {
    db.io = 0;
    const t = performance.now();
    await fn(i);
    best = Math.min(best, performance.now() - t - db.io);
  }
  return best;
}

test("CPU: a backfill request (a batch of rows, a full server) stays far inside 10 ms", async () => {
  W.resetIsolate();
  const db = new TimedD1();
  const e = env({ TELEMETRY: db });
  const pl = crowd(CPU_PLAYERS);
  const body = JSON.stringify({ live: false, rows: Array.from({ length: AGENT_BACKFILL_BATCH }, (_, i) => fullRow(MIN - (i + 1) * 60000, i, pl)) });
  await W.ingestExperience(e, NOW, JSON.parse(body)); // schema + tables
  const ms = await cpuMs(db, () => W.ingestExperience(e, NOW, JSON.parse(body))); // parse included, as the Worker does
  console.log(`ingest: ${AGENT_BACKFILL_BATCH} rows x ${CPU_PLAYERS} players, ${body.length} B: ${ms.toFixed(2)} ms CPU`);
  assert.ok(ms < CPU_BUDGET_MS, `${ms.toFixed(2)} ms`);
});

test("CPU: one cron maintenance unit (an hour's rollup of a full server) stays far inside 10 ms", async () => {
  W.resetIsolate();
  const db = new TimedD1();
  const e = env({ TELEMETRY: db, EXPERIENCE: JSON.stringify({ minecraft_version: "" }) });
  const hour = Math.floor(NOW / 3600000) * 3600000 - 3600000;
  const pl = crowd(CPU_PLAYERS);
  for (let i = 0; i < 60; i += 6) await W.ingestExperience(e, NOW, { live: false, rows: Array.from({ length: 6 }, (_, k) => fullRow(hour + (i + k) * 60000, i + k, pl)) });
  const ms = await cpuMs(db, async (i) => {
    await db.prepare("DELETE FROM exp_state WHERE key = 'maint'").run();
    const d = await W.maintainExperience(e, NOW + i, rt);
    assert.equal(d.unit, `rollup:${hour}`);
  });
  console.log(`rollup: 60 minutes x ${CPU_PLAYERS} players: ${ms.toFixed(2)} ms CPU`);
  assert.ok(ms < CPU_BUDGET_MS, `${ms.toFixed(2)} ms`);
  const ph = await q(e, "SELECT * FROM exp_player_1h WHERE ts = ?1 AND name = 'Player7'", hour);
  assert.deepEqual([ph[0].minutes, ph[0].ping_avg, ph[0].ping_max], [60, 47, 67]); // the SQL aggregation is right
});

test("cron: a unit whose runs keep dying is retried, then skipped and reported, never blocking the queue", async () => {
  W.resetIsolate();
  const db = new SqliteD1();
  let kill = true;
  const killing = {
    prepare: (sql) => {
      if (kill && sql.includes("INSERT OR REPLACE INTO exp_1h")) throw new Error("Worker exceeded CPU time limit");
      return db.prepare(sql);
    },
    batch: (s) => db.batch(s),
  };
  const e = env({ TELEMETRY: killing, EXPERIENCE: JSON.stringify({ minecraft_version: "" }) });
  const hour = Math.floor(NOW / 3600000) * 3600000 - 3600000;
  await W.ingestExperience(e, NOW, { live: false, rows: [row(hour + 60000)] });
  for (let i = 1; i <= W.MAINT_MAX_TRIES; i += 1) {
    await assert.rejects(W.maintainExperience(e, NOW + i, rt), /CPU time limit/);
    const st = JSON.parse((await q(e, "SELECT value FROM exp_state WHERE key = 'maint'"))[0].value);
    assert.deepEqual([st.inflight.key, st.inflight.tries], [`rollup:${hour}`, i]); // saved before the work
  }
  const skipped = await W.maintainExperience(e, NOW + 10, rt);
  assert.equal(skipped.skipped, `rollup:${hour}`);
  kill = false;
  const next = await W.maintainExperience(e, NOW + 20, rt);
  assert.equal(next.unit, `dirty:${hour}`); // the queue moves on (and the dirty mark re-rolls that hour)
  assert.equal((await q(e, "SELECT minutes FROM exp_1h WHERE ts = ?1", hour))[0].minutes, 1);
  assert.equal(W.nextMaintUnit({ rolled: hour + 3600000, retention: NOW, remedies: NOW }, hour + 3600000, null, { minecraft_version: "26.3" }, NOW), null);
});

test("v1.0.1: a smooth server and a fine connection: /lag says it's likely the client's FPS (and nothing else)", () => {
  const ok = W.judgePlayer(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { view: 8, send: 8 })] }), NOW).pl[0], row().srv, { verdict: "good" }, null);
  assert.equal(ok.verdict, "good");
  assert.deepEqual(ok.tips, [W.MESSAGES.fps().text]);
  assert.match(ok.tips[0], /^The server and your connection look fine, so stutter is likely your PC's frame rate\. Press F3/);
  const laggy = W.judgePlayer(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K", { ping: 400 })] }), NOW).pl[0], row().srv, { verdict: "good" }, null);
  assert.ok(!laggy.tips.includes(W.MESSAGES.fps().text), "not when the connection is the cause");
  const busy = W.judgePlayer(W.cleanRow(row(MIN, { pl: [player(ED, "Em_K")] }), NOW).pl[0], row().srv, { verdict: "poor" }, null);
  assert.ok(!busy.tips.includes(W.MESSAGES.fps().text), "not when the server lags");
});

test("v1.2: the client mod's FPS reports: 'server fine, your PC' with the measured frame rate", () => {
  const judgeOne = (extra, server = { verdict: "good" }) => W.judgePlayer(W.cleanRow(row(MIN, { pl: [player(MIA, "Mia", extra)] }), NOW).pl[0], row().srv, server, null);
  const smooth = judgeOne({ fps: 90, fps_min: 70 });
  assert.equal(smooth.verdict, "good");
  assert.deepEqual(smooth.tips, [], "a measured, fine frame rate: no F3 guesswork");
  const slow = judgeOne({ fps: 24, fps_min: 12 });
  assert.equal(slow.verdict, "fair");
  assert.equal(slow.headline, "Your PC");
  assert.deepEqual(slow.reasons, ["Server fine, your PC: 24 FPS (lowest 12)"]);
  assert.ok(slow.causes.includes("client_fps"));
  assert.equal(judgeOne({ fps: 11 }).verdict, "poor");
  const both = judgeOne({ fps: 15 }, { verdict: "poor" });
  assert.equal(both.headline, "Server lag");
  assert.ok(both.reasons.includes("Your PC is also slow: 15 FPS"));
  // no reports (no mod): the v1.0.1 hint stays
  assert.deepEqual(judgeOne({}).tips, [W.MESSAGES.fps().text]);
  // a poor frame rate on a smooth server: the private message names the PC, not the server
  const clean = W.cleanRow(row(MIN, { pl: [player(MIA, "Mia", { fps: 11 })] }), NOW);
  const v = W.judge(clean);
  const msgs = W.messagesFor(clean, v, { lastPoor: {} }, { messages: { enabled: true, join_summary: true } }, NOW);
  assert.equal(msgs.length, 1);
  assert.match(msgs[0].text, /^The server and your connection are fine: your PC is drawing 11 FPS\./);
});
