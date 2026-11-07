// P10 tests for watchdog/worker.js: panel actions (POST /api/action): argument checks, re-auth freshness for the
// destructive ones (stop, plugin, restore), the typed restore confirmation, signing for the agent and the audit row
// with the Access identity; the agent's job (POST /ingest/job -> GET /api/job); the Mojang name lookup; P10's
// inventory fields (cleaned) and warnings; restic's snapshot list on the Backups API.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import worker from "../worker.js";
import { AUD, CONFIRM_AUD, CONFIRM_HOST, EMAIL, PANEL_HOST, claimsFor, connectAgent, makePanelEnv, makeTeam, runtimeFor, seed, signJwt } from "./panel-fixtures.mjs";

const W = worker._internals;
const NOW = Date.UTC(2026, 9, 31, 14, 13, 0);
const team = await makeTeam();
const ORIGIN = { Origin: `https://${PANEL_HOST}`, "Sec-Fetch-Site": "same-origin" };
const mojang = new Map([["felix_2011", { status: 200, body: { id: "0d61aa00bb0011224455667788997f90", name: "Felix_2011" } }]]);
let mojangCalls = 0;
let fallbackCalls = 0;
const rt = runtimeFor(team, async (url) => {
  const m = /^https:\/\/api\.mojang\.com\/users\/profiles\/minecraft\/(.+)$/.exec(url);
  const f = /^https:\/\/api\.minecraftservices\.com\/minecraft\/profile\/lookup\/name\/(.+)$/.exec(url);
  if (!m && !f) throw new Error(`unexpected fetch ${url}`);
  const name = decodeURIComponent((m || f)[1]).toLowerCase();
  if (f) {
    // the fallback: answers unless the test says it's down too
    fallbackCalls += 1;
    if (name === "brokenmojang") return new Response("down", { status: 503 });
    const hit = mojang.get(name.replace(/^blk_/, ""));
    return hit ? new Response(JSON.stringify(hit.body), { status: 200 }) : new Response("{}", { status: 404 });
  }
  mojangCalls += 1;
  if (name === "brokenmojang") return new Response("oops", { status: 500 });
  if (name.startsWith("blk_")) return new Response("forbidden", { status: 403 }); // Cloudflare egress, v1.2.2
  const hit = mojang.get(name);
  return hit ? new Response(JSON.stringify(hit.body), { status: hit.status }) : new Response(null, { status: 204 });
});

const CONFIRM_ORIGIN = { Origin: `https://${CONFIRM_HOST}`, "Sec-Fetch-Site": "same-origin" };

