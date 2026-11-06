// P9 UI smoke test: the real panel (watchdog/panel/ served by the real Worker code) in Chromium via Playwright,
// against a local mock API: an HTTP server that hands each request to worker.js with a fake env (node:sqlite D1
// seeded with two hours of samples, logs, inventory, watchdog state), plays the Access edge (adds a signed
// Cf-Access-Jwt-Assertion for a test team) and fakes the agent (answers console commands, streams console lines).
// Checks desktop and phone widths, dark and light: no console errors (CSP violations included), no horizontal
// scroll, every tab renders, a console command round-trips. P10: the action dialogs (restart with a countdown
// that shows in the header banner and can be cancelled, whitelist add with the Mojang lookup, kick/ban, backup
// now) against a fake agent that answers each signed action and reports jobs. P10 review: plugin switches and
// restores open the confirmation page on the confirmation host (its own Access app, routed by Playwright to the
// same Worker): a stale confirmation login is refused there, a fresh one signs the action. Optional screenshots.
// Panel performance: a scripted scroll of every tab under CDP CPU throttling (4x, a slow laptop) measures frame
// times (rAF deltas) on desktop and phone and asserts a generous p95; the effects switch (auto/full/light) is
// persisted. v1.2: the client setup page too (client-smoke.mjs; CLIENT_SHOTS_DIR overrides its screenshot dir).
// PANEL_SHOTS=before|after also screenshots every tab (desktop + phone) as panel-<tag>-<tab>-<width>.png
// and writes panel-perf-<tag>.json (into PANEL_SHOTS_DIR, default the screenshot dir) for before/after comparisons.
//
//   node --import ./watchdog/test/register.mjs watchdog/test/ui.smoke.mjs [screenshot-dir]
// Scale 2 for every screenshot people look at (desktop too while a screenshot dir is given); scripts/screenshots.py
// regenerates docs/images/ with it.
// Needs Playwright with Chromium (resolved through NODE_PATH); tests/test_panel.py skips it when absent.
import { readFileSync } from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";

import worker from "../worker.js";
import { CONFIRM_AUD, CONFIRM_HOST, PANEL_HOST, claimsFor, connectAgent, makePanelEnv, makeTeam, runtimeFor, seed, seedExperience, signJwt } from "./panel-fixtures.mjs";
import { SqliteD1 } from "./d1-sqlite.mjs";
import { clientSmoke } from "./client-smoke.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const shotDir = process.argv[2] || "";
const SHOTS = /^[a-z0-9-]+$/.test(process.env.PANEL_SHOTS || "") ? process.env.PANEL_SHOTS : "";
const SHOTS_DIR = process.env.PANEL_SHOTS_DIR || shotDir;
const THROTTLE = 4; // CDP CPU throttling: the owner's slow laptop
const P95_LIMIT_MS = 50; // generous (60 Hz = 16.7 ms; before the panel perf work: 33-117 ms on desktop)

