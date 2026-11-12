// P9 tests for watchdog/worker.js: the panel on panel.<zone>. Cloudflare Access JWT verification (a test team's
// JWKS: RS256 only, signature, issuer, audience, expiry, allowed emails; 403 without a token even if Access were
// off), the read API over D1 (real SQL through node:sqlite) and R2, the live console through the Control object,
// security headers and same-origin POSTs. node --import ./watchdog/test/register.mjs --test watchdog/test/*.test.mjs
import assert from "node:assert/strict";
import { createHmac, webcrypto } from "node:crypto";
import { test } from "node:test";

import worker from "../worker.js";
import { AUD, EMAIL, PANEL_HOST, TEAM, claimsFor, connectAgent, makePanelEnv, makeTeam, runtimeFor, seed, signJwt } from "./panel-fixtures.mjs";

const W = worker._internals;
const NOW = Date.UTC(2026, 9, 31, 14, 13, 0);
const team = await makeTeam();
const otherTeam = await makeTeam(team.kid); // same kid, different key: a forgery
const rt = runtimeFor(team);

function req(path, { token, host = PANEL_HOST, method = "GET", body, headers = {} } = {}) {
  const h = { ...headers };
  if (token) h["Cf-Access-Jwt-Assertion"] = token;
  if (body !== undefined) h["Content-Type"] ??= "application/json";
  return new Request(`https://${host}${path}`, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
}

async function good(extra = {}) {
  return signJwt(team, claimsFor(NOW, extra));
}

async function call(env, path, opts = {}) {
  const resp = await W.handle(req(path, opts), env, NOW, rt);
  return { status: resp.status, headers: resp.headers, body: resp.headers.get("Content-Type")?.includes("json") ? await resp.json() : await resp.text() };
}

test("unauthenticated requests get 403 on every panel path, even with the Access app gone", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  for (const path of ["/", "/index.html", "/panel.js", "/seeds.json", "/api/me", "/api/overview", "/api/logs?q=x", "/ingest/metrics", "/status", "/nope"]) {
    const r = await call(env, path);
    assert.equal(r.status, 403, path);
    assert.match(r.headers.get("Content-Security-Policy"), /default-src 'none'/);
  }
  const html = await W.handle(req("/", { headers: { Accept: "text/html" } }), env, NOW, rt);
  assert.equal(html.status, 403);
  assert.match(await html.text(), /Cloudflare Access/);
  // a POST without a token changes nothing
  const before = env.DB.written;
  assert.equal((await call(env, "/api/console/command", { method: "POST", body: { command: "list" } })).status, 403);
  assert.equal(env.DB.written, before);
});

test("forged, expired, foreign or unlisted tokens are refused", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const t = Math.floor(NOW / 1000);
  const none = `${Buffer.from(JSON.stringify({ alg: "none", kid: team.kid })).toString("base64url")}.${Buffer.from(JSON.stringify(claimsFor(NOW))).toString("base64url")}.`;
  const hsKey = await webcrypto.subtle.importKey("raw", new TextEncoder().encode("secret"), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const hsHead = Buffer.from(JSON.stringify({ alg: "HS256", kid: team.kid })).toString("base64url");
  const hsBody = Buffer.from(JSON.stringify(claimsFor(NOW))).toString("base64url");
  const hsSig = Buffer.from(await webcrypto.subtle.sign("HMAC", hsKey, new TextEncoder().encode(`${hsHead}.${hsBody}`))).toString("base64url");
  const valid = await good();
  const [h, b] = valid.split(".");
  const cases = {
    garbage: "not.a.jwt",
    "two parts": "abc.def",
    "alg none": none,
    "HS256 with a guessed secret": `${hsHead}.${hsBody}.${hsSig}`,
    "signed by another key with the same kid": await signJwt(otherTeam, claimsFor(NOW)),
    "unknown kid": await signJwt(team, claimsFor(NOW), { kid: "rotated-away" }),
    "claims edited after signing": `${h}.${Buffer.from(JSON.stringify(claimsFor(NOW, { email: "evil@example.com" }))).toString("base64url")}.${valid.split(".")[2]}`,
    "wrong audience": await good({ aud: ["b".repeat(64)] }),
    "wrong issuer": await good({ iss: "https://evil.cloudflareaccess.com" }),
    expired: await good({ exp: t - 120 }),
    "not yet valid": await good({ nbf: t + 600 }),
    "issued in the future": await good({ iat: t + 600 }),
    "no iat": await good({ iat: undefined }),
    "email not on panel.access.emails": await good({ email: "someone@example.com" }),
    "no email (service token)": await good({ email: undefined }),
  };
  for (const [name, token] of Object.entries(cases)) {
    assert.equal((await call(env, "/api/me", { token })).status, 403, name);
  }
  assert.ok(b);
  const ok = await call(env, "/api/me", { token: valid });
  assert.equal(ok.status, 200);
  assert.equal(ok.body.email, EMAIL);
  assert.equal(ok.body.reauth_minutes, 10);
  assert.equal(ok.body.login_age_s, 30);
  // emails compare case-insensitively; a second allowed address works too
  assert.equal((await call(env, "/api/me", { token: await good({ email: "Friend@Example.com" }) })).status, 200);
});