async function call(env, path, { method = "GET", body, age = 30, headers = {}, host = PANEL_HOST, aud = null } = {}) {
  const t = Math.floor(NOW / 1000);
  const audience = aud || (host === CONFIRM_HOST ? CONFIRM_AUD : AUD);
  const token = await signJwt(team, claimsFor(NOW, { iat: t - age, aud: [audience] }));
  const h = { "Cf-Access-Jwt-Assertion": token, ...headers };
  if (body !== undefined) h["Content-Type"] = "application/json";
  const req = new Request(`https://${host}${path}`, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  const resp = await W.handle(req, env, NOW, rt);
  return { status: resp.status, body: await resp.json() };
}

const act = (env, action, args, opts = {}) => call(env, "/api/action", { method: "POST", body: { action, args }, headers: ORIGIN, ...opts });
// destructive actions: on the confirmation host, with that host's Access app (P10 review)
const confirmAct = (env, action, args, opts = {}) =>
  call(env, "/api/action", { method: "POST", body: { action, args }, headers: CONFIRM_ORIGIN, host: CONFIRM_HOST, ...opts });

async function auditRows(env) {
  const { results } = await env.DB.prepare("SELECT login, action, args, cmd_id, result, ok FROM audit ORDER BY id").all();
  return results.map((r) => ({ ...r }));
}

test("actions are same-origin, checked, signed for the agent with the Access identity, and audited", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  assert.equal((await call(env, "/api/action", { method: "POST", body: { action: "restart", args: {} } })).status, 403); // no Origin
  const before = (await auditRows(env)).length;
  for (const [action, args] of [
    ["restart", { countdown: 7 }],
    ["whitelist", { op: "add", name: "x" }],
    ["whitelist", { op: "op", name: "Steve" }],
    ["moderate", { verb: "op", target: "Steve" }],
    ["moderate", { verb: "kick", target: "@a" }],
    ["moderate", { verb: "pardon-ip", target: "Steve" }],
    ["plugin", { name: "../evil", enabled: false }],
    ["plugin", { name: "chunky", enabled: "no" }],
    ["restore", { snapshot: "zz", confirm: "restore zz" }],
    ["execute", {}],
  ]) {
    const r = await act(env, action, args);
    assert.equal(r.status, 400, `${action} ${JSON.stringify(args)}`);
  }
  assert.equal((await auditRows(env)).length, before, "a malformed request changes nothing");
  assert.equal(agent.sent.length, 0);

  const r = await act(env, "restart", { countdown: 60, backup: true, extra: "dropped" });
  assert.equal(r.status, 200);
  const sent = agent.sent.find((m) => m.type === "command");
  assert.equal(sent.sig, createHmac("sha256", env.COMMAND_HMAC_KEY).update(sent.payload).digest("hex"));
  const payload = JSON.parse(sent.payload);
  assert.deepEqual([payload.action, payload.args, payload.login], ["restart", { countdown: 60, backup: true }, EMAIL]);
  const kick = await act(env, "moderate", { verb: "kick", target: "jonas_r", reason: "please\nrejoin  later" });
  assert.equal(kick.status, 200);
  assert.deepEqual(JSON.parse(agent.sent.at(-1).payload).args, { verb: "kick", target: "jonas_r", reason: "please rejoin later" });
  const rows = (await auditRows(env)).slice(before);
  assert.deepEqual(rows.map((x) => [x.login, x.action, x.result, x.cmd_id === r.body.id || x.cmd_id === kick.body.id]), [
    [EMAIL, "restart", "queued", true],
    [EMAIL, "moderate", "queued", true],
  ]);
  // the agent's result lands in the audit row
  await env.control.webSocketMessage(agent, JSON.stringify({ type: "result", id: r.body.id, ok: true, output: "restart scheduled" }));
  assert.equal((await call(env, `/api/commands/${r.body.id}`)).body.result.ok, true);
  assert.equal((await auditRows(env)).find((x) => x.cmd_id === r.body.id).ok, 1);
});

test("stop, plugin switches and restore run only on the confirmation host, with its own fresh Access login", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  const old = 11 * 60; // reauth_minutes is 10 in the fixture
  const snap = `f0a93b11${"0".repeat(56)}`;
  const destructive = [
    ["stop", { countdown: 10 }],
    ["plugin", { name: "chunky", enabled: false, countdown: 60 }],
    ["restore", { snapshot: snap, confirm: "restore f0a93b11", countdown: 60 }],
  ];
  for (const [action, args] of destructive) {
    // never on the panel host, however fresh the login
    const main = await act(env, action, args, { age: 5 });
    assert.equal(main.status, 403, action);
    assert.equal(main.body.confirm, true);
    assert.equal(main.body.confirm_url, `https://${CONFIRM_HOST}/`);
    // the panel app's token isn't accepted on the confirmation host (other AUD): 403
    assert.equal((await confirmAct(env, action, args, { aud: AUD })).status, 403, action);
    // the confirmation host's Origin is required there
    assert.equal((await confirmAct(env, action, args, { headers: ORIGIN })).status, 403, action);
    const stale = await confirmAct(env, action, args, { age: old });
    assert.equal(stale.status, 401, action);
    assert.equal(stale.body.reauth, true);
    assert.equal(stale.body.reauth_minutes, 10);
    const fresh = await confirmAct(env, action, args, { age: 60 });
    assert.equal(fresh.status, 200, action);
  }
  // the confirmation host takes destructive actions only
  assert.equal((await confirmAct(env, "restart", {})).status, 400);
  for (const [action, args] of [["restart", {}], ["backup", {}], ["start", {}], ["whitelist", { op: "add", name: "Felix_2011" }], ["cancel", {}]]) {
    assert.equal((await act(env, action, args, { age: old })).status, 200, action);
  }
  const rows = await auditRows(env);
  assert.deepEqual(rows.filter((x) => x.result.startsWith("refused: confirmation login")).map((x) => x.action), ["stop", "plugin", "restore"]);
  assert.deepEqual(rows.filter((x) => x.result.startsWith("refused: destructive")).map((x) => x.action), ["stop", "plugin", "restore"]);
  assert.deepEqual(rows.filter((x) => x.result === "queued (confirmed)").map((x) => x.action), ["stop", "plugin", "restore"]);
  assert.ok(rows.filter((x) => x.result.startsWith("refused")).every((x) => x.login === EMAIL && x.ok === 0));
  const actions = agent.sent.filter((m) => m.type === "command").map((m) => JSON.parse(m.payload).action);
  assert.deepEqual(actions, ["stop", "plugin", "restore", "restart", "backup", "start", "whitelist", "cancel"]);
});

