// P14 tests for watchdog/worker.js: Discord slash commands (POST /discord/interactions). Ed25519 verification (RFC
// 8032 test vectors, Discord's header pair over timestamp + body), PING/PONG, 401 for every bad or stale signature,
// the allowlists (application, guild, channel, user; DMs refused) with audit rows, replay and rate limits, each read
// command against seeded D1 data (P9 samples/inventory, P13 experience rows), and the two actions (`/backup now`,
// `/whitelist add`) as signed agent commands with the follow-up edit of the reply. Free-plan CPU per interaction.
// No network: Discord's follow-up endpoint is a fake fetch.
import assert from "node:assert/strict";
import { createHmac, webcrypto } from "node:crypto";
import { test } from "node:test";

import worker from "../worker.js";
import { connectAgent, makePanelEnv, makeTeam, runtimeFor, seed, seedExperience } from "./panel-fixtures.mjs";
import { SqliteD1 } from "./d1-sqlite.mjs";

const W = worker._internals;
const subtle = webcrypto.subtle;
const NOW = Date.UTC(2026, 10, 2, 9, 30, 20);
const APP = "1300000000000000001";
const GUILD = "1300000000000000002";
const CHANNEL = "1300000000000000003";
const ED = "1300000000000000004";
const OTHER = "1300000000000000099";
const team = await makeTeam();
const hex = (b) => Buffer.from(b).toString("hex");

const pair = await subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const PUBLIC_KEY = hex(await subtle.exportKey("raw", pair.publicKey));
const strangerPair = await subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);

const patched = [];
function rtWith(waits) {
  const base = runtimeFor(team, async (url, init) => {
    const m = /^https:\/\/discord\.com\/api\/v10\/webhooks\/(\d+)\/([A-Za-z0-9._-]+)\/messages\/@original$/.exec(url);
    if (!m) throw new Error(`unexpected fetch ${url}`);
    patched.push({ app: m[1], token: m[2], method: init.method, body: JSON.parse(init.body) });
    return new Response("{}", { status: 200 });
  });
  return { ...base, waitUntil: (p) => waits.push(p) };
}

function discordEnv(extra = {}) {
  return makePanelEnv({
    DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [ED], ephemeral: true, title: "Lighthouse" }),
    ...extra,
  });
}

let seq = 0;
function interaction(name, { sub = null, options = [], user = ED, guild = GUILD, channel = CHANNEL, app = APP, dm = false, roles = [] } = {}) {
  seq += 1;
  const opts = sub ? [{ type: 1, name: sub, options }] : options;
  const i = {
    id: String(1310000000000000000n + BigInt(seq)),
    application_id: app,
    type: 2,
    token: `aW50ZXJhY3Rpb24tdG9rZW4.${seq}.test-token-value`,
    data: { id: "1", name, type: 1, options: opts },
    channel_id: channel,
    channel: { id: channel, type: 0 },
  };
  if (dm) i.user = { id: user, username: "ed" };
  else {
    i.guild_id = guild;
    i.member = { user: { id: user, username: "ed" }, roles };
  }
  return i;
}

const enc = (text) => new TextEncoder().encode(text);
const cat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
};

// Discord signs the timestamp's bytes followed by the body's raw bytes (a string body: its UTF-8)
async function signed(body, { ts = Math.floor(NOW / 1000), key = pair.privateKey } = {}) {
  const bytes = body instanceof Uint8Array ? body : enc(body);
  const sig = new Uint8Array(await subtle.sign("Ed25519", key, cat(enc(String(ts)), bytes)));
  return { "X-Signature-Ed25519": hex(sig), "X-Signature-Timestamp": String(ts), "Content-Type": "application/json" };
}

async function send(env, payload, { headers = null, rt = rtWith([]), now = NOW, method = "POST", path = "/discord/interactions" } = {}) {
  const body = typeof payload === "string" || payload instanceof Uint8Array ? payload : JSON.stringify(payload);
  const h = headers || (await signed(body));
  const resp = await W.handle(new Request(`https://hb.mc.example.com${path}`, { method, headers: h, body: method === "GET" ? undefined : body }), env, now, rt);
  return { status: resp.status, body: await resp.json() };
}