test("the team's certs are fetched once and cached; a fetch failure fails closed", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const t2 = await makeTeam();
  const r2 = runtimeFor(t2);
  const token = await signJwt(t2, claimsFor(NOW));
  for (let i = 0; i < 5; i++) assert.equal((await W.handle(req("/api/me", { token }), env, NOW + i, r2)).status, 200);
  assert.equal(t2.fetches, 1);
  W.resetIsolate();
  const broken = { ...r2, fetch: async () => new Response("down", { status: 502 }) };
  assert.equal((await W.handle(req("/api/me", { token }), env, NOW, broken)).status, 403);
  const throws = { ...r2, fetch: async () => { throw new TypeError("network"); } };
  assert.equal((await W.handle(req("/api/me", { token }), env, NOW, throws)).status, 403);
});

test("P9 review: certs failures are negative-cached; the last good keys survive a short outage, then fail closed", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const t3 = await makeTeam();
  const good3 = runtimeFor(t3);
  const token = async (at) => signJwt(t3, claimsFor(at));
  let calls = 0;
  const down = { ...good3, fetch: async () => { calls += 1; return new Response("down", { status: 503 }); } };
  // never had keys: closed, and the failing URL is asked at most once a minute
  assert.equal((await W.handle(req("/api/me", { token: await token(NOW) }), env, NOW, down)).status, 403);
  assert.equal((await W.handle(req("/api/me", { token: await token(NOW + 5000) }), env, NOW + 5000, down)).status, 403);
  assert.equal(calls, 1);
  // good keys, then the certs URL breaks after the 1 h TTL: the old keys still verify for the grace window
  W.resetIsolate();
  assert.equal((await W.handle(req("/api/me", { token: await token(NOW) }), env, NOW, good3)).status, 200);
  calls = 0;
  const later = NOW + 3600000 + 60000;
  assert.equal((await W.handle(req("/api/me", { token: await token(later) }), env, later, down)).status, 200);
  assert.equal((await W.handle(req("/api/me", { token: await token(later + 1000) }), env, later + 1000, down)).status, 200);
  assert.equal(calls, 1, "one refetch attempt, then the negative cache");
  // past TTL + grace: no usable keys, closed
  const muchLater = NOW + 3600000 + 16 * 60000;
  assert.equal((await W.handle(req("/api/me", { token: await token(muchLater) }), env, muchLater, down)).status, 403);
  // and the URL recovering brings it back
  const back = muchLater + 61000;
  assert.equal((await W.handle(req("/api/me", { token: await token(back) }), env, back, good3)).status, 200);
});

test("a misconfigured panel binding fails closed", async () => {
  W.resetIsolate();
  for (const bad of [{ aud: "" }, { emails: [] }, { team_domain: "bad domain/" }]) {
    const env = makePanelEnv();
    env.PANEL = JSON.stringify({ ...JSON.parse(env.PANEL), ...bad });
    assert.equal((await call(env, "/api/me", { token: await good() })).status, 403, JSON.stringify(bad));
  }
});

