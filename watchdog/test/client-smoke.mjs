// v1.2 UI smoke test of the client setup page (watchdog/panel/client/), run by ui.smoke.mjs: the real page served
// by the real Worker for the client host (Playwright routes https://play.example.test to it), fixture data only
// (a published index with four tiers, launcher zips stored). Desktop and phone: dark by default, the light toggle
// persists, the stepper switches (clicks, Next, arrow keys), the OS choice changes the install steps, the tier
// choice changes the memory and the extra-mods list, the zip link downloads, no console errors (CSP included), no
// horizontal scroll. With a screenshot directory: client-setup-{desktop,phone}[-step1|-light].png (scale 2).
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

import worker from "../worker.js";
import { SqliteD1 } from "./d1-sqlite.mjs";
import { makePanelEnv } from "./panel-fixtures.mjs";

const HOST = "play.example.test";
const HB = "hb.example.test";
const AGENT = "a".repeat(48);

class MemR2 {
  constructor() {
    this.objects = new Map();
  }
  async head(key) {
    const v = this.objects.get(key);
    return v ? { size: v.length } : null;
  }
  async get(key) {
    const v = this.objects.get(key);
    return v ? { size: v.length, body: v, text: async () => new TextDecoder().decode(v) } : null;
  }
  async put(key, body) {
    this.objects.set(key, typeof body === "string" ? new TextEncoder().encode(body) : new Uint8Array(await new Response(body).arrayBuffer()));
  }
  async delete(key) {
    this.objects.delete(key);
  }
}

const TIERS = [
  ["low", "Low", "Older laptops and computers without a gaming graphics card.", 4, false],
  ["medium", "Medium", "A typical laptop or desktop. Pick this if you're not sure.", 6, true],
  ["high", "High", "A gaming PC. Adds Distant Horizons and Iris.", 6, false],
  ["maximum", "Maximum", "A powerful graphics card. Far terrain, shaders on.", 8, false],
];

function fixtureIndex(zip) {
  const sha = createHash("sha256").update(zip).digest("hex");
  const map = (folder, name, optional) => ({ name, filename: `${name.toLowerCase().replace(/[^a-z]+/g, "-")}.jar`, folder, url: `https://cdn.example.test/${encodeURIComponent(name)}.jar`, sha512: "0".repeat(128), optional });
  return {
    server: "Smallworld",
    minecraft_version: "26.3",
    tiers: TIERS.map(([id, title, description, memory, recommended]) => ({
      id, title, description, example: "e.g. a recent laptop", recommended, size_mb: 30.5, memory_gb: memory,
      includes: ["Fabric API", "Sodium", "Xaero's Minimap", "Chat Heads", "Smallworld"],
      filename: `Smallworld-${title}.mrpack`, version_id: "26.3-0123456789ab",
      launcher: { dir: `smallworld-${id}`, filename: `Smallworld-${title}.zip`, profile_id: "fabric-loader-0.20.1-26.3", sha256: sha, size_mb: 24.1 },
      missing: [map("mods", "Xaero's Minimap", true), map("mods", "Xaero's World Map", true), ...(id === "maximum" ? [map("shaderpacks", "Complementary Shaders - Unbound", false)] : [])],
    })),
    pending: ["emotecraft"],
    shader_credit: "Shaders: Complementary Shaders - Unbound, downloaded from Modrinth, unmodified.",
    shader_links: [],
    modrinth_app_url: "https://modrinth.example.test/app",
  };
}