async function auditRows(env) {
  const { results } = await env.DB.prepare("SELECT login, action, args, cmd_id, result, ok FROM audit ORDER BY id").all();
  return results.map((r) => ({ ...r }));
}

// --- Ed25519 -----------------------------------------------------------------------------------------------

// RFC 8032 section 7.1, TEST 1 (empty message) and TEST 2 (one byte 0x72): public key, message, signature.
const RFC8032 = [
  ["d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "", "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"],
  ["3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "r", "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"],
];

test("Ed25519: RFC 8032 test vectors verify; a flipped bit, a wrong key or a malformed signature don't", async () => {
  W.resetIsolate();
  // the vectors, through the same WebCrypto calls the Worker makes (verifyDiscord needs a numeric timestamp prefix,
  // which neither vector's message has: an empty timestamp is refused outright)
  assert.equal(await W.verifyDiscord(RFC8032[0][0], RFC8032[0][2], "", enc("")), false);
  for (const [pk, msg, sig] of RFC8032) {
    const key = await subtle.importKey("raw", Buffer.from(pk, "hex"), { name: "Ed25519" }, false, ["verify"]);
    assert.equal(await subtle.verify("Ed25519", key, Buffer.from(sig, "hex"), new TextEncoder().encode(msg)), true);
  }
  // Discord's shape: timestamp + body, signed with a fresh key pair
  const body = enc('{"type":1}');
  const h = await signed(body, { ts: 1730000000 });
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, h["X-Signature-Ed25519"], "1730000000", body), true);
  const flipped = h["X-Signature-Ed25519"].replace(/^./, (c) => (c === "0" ? "1" : "0"));
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, flipped, "1730000000", body), false);
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, h["X-Signature-Ed25519"], "1730000001", body), false, "the timestamp is signed");
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, h["X-Signature-Ed25519"], "1730000000", enc('{"type":1} ')), false, "the body is signed");
  assert.equal(await W.verifyDiscord(RFC8032[0][0], h["X-Signature-Ed25519"], "1730000000", body), false, "another key");
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, "zz", "1730000000", body), false);
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, "", "1730000000", body), false);
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, h["X-Signature-Ed25519"], "1730000000", '{"type":1}'), false, "raw bytes only, never a decoded string");
});

test("Ed25519 covers the exact bytes: timestamp || raw body, no text round trip (BOM, invalid UTF-8 fail closed)", async () => {
  W.resetIsolate();
  const env = discordEnv();
  const ts = String(Math.floor(NOW / 1000));
  // exact-byte vector: multi-byte UTF-8 in the body; the signature is over the timestamp's bytes then these bytes
  const raw = enc('{"id":"1","application_id":"' + APP + '","type":1,"token":"x","note":"\u00e9\u{1F600}"}');
  const h = await signed(raw);
  const key = await subtle.importKey("raw", Buffer.from(PUBLIC_KEY, "hex"), { name: "Ed25519" }, false, ["verify"]);
  assert.equal(await subtle.verify("Ed25519", key, Buffer.from(h["X-Signature-Ed25519"], "hex"), cat(enc(ts), raw)), true);
  assert.equal(await W.verifyDiscord(PUBLIC_KEY, h["X-Signature-Ed25519"], ts, raw), true);
  assert.deepEqual(await send(env, raw, { headers: h }), { status: 200, body: { type: 1 } });
  const ping = enc(JSON.stringify({ id: "1", application_id: APP, type: 1, token: "x", version: 1 }));
  const bom = cat(new Uint8Array([0xef, 0xbb, 0xbf]), ping);
  // a BOM in front: signed over the bytes Discord sent, the signature holds but the body isn't JSON -> 400, never PONG
  assert.equal((await send(env, bom, { headers: await signed(bom) })).status, 400);
  // signed without the BOM (what a decoder that strips it would verify) but sent with it -> 401
  assert.equal((await send(env, bom, { headers: await signed(ping) })).status, 401);
  // invalid UTF-8 (0xff) inside a JSON string: signed exactly -> 400 (strict decoding); signed over the replacement
  // character's bytes (what decode + encode would have produced) -> 401
  const before = enc('{"id":"1","application_id":"' + APP + '","type":1,"token":"x","note":"');
  const after = enc('"}');
  const invalid = cat(before, new Uint8Array([0xff]), after);
  assert.equal((await send(env, invalid, { headers: await signed(invalid) })).status, 400);
  const replaced = cat(before, new Uint8Array([0xef, 0xbf, 0xbd]), after);
  assert.equal((await send(env, invalid, { headers: await signed(replaced) })).status, 401);
  assert.equal((await send(env, replaced, { headers: await signed(replaced) })).status, 200, "the valid form itself is fine");
});