test("the confirmation host serves only its page, its API and fails closed without its app", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const page = await W.handle(new Request(`https://${CONFIRM_HOST}/`, { headers: { "Cf-Access-Jwt-Assertion": await signJwt(team, claimsFor(NOW, { aud: [CONFIRM_AUD] })) } }), env, NOW, rt);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /confirm\.js/);
  assert.match(page.headers.get("Content-Security-Policy"), /script-src 'self'/);
  for (const path of ["/index.html", "/panel.js", "/api/overview", "/api/logs"]) {
    const r = await call(env, path, { host: CONFIRM_HOST });
    assert.equal(r.status, 404, path);
  }
  const me = await call(env, "/api/me", { host: CONFIRM_HOST });
  assert.equal(me.body.panel_url, `https://${PANEL_HOST}/`);
  assert.equal((await call(env, "/api/me", { host: CONFIRM_HOST, aud: AUD })).status, 403);
  assert.equal((await call(env, "/api/me")).body.confirm_url, `https://${CONFIRM_HOST}/`);
  // a PANEL binding without the confirmation app: destructive actions can't run anywhere
  const panel = JSON.parse(env.PANEL);
  delete panel.confirm_aud;
  const env2 = makePanelEnv({ PANEL: JSON.stringify(panel) });
  assert.equal((await confirmAct(env2, "stop", { countdown: 0 })).status, 403);
  assert.equal((await act(env2, "stop", { countdown: 0 })).body.confirm_url, null);
});

test("a restore needs the typed confirmation for exactly that snapshot", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  connectAgent(env);
  const snap = `8c41d2e7${"1".repeat(56)}`;
  for (const confirm of [undefined, "", "restore", "restore f0a93b11", "Restore 8c41d2e7", "restore 8c41d2e7 "]) {
    const r = await confirmAct(env, "restore", { snapshot: snap, confirm, countdown: 60 });
    assert.equal(r.status, 400, String(confirm));
    assert.match(r.body.error, /type "restore 8c41d2e7"/);
  }
  const ok = await confirmAct(env, "restore", { snapshot: snap, confirm: "restore 8c41d2e7", countdown: 0 });
  assert.equal(ok.status, 200);
  const row = (await auditRows(env)).at(-1);
  assert.deepEqual(JSON.parse(row.args), { snapshot: snap, countdown: 0 }); // the confirmation text isn't stored
});

test("the agent's job: POST /ingest/job (agent token) -> GET /api/job, cleaned", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const job = { id: "abc", action: "restart", label: "Restart", login: EMAIL, state: "countdown", step: "countdown 60 s, 3 player(s) online", countdown: 60, deadline: NOW + 60000, started: NOW, finished: null, players: 3, evil: "<script>" };
  const post = (body, auth = `Bearer ${env.AGENT_TOKEN}`) =>
    W.handle(new Request("https://hb.mc.example.com/ingest/job", { method: "POST", headers: { Authorization: auth, "Content-Type": "application/json" }, body: JSON.stringify(body) }), env, NOW, rt);
  assert.equal((await post({ job }, "Bearer nope")).status, 401);
  assert.equal((await post({ job })).status, 200);
  const got = (await call(env, "/api/job")).body.job;
  assert.equal(got.state, "countdown");
  assert.equal(got.deadline, NOW + 60000);
  assert.equal(got.evil, undefined);
  assert.equal((await post({ job: { ...job, state: "pwned", action: "rm" } })).status, 200);
  const bad = (await call(env, "/api/job")).body.job;
  assert.deepEqual([bad.state, bad.action], ["", ""]);
});