export async function clientSmoke(browser, expect, shotDir) {
  const zip = new Uint8Array([0x50, 0x4b, 0x05, 0x06, ...new Array(18).fill(0)]);
  const env = {
    ...makePanelEnv(),
    DB: new SqliteD1(),
    ARTIFACTS: new MemR2(),
    CLIENT: JSON.stringify({ host: HOST, title: "Smallworld" }),
    CLIENT_KEY: "k".repeat(64),
    PUBLISH_TOKEN: "p".repeat(48),
    AGENT_TOKEN: AGENT,
  };
  const index = fixtureIndex(zip);
  await env.ARTIFACTS.put("client/index.json", JSON.stringify(index));
  await env.ARTIFACTS.put(`artifacts/${index.tiers[0].launcher.sha256}/Smallworld-Medium.zip`, zip);
  const handle = (req) => worker._internals.handle(req, env, Date.now(), {});
  const mint = async () => {
    const r = await handle(new Request(`https://${HB}/agent/client-code`, { method: "POST", headers: { Authorization: `Bearer ${AGENT}`, "Content-Type": "application/json" }, body: JSON.stringify({ uuid: "0f8fad5b-d9cb-469f-a165-70867728950e", name: "Steve" }) }));
    return (await r.json()).code;
  };
  const runs = [
    ["desktop", { width: 1280, height: 900 }, false],
    ["phone", { width: 390, height: 844 }, true],
  ];
  for (const [name, viewport, phone] of runs) {
    // the light colour scheme on purpose: the page stays dark until the visitor picks light
    const ctx = await browser.newContext({ viewport, colorScheme: "light", deviceScaleFactor: 2, isMobile: phone, hasTouch: phone, userAgent: phone ? undefined : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36" });
    await ctx.route(`https://${HOST}/**`, async (route) => {
      const r = route.request();
      const headers = new Headers();
      for (const [k, v] of Object.entries(r.headers())) if (typeof v === "string" && k !== "host") headers.set(k, v);
      headers.set("CF-Connecting-IP", "203.0.113.9");
      const body = r.postDataBuffer();
      const resp = await handle(new Request(r.url(), { method: r.method(), headers, body: ["GET", "HEAD"].includes(r.method()) ? undefined : body }));
      await route.fulfill({ status: resp.status, headers: Object.fromEntries(resp.headers), body: Buffer.from(await resp.arrayBuffer()) });
    });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
    page.on("console", (m) => {
      if (m.type() === "error" || /Content Security Policy|Refused to/.test(m.text())) errors.push(`console: ${m.text()}`);
    });
    await page.goto(`https://${HOST}/?c=${await mint()}`);
    await page.waitForSelector("#setup:not([hidden])", { timeout: 15000 });
    const over = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect((await page.evaluate(() => document.documentElement.dataset.theme)) === "dark", `client ${name}: dark by default`);
    expect(!/c=/.test(page.url()), `client ${name}: the code left the address bar`);
    expect(await page.isVisible("#step-os") && !(await page.isVisible("#step-install")), `client ${name}: step 1 shown alone`);
    if (!phone) expect((await page.getAttribute('[data-os="windows"]', "aria-checked")) === "true", `client ${name}: Windows detected`);
    if (shotDir) await page.screenshot({ path: `${shotDir}/client-setup-${name}-step1.png` });
    // step 1 -> 2 with Next; the recommended tier is preselected; pick Maximum
    await page.click('#step-os [data-go="tier"]');
    expect(await page.isVisible("#step-tier"), `client ${name}: Next opens step 2`);
    expect((await page.getAttribute('[data-tier="medium"]', "aria-checked")) === "true", `client ${name}: the recommended tier is preselected`);
    await page.click('[data-tier="maximum"]');
    // step 3 by its tab: Linux chosen in step 1 changes the steps
    await page.click("#tab-os");
    await page.click('[data-os="linux"]');
    await page.click("#tab-install");
    expect((await page.textContent("#osPath")) === "~/.minecraft", `client ${name}: Linux shows ~/.minecraft`);
    expect((await page.textContent("#installTitle")) === "Install on Linux", `client ${name}: the install step names the OS`);
    expect((await page.textContent("#xmx")) === "-Xmx8G", `client ${name}: Maximum needs -Xmx8G`);
    expect((await page.locator("#extras li").count()) === 3, `client ${name}: Maximum lists three files to add`);
    expect(/smallworld-maximum\/shaderpacks/.test(await page.textContent("#extras")), `client ${name}: the shader goes into shaderpacks`);
    expect(!(await page.isVisible("#dlBtn")), `client ${name}: the .mrpack hides behind its disclosure`);
    await page.click("#altFold summary");
    expect(await page.isVisible("#dlBtn"), `client ${name}: the disclosure shows the .mrpack`);
    // back to Windows and Medium, then the zip downloads through its signed link
    await page.click("#tab-os");
    await page.click('[data-os="windows"]');
    await page.focus("#tab-os");
    await page.keyboard.press("ArrowRight");
    expect((await page.getAttribute("#tab-tier", "aria-selected")) === "true", `client ${name}: arrow keys move between steps`);
    await page.click('[data-tier="medium"]');
    await page.click("#tab-install");
    expect((await page.textContent("#osPath")) === "%appdata%\\.minecraft", `client ${name}: Windows shows %appdata%\\.minecraft`);
    expect((await page.textContent("#gameDir")) === "smallworld-medium" && (await page.textContent("#profileId")) === "fabric-loader-0.20.1-26.3", `client ${name}: the version and game directory are named`);
    const dl = page.waitForEvent("download");
    await page.click("#zipBtn");
    const file = await dl;
    expect(file.suggestedFilename() === "Smallworld-Medium.zip", `client ${name}: the zip downloads (${file.suggestedFilename()})`);
    expect((await over()) <= 0, `client ${name}: no horizontal scroll on the install step (${await over()}px)`);
    if (shotDir) await page.screenshot({ path: `${shotDir}/client-setup-${name}.png`, fullPage: true });
    await page.click("#tab-play");
    expect((await page.locator("#step-play details").count()) >= 6, `client ${name}: troubleshooting sections`);
    expect((await over()) <= 0, `client ${name}: no horizontal scroll on the play step`);
    // the light theme: toggled, remembered across a reload
    await page.click("#themeBtn");
    expect((await page.evaluate(() => document.documentElement.dataset.theme)) === "light", `client ${name}: the toggle switches to light`);
    await page.reload();
    await page.waitForSelector("#setup:not([hidden])", { timeout: 15000 });
    expect((await page.evaluate(() => document.documentElement.dataset.theme)) === "light", `client ${name}: light is remembered`);
    if (shotDir) await page.screenshot({ path: `${shotDir}/client-setup-${name}-light.png` });
    expect(errors.length === 0, `client ${name}: no console errors (${errors.slice(0, 3).join(" | ")})`);
    await ctx.close();
  }
}

// Alone: node --import ./watchdog/test/register.mjs watchdog/test/client-smoke.mjs [screenshot-dir]
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { chromium } = createRequire(import.meta.url)("playwright");
  const browser = await chromium.launch();
  const failures = [];
  try {
    await clientSmoke(browser, (ok, what) => {
      console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
      if (!ok) failures.push(what);
    }, process.argv[2] || "");
  } finally {
    await browser.close();
  }
  console.log(`${failures.length} failure(s)`);
  process.exit(failures.length ? 1 : 0);
}