// --- the endpoint ------------------------------------------------------------------------------------------

test("PING is answered PONG only with a valid, fresh signature; everything else is 401", async () => {
  W.resetIsolate();
  const env = discordEnv();
  const ping = JSON.stringify({ id: "1", application_id: APP, type: 1, token: "x", version: 1 });
  const ok = await send(env, ping);
  assert.deepEqual([ok.status, ok.body], [200, { type: 1 }]);
  // Discord's own checks send invalid signatures: each must be 401
  const good = await signed(ping);
  for (const headers of [
    { "Content-Type": "application/json" },
    { ...good, "X-Signature-Ed25519": "0".repeat(128) },
    { ...good, "X-Signature-Timestamp": String(Number(good["X-Signature-Timestamp"]) + 1) },
    await signed(ping, { key: strangerPair.privateKey }),
    await signed(ping, { ts: Math.floor(NOW / 1000) - 301 }), // stale (signed correctly, 5 min + 1 s old)
    await signed(ping, { ts: Math.floor(NOW / 1000) + 301 }),
  ]) {
    const r = await send(env, ping, { headers });
    assert.equal(r.status, 401, JSON.stringify(headers).slice(0, 80));
  }
  const tampered = await send(env, ping.replace('"type":1', '"type":2'), { headers: good });
  assert.equal(tampered.status, 401);
  assert.equal((await send(env, ping, { method: "GET", headers: {} })).status, 405);
  assert.equal((await send(env, "x".repeat(40000))).status, 413);
  assert.equal(env.DB.written, 0, "nothing is written for a bad request (or a PING)");
});

test("without a complete DISCORD config the endpoint doesn't exist (404), signed or not", async () => {
  W.resetIsolate();
  const ping = JSON.stringify({ type: 1 });
  assert.equal((await send(makePanelEnv(), ping)).status, 404);
  const partial = makePanelEnv({ DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [] }) });
  assert.equal((await send(partial, ping)).status, 404);
  assert.equal(W.discordConfig({ DISCORD: "{bad" }), null);
  assert.equal(W.discordConfig({ DISCORD: JSON.stringify({ app_id: APP, public_key: "A".repeat(64), guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [ED] }) }).ok, true, "hex in either case");
});

test("allowlists: other users, guilds, channels, applications and DMs are refused and audited", async () => {
  W.resetIsolate();
  const env = discordEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  for (const [opts, why] of [
    [{ user: OTHER }, /allowlist/],
    [{ guild: OTHER }, /this server/],
    [{ channel: OTHER }, /this channel/],
    [{ dm: true }, /direct messages/],
  ]) {
    const r = await send(env, interaction("backup", { sub: "now", ...opts }));
    assert.equal(r.status, 200);
    assert.equal(r.body.type, 4);
    assert.match(r.body.data.content, why);
    assert.equal(r.body.data.flags, 64, "refusals are ephemeral");
  }
  assert.equal((await send(env, interaction("status", { app: OTHER }))).status, 401);
  assert.equal(agent.sent.length, 0, "nothing reached the agent");
  const rows = (await auditRows(env)).filter((r) => r.login.startsWith("discord:"));
  assert.equal(rows.length, 4);
  assert.ok(rows.every((r) => r.ok === 0 && r.result.startsWith("refused: ") && r.action === "discord backup now"));
  assert.equal(rows[0].login, `discord:${OTHER}`);
});