test("Mojang lookup: found, not found, failure, cached, and only for valid names", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  mojangCalls = 0;
  const found = await call(env, "/api/mojang?name=felix_2011");
  assert.deepEqual(found.body, { found: true, name: "Felix_2011", uuid: "0d61aa00-bb00-1122-4455-667788997f90" });
  await call(env, "/api/mojang?name=FELIX_2011");
  assert.equal(mojangCalls, 1, "cached per isolate");
  assert.deepEqual((await call(env, "/api/mojang?name=NoSuchPlayer")).body, { found: false });
  assert.equal(fallbackCalls, 0, "the fallback only when api.mojang.com doesn't answer");
  // v1.2.2: api.mojang.com refuses the Worker (403): the fallback answers
  const viaFallback = await call(env, "/api/mojang?name=blk_felix_2011");
  assert.equal(viaFallback.status, 200);
  assert.deepEqual(viaFallback.body, { found: true, name: "Felix_2011", uuid: "0d61aa00-bb00-1122-4455-667788997f90" });
  assert.equal(fallbackCalls, 1);
  // neither answers: not an error, the form may add it unverified (the server resolves the name)
  const down = await call(env, "/api/mojang?name=brokenmojang");
  assert.equal(down.status, 200);
  assert.equal(down.body.found, null);
  assert.equal(down.body.unverified, true);
  assert.match(down.body.detail, /api\.mojang\.com: HTTP 500; api\.minecraftservices\.com: HTTP 503/);
  await call(env, "/api/mojang?name=brokenmojang");
  assert.equal(fallbackCalls, 3, "an unverified answer isn't cached");
  for (const bad of ["ab", "has space", "x".repeat(17), "../../x"]) {
    assert.equal((await call(env, `/api/mojang?name=${encodeURIComponent(bad)}`)).status, 400, bad);
  }
});

test("P10 inventory fields are cleaned; warnings show pending changes, drift and Git errors; backups list restic's snapshots", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const inventory = {
    ts: NOW,
    whitelist: [],
    pending: [{ uuid: "8f2a0000-0000-4000-8000-000000006c41", name: "nora_k", op: "delete-everything", ts: NOW, login: EMAIL, pr: 3 }],
    plugins_disabled: ["chunky", "../../etc", 42],
    whitelist_sync: { configured: true, pr: { number: 3, url: "javascript:alert(1)" }, error: "GitHub: POST /repos/o/r/pulls: HTTP 422", expired: [{ name: "Old_Friend", op: "add", ts: NOW - 3600000 }] },
    snapshots: [{ id: `ab12cd34${"0".repeat(56)}`, short_id: "ab12cd34", time: new Date(NOW - 60000).toISOString(), tags: "pre-restore", size: 10 }],
    job: { id: "j", action: "plugin", state: "running", label: "Restart", step: "moving the jar" },
  };
  const post = new Request("https://hb.mc.example.com/ingest/inventory", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.AGENT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ inventory }),
  });
  assert.equal((await W.handle(post, env, NOW, rt)).status, 200);
  const inv = (await call(env, "/api/inventory")).body.inventory;
  assert.equal(inv.pending[0].op, "add");
  assert.deepEqual(inv.plugins_disabled, ["chunky"]);
  assert.equal(inv.whitelist_sync.pr.url, "");
  assert.equal(inv.job.step, "moving the jar");
  const o = (await call(env, "/api/overview")).body;
  const texts = o.warnings.map((w) => w.text).join("\n");
  assert.match(texts, /1 whitelist change\(s\) made here are live but not in Git yet \(pull request #3\)/);
  assert.match(texts, /Whitelist -> Git: GitHub: POST/);
  assert.match(texts, /Old_Friend never reached Git/);
  assert.doesNotMatch(texts, /token expires/); // App installation tokens rotate by themselves (P10 review)
  assert.match(texts, /Server differs from Git: chunky switched off/);
  assert.equal(o.job.state, "running");
  const b = (await call(env, "/api/backups")).body;
  assert.equal(b.source, "restic");
  assert.deepEqual(b.restic.map((x) => [x.short_id, x.tags]), [["ab12cd34", "pre-restore"]]);
});

// --- P10 review: the panel's GitHub App (the Worker mints installation tokens; the key never leaves it) ----------

import { generateKeyPairSync, createVerify } from "node:crypto";

const MINTED = ["ghs", "minted0123456789abcdef"].join("_"); // built at run time (no literal for gitleaks)