const team = await makeTeam();
const rt = runtimeFor(team, async (url) => {
  const m = /^https:\/\/api\.mojang\.com\/users\/profiles\/minecraft\/(.+)$/.exec(url);
  if (m && decodeURIComponent(m[1]).toLowerCase() === "felix_2011") {
    return new Response(JSON.stringify({ id: "0d61aa00bb0011224455667788997f90", name: "Felix_2011" }), { status: 200 });
  }
  if (m) return new Response(null, { status: 204 });
  throw new Error(`unexpected fetch ${url}`);
});
let staleConfirm = false; // the re-auth check: sign the confirmation host's requests with a 1 h old Access login
const actions = []; // what the fake agent was asked to do
const env = makePanelEnv({ TELEMETRY: new SqliteD1(), EXPERIENCE: JSON.stringify({ minecraft_version: "26.3" }) });
await seed(env, Date.now());
await seedExperience(env, Date.now(), worker._internals.handle, rt); // P13
const agent = connectAgent(env);
// keep the newest sample fresh: the whole run takes longer than the overview's 3-minute "No data" threshold
async function freshSample() {
  const t = `samples_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}`;
  const row = await env.DB.prepare(`SELECT data FROM ${t} ORDER BY ts DESC LIMIT 1`).first();
  if (row) await env.DB.prepare(`INSERT OR IGNORE INTO ${t} (ts, data) VALUES (?1, ?2)`).bind(Date.now(), row.data).run();
}
const freshTimer = setInterval(() => freshSample().catch(() => {}), 30000);
let streaming = null;
const realSend = agent.send.bind(agent);
agent.send = (text) => {
  realSend(text);
  const msg = JSON.parse(text);
  if (msg.type !== "command") return;
  const cmd = JSON.parse(msg.payload);
  setTimeout(async () => {
    const reply = (ok, output, error = "") => env.control.webSocketMessage(agent, JSON.stringify({ type: "result", id: cmd.id, ok, output, error }));
    const job = (state, extra = {}) =>
      env.control.fetch(new Request("https://control/job", { method: "POST", body: JSON.stringify({ job: { id: cmd.id, action: cmd.action, label: cmd.action === "stop" ? "Server stopping" : cmd.action === "restore" ? "Restore" : cmd.action.startsWith("world_") ? "World switch" : "Restart", login: cmd.login, state, step: "", countdown: cmd.args.countdown || 0, deadline: Date.now() + (cmd.args.countdown || 0) * 1000, started: Date.now(), players: 3, ...extra } }) }));
    if (!["console", "tail"].includes(cmd.action)) actions.push(cmd);
    if (["restart", "stop", "plugin", "restore", "world_create", "world_switch", "world_delete"].includes(cmd.action)) {
      await job("countdown", { step: `countdown ${cmd.args.countdown} s, 3 player(s) online` });
      await reply(true, `${cmd.action} scheduled (countdown ${cmd.args.countdown} s)`);
    } else if (cmd.action === "cancel") {
      await job("cancelled", { label: "Restart", action: "restart", step: "cancelled during the countdown" });
      await reply(true, "restart cancelled");
    } else if (["whitelist", "moderate", "backup", "start", "rejected_dismiss"].includes(cmd.action)) {
      await reply(true, cmd.action === "whitelist" ? `Added ${cmd.args.name} to the whitelist (live now; recorded for players.yml in Git)` : `${cmd.action} ok`);
    } else if (cmd.action === "console") {
      const out = cmd.args.command === "list" ? "There are 3 of a max of 20 players online: Em_K, MiaBuilds, jonas_r" : "Unknown command";
      await env.control.webSocketMessage(agent, JSON.stringify({ type: "result", id: cmd.id, ok: true, output: out }));
    } else if (cmd.action === "tail" && !streaming) {
      await env.control.webSocketMessage(agent, JSON.stringify({ type: "result", id: cmd.id, ok: true, output: "live console for 120 s" }));
      let n = 0;
      streaming = setInterval(() => {
        const t = new Date().toTimeString().slice(0, 8);
        const lines = [{ ts: Date.now(), prio: 6, msg: n % 3 === 0 ? `[${t} INFO]: <MiaBuilds> live line ${n}` : `[${t} INFO]: Saving chunks for level 'ServerLevel[world]' (${n})` }];
        n += 1;
        env.control.webSocketMessage(agent, JSON.stringify({ type: "console", lines }));
      }, 700);
    }
  }, 150);
};