test("replays: the same interaction id is answered once; the per-user rate limit holds", async () => {
  W.resetIsolate();
  const env = discordEnv();
  await seed(env, NOW);
  const i = interaction("players");
  const body = JSON.stringify(i);
  const headers = await signed(body);
  assert.equal((await send(env, body, { headers })).status, 200);
  assert.equal((await send(env, body, { headers })).status, 409, "a replayed (validly signed) interaction is refused");
  let limited = null;
  for (let k = 0; k < W.DISCORD_RATE.per_minute + 2 && !limited; k += 1) {
    const r = await send(env, interaction("players"));
    if (/Slow down/.test(r.body.data.content)) limited = k;
  }
  assert.equal(limited, W.DISCORD_RATE.per_minute - 1, "the 11th command within a minute is refused");
  // a minute later it works again (the window counts the last 60 s)
  const r = await send(env, interaction("players"), { now: NOW + 61000 });
  assert.doesNotMatch(r.body.data.content, /Slow down/);
});

// --- v1.0.1: the helper tier -------------------------------------------------------------------------------

const HELPER = "1300000000000000010";
const ROLE = "1300000000000000020";
const ROLE_MEMBER = "1300000000000000011";

function helperEnv() {
  return discordEnv({
    DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [ED], helper_user_ids: [HELPER], helper_role_id: ROLE, ephemeral: true }),
  });
}