test("the panel host serves only the panel; the watchdog host serves no panel", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const token = await good();
  const before = env.DB.written;
  const ingest = await call(env, "/ingest/metrics", { token, method: "POST", body: { samples: [{ ts: NOW }] }, headers: { Authorization: `Bearer ${env.AGENT_TOKEN}`, Origin: `https://${PANEL_HOST}` } });
  assert.equal(ingest.status, 405); // not an ingest route here
  assert.equal(env.DB.written, before);
  assert.equal((await call(env, "/status", { token })).status, 404);
  assert.equal((await call(env, "/api/overview", { host: "hb.mc.example.com", token })).status, 404);
  assert.equal((await call(env, "/status", { host: "hb.mc.example.com" })).status, 200);
});

test("assets come from the ASSETS binding with a strict CSP; unknown paths are 404", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const token = await good();
  const page = await call(env, "/", { token });
  assert.equal(page.status, 200);
  assert.match(page.body, /<script type="module" src="\/panel.js"><\/script>/);
  assert.doesNotMatch(page.body, /<script>|style="/); // nothing inline: the CSP has no 'unsafe-inline'
  const csp = page.headers.get("Content-Security-Policy");
  assert.match(csp, /script-src 'self'/);
  assert.doesNotMatch(csp, /unsafe/);
  assert.equal(page.headers.get("X-Frame-Options"), "DENY");
  const js = await call(env, "/panel.js", { token });
  assert.equal(js.status, 200);
  assert.match(js.headers.get("Content-Type"), /javascript/);
  assert.equal((await call(env, "/fonts/chakra-petch-600.woff2", { token })).status, 200);
  // v1.2: the seed catalogue (verified on the pinned build) for the Worlds tab; behind Access like every asset
  const seeds = await call(env, "/seeds.json", { token });
  assert.equal(seeds.status, 200);
  const catalogue = seeds.body;
  assert.ok(catalogue.minecraft_version && catalogue.seeds.length >= 10 && catalogue.seeds.every((e) => e.verified === true));
  for (const path of ["/../worker.js", "/test/panel.test.mjs", "/fonts/OFL.txt", "/x.html"]) {
    assert.equal((await call(env, path, { token })).status, 404, path);
  }
  assert.equal((await call(env, "/", { token, method: "POST", body: {} })).status, 405);
});

test("P17: the resource pack is public on the watchdog host only, by its sha1, immutable", async () => {
  W.resetIsolate();
  // v1.2: mcctl render stages the pack next to the panel's files (rendered/watchdog/pack/<sha1>.zip; the
  // product commits none), so the test adds one to the panel's assets
  const { createHash } = await import("node:crypto");
  const want = Buffer.from("PK\x03\x04 a stand-in for the rendered pack");
  const file = `${createHash("sha1").update(want).digest("hex")}.zip`;
  const base = makePanelEnv();
  const env = {
    ...base,
    ASSETS: {
      fetch: async (r) =>
        new URL(r.url).pathname === `/pack/${file}`
          ? new Response(r.method === "HEAD" ? null : want, { headers: { "Content-Type": "application/zip" } })
          : base.ASSETS.fetch(r),
    },
  };
  const host = "hb.mc.example.com";
  const resp = await W.handle(req(`/pack/${file}`, { host }), env, NOW, rt); // no credentials: Minecraft clients
  assert.equal(resp.status, 200);
  assert.equal(resp.headers.get("Content-Type"), "application/zip");
  assert.match(resp.headers.get("Cache-Control"), /immutable/);
  assert.deepEqual(Buffer.from(await resp.arrayBuffer()), want);
  const head = await W.handle(req(`/pack/${file}`, { host, method: "HEAD" }), env, NOW, rt);
  assert.equal(head.status, 200);
  assert.equal((await call(env, `/pack/${file}`, { host, method: "POST", body: {} })).status, 405);
  assert.equal((await call(env, `/pack/${"0".repeat(40)}.zip`, { host })).status, 404); // not uploaded
  for (const path of ["/pack/x.zip", "/pack/../index.html", `/pack/${file}/x`, "/index.html", "/panel.js"]) {
    assert.equal((await call(env, path, { host })).status, 404, path); // nothing else of the assets is public
  }
  assert.equal((await call(env, `/pack/${file}`)).status, 403); // the panel host keeps its Access check
  const noAssets = { ...env, ASSETS: undefined };
  assert.equal((await call(noAssets, `/pack/${file}`, { host })).status, 404);
});