const server = http.createServer(async (req, res) => {
  try {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) if (typeof v === "string" && k !== "host") headers.set(k, v);
    // the browser talks to 127.0.0.1; the Worker sees what Cloudflare would send it for panel.<zone>
    if (headers.get("origin")) headers.set("origin", `https://${PANEL_HOST}`);
    const t = Math.floor(Date.now() / 1000);
    headers.set("Cf-Access-Jwt-Assertion", await signJwt(team, claimsFor(Date.now())));
    const request = new Request(`https://${PANEL_HOST}${req.url}`, {
      method: req.method,
      headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : Buffer.concat(chunks),
    });
    const resp = await worker._internals.handle(request, env, Date.now(), rt);
    res.writeHead(resp.status, Object.fromEntries(resp.headers));
    res.end(Buffer.from(await resp.arrayBuffer()));
  } catch (e) {
    res.writeHead(500);
    res.end(String(e && e.stack));
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

const failures = [];
const expect = (ok, what) => {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures.push(what);
};

async function confirm(page, okText) {
  await page.waitForSelector("#dlg[open]");
  if (okText) expect((await page.textContent("#dlgOk")) === okText, `dialog OK button says "${okText}"`);
  await page.click("#dlgOk");
  await page.waitForSelector("#dlg:not([open])", { state: "attached" });
}

// The confirmation host (<panel>-confirm.<zone>), as Cloudflare would send it to the Worker: its own Access app.
async function routeConfirm(ctx) {
  await ctx.route(`https://${CONFIRM_HOST}/**`, async (route) => {
    const r = route.request();
    const headers = new Headers();
    for (const [k, v] of Object.entries(r.headers())) if (typeof v === "string" && k !== "host") headers.set(k, v);
    const t = Math.floor(Date.now() / 1000);
    headers.set("Cf-Access-Jwt-Assertion", await signJwt(team, claimsFor(Date.now(), { aud: [CONFIRM_AUD], ...(staleConfirm ? { iat: t - 3600 } : {}) })));
    const body = r.postDataBuffer();
    const resp = await worker._internals.handle(new Request(r.url(), { method: r.method(), headers, body: ["GET", "HEAD"].includes(r.method()) ? undefined : body }), env, Date.now(), rt);
    await route.fulfill({ status: resp.status, headers: Object.fromEntries(resp.headers), body: Buffer.from(await resp.arrayBuffer()) });
  });
}

// OK in the panel's dialog opens the confirmation window; confirm there; the panel shows its "confirm in the new
// window" note meanwhile. Returns the confirmation page's final status line.
async function confirmInWindow(ctx, page, name, okText, { stale = false } = {}) {
  const opened = ctx.waitForEvent("page");
  await page.waitForSelector("#dlg[open]");
  expect((await page.textContent("#dlgOk")) === okText, `dialog OK button says "${okText}"`);
  await page.click("#dlgOk"); // the panel opens the confirmation window and its own note at once
  const popup = await opened;
  const perr = [];
  popup.on("pageerror", (e) => perr.push(e.message));
  popup.on("console", (m) => {
    if (m.type() === "error" && !/401/.test(m.text())) perr.push(m.text());
  });
  await popup.waitForSelector("#cOk", { timeout: 15000 });
  await popup.waitForFunction(() => !/Checking/.test(document.querySelector("#cWho").textContent), null, { timeout: 15000 });
  if (await popup.$("#cTyped")) {
    // p11-review: a restore is typed on the confirmation page itself; the fragment can't pre-fill it
    expect(await popup.isDisabled("#cOk"), `${name}: confirmation page OK disabled before typing`);
    expect((await popup.inputValue("#cTyped")) === "", `${name}: the typed confirmation isn't pre-filled`);
    const phrase = (await popup.textContent('label[for="cTyped"] .typed')).trim();
    await popup.fill("#cTyped", phrase);
  }
  await popup.waitForSelector("#cOk:not([disabled])", { timeout: 15000 });
  const over = await popup.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(over <= 0, `${name}: the confirmation page has no horizontal scroll (${over}px over)`);
  staleConfirm = stale;
  await popup.click("#cOk");
  await popup.waitForFunction(() => /scheduled|refused|sign out|Not sent|no answer/i.test(document.querySelector("#cStatus").textContent), null, { timeout: 15000 });
  staleConfirm = false;
  const text = await popup.textContent("#cStatus");
  if (shotDir && name === "desktop" && !stale) await popup.screenshot({ path: `${shotDir}/panel-confirm-${name}.png` });
  expect(perr.length === 0, `${name}: no errors on the confirmation page (${perr.slice(0, 3).join(" | ")})`);
  await popup.close();
  await page.waitForSelector("#dlg[open]");
  expect((await page.textContent("#dlgTitle")) === "Confirm in the new window", `${name}: the panel points to the confirmation window`);
  await page.click("#dlgOk");
  await page.waitForSelector("#dlg:not([open])", { state: "attached" });
  return text;
}

async function p10Dialogs(page, name, ctx) {
  // Restart: the dialog with countdown choices and the players' preview; the countdown shows in the banner.
  await page.click("#tab-overview");
  await page.click("#btnRestart");
  await page.waitForSelector("#dlg[open] #cdSeg");
  expect((await page.textContent("#cdPreview")).includes("Restart in 1 minute"), `${name}: restart dialog previews the warning`);
  await page.click('#cdSeg [data-v="10"]');
  expect((await page.textContent("#cdPreview")).includes("Restart in 10 seconds"), `${name}: countdown choice updates the preview`);
  expect(await page.isChecked("#cdBackup"), `${name}: backup first is on by default`);
  if (shotDir) await page.screenshot({ path: `${shotDir}/panel-restart-${name}.png`, fullPage: false });
  await confirm(page, "Start countdown");
  await page.waitForFunction(() => document.querySelector("#countdown")?.classList.contains("on"), null, { timeout: 10000 });
  expect(/Restart in \d+s/.test(await page.textContent("#cdText")), `${name}: the header banner counts down`);
  const sent = actions.filter((a) => a.action === "restart").at(-1);
  expect(sent && sent.args.countdown === 10 && sent.args.backup === true && sent.login === "sam@example.com", `${name}: restart signed with countdown 10, backup, Access identity`);
  await page.click("#cdCancel");
  await page.waitForFunction(() => !document.querySelector("#countdown")?.classList.contains("on"), null, { timeout: 10000 });
  expect(actions.at(-1).action === "cancel", `${name}: cancel reaches the agent and the banner goes away`);

  // Backup now
  await page.click("#btnBackup");
  await confirm(page, "Back up now");
  await page.waitForFunction(() => /backup ok/.test(document.querySelector("#toast")?.textContent || ""), null, { timeout: 10000 });
  expect(actions.at(-1).action === "backup", `${name}: backup now`);

  // Whitelist add with the Mojang lookup
  await page.click("#tab-players");
  await page.waitForSelector("#wlName");
  await page.waitForTimeout(800);
  await page.fill("#wlName", "NoSuchPlayer");
  await page.waitForFunction(() => /No Java account/.test(document.querySelector("#wlLookup")?.textContent || ""), null, { timeout: 10000 });
  expect(await page.isDisabled("#wlAdd"), `${name}: unknown names can't be added`);
  await page.fill("#wlName", "felix_2011");
  await page.waitForFunction(() => /Found Java account Felix_2011/.test(document.querySelector("#wlLookup")?.textContent || ""), null, { timeout: 10000 });
  await page.click("#wlAdd");
  await confirm(page, "Add to whitelist");
  await page.waitForFunction(() => /Added Felix_2011/.test(document.querySelector("#toast")?.textContent || ""), null, { timeout: 10000 });
  const wl = actions.filter((a) => a.action === "whitelist").at(-1);
  expect(wl && wl.args.op === "add" && wl.args.name === "Felix_2011", `${name}: whitelist add uses Mojang's spelling`);
  expect((await page.textContent("#main")).includes("xXcreeperXx"), `${name}: rejected logins are listed`);
  expect((await page.textContent("#main")).includes("Whitelist → Git"), `${name}: pending changes and their PR are shown`);

  // Kick an online player (reason dialog)
  await page.click('[data-act="kick"][data-name="jonas_r"]');
  await page.fill("#reason", "brb");
  await confirm(page, "Kick");
  await page.waitForTimeout(800);
  const kick = actions.filter((a) => a.action === "moderate").at(-1);
  expect(kick && kick.args.verb === "kick" && kick.args.target === "jonas_r" && kick.args.reason === "brb", `${name}: kick with a reason`);

  // Plugin switch: confirmed in the confirmation window; a stale confirmation login is refused there
  await page.click("#tab-plugins");
  await page.waitForSelector('[data-plg="chunky"]');
  const before = actions.filter((a) => a.action === "plugin").length;
  await page.click('.switch[data-plg="chunky"]');
  const refused = await confirmInWindow(ctx, page, name, "Start restart countdown", { stale: true });
  expect(/sign out/i.test(refused) && actions.filter((a) => a.action === "plugin").length === before, `${name}: a stale confirmation login is refused (${refused})`);
  await page.click('.switch[data-plg="chunky"]');
  const done = await confirmInWindow(ctx, page, name, "Start restart countdown");
  await page.waitForTimeout(300);
  const plg = actions.filter((a) => a.action === "plugin").at(-1);
  expect(/scheduled/.test(done) && plg && plg.args.name === "chunky" && plg.args.enabled === false && plg.args.countdown === 60, `${name}: plugin disable signed after confirming (${done})`);

  // Restore: the OK button stays disabled until the snapshot's id is typed
  await page.click("#tab-backups");
  await page.waitForSelector('[data-restore]');
  await page.locator("[data-restore]").first().click();
  await page.waitForSelector("#dlg[open] #typedConfirm");
  expect(await page.isDisabled("#dlgOk"), `${name}: restore OK disabled before typing`);
  await page.fill("#typedConfirm", "restore wrong");
  expect(await page.isDisabled("#dlgOk"), `${name}: restore OK disabled for the wrong text`);
  await page.fill("#typedConfirm", "restore f0a93b11");
  expect(!(await page.isDisabled("#dlgOk")), `${name}: restore OK enabled for the right text`);
  if (shotDir) await page.screenshot({ path: `${shotDir}/panel-restore-${name}.png`, fullPage: false });
  await confirmInWindow(ctx, page, name, "Continue to confirmation");
  await page.waitForTimeout(500);
  const rs = actions.filter((a) => a.action === "restore").at(-1);
  expect(rs && rs.args.snapshot.startsWith("f0a93b11") && rs.args.countdown === 60 && !("confirm" in rs.args), `${name}: restore checked (typed confirmation) and signed`);
  // leave no job running for the next run
  await env.control.fetch(new Request("https://control/job", { method: "POST", body: JSON.stringify({ job: { id: "x", action: "restore", label: "Restore", state: "done", step: "RESTORED f0a93b11", deadline: Date.now(), started: Date.now() } }) }));
  await page.waitForTimeout(2500); // the panel sees the finished job (buttons enabled again)

  // v1.2 world library: switch (typed "switch to <name>" on the confirmation page) and create
  await page.click("#tab-worlds");
  await page.waitForSelector('[data-wsw="creative"]:not([disabled])', { timeout: 15000 });
  expect(await page.isDisabled('[data-wsw="old-smp"]') === false, `${name}: an older world can be switched to`);
  expect(/Archives use 11\.5 GB in R2/.test(await page.textContent("#main")), `${name}: the R2 storage note shows above 10 GB`);
  if (shotDir) await page.screenshot({ path: `${shotDir}/panel-worlds-${name}.png`, fullPage: false });
  await page.click('[data-wsw="old-smp"]');
  await page.waitForSelector("#dlg[open]");
  expect(/upgraded when it loads, one way/.test(await page.textContent("#dlgBody")), `${name}: the switch dialog warns about the one-way upgrade`);
  await confirmInWindow(ctx, page, name, "Continue to confirmation");
  await page.waitForTimeout(300);
  const sw = actions.filter((a) => a.action === "world_switch").at(-1);
  expect(sw && sw.args.name === "old-smp" && sw.args.countdown === 60 && !("confirm" in sw.args), `${name}: world switch typed and signed`);
  await env.control.fetch(new Request("https://control/job", { method: "POST", body: JSON.stringify({ job: { id: "y", action: "world_switch", label: "World switch", state: "done", step: "STEP 7/7: verified", deadline: Date.now(), started: Date.now() } }) }));
  await page.waitForTimeout(2500);
  await page.waitForSelector("#wCreate:not([disabled])", { timeout: 15000 });
  await page.click("#wCreate");
  await page.waitForSelector("#dlg[open] #wName");
  await page.fill("#wName", "farm");
  await page.fill("#wSeed", "-42");
  await page.selectOption("#wType", "flat_void");
  await page.selectOption("#wPreset", "farm_test");
  await page.fill("#wBorderO", "500");
  await confirmInWindow(ctx, page, name, "Continue to confirmation");
  await page.waitForTimeout(300);
  const cr = actions.filter((a) => a.action === "world_create").at(-1);
  expect(cr && cr.args.name === "farm" && cr.args.seed === "-42" && cr.args.type === "flat_void" && cr.args.border_radius["minecraft:overworld"] === 500, `${name}: world create signed with its settings`);
  await env.control.fetch(new Request("https://control/job", { method: "POST", body: JSON.stringify({ job: { id: "z", action: "world_create", label: "World switch", state: "done", step: "STEP 7/7: verified", deadline: Date.now(), started: Date.now() } }) }));

  // v1.2 seed catalogue: picking a verified seed fills the seed (default type) and shows its badge and claims;
  // the action is the same world_create with that seed
  const catalogue = JSON.parse(readFileSync(new URL("../panel/seeds.json", import.meta.url), "utf8"));
  const pick = catalogue.seeds[0];
  await page.waitForTimeout(2500);
  await page.waitForSelector("#wCreate:not([disabled])", { timeout: 15000 });
  await page.click("#wCreate");
  await page.waitForSelector("#dlg[open] #wCat");
  await page.selectOption("#wType", "amplified");
  await page.selectOption("#wCat", pick.id);
  expect((await page.inputValue("#wSeed")) === pick.seed && (await page.inputValue("#wType")) === "default", `${name}: a catalogue seed fills the seed and the default type`);
  const info = await page.textContent("#wCatInfo");
  expect(info.includes(`verified on ${catalogue.minecraft_version}`) && info.includes(pick.description) && pick.claims.every((c) => info.includes(`${c.distance} blocks from spawn`)), `${name}: the catalogue entry shows its badge, description and measured claims`);
  if (shotDir) await page.screenshot({ path: `${shotDir}/panel-seeds-${name}.png`, fullPage: false });
  await page.fill("#wName", "catalogue");
  await confirmInWindow(ctx, page, name, "Continue to confirmation");
  await page.waitForTimeout(300);
  const cc = actions.filter((a) => a.action === "world_create").at(-1);
  expect(cc && cc.args.name === "catalogue" && cc.args.seed === pick.seed && cc.args.type === "default", `${name}: world create signed with the catalogue's seed`);
  await env.control.fetch(new Request("https://control/job", { method: "POST", body: JSON.stringify({ job: { id: "s", action: "world_create", label: "World switch", state: "done", step: "STEP 7/7: verified", deadline: Date.now(), started: Date.now() } }) }));
}

// Scroll the page (and the console's log) down and back up over ~2.4 s, one step per animation frame, and return
// the frame times (rAF deltas): what the owner feels as scroll lag.
async function scrollFrames(page, target) {
  return page.evaluate(async (sel) => {
    const el = sel ? document.querySelector(sel) : document.scrollingElement;
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = 0;
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const deltas = [];
    const dur = 2400;
    await new Promise((done) => {
      let t0 = 0;
      let last = 0;
      const step = (now) => {
        if (!t0) t0 = last = now;
        else deltas.push(now - last);
        last = now;
        const p = (now - t0) / dur;
        if (p >= 1) return done();
        el.scrollTop = Math.round(max * (p < 0.5 ? p * 2 : 2 - p * 2));
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
    deltas.sort((a, b) => a - b);
    const q = (f) => Math.round(deltas[Math.min(deltas.length - 1, Math.floor(f * deltas.length))] * 10) / 10;
    return { frames: deltas.length, scroll_px: max, p50: q(0.5), p95: q(0.95), max: q(1) };
  }, target);
}

// Every tab, desktop and phone, with the effects forced to "full" (what the owner sees) and on "auto".
async function perfRun(name, viewport, phone, fx) {
  const ctx = await browser.newContext({ viewport, colorScheme: "dark", deviceScaleFactor: phone ? 2 : 1, isMobile: phone, hasTouch: phone });
  await ctx.addInitScript((v) => {
    try {
      localStorage.setItem("mc-fx", v);
    } catch {
      // storage blocked: the panel's default
    }
  }, fx);
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await page.goto(`${base}/#overview`);
  await page.waitForFunction(() => document.querySelector("#statePill")?.textContent === "Online", null, { timeout: 15000 });
  await page.evaluate(() => document.fonts.ready);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: THROTTLE });
  const out = {};
  for (const tab of TABS) {
    await page.click(`#tab-${tab}`);
    await page.waitForTimeout(tab === "console" ? 2500 : 1200);
    out[tab] = await scrollFrames(page, null);
    if (tab === "console") out["console-log"] = await scrollFrames(page, "#console");
  }
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 1 });
  const fxNow = await page.evaluate(() => document.documentElement.dataset.fx || "");
  await ctx.close();
  const p95s = Object.values(out).map((r) => r.p95).sort((a, b) => a - b);
  const worst = p95s.at(-1);
  console.log(`perf ${name} fx=${fx} (${fxNow || "n/a"}) ${THROTTLE}x CPU: ${Object.entries(out).map(([t, r]) => `${t} p95 ${r.p95}`).join(", ")}`);
  expect(worst < P95_LIMIT_MS, `${name}: scrolling every tab under ${THROTTLE}x CPU throttling stays under p95 ${P95_LIMIT_MS} ms (worst ${worst} ms, fx=${fx})`);
  return { throttle: THROTTLE, fx, fx_applied: fxNow, median_p95: p95s[Math.floor(p95s.length / 2)], worst_p95: worst, tabs: out };
}

const browser = await chromium.launch();
const TABS = ["overview", "console", "players", "experience", "bans", "plugins", "backups", "worlds", "logs", "health", "audit"];
const RUNS = [
  ["desktop", { width: 1440, height: 1000 }, "dark"],
  ["phone", { width: 390, height: 844 }, "dark"],
  ["desktop-light", { width: 1440, height: 1000 }, "light"],
];
try {
  for (const [name, viewport, colorScheme] of RUNS) {
    const ctx = await browser.newContext({ viewport, colorScheme, deviceScaleFactor: name === "phone" || shotDir ? 2 : 1, isMobile: name === "phone", hasTouch: name === "phone" });
    await routeConfirm(ctx);
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
    page.on("console", (m) => {
      if (m.type() === "error" || /Content Security Policy|Refused to/.test(m.text())) errors.push(`console: ${m.text()}`);
    });
    page.on("response", (r) => {
      if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`);
    });
    await page.goto(`${base}/#overview`);
    await page.waitForFunction(() => document.querySelector("#statePill")?.textContent === "Online", null, { timeout: 15000 });
    await page.waitForSelector("#g-tps .big");
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(900);
    expect((await page.textContent("#title")) === "Lighthouse", `${name}: title from the API`);
    expect((await page.locator(".avatars .av").count()) === 4, `${name}: three player avatars + free slots`);
    expect((await page.textContent("#g-tps .big")) === "20.0", `${name}: TPS gauge shows live data`);
    expect(await page.evaluate(() => document.fonts.check("700 16px 'Chakra Petch'")), `${name}: self-hosted fonts loaded`);
    if (shotDir && name !== "desktop-light") await page.screenshot({ path: `${shotDir}/panel-${name}.png`, fullPage: false });
    if (shotDir && name === "desktop-light") await page.screenshot({ path: `${shotDir}/panel-light.png`, fullPage: false });
    for (const tab of TABS) {
      await page.click(`#tab-${tab}`);
      await page.waitForTimeout(tab === "console" ? 2500 : 700);
      // (on a phone an overflowing element also widens the layout viewport: innerWidth then exceeds the device's)
      const over = await page.evaluate((w) => Math.max(document.documentElement.scrollWidth, window.innerWidth) - w, viewport.width);
      expect(over <= 0, `${name}: ${tab} has no horizontal scroll (${over}px over)`);
      const text = await page.textContent("#main");
      expect(!/Loading…/.test(text) && text.length > 80, `${name}: ${tab} rendered`);
      if (SHOTS && SHOTS_DIR && name !== "desktop-light") {
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(400);
        await page.screenshot({ path: `${SHOTS_DIR}/panel-${SHOTS}-${tab}-${name}.png`, fullPage: false, scale: "css" });
      }
    }
    // P13: the Experience tab: verdicts, remedies, history with an annotation, range switch, a player's detail
    await page.click("#tab-experience");
    await page.waitForSelector("#h-xpl");
    const xp = await page.textContent("#main");
    expect(/jonas_r/.test(xp) && /Ping is a bit high/.test(xp), `${name}: experience lists players with their verdict and reason`);
    expect(/Product overhead vs Minecraft/.test(xp) && /mc-agent/.test(xp), `${name}: the overhead card`);
    expect((await page.locator("section[aria-labelledby=\"h-xph\"] .metrics svg line title").count()) > 0, `${name}: history charts carry the deploy annotation`);
    await page.click('button[data-xprange="7d"]');
    await page.waitForFunction(() => document.querySelector('button[data-xprange="7d"]')?.getAttribute("aria-pressed") === "true", null, { timeout: 10000 });
    await page.click('button[data-xpp="5f2a0000-0000-4000-8000-000000002c41"]');
    await page.waitForSelector("#h-xpp", { timeout: 10000 });
    expect(/jonas_r/.test(await page.textContent("#h-xpp")), `${name}: a player's detail opens`);
    const overXp = await page.evaluate((w) => Math.max(document.documentElement.scrollWidth, window.innerWidth) - w, viewport.width);
    expect(overXp <= 0, `${name}: experience detail has no horizontal scroll (${overXp}px over)`);
    if (shotDir && name !== "desktop-light") await page.screenshot({ path: `${shotDir}/panel-experience-${name}.png`, fullPage: true });
    await page.click("#tab-console");
    await page.waitForSelector("#cmdInput");
    await page.fill("#cmdInput", "list");
    await page.press("#cmdInput", "Enter");
    await page.waitForFunction(() => /There are 3 of a max of 20 players online/.test(document.querySelector("#console")?.textContent || ""), null, { timeout: 15000 });
    expect(true, `${name}: console command round-trips through the signed queue`);
    await page.waitForFunction(() => /live line/.test(document.querySelector("#console")?.textContent || ""), null, { timeout: 10000 });
    expect((await page.textContent("#liveBadge")) === "live", `${name}: live console streams`);
    if (shotDir && name === "desktop") await page.screenshot({ path: `${shotDir}/panel-console.png`, fullPage: false });
    // keyboard: arrow keys move between tabs
    await page.focus("#tab-console");
    await page.keyboard.press("ArrowRight");
    expect((await page.getAttribute("#tab-players", "aria-selected")) === "true", `${name}: arrow keys switch tabs`);
    if (name !== "desktop-light") await p10Dialogs(page, name, ctx);
    expect(errors.filter((e) => !/401/.test(e)).length === 0, `${name}: no console errors (${errors.slice(0, 5).join(" | ")})`);
    await ctx.close();
  }
  // v1.2: the client setup page (watchdog/test/client-smoke.mjs): stepper, dark default, desktop + phone
  await clientSmoke(browser, expect, process.env.CLIENT_SHOTS_DIR || shotDir);
  // the effects switch: auto -> full -> light -> auto, persisted per browser
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "dark" });
    const page = await ctx.newPage();
    await page.goto(`${base}/#overview`);
    await page.waitForSelector("#g-tps .big");
    if (await page.$("#fxBtn")) {
      const seen = [];
      for (let i = 0; i < 3; i++) {
        await page.click("#fxBtn");
        seen.push(await page.evaluate(() => [document.documentElement.dataset.fxMode, document.documentElement.dataset.fx, localStorage.getItem("mc-fx")].join("/")));
      }
      expect(seen.join(" ") === "full/full/full light/light/light auto/full/auto", `effects switch cycles and persists (${seen.join(" ")})`);
      await page.click("#fxBtn");
      await page.reload();
      await page.waitForSelector("#g-tps .big");
      expect((await page.evaluate(() => document.documentElement.dataset.fxMode)) === "full", "effects choice survives a reload");
    } else {
      expect(SHOTS === "before", "the header has the effects switch");
    }
    await ctx.close();
  }
  // scroll performance of every tab (the newest sample is kept fresh by freshSample)
  const perf = {};
  for (const [name, viewport, phone] of [["desktop", { width: 1440, height: 1000 }, false], ["phone", { width: 390, height: 844 }, true]]) {
    perf[`${name}-full`] = await perfRun(name, viewport, phone, "full");
    if (SHOTS !== "before") perf[`${name}-auto`] = await perfRun(name, viewport, phone, "auto");
  }
  if (SHOTS && SHOTS_DIR) {
    const { writeFileSync } = await import("node:fs");
    writeFileSync(`${SHOTS_DIR}/panel-perf-${SHOTS}.json`, `${JSON.stringify(perf, null, 2)}\n`);
  }
} finally {
  clearInterval(streaming);
  clearInterval(freshTimer);
  await browser.close();
  server.close();
}
console.log(`${failures.length} failure(s)`);
process.exit(failures.length ? 1 : 0);