test("helpers (by id or by role) may read and /whitelist add; /backup now stays admin-only", async () => {
  W.resetIsolate();
  const env = helperEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  const cfg = W.discordConfig(env);
  assert.deepEqual([cfg.helper_user_ids, cfg.helper_role_id, cfg.ok], [[HELPER], ROLE, true]);
  assert.equal(W.discordTier(cfg, ED, { roles: [] }), "admin");
  assert.equal(W.discordTier(cfg, HELPER, { roles: [] }), "helper");
  assert.equal(W.discordTier(cfg, ROLE_MEMBER, { roles: ["1300000000000000077", ROLE] }), "helper");
  assert.equal(W.discordTier(cfg, ROLE_MEMBER, { roles: ["1300000000000000077"] }), null);
  assert.equal(W.discordTier(cfg, ROLE_MEMBER, { roles: ROLE }), null, "roles must be an array");
  assert.equal(W.discordTier(cfg, ROLE_MEMBER, undefined), null);
  assert.equal(W.discordTier({ ...cfg, helper_role_id: "" }, ROLE_MEMBER, { roles: [""] }), null);
  for (const [user, roles] of [[HELPER, []], [ROLE_MEMBER, [ROLE]]]) {
    for (const [name, sub] of [["status"], ["players"], ["backups"], ["rejected"], ["whitelist", "list"], ["perf"], ["lag"]]) {
      const r = await send(env, interaction(name, { sub, user, roles }), { now: NOW + 70000 * (user === HELPER ? 1 : 2) });
      assert.doesNotMatch(r.body.data.content, /Not allowed/, `${user} /${name}`);
    }
    const b = await send(env, interaction("backup", { sub: "now", user, roles }));
    assert.match(b.body.data.content, /^Not allowed: \/backup now is for the server's admins only/);
    assert.equal(b.body.data.flags, 64);
  }
  // the role only counts in the signed payload of this guild's member: without it, the same user is refused
  const stranger = await send(env, interaction("status", { user: ROLE_MEMBER, roles: [] }));
  assert.match(stranger.body.data.content, /not on this server's Discord allowlist/);
  // a helper outside the allowed channel(s) is refused like anyone
  const elsewhere = await send(env, interaction("status", { user: HELPER, channel: OTHER }));
  assert.match(elsewhere.body.data.content, /this channel/);
  assert.equal(agent.sent.filter((m) => m.type === "command").length, 0, "no refused action reached the agent");

  // a helper's whitelist add is a signed agent command, audited with their Discord id
  const wl = await send(env, interaction("whitelist", { sub: "add", user: ROLE_MEMBER, roles: [ROLE], options: [{ type: 3, name: "name", value: "Felix_2011" }] }), { rt: rtWith([]), now: NOW + 300000 });
  assert.match(wl.body.data.content, /^Whitelist add Felix\\_2011: queued/);
  const cmd = JSON.parse(agent.sent.filter((m) => m.type === "command").at(-1).payload);
  assert.deepEqual([cmd.action, cmd.args, cmd.login], ["whitelist", { op: "add", name: "Felix_2011" }, `discord:${ROLE_MEMBER} (helper)`]);
  const rows = await auditRows(env);
  assert.ok(rows.some((r) => r.login === `discord:${ROLE_MEMBER} (helper)` && r.action === "whitelist" && r.cmd_id === cmd.id));
  assert.ok(rows.some((r) => r.login === `discord:${HELPER} (helper)` && r.action === "discord backup now" && r.result === "refused: /backup now is for the server's admins only"));
  // admins keep /backup now
  const admin = await send(env, interaction("backup", { sub: "now" }), { rt: rtWith([]), now: NOW + 300000 });
  assert.match(admin.body.data.content, /^Backup: queued/);
});

test("helper rate limits are per user, like everyone's", async () => {
  W.resetIsolate();
  const env = helperEnv();
  await seed(env, NOW);
  let limited = null;
  for (let k = 0; k < W.DISCORD_RATE.per_minute + 2 && !limited; k += 1) {
    const r = await send(env, interaction("players", { user: HELPER }));
    if (/Slow down/.test(r.body.data.content)) limited = k;
  }
  assert.equal(limited, W.DISCORD_RATE.per_minute);
  const other = await send(env, interaction("players", { user: ROLE_MEMBER, roles: [ROLE] }));
  assert.doesNotMatch(other.body.data.content, /Slow down/, "another helper has a limit of their own");
  assert.deepEqual(W.DISCORD_HELPER_COMMANDS.filter((c) => !c.startsWith("whitelist")).sort(), ["backups", "lag", "perf", "players", "rejected", "status"]);
});

// --- read commands -----------------------------------------------------------------------------------------

test("read commands answer from D1: status, players, backups, whitelist, rejected (markdown escaped, no mentions)", async () => {
  W.resetIsolate();
  const env = discordEnv();
  await seed(env, NOW);
  const status = (await send(env, interaction("status"))).body;
  assert.equal(status.type, 4);
  assert.deepEqual(status.data.allowed_mentions, { parse: [] });
  assert.equal(status.data.flags, 64);
  assert.match(status.data.content, /\*\*Lighthouse\*\*: up, 3 online · TPS 20\.0 · MSPT \d+\.\d/);
  assert.match(status.data.content, /Watchdog: v4 ok, v6 ok · health ok, backup ok/);
  const players = (await send(env, interaction("players"))).body.data.content;
  assert.equal(players, "**3/20 online:** Em\\_K, MiaBuilds, jonas\\_r");
  const backups = (await send(env, interaction("backups"))).body.data.content;
  assert.match(backups, /^\*\*Backups:\*\* last OK 10 min ago/);
  assert.match(backups, /`f0a93b11` 2026-11-02 09:20 UTC/);
  assert.ok(backups.indexOf("f0a93b11") < backups.indexOf("8c41d2e7"), "newest first");
  const wl = (await send(env, interaction("whitelist", { sub: "list" }))).body.data.content;
  assert.match(wl, /^\*\*Whitelist \(7\):\*\* Em\\_K, jonas\\_r, Lukas\\_P/);
  assert.match(wl, /Not in Git yet: \+nora\\_k/);
  const rej = (await send(env, interaction("rejected"))).body.data.content;
  assert.match(rej, /xXcreeperXx: 3×, last 23 min ago/);
  assert.match(rej, /Felix\\_2011: 1×, last 3 h ago/);
  // a stale server
  const late = discordEnv();
  await seed(late, NOW - 3600000);
  assert.match((await send(late, interaction("players"))).body.data.content, /No fresh sample from the server \(last 60 min ago\)/);
  // not ephemeral when server.yml says so
  const pub = discordEnv({ DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [ED], ephemeral: false }) });
  await seed(pub, NOW);
  assert.equal((await send(pub, interaction("players"))).body.data.flags, 0);
});