test("POST /agent/github-token mints a narrowed installation token with an RS256 App JWT; errors carry no secret", async () => {
  W.resetIsolate();
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const calls = [];
  let fail = false;
  const ghrt = runtimeFor(team, async (url, init) => {
    calls.push({ url, method: init.method, auth: init.headers.Authorization, body: init.body ? JSON.parse(init.body) : null });
    if (fail) return new Response("{}", { status: 500 });
    if (url === "https://api.github.com/repos/octocat/mc-config/installation") return new Response(JSON.stringify({ id: 4242 }), { status: 200 });
    if (url === "https://api.github.com/app/installations/4242/access_tokens") {
      return new Response(JSON.stringify({ token: MINTED, expires_at: "2026-10-31T15:13:00Z", permissions: { contents: "write", pull_requests: "write", metadata: "read" } }), { status: 201 });
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  for (const pem of [privateKey.export({ type: "pkcs1", format: "pem" }), privateKey.export({ type: "pkcs8", format: "pem" })]) {
    W.resetIsolate();
    calls.length = 0;
    const env = makePanelEnv({ GITHUB_APP: JSON.stringify({ app_id: "123456", repository: "octocat/mc-config" }), GITHUB_APP_KEY: pem });
    const ask = (auth = `Bearer ${env.AGENT_TOKEN}`) =>
      W.handle(new Request("https://hb.mc.example.com/agent/github-token", { method: "POST", headers: { Authorization: auth, "Content-Type": "application/json" }, body: "{}" }), env, NOW, ghrt);
    assert.equal((await ask("Bearer nope")).status, 401);
    const r = await ask();
    assert.equal(r.status, 200);
    // v1.3: the granted permissions go along for the agent's Panel -> Git self-test (cached with the token)
    const perms = { contents: "write", pull_requests: "write", metadata: "read" };
    assert.deepEqual(await r.json(), { token: MINTED, expires_at: "2026-10-31T15:13:00.000Z", permissions: perms });
    assert.deepEqual(calls.map((c) => c.method), ["GET", "POST"]);
    assert.deepEqual(calls[1].body, { repositories: ["mc-config"], permissions: { contents: "write", pull_requests: "write" } });
    const jwt = calls[0].auth.replace("Bearer ", "");
    const [h, p, sig] = jwt.split(".");
    assert.deepEqual(JSON.parse(Buffer.from(h, "base64url")), { alg: "RS256", typ: "JWT" });
    const claims = JSON.parse(Buffer.from(p, "base64url"));
    const t = Math.floor(NOW / 1000);
    assert.deepEqual(claims, { iat: t - 60, exp: t + 540, iss: "123456" });
    assert.ok(createVerify("RSA-SHA256").update(`${h}.${p}`).verify(publicKey, Buffer.from(sig, "base64url")));
    // cached: no more GitHub calls within the token's life (minus 10 minutes)
    assert.deepEqual((await (await ask()).json()).permissions, perms);
    assert.equal(calls.length, 2);
  }
  W.resetIsolate();
  fail = true;
  const pem = privateKey.export({ type: "pkcs1", format: "pem" });
  const env = makePanelEnv({ GITHUB_APP: JSON.stringify({ app_id: "123456", repository: "octocat/mc-config" }), GITHUB_APP_KEY: pem });
  const bad = await W.handle(new Request("https://hb.mc.example.com/agent/github-token", { method: "POST", headers: { Authorization: `Bearer ${env.AGENT_TOKEN}` }, body: "{}" }), env, NOW, ghrt);
  assert.equal(bad.status, 502);
  const text = await bad.text();
  assert.match(text, /HTTP 500/);
  assert.ok(!text.includes("PRIVATE KEY") && !text.includes("eyJ"));
  // no App configured: 404, nothing fetched
  const none = makePanelEnv();
  const n = await W.handle(new Request("https://hb.mc.example.com/agent/github-token", { method: "POST", headers: { Authorization: `Bearer ${none.AGENT_TOKEN}` }, body: "{}" }), none, NOW, ghrt);
  assert.equal(n.status, 404);
});

// --- v1.2: the world library (panel only, confirmation host, typed confirmation; never from Discord) ------------

test("world create/switch/delete: shaped, typed, only on the confirmation host, signed for the agent", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  const create = { name: "creative", seed: "-42", type: "flat_void", preset: "farm_test", border_radius: { overworld: 1000 }, pregen: false, countdown: 60 };
  const worldActions = [
    ["world_create", { ...create, confirm: "create creative" }],
    ["world_switch", { name: "creative", confirm: "switch to creative", countdown: 10 }],
    ["world_delete", { name: "old", confirm: "delete old" }],
  ];
  for (const [action, args] of worldActions) {
    const main = await act(env, action, args, { age: 5 });
    assert.equal(main.status, 403, action);
    assert.equal(main.body.confirm, true);
    assert.equal((await confirmAct(env, action, args, { age: 11 * 60 })).status, 401, action);
    assert.equal((await confirmAct(env, action, args, { age: 30 })).status, 200, action);
  }
  for (const [action, args, why] of [
    ["world_create", { ...create, confirm: "create Creative" }, /type "create creative"/],
    ["world_create", { ...create, name: "Creative", confirm: "create Creative" }, /world name/],
    ["world_create", { ...create, seed: "abc", confirm: "create creative" }, /seed/],
    ["world_create", { ...create, type: "debug", confirm: "create creative" }, /type must be/],
    ["world_create", { ...create, preset: "pvp", confirm: "create creative" }, /preset must be/],
    ["world_create", { ...create, border_radius: { overworld: -1 }, confirm: "create creative" }, /border radius/],
    ["world_create", { ...create, border_radius: { mars: 5 }, confirm: "create creative" }, /only overworld/],
    ["world_switch", { name: "creative", confirm: "switch creative" }, /type "switch to creative"/],
    ["world_switch", { name: "../x", confirm: "switch to ../x" }, /world name/],
    ["world_delete", { name: "old" }, /type "delete old"/],
  ]) {
    const r = await confirmAct(env, action, args);
    assert.equal(r.status, 400, `${action} ${JSON.stringify(args)}`);
    assert.match(r.body.error, why);
  }
  const sent = agent.sent.filter((m) => m.type === "command").map((m) => JSON.parse(m.payload));
  assert.deepEqual(sent.map((p) => p.action), ["world_create", "world_switch", "world_delete"]);
  assert.deepEqual(sent[0].args, {
    name: "creative", seed: "-42", type: "flat_void", preset: "farm_test", pregen: false, countdown: 60,
    border_radius: { "minecraft:overworld": 1000, "minecraft:the_nether": null, "minecraft:the_end": null },
  });
  assert.deepEqual(sent[2].args, { name: "old" }); // no confirmation text, no countdown: the server isn't touched
  assert.ok(sent.every((p) => p.login === EMAIL));
  const rows = await auditRows(env);
  assert.deepEqual(rows.filter((x) => x.result === "queued (confirmed)").map((x) => x.action), ["world_create", "world_switch", "world_delete"]);
});

test("the agent's world library inventory is cleaned for the Worlds tab", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const worlds = {
    active: "main",
    since: "2026-11-05T10:00:00+00:00",
    data_version: 4671,
    archives: [
      { name: "creative", short_id: "cccccccc", time: "2026-11-02T10:00:00Z", size: 2e9, data_version: 4600, count: 2 },
      { name: "<img src=x>", short_id: "dddddddd" },
    ],
    library: [{ name: "creative", seed: "7", type: "flat_classic", preset: "creative_build", border_radius: { "minecraft:overworld": 1000, evil: 1 } }, { name: "x", type: "rm -rf" }],
    deleted: ["gone", "../x"],
    archive_bytes: 2e9,
    storage_note: "yes",
    pending: { what: "switch to main", error: "", pr: 9, url: "javascript:alert(1)" },
  };
  const post = new Request("https://hb.mc.example.com/ingest/inventory", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.AGENT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ inventory: { ts: NOW, worlds } }),
  });
  assert.equal((await W.handle(post, env, NOW, rt)).status, 200);
  const w = (await call(env, "/api/inventory")).body.inventory.worlds;
  assert.deepEqual(w.archives.map((a) => a.name), ["creative"]);
  assert.equal(w.archives[0].data_version, 4600);
  assert.deepEqual(w.library[0].border_radius, { "minecraft:overworld": 1000, "minecraft:the_nether": null, "minecraft:the_end": null });
  assert.equal(w.library[1].type, "default");
  assert.deepEqual(w.deleted, ["gone"]);
  assert.equal(w.storage_note, false);
  assert.equal(w.pending.url, "");
  assert.equal(w.pending.pr, 9);
});