test("overview: latest sample, two hours of history, watchdog, inventory, audit and warnings", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const r = await call(env, "/api/overview", { token: await good() });
  assert.equal(r.status, 200);
  const o = r.body;
  assert.equal(o.server.title, "Lighthouse");
  assert.equal(o.server.version, "Purpur 26.3");
  assert.deepEqual(o.sample.players_online, ["Em_K", "MiaBuilds", "jonas_r"]);
  assert.equal(o.sample.cpu_cores, 4);
  assert.equal(o.sample_age_s, 7);
  assert.ok(o.history.length >= 115 && o.history.length <= 121, `${o.history.length}`);
  assert.ok(o.history.every((p) => !("players_online" in p)));
  assert.equal(o.watchdog.probes.v4.ok, true);
  assert.equal(o.watchdog.probes.v4.online, 3);
  assert.deepEqual(o.inventory, { whitelist: 7, ops: 1, op_names: ["Em_K"], bans: 2, plugins: 1, disabled_plugins: 0, received: NOW - 300000, pending: 1, rejected: 2 });
  assert.equal(o.audit[0].source, "server");
  assert.ok(o.audit.some((a) => a.source === "panel" && a.login === EMAIL));
  assert.ok(o.warnings.some((w) => /MSPT went over 50 ms/.test(w.text) && /1891.3/.test(w.text)));
  assert.ok(!o.warnings.some((w) => /No sample/.test(w.text)));
  // nothing stored yet: still answers, warns that the agent is silent
  W.resetIsolate();
  const empty = (await call(makePanelEnv(), "/api/overview", { token: await good() })).body;
  assert.equal(empty.sample, null);
  assert.ok(empty.warnings.some((w) => /No sample/.test(w.text)));
});

test("metrics ranges: raw for 2 h, bucketed beyond, rollups for 30/90 days", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS samples_5m (ts INTEGER PRIMARY KEY, data TEXT NOT NULL)").run();
  await env.DB.prepare("INSERT INTO samples_5m (ts, data) VALUES (?1, ?2)").bind(NOW - 20 * 86400000, JSON.stringify({ tps: 19.5, mspt: 20 })).run();
  const token = await good();
  const raw = (await call(env, "/api/metrics?range=2h", { token })).body;
  assert.equal(raw.step_ms, null);
  const day = (await call(env, "/api/metrics?range=24h", { token })).body;
  assert.equal(day.step_ms, 300000);
  assert.ok(day.points.length < raw.points.length && day.points.length >= 24);
  const month = (await call(env, "/api/metrics?range=30d", { token })).body;
  assert.equal(month.points[0].tps, 19.5);
  assert.equal((await call(env, "/api/metrics?range=bogus", { token })).body.range, "2h");
});

test("log search: substring (LIKE-escaped), unit, level, audit flag, paging, days", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const token = await good();
  const all = (await call(env, "/api/logs", { token })).body;
  assert.equal(all.day, "20261031");
  assert.equal(all.rows.length, 15);
  assert.ok(all.rows[0].id > all.rows[1].id); // newest first
  const q = (await call(env, "/api/logs?q=joined", { token })).body;
  assert.deepEqual(q.rows.map((r) => r.msg.slice(-15)), ["joined the game", "joined the game"]);
  assert.equal((await call(env, "/api/logs?q=100%25", { token })).body.rows.length, 0); // % is literal
  assert.equal((await call(env, "/api/logs?level=warn", { token })).body.rows.length, 2);
  assert.equal((await call(env, "/api/logs?unit=mc-backup.service", { token })).body.rows.length, 4);
  assert.equal((await call(env, "/api/logs?audit=1", { token })).body.rows.length, 2);
  const page1 = (await call(env, "/api/logs?limit=10", { token })).body;
  assert.equal(page1.rows.length, 10);
  const page2 = (await call(env, `/api/logs?limit=10&before=${page1.next_before}`, { token })).body;
  assert.equal(page2.rows.length, 5);
  assert.equal(page2.next_before, null);
  assert.deepEqual((await call(env, "/api/logs?day=20200101", { token })).body.rows, []);
  assert.equal((await call(env, "/api/logs?day=x;DROP", { token })).status, 200); // never interpolated
});