test("read commands: /lag (server, a player), /perf (ranges) from the P13 experience tables", async () => {
  W.resetIsolate();
  const rt = rtWith([]);
  const env = discordEnv({ TELEMETRY: new SqliteD1(), EXPERIENCE: JSON.stringify({ minecraft_version: "26.3", messages: { enabled: false } }) });
  await seed(env, NOW);
  await seedExperience(env, NOW, W.handle, rt);
  const lag = (await send(env, interaction("lag"), { rt })).body.data.content;
  assert.match(lag, /^\*\*Server:\*\* .+ \(slowest 1% of ticks \d+ ms\)/);
  assert.match(lag, /jonas\\_r: /, "the player with a high ping is listed");
  const one = (await send(env, interaction("lag", { options: [{ type: 3, name: "player", value: "JONAS_R" }] }), { rt })).body.data.content;
  assert.match(one, /^\*\*jonas\\_r\*\*: .+ \(ping \d+ ms\)/);
  assert.match(one, /\n- .*[Pp]ing/);
  assert.match((await send(env, interaction("lag", { options: [{ type: 3, name: "player", value: "Nobody" }] }), { rt })).body.data.content, /Nobody isn't online now/);
  for (const range of ["24h", "7d", "30d"]) {
    const perf = (await send(env, interaction("perf", { options: [{ type: 3, name: "range", value: range }] }), { rt })).body.data.content;
    if (range === "30d") assert.match(perf, /No data|Last 30d/); // hourly rollups: none yet in a fresh database
    else assert.match(perf, new RegExp(`^\\*\\*Last ${range}:\\*\\* tick p99 avg \\d+\\.\\d ms, worst 72\\.0 ms`));
  }
  assert.match((await send(env, interaction("perf", { options: [{ type: 3, name: "range", value: "1y" }] }), { rt })).body.data.content, /Last 24h/, "an unknown range falls back to 24h");
  // without telemetry
  const plain = discordEnv();
  await seed(plain, NOW);
  assert.match((await send(plain, interaction("lag"))).body.data.content, /telemetry is off/);
  assert.match((await send(plain, interaction("perf"))).body.data.content, /^\*\*Last 24h:\*\* TPS min \d+\.\d, avg \d+\.\d · MSPT max 390\.9/);
  assert.match((await send(plain, interaction("teleport"))).body.data.content, /Unknown command/);
});

// --- actions -----------------------------------------------------------------------------------------------

test("/backup now and /whitelist add go to the agent as signed commands, audited, with the result edited in", async () => {
  W.resetIsolate();
  patched.length = 0;
  const env = discordEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  const waits = [];
  const rt = rtWith(waits);
  // the agent answers each command as soon as it is sent (the Control object stores the first result)
  const answer = async (ok, output, error = "") => {
    const cmd = agent.sent.filter((m) => m.type === "command").at(-1);
    const id = JSON.parse(cmd.payload).id;
    await env.control.fetch(new Request("https://control/results", { method: "POST", body: JSON.stringify({ results: [{ id, ok, output, error }] }) }));
    return id;
  };
  const r = await send(env, interaction("backup", { sub: "now" }), { rt });
  assert.equal(r.status, 200);
  assert.match(r.body.data.content, /^Backup: queued for the server \(command [0-9a-f]{8}\)/);
  const cmd = agent.sent.find((m) => m.type === "command");
  assert.equal(cmd.sig, createHmac("sha256", env.COMMAND_HMAC_KEY).update(cmd.payload).digest("hex"));
  const payload = JSON.parse(cmd.payload);
  assert.deepEqual([payload.action, payload.args, payload.login], ["backup", {}, `discord:${ED}`]);
  const id = await answer(true, "backup started");
  await Promise.all(waits);
  assert.equal(patched.length, 1);
  assert.equal(patched[0].method, "PATCH");
  assert.equal(patched[0].app, APP);
  assert.match(patched[0].body.content, /^Backup: done\. backup started/);
  assert.deepEqual(patched[0].body.allowed_mentions, { parse: [] });
  let rows = await auditRows(env);
  const row = rows.find((x) => x.cmd_id === id);
  assert.deepEqual([row.login, row.action, row.result, row.ok], [`discord:${ED}`, "backup", "ok", 1]);

  // whitelist add: the name is checked like the panel's; the agent's failure is reported
  const bad = await send(env, interaction("whitelist", { sub: "add", options: [{ type: 3, name: "name", value: "no spaces!" }] }), { rt });
  assert.match(bad.body.data.content, /^Refused: a Java name is 3-16 letters/);
  waits.length = 0;
  const wl = await send(env, interaction("whitelist", { sub: "add", options: [{ type: 3, name: "name", value: "Felix_2011" }] }), { rt });
  assert.match(wl.body.data.content, /^Whitelist add Felix\\_2011: queued/);
  const p2 = JSON.parse(agent.sent.filter((m) => m.type === "command").at(-1).payload);
  assert.deepEqual([p2.action, p2.args], ["whitelist", { op: "add", name: "Felix_2011" }]);
  await answer(false, "", "whitelist: the server didn't add Felix_2011 (That player does not exist)");
  await Promise.all(waits);
  assert.match(patched.at(-1).body.content, /^Whitelist add Felix\\_2011: failed: whitelist: the server didn't add Felix\\_2011 \\\(That player does not exist\\\)/);

  // nothing else is an action: /whitelist list reads, and the action rate limit (3 per 10 minutes) holds
  waits.length = 0;
  const third = await send(env, interaction("backup", { sub: "now" }), { rt });
  assert.match(third.body.data.content, /queued/);
  const fourth = await send(env, interaction("backup", { sub: "now" }), { rt });
  assert.match(fourth.body.data.content, /at most 3 actions in 10 minutes/);
  rows = await auditRows(env);
  assert.equal(rows.at(-1).result, "refused: rate limit (Discord actions)");
  assert.deepEqual(Object.values(W.DISCORD_ACTIONS).sort(), ["backup", "whitelist"], "never stop/restart/restore/plugin/console");
});

test("an action with no agent result within the poll window says so; destructive commands don't exist", async () => {
  W.resetIsolate();
  patched.length = 0;
  const env = discordEnv();
  await seed(env, NOW);
  connectAgent(env);
  const waits = [];
  const rt = rtWith(waits);
  await send(env, interaction("backup", { sub: "now" }), { rt });
  await Promise.all(waits);
  assert.match(patched[0].body.content, /no result within 24 s: see the panel's audit/);
  for (const name of ["stop", "restart", "restore", "plugin", "console"]) {
    assert.ok(!W.DISCORD_COMMANDS.includes(name), name);
    assert.match((await send(env, interaction(name, { sub: "now" }), { rt })).body.data.content, /Unknown command/);
  }
});

test("refusals: each user's first few per minute are audited, the rest counted into one aggregate row", async () => {
  W.resetIsolate();
  const env = discordEnv();
  await seed(env, NOW);
  const before = (await auditRows(env)).length;
  const cap = W.DISCORD_REFUSAL_AUDIT.per_user_minute;
  const minute = Math.floor(NOW / 60000) * 60000;
  for (let k = 0; k < cap + 15; k += 1) {
    const r = await send(env, interaction("status", { user: OTHER }), { now: minute + k * 100 });
    assert.match(r.body.data.content, /^Not allowed: you are not on this server's Discord allowlist/); // every one answered
  }
  let rows = (await auditRows(env)).slice(before);
  assert.equal(rows.length, cap, "only the first few refusals of the minute are written");
  assert.ok(rows.every((r) => r.login === `discord:${OTHER}` && r.ok === 0));
  // another user has a cap of their own
  await send(env, interaction("status", { user: "1300000000000000098" }), { now: minute + 2000 });
  // the next minute: one aggregate row for the 15 not written, then the new minute's refusal
  await send(env, interaction("status", { user: OTHER }), { now: minute + 60000 });
  rows = (await auditRows(env)).slice(before);
  assert.equal(rows.length, cap + 3);
  const agg = rows.at(-2);
  assert.deepEqual([agg.login, agg.action, agg.result], [`discord:${OTHER}`, "discord (refusals)", "refused: 15 more in that minute (not audited one by one)"]);
  assert.equal(JSON.parse(agg.args).dropped, 15);
});

test("the Discord action limit is atomic: concurrent requests can't all pass one count", async () => {
  W.resetIsolate();
  const env = discordEnv({ DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [ED, OTHER], ephemeral: true }) });
  await seed(env, NOW);
  connectAgent(env);
  const rt = rtWith([]);
  const bodies = await Promise.all(
    Array.from({ length: 6 }, (_, k) => send(env, interaction("backup", { sub: "now", user: k % 2 ? OTHER : ED }), { rt })),
  );
  const queued = bodies.filter((r) => /queued for the server/.test(r.body.data.content));
  const limited = bodies.filter((r) => /at most 3 actions in 10 minutes/.test(r.body.data.content));
  assert.deepEqual([queued.length, limited.length], [W.DISCORD_RATE.actions_per_10_minutes, 3]);
  const rows = (await auditRows(env)).filter((r) => r.action === "backup");
  assert.equal(rows.filter((r) => r.result === "queued (discord)").length, 3);
  assert.ok(rows.every((r) => r.cmd_id !== "reserved"), "every slot got its command id");
});

test("a refused enqueue is always ephemeral (whatever discord.ephemeral) and frees its slot", async () => {
  W.resetIsolate();
  const env = discordEnv({
    DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: [ED], ephemeral: false }),
    COMMAND_HMAC_KEY: "",
  });
  await seed(env, NOW);
  const r = await send(env, interaction("backup", { sub: "now" }));
  assert.match(r.body.data.content, /^Refused: no command key/);
  assert.equal(r.body.data.flags, 64);
  const row = (await auditRows(env)).filter((x) => x.action === "backup").at(-1);
  assert.deepEqual([row.cmd_id, row.result, row.ok], [null, "no command key", 0]);
  // a read command keeps the configured (public) reply
  assert.equal((await send(env, interaction("players"))).body.data.flags, 0);
});