// --- v1.3: the name check through the box, the Panel -> Git self-test, versions everywhere ---------------------

// An agent on the WebSocket that answers each command (answer(payload) -> {ok, output|error}, or null: silent).
function answeringAgent(env, answer) {
  const ws = {
    sent: [],
    send(text) {
      const m = JSON.parse(text);
      ws.sent.push(m);
      if (m.type !== "command") return;
      const p = JSON.parse(m.payload);
      const out = answer(p);
      if (out) setTimeout(() => env.control.webSocketMessage(ws, JSON.stringify({ type: "result", id: p.id, ...out })), 5);
    },
    close() {},
  };
  env.ctx.acceptWebSocket(ws, ["agent"]);
  return ws;
}

test("v1.3 name check: the agent resolves the name from the box first; the Worker's lookup is the fallback", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const asked = [];
  const agent = answeringAgent(env, (p) => {
    asked.push([p.action, p.args.name, p.login]);
    const n = p.args.name.toLowerCase();
    if (n === "felix_2011") return { ok: true, output: JSON.stringify({ found: true, name: "Felix_2011", uuid: "0d61aa00-bb00-1122-4455-667788997f90", via: "api.mojang.com" }) };
    if (n === "nosuchplayer") return { ok: true, output: JSON.stringify({ found: false, via: "api.mojang.com" }) };
    if (n === "boxcantreach") return { ok: true, output: JSON.stringify({ found: null, unverified: true, detail: "api.mojang.com: URLError" }) };
    if (n === "refused") return { ok: false, error: "lookup: rate limit (20 per minute)" };
    return null; // silent: the Worker gives up after the wait
  });
  mojangCalls = 0;
  fallbackCalls = 0;
  const found = await call(env, "/api/mojang?name=felix_2011");
  assert.deepEqual(found.body, { found: true, name: "Felix_2011", uuid: "0d61aa00-bb00-1122-4455-667788997f90", via: "server" });
  assert.deepEqual(asked, [["lookup", "felix_2011", EMAIL]]);
  assert.equal(mojangCalls + fallbackCalls, 0, "the Worker didn't ask Mojang itself");
  const cmd = agent.sent.find((m) => m.type === "command");
  assert.equal(cmd.sig, createHmac("sha256", env.COMMAND_HMAC_KEY).update(cmd.payload).digest("hex"), "signed like every command");
  await call(env, "/api/mojang?name=FELIX_2011");
  assert.equal(asked.length, 1, "a definite answer is cached");
  assert.deepEqual((await call(env, "/api/mojang?name=NoSuchPlayer")).body, { found: false, via: "server" });
  // the box couldn't tell, or refused: the Worker's own lookup (v1.2.2 code) answers
  assert.deepEqual((await call(env, "/api/mojang?name=boxcantreach")).body.found, false);
  assert.equal(mojangCalls, 1);
  assert.deepEqual((await call(env, "/api/mojang?name=refused")).body.found, false);
  assert.equal(mojangCalls, 2);
  // a silent agent: null after the wait, then the Worker's lookup
  assert.equal(await W.agentLookup(env, "Silent_One", EMAIL, NOW, 50), null);
  // lookup is never a panel action
  assert.equal((await act(env, "lookup", { name: "felix_2011" })).status, 400);
  // no agent on the WebSocket: straight to the Worker's lookup, no command queued
  W.resetIsolate();
  const offline = makePanelEnv();
  mojangCalls = 0;
  assert.equal((await call(offline, "/api/mojang?name=felix_2011")).body.found, true);
  assert.equal(mojangCalls, 1);
  assert.equal([...offline.ctx.storage.map.keys()].filter((k) => k.startsWith("cmd:")).length, 0);
});