test("backups from restic's journal lines; audit merges panel and server entries; archives listed", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const token = await good();
  const b = (await call(env, "/api/backups", { token })).body;
  assert.deepEqual(b.snapshots.map((s) => [s.id, s.added]), [["f0a93b11", "188.004 MiB"], ["8c41d2e7", "212.345 MiB"]]);
  assert.equal(b.age_s, 600);
  const a = (await call(env, "/api/audit", { token })).body.entries;
  assert.deepEqual(a.map((e) => e.source), ["server", "panel", "server"]); // newest first, merged
  const ar = (await call(env, "/api/archives?day=20261031", { token })).body;
  assert.equal(ar.objects.length, 1);
  const inv = (await call(env, "/api/inventory", { token })).body;
  assert.equal(inv.inventory.whitelist.length, 7);
  assert.deepEqual(inv.online, ["Em_K", "MiaBuilds", "jonas_r"]);
});

test("P9 review: inventory flags are booleans whatever the agent sends", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const plugins = [
    { file: "a.jar", name: "a", enabled: 'true" onmouseover="alert(1)' },
    { file: "b.jar", name: "b", enabled: 1 },
    { file: "c.jar", name: "c", enabled: true },
    { file: "d.jar", name: "d" },
  ];
  const whitelist = [
    { name: "Dora_K", uuid: "9e1d0000-0000-4000-8000-0000000000d4", unmanaged: true },
    { name: "eli", uuid: "7b2e0000-0000-4000-8000-0000000000e5", unmanaged: "yes" },
    { name: "Em_K", uuid: "3f2a0000-0000-4000-8000-000000000c41" },
  ];
  const post = new Request("https://hb.mc.example.com/ingest/inventory", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.AGENT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ inventory: { ts: NOW, plugins, whitelist } }),
  });
  assert.equal((await W.handle(post, env, NOW, rt)).status, 200);
  const inv = (await call(env, "/api/inventory", { token: await good() })).body.inventory;
  assert.deepEqual(inv.plugins.map((p) => p.enabled), [false, false, true, false]);
  // v1.3 whitelist guard: "kept by the last deploy, not in players.yml" is a boolean too
  assert.deepEqual(inv.whitelist.map((w) => w.unmanaged), [true, false, false]);
  assert.deepEqual(inv.whitelist[0], { name: "Dora_K", uuid: "9e1d0000-0000-4000-8000-0000000000d4", unmanaged: true });
});

test("v1.5.3: rejected logins of players whitelisted outside the panel are neither counted nor served", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const whitelist = [
    { name: "Em_K", uuid: "3f2a0000-0000-4000-8000-000000000c41" },
    { name: "felix_2011", uuid: "4f2a0000-0000-4000-8000-000000000c42" }, // added through players.yml, other case
  ];
  const rejected = [
    { name: "xXcreeperXx", count: 3, first: NOW - 86400000, last: NOW - 1400000 },
    { name: "Felix_2011", count: 1, first: NOW - 10800000, last: NOW - 10800000 },
    { name: "EM_K", count: 2, first: NOW - 7200000, last: NOW - 3600000 },
  ];
  const post = new Request("https://hb.mc.example.com/ingest/inventory", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.AGENT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ inventory: { ts: NOW, whitelist, rejected } }),
  });
  assert.equal((await W.handle(post, env, NOW, rt)).status, 200);
  const token = await good();
  assert.equal((await call(env, "/api/overview", { token })).body.inventory.rejected, 1); // the "1 req" badge
  const inv = (await call(env, "/api/inventory", { token })).body.inventory;
  assert.deepEqual(inv.rejected.map((r) => r.name), ["xXcreeperXx"]);
  assert.equal(inv.whitelist.length, 2); // the rest of the inventory is untouched
  // everyone on the list already: no badge, nothing listed
  const post2 = new Request("https://hb.mc.example.com/ingest/inventory", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.AGENT_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ inventory: { ts: NOW, whitelist: [...whitelist, { name: "XXCREEPERXX" }], rejected } }),
  });
  assert.equal((await W.handle(post2, env, NOW, rt)).status, 200);
  assert.equal((await call(env, "/api/overview", { token })).body.inventory.rejected, 0);
  assert.deepEqual((await call(env, "/api/inventory", { token })).body.inventory.rejected, []);
});