test("the follow-up polls for at most 24 s in total (inside waitUntil's 30 s, with room for the PATCH)", async () => {
  W.resetIsolate();
  patched.length = 0;
  const env = discordEnv();
  await seed(env, NOW);
  connectAgent(env);
  const waits = [];
  const base = rtWith(waits);
  let slept = 0;
  const rt = { ...base, sleep: async (ms) => { slept += ms; } };
  await send(env, interaction("backup", { sub: "now" }), { rt });
  await Promise.all(waits);
  assert.ok(slept <= W.DISCORD_POLL.max_ms && W.DISCORD_POLL.max_ms <= 24000, `slept ${slept} ms`);
  assert.equal(patched.length, 1);
});

// --- Free-plan CPU -----------------------------------------------------------------------------------------
// Same proxy as experience.test.mjs: wall time of the Worker's code minus the time inside SQLite; the minimum of a
// few runs must stay under CPU_BUDGET_MS (Workers Free: 10 ms per invocation).
const CPU_BUDGET_MS = 4;

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

test("CPU: verifying and answering an interaction (the heaviest read: /perf 24h, /status) stays far inside 10 ms", async () => {
  W.resetIsolate();
  const db = new TimedD1();
  const tel = new TimedD1();
  // many allowed users, one per request, so the per-user rate limit never answers instead of the command
  const users = Array.from({ length: 40 }, (_, k) => String(1320000000000000000n + BigInt(k)));
  const env = discordEnv({ DB: db, TELEMETRY: tel, EXPERIENCE: JSON.stringify({ minecraft_version: "26.3", messages: { enabled: false } }), DISCORD: JSON.stringify({ app_id: APP, public_key: PUBLIC_KEY, guild_id: GUILD, channel_ids: [CHANNEL], user_ids: users }) });
  let u = 0;
  await seed(env, NOW);
  await seedExperience(env, NOW, W.handle, rtWith([]));
  const rt = rtWith([]);
  for (const [name, options] of [["perf", [{ type: 3, name: "range", value: "24h" }]], ["status", []], ["lag", []], ["backups", []]]) {
    let best = Infinity;
    for (let k = 0; k < 8; k += 1) {
      const body = JSON.stringify(interaction(name, { options, user: users[u++] }));
      const headers = await signed(body);
      db.io = 0;
      tel.io = 0;
      const t = performance.now();
      const resp = await W.handle(new Request("https://hb.mc.example.com/discord/interactions", { method: "POST", headers, body }), env, NOW + k, rt);
      const out = await resp.json();
      assert.doesNotMatch(out.data.content, /Slow down|didn't work/);
      best = Math.min(best, performance.now() - t - db.io - tel.io);
    }
    console.log(`discord /${name}: ${best.toFixed(2)} ms CPU`);
    assert.ok(best < CPU_BUDGET_MS, `${name}: ${best.toFixed(2)} ms`);
  }
});