test("v1.3 selftest: a non-destructive panel action, signed with no arguments; its result is cleaned into the inventory", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  const r = await act(env, "selftest", { anything: "dropped" });
  assert.equal(r.status, 200);
  const payload = JSON.parse(agent.sent.find((m) => m.type === "command").payload);
  assert.deepEqual([payload.action, payload.args, payload.login], ["selftest", {}, EMAIL]);
  const inv = (await kvInventory(env)) || {};
  const post = (inventory) =>
    W.handle(new Request("https://hb.mc.example.com/ingest/inventory", { method: "POST", headers: { Authorization: `Bearer ${env.AGENT_TOKEN}` }, body: JSON.stringify({ inventory }) }), env, NOW, rt);
  const selftest = {
    ok: false, ts: NOW - 60000, configured: true, trigger: "daily", failures: 2, error: "token: the Worker returned no usable GitHub token (390 characters, unexpected '!')",
    steps: [{ step: "token", ok: false, detail: "the Worker returned no usable GitHub token (390 characters, unexpected '!')", token: "ghs_secret" }, { step: "evil", ok: true }],
  };
  assert.equal((await post({ ...inv, selftest, product: { version: "1.3.0", commit: "a".repeat(40), running: "1.2.2", evil: "x" } })).status, 200);
  const o = (await call(env, "/api/overview")).body;
  assert.equal(o.selftest.ok, false);
  assert.deepEqual(o.selftest.steps, [{ step: "token", ok: false, detail: "the Worker returned no usable GitHub token (390 characters, unexpected '!')" }]);
  assert.equal(o.selftest.failures, 2);
  assert.ok(o.warnings.some((w) => /Panel -> Git self-test failed at token:/.test(w.text)));
  assert.deepEqual(o.versions.server, { version: "1.3.0", commit: "a".repeat(40), running: "1.2.2", running_commit: "" });
  assert.ok(o.warnings.some((w) => /mc-agent still runs v1\.2\.2 code although v1\.3\.0 is installed/.test(w.text)));
  assert.equal(o.server.product, "1.3.0");
  assert.equal(o.versions.worker, null, "no version.json in the product's own assets: unknown");
});