test("console: same-origin POSTs only; a command is signed for the agent, audited, and its result recorded", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const agent = connectAgent(env);
  const token = await good();
  const origin = { Origin: `https://${PANEL_HOST}`, "Sec-Fetch-Site": "same-origin" };
  assert.equal((await call(env, "/api/console/command", { token, method: "POST", body: { command: "list" } })).status, 403);
  assert.equal((await call(env, "/api/console/command", { token, method: "POST", body: { command: "list" }, headers: { Origin: "https://evil.example" } })).status, 403);
  assert.equal((await call(env, "/api/console/command", { token, method: "POST", body: { command: "list" }, headers: { ...origin, "Sec-Fetch-Site": "cross-site" } })).status, 403);
  assert.equal((await call(env, "/api/console/command", { token, method: "POST", body: { command: "list" }, headers: { ...origin, "Content-Type": "text/plain" } })).status, 403);
  const r = await call(env, "/api/console/command", { token, method: "POST", body: { command: "list" }, headers: origin });
  assert.equal(r.status, 200);
  const sent = agent.sent.find((m) => m.type === "command");
  const payload = JSON.parse(sent.payload);
  assert.deepEqual([payload.action, payload.args, payload.login], ["console", { command: "list" }, EMAIL]);
  assert.equal(sent.sig, createHmac("sha256", env.COMMAND_HMAC_KEY).update(sent.payload).digest("hex"));
  assert.equal((await call(env, `/api/commands/${r.body.id}`, { token })).body.result, null);
  await env.control.webSocketMessage(agent, JSON.stringify({ type: "result", id: r.body.id, ok: true, output: "There are 3 of a max of 20 players online" }));
  const res = (await call(env, `/api/commands/${r.body.id}`, { token })).body.result;
  assert.equal(res.ok, true);
  const { results } = await env.DB.prepare("SELECT login, action, args, result, ok FROM audit WHERE cmd_id = ?1").bind(r.body.id).all();
  assert.deepEqual({ ...results[0] }, { login: EMAIL, action: "console", args: '{"command":"list"}', result: "ok", ok: 1 });
  assert.equal((await call(env, "/api/console/command", { token, method: "POST", body: { command: "" }, headers: origin })).status, 400);
});

test("live console: a viewer's subscription sends one signed tail; streamed lines come back in order", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  await seed(env, NOW);
  const token = await good();
  const origin = { Origin: `https://${PANEL_HOST}` };
  const first = (await call(env, "/api/console", { token })).body;
  assert.equal(first.live, false);
  assert.equal(first.backlog.length, 8); // minecraft.service lines from D1
  assert.equal((await call(env, "/api/console/live", { token, method: "POST", body: {}, headers: origin })).body.live, false); // no agent
  const agent = connectAgent(env);
  assert.equal((await call(env, "/api/console/live", { token, method: "POST", body: {}, headers: origin })).body.live, true);
  assert.equal((await call(env, "/api/console/live", { token, method: "POST", body: {}, headers: origin })).body.live, true);
  const tails = agent.sent.filter((m) => m.type === "command").map((m) => JSON.parse(m.payload));
  assert.equal(tails.length, 1); // renewed only near the end of its window
  assert.deepEqual([tails[0].action, tails[0].args], ["tail", { seconds: 120 }]);
  await env.control.webSocketMessage(agent, JSON.stringify({ type: "console", lines: [{ ts: NOW, prio: 6, msg: "[14:13:01 INFO]: Em_K left the game" }, { ts: NOW, prio: 4, msg: "warn line" }] }));
  const live = (await call(env, "/api/console?after=0", { token })).body;
  assert.equal(live.live, true);
  assert.deepEqual(live.lines.map((l) => [l.seq, l.msg]), [[1, "[14:13:01 INFO]: Em_K left the game"], [2, "warn line"]]);
  assert.deepEqual((await call(env, `/api/console?after=${live.next}`, { token })).body.lines, []);
});

test("every API response is no-store with the panel's security headers", async () => {
  W.resetIsolate();
  const env = makePanelEnv();
  const token = await good();
  for (const path of ["/api/me", "/api/health", "/api/logs", "/api/nope"]) {
    const r = await call(env, path, { token });
    assert.equal(r.headers.get("Cache-Control"), "no-store", path);
    assert.equal(r.headers.get("X-Content-Type-Options"), "nosniff");
    assert.equal(r.headers.get("Referrer-Policy"), "no-referrer");
  }
  assert.ok(AUD && TEAM);
});