async function kvInventory(env) {
  const { results } = await env.DB.prepare("SELECT value FROM watchdog WHERE key = 'inventory'").all();
  return results.length ? JSON.parse(results[0].value) : null;
}

function withStamp(env, stamp) {
  const assets = env.ASSETS;
  env.ASSETS = {
    fetch: async (req) => (new URL(req.url).pathname === "/version.json" ? new Response(JSON.stringify(stamp)) : assets.fetch(req)),
  };
  return env;
}

test("v1.3 versions: the Worker's stamp against the server's version, each mismatch with its fix", async () => {
  assert.equal(W.cmpVersion("1.2.10", "v1.2.9"), 1);
  assert.equal(W.cmpVersion("v1.3.0", "1.3.0"), 0);
  assert.equal(W.cmpVersion("1.2.2", "1.3.0"), -1);
  assert.equal(W.cmpVersion("", "1.3.0"), null);
  const pin = "b".repeat(40);
  const stamp = { version: "1.2.1", pin_sha: pin, pin_version: "v1.2.1", repository: "edvardk/smallworld" };
  const stale = W.versions({ version: "1.2.2", commit: "c".repeat(40), running: "1.2.2" }, stamp);
  assert.deepEqual(stale.problems.map((p) => p.text), ["The Worker is v1.2.1, the server is v1.2.2: run `mcctl render` + `mcctl apply` to update the Worker."]);
  const behind = W.versions({ version: "1.2.1", commit: "c".repeat(40), running: "1.2.1" }, { ...stamp, version: "1.2.2" });
  assert.match(behind.problems[0].text, /The server is v1\.2\.1, the Worker is v1\.2\.2: deploy/);
  const commit = W.versions({ version: "1.2.1", commit: "c".repeat(40), running: "1.2.1" }, stamp);
  assert.deepEqual(commit.problems.map((p) => p.level), ["info"]);
  assert.deepEqual(W.versions({ version: "1.2.1", commit: pin, running: "1.2.1" }, stamp).problems, []);
  assert.deepEqual(W.versions(null, null).problems, []);

  // through the panel: the stamp from the staged assets (version.json, never served), the header's product version
  W.resetIsolate();
  const env = withStamp(makePanelEnv(), { product: "1.2.1", pin: { sha: pin, version: "v1.2.1" }, repository: "edvardk/smallworld" });
  await seed(env, NOW);
  const inv = await kvInventory(env);
  await W.handle(new Request("https://hb.mc.example.com/ingest/inventory", { method: "POST", headers: { Authorization: `Bearer ${env.AGENT_TOKEN}` }, body: JSON.stringify({ inventory: { ...inv, product: { version: "1.2.2", commit: "c".repeat(40), running: "1.2.2" } } }) }), env, NOW, rt);
  const o = (await call(env, "/api/overview")).body;
  assert.deepEqual(o.versions.worker, stamp);
  assert.equal(o.server.product, "1.2.2");
  assert.ok(o.warnings.some((w) => w.text.includes("run `mcctl render` + `mcctl apply`")));
  assert.equal((await call(env, "/version.json")).status, 404, "the stamp isn't served");

  // the cron keeps the stamp in the key/value table for `mcctl doctor`, written only when it changes
  W.resetIsolate();
  const cronEnv = withStamp(makePanelEnv({ TARGET: JSON.stringify({ targets: [] }) }), { product: "1.2.1", pin: { sha: pin, version: "v1.2.1" } });
  const worker = async () => {
    const { results } = await cronEnv.DB.prepare("SELECT value, updated_at FROM watchdog WHERE key = 'worker'").all();
    return results[0] || null;
  };
  await W.runChecks(cronEnv, NOW, rt);
  assert.deepEqual(JSON.parse((await worker()).value), { version: "1.2.1", pin_sha: pin, pin_version: "v1.2.1", repository: "" });
  await W.runChecks(cronEnv, NOW + 300000, rt);
  assert.equal((await worker()).updated_at, NOW, "unchanged: not written again");
});
