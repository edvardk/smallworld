// v1.1 tests for watchdog/worker.js: the client modpack. One-time codes (minted only for mc-agent's bearer token,
// stored as HMACs, single use, expiring, rate-limited per player, per IP and globally), the session cookie, signed
// download and mirror links, the per-download .mrpack (a valid zip: CRC, offsets, the index's downloads), the
// publish/mirror routes (immutable artifacts, tokens), the setup page's headers and assets, the daily cleanup.
// node --import ./watchdog/test/register.mjs --test watchdog/test/client.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHash } from "node:crypto";
import { inflateRawSync } from "node:zlib";

import worker from "../worker.js";
import { SqliteD1 } from "./d1-sqlite.mjs";
import { install as installStreams } from "./workers-streams.mjs";

installStreams(); // FixedLengthStream, crypto.DigestStream (Workers-only)

const W = worker._internals;
const NOW = Date.UTC(2026, 10, 3, 12, 0, 0);
const HOST = "play.example.com";
const HB = "hb.mc.example.com";
const AGENT = "a".repeat(48);
const PUBLISH = "p".repeat(48);
const UUID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const SHA = "ab".repeat(32);

class FakeR2 {
  constructor() {
    this.objects = new Map();
    this.puts = 0;
    this.options = [];
    this.failPut = null;
  }
  async head(key) {
    const v = this.objects.get(key);
    return v ? { size: v.length } : null;
  }
  async get(key) {
    const v = this.objects.get(key);
    if (!v) return null;
    return { size: v.length, body: v, text: async () => new TextDecoder().decode(v), arrayBuffer: async () => v.buffer };
  }
  async put(key, body, options = {}) {
    this.puts++;
    this.options.push(options);
    if (this.failPut) throw new Error(this.failPut);
    const data = typeof body === "string" ? new TextEncoder().encode(body) : new Uint8Array(await new Response(body).arrayBuffer());
    this.objects.set(key, data);
  }
  async delete(key) {
    this.objects.delete(key);
  }
}

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

// A streamed upload (duplex "half"), like a client still sending: `cancelled` says whether the Worker dropped it.
function streamingPut(env, path, data, headers) {
  const state = { cancelled: false, pulled: 0 };
  const body = new ReadableStream({
    pull(ctl) {
      if (state.pulled >= data.length) return ctl.close();
      ctl.enqueue(data.subarray(state.pulled, state.pulled + 4));
      state.pulled += 4;
    },
    cancel() {
      state.cancelled = true;
    },
  });
  const r = new Request(`https://${HB}${path}`, { method: "PUT", body, duplex: "half", headers: { "Content-Type": "application/octet-stream", "Content-Length": String(data.length), ...headers } });
  return W.handle(r, env, NOW, {}).then(async (resp) => ({ status: resp.status, body: await resp.json(), state }));
}

function plan() {
  const enc = (s) => Buffer.from(s).toString("base64");
  return {
    tier: "medium",
    title: "Medium",
    filename: "Lighthouse-Medium.mrpack",
    name: "Lighthouse (Medium)",
    summary: "A typical laptop or desktop.",
    version_id: "26.3-abcdef012345",
    dependencies: { minecraft: "26.3", "fabric-loader": "0.20.1" },
    files: [
      { path: "mods/sodium.jar", hashes: { sha1: "1".repeat(40), sha512: "2".repeat(128) }, env: { client: "required", server: "unsupported" }, fileSize: 5, upstream: "https://cdn.modrinth.com/data/x/sodium.jar", mirror: SHA, filename: "sodium.jar" },
      { path: "shaderpacks/shader.zip", hashes: { sha1: "3".repeat(40), sha512: "4".repeat(128) }, env: { client: "required", server: "unsupported" }, fileSize: 7, upstream: "https://cdn.modrinth.com/data/y/shader.zip", mirror: null, filename: "shader.zip" },
    ],
    overrides: [
      { path: "client-overrides/options.txt", data: enc("version:5023\nrenderDistance:12\n") },
      { path: "client-overrides/servers.dat", data: enc("\n\u0000\u0000") },
    ],
  };
}

function makeEnv() {
  const env = {
    DB: new SqliteD1(),
    ARTIFACTS: new FakeR2(),
    CLIENT: JSON.stringify({ host: HOST, title: "Lighthouse" }),
    CLIENT_KEY: "k".repeat(64),
    PUBLISH_TOKEN: PUBLISH,
    AGENT_TOKEN: AGENT,
    MONITORING: "{}",
    CONTROL: {},
    LOGS: {},
    ASSETS: { fetch: async (r) => (new URL(r.url).pathname === "/client/index.html" || new URL(r.url).pathname === "/fonts/inter-400.woff2" ? new Response("<!doctype html>page", { headers: { "Content-Type": "text/html" } }) : new Response("nope", { status: 404 })) },
  };
  env.ARTIFACTS.objects.set("client/medium.json", new TextEncoder().encode(JSON.stringify(plan())));
  env.ARTIFACTS.objects.set("client/index.json", new TextEncoder().encode(JSON.stringify({ server: "Lighthouse", tiers: [{ id: "medium", title: "Medium", filename: "Lighthouse-Medium.mrpack" }] })));
  env.ARTIFACTS.objects.set(`artifacts/${SHA}/sodium.jar`, new Uint8Array([1, 2, 3, 4, 5]));
  return env;
}

async function call(env, path, { host = HOST, method = "GET", body, headers = {}, now = NOW } = {}) {
  const h = { ...headers };
  if (body !== undefined) h["Content-Type"] ??= "application/json";
  const r = new Request(`https://${host}${path}`, { method, headers: h, body: body === undefined ? undefined : typeof body === "string" || body instanceof Uint8Array ? body : JSON.stringify(body) });
  const resp = await W.handle(r, env, now, {});
  const type = resp.headers.get("Content-Type") || "";
  return { status: resp.status, headers: resp.headers, body: type.includes("json") ? await resp.json() : new Uint8Array(await resp.arrayBuffer()) };
}

const same = { Origin: `https://${HOST}`, "Sec-Fetch-Site": "same-origin", "CF-Connecting-IP": "203.0.113.7" };

async function mint(env, now = NOW) {
  return call(env, "/agent/client-code", { host: HB, method: "POST", body: { uuid: UUID, name: "Steve" }, headers: { Authorization: `Bearer ${AGENT}` }, now });
}

async function login(env) {
  const { body } = await mint(env);
  const r = await call(env, "/api/code", { method: "POST", body: { code: body.code }, headers: same });
  assert.equal(r.status, 200);
  const cookie = r.headers.get("Set-Cookie").split(";")[0];
  return cookie;
}

// a minimal zip reader: checks every local header against the central directory and the CRC (deflate never used)
function unzip(buf) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const endAt = buf.length - 22;
  assert.equal(dv.getUint32(endAt, true), 0x06054b50);
  const count = dv.getUint16(endAt + 10, true);
  let at = dv.getUint32(endAt + 16, true);
  const out = new Map();
  for (let i = 0; i < count; i++) {
    assert.equal(dv.getUint32(at, true), 0x02014b50);
    const crc = dv.getUint32(at + 16, true);
    const size = dv.getUint32(at + 24, true);
    const nameLen = dv.getUint16(at + 28, true);
    const local = dv.getUint32(at + 42, true);
    const name = new TextDecoder().decode(buf.subarray(at + 46, at + 46 + nameLen));
    assert.equal(dv.getUint32(local, true), 0x04034b50);
    assert.equal(dv.getUint16(local + 8, true), 0, "stored");
    const data = buf.subarray(local + 30 + nameLen, local + 30 + nameLen + size);
    assert.equal(W.crc32(data), crc, name);
    out.set(name, data);
    at += 46 + nameLen;
  }
  return out;
}

test("codes are minted only for the agent's token, rate-limited per player, never stored in clear", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const noAuth = await call(env, "/agent/client-code", { host: HB, method: "POST", body: { uuid: UUID, name: "Steve" } });
  assert.equal(noAuth.status, 401);
  const bad = await call(env, "/agent/client-code", { host: HB, method: "POST", body: { uuid: "x", name: "Steve" }, headers: { Authorization: `Bearer ${AGENT}` } });
  assert.equal(bad.status, 400);
  const ok = await mint(env);
  assert.equal(ok.status, 200);
  assert.match(ok.body.code, /^[0-9]{6}$/);
  assert.equal(ok.body.expires_ms, NOW + 15 * 60000);
  const rows = (await env.DB.prepare("SELECT * FROM client_codes").all()).results;
  assert.equal(rows.length, 1);
  assert.ok(!JSON.stringify(rows).includes(ok.body.code), "only the HMAC is stored");
  for (let i = 0; i < 4; i++) assert.equal((await mint(env)).status, 200);
  assert.equal((await mint(env)).status, 429, "codes_per_player_hour");
  assert.equal((await mint(env, NOW + 3600001)).status, 200, "a new hour");
  // off without the CLIENT binding
  const off = makeEnv();
  delete off.CLIENT;
  assert.equal((await mint(off)).status, 404);
});

test("/status says whether the client page is set up (a flag only: never the host or a key)", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const on = await call(env, "/status", { host: HB });
  assert.equal(on.status, 200);
  assert.equal(on.body.client, true);
  const text = JSON.stringify(on.body);
  assert.ok(!text.includes(HOST) && !text.includes(env.CLIENT_KEY), text);
  // v1.1 rollout: the deploy ran but `mcctl apply` didn't, so the Worker has no CLIENT / CLIENT_KEY
  for (const missing of ["CLIENT", "CLIENT_KEY"]) {
    const off = makeEnv();
    delete off[missing];
    assert.equal((await call(off, "/status", { host: HB })).body.client, false, missing);
  }
});

test("a code opens one session, once; wrong, reused and expired codes don't; cross-site posts are refused", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const { body } = await mint(env);
  assert.equal((await call(env, "/api/code", { method: "POST", body: { code: body.code }, headers: { ...same, Origin: "https://evil.example" } })).status, 403);
  const wrong = await call(env, "/api/code", { method: "POST", body: { code: body.code === "000000" ? "000001" : "000000" }, headers: same });
  assert.equal(wrong.status, 401);
  const r = await call(env, "/api/code", { method: "POST", body: { code: `${body.code.slice(0, 3)} ${body.code.slice(3)}` }, headers: same });
  assert.equal(r.status, 200);
  assert.equal(r.body.name, "Steve");
  const cookie = r.headers.get("Set-Cookie");
  assert.match(cookie, /^wc_session=[A-Za-z0-9_-]+\.[0-9a-f]{32}; Max-Age=3600; Path=\/; HttpOnly; Secure; SameSite=Lax$/);
  assert.match(r.headers.get("Content-Security-Policy"), /default-src 'none'/);
  assert.equal((await call(env, "/api/code", { method: "POST", body: { code: body.code }, headers: same })).status, 401, "single use");
  const late = await mint(env);
  assert.equal((await call(env, "/api/code", { method: "POST", body: { code: late.body.code }, headers: same, now: NOW + 15 * 60000 + 1 })).status, 401, "expired");
  const audit = (await env.DB.prepare("SELECT uuid, name, kind FROM client_audit").all()).results.map((r) => ({ ...r }));
  assert.deepEqual(audit, [{ uuid: UUID, name: "Steve", kind: "code" }]);
});

test("attempts are limited per IP (by HMAC, never the address) and globally", async () => {
  W.resetIsolate();
  const env = makeEnv();
  for (let i = 0; i < 10; i++) assert.equal((await call(env, "/api/code", { method: "POST", body: { code: "123456" }, headers: same })).status, 401);
  assert.equal((await call(env, "/api/code", { method: "POST", body: { code: "123456" }, headers: same })).status, 429);
  const keys = (await env.DB.prepare("SELECT key FROM client_rate").all()).results.map((r) => r.key);
  assert.ok(!keys.some((k) => k.includes("203.0.113.7")));
  assert.equal((await call(env, "/api/code", { method: "POST", body: { code: "123456" }, headers: same, now: NOW + 600001 })).status, 401, "a new window");
  // global: past failures_per_hour, everyone waits
  W.resetIsolate();
  const env2 = makeEnv();
  const cfg = JSON.parse(env2.CLIENT);
  env2.CLIENT = JSON.stringify({ ...cfg, failures_per_hour: 3 });
  for (let i = 0; i < 3; i++) {
    await call(env2, "/api/code", { method: "POST", body: { code: "123456" }, headers: { ...same, "CF-Connecting-IP": `198.51.100.${i}` } });
  }
  const { body } = await mint(env2);
  assert.equal((await call(env2, "/api/code", { method: "POST", body: { code: body.code }, headers: { ...same, "CF-Connecting-IP": "198.51.100.99" } })).status, 429);
});

test("the session lists the tiers; a signed link downloads a valid pack whose mods come from the mirror first", async () => {
  W.resetIsolate();
  const env = makeEnv();
  assert.equal((await call(env, "/api/session")).status, 401);
  const cookie = await login(env);
  const s = await call(env, "/api/session", { headers: { Cookie: cookie } });
  assert.equal(s.status, 200);
  assert.equal(s.body.name, "Steve");
  assert.equal(s.body.tiers[0].id, "medium");
  // a tampered cookie
  assert.equal((await call(env, "/api/session", { headers: { Cookie: cookie.replace(/.$/, (c) => (c === "0" ? "1" : "0")) } })).status, 401);
  assert.equal((await call(env, "/api/session", { headers: { Cookie: cookie }, now: NOW + 3600001 })).status, 401, "expired session");
  assert.equal((await call(env, "/api/link", { method: "POST", body: { tier: "medium" }, headers: { Cookie: cookie, ...same, Origin: "https://evil.example" } })).status, 403);
  assert.equal((await call(env, "/api/link", { method: "POST", body: { tier: "ultra" }, headers: { Cookie: cookie, ...same } })).status, 400);
  const link = await call(env, "/api/link", { method: "POST", body: { tier: "medium" }, headers: { Cookie: cookie, ...same } });
  assert.equal(link.status, 200);
  assert.match(link.body.url, /^\/dl\/medium\/\d+\/[0-9a-f]{32}\/Lighthouse-Medium\.mrpack\?u=/);
  const dl = await call(env, link.body.url);
  assert.equal(dl.status, 200);
  assert.equal(dl.headers.get("Content-Disposition"), 'attachment; filename="Lighthouse-Medium.mrpack"');
  const files = unzip(dl.body);
  assert.deepEqual([...files.keys()], ["modrinth.index.json", "client-overrides/options.txt", "client-overrides/servers.dat"]);
  const index = JSON.parse(new TextDecoder().decode(files.get("modrinth.index.json")));
  assert.equal(index.formatVersion, 1);
  assert.deepEqual(index.dependencies, { minecraft: "26.3", "fabric-loader": "0.20.1" });
  const [mod, shader] = index.files;
  assert.equal(mod.downloads.length, 2);
  assert.match(mod.downloads[0], new RegExp(`^https://${HOST}/m/\\d+/[0-9a-f]{32}/${SHA}/sodium\\.jar$`));
  assert.equal(mod.downloads[1], "https://cdn.modrinth.com/data/x/sodium.jar");
  assert.deepEqual(shader.downloads, ["https://cdn.modrinth.com/data/y/shader.zip"], "not mirrored: the official URL only");
  // the mirror link serves the artifact; tampered or expired links don't
  const path = new URL(mod.downloads[0]).pathname;
  const got = await call(env, path);
  assert.equal(got.status, 200);
  assert.deepEqual([...got.body], [1, 2, 3, 4, 5]);
  assert.equal((await call(env, path.replace(SHA, "cd".repeat(32)))).status, 410);
  assert.equal((await call(env, path, { now: NOW + 6 * 3600000 + 1000 })).status, 410);
  // the link itself expires; a link for another player's name doesn't verify
  assert.equal((await call(env, link.body.url, { now: NOW + 10 * 60000 + 1000 })).status, 410);
  assert.equal((await call(env, link.body.url.replace("n=Steve", "n=Alex"))).status, 410);
  const audit = (await env.DB.prepare("SELECT kind, tier FROM client_audit ORDER BY ts").all()).results;
  assert.deepEqual(audit.map((a) => `${a.kind}:${a.tier}`), ["code:null", "download:medium"]);
});

test("v1.2: the official launcher's zip: a signed link to the stored artifact the index names, audited", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const cookie = await login(env);
  const ask = () => call(env, "/api/link", { method: "POST", body: { tier: "medium", kind: "launcher" }, headers: { Cookie: cookie, ...same } });
  const early = await ask();
  assert.equal(early.status, 404, "no zip published yet: the page offers the .mrpack");
  assert.match(early.body.error, /isn't ready yet/);
  const zip = new Uint8Array([0x50, 0x4b, 5, 6, 7]);
  const zsha = sha256(zip);
  env.ARTIFACTS.objects.set(`artifacts/${zsha}/Lighthouse-Medium.zip`, zip);
  const tiers = [{ id: "medium", title: "Medium", filename: "Lighthouse-Medium.mrpack", launcher: { dir: "smallworld-medium", filename: "Lighthouse-Medium.zip", sha256: zsha, size_mb: 0 } }];
  env.ARTIFACTS.objects.set("client/index.json", new TextEncoder().encode(JSON.stringify({ server: "Lighthouse", tiers })));
  const link = await ask();
  assert.equal(link.status, 200);
  assert.match(link.body.url, /^\/dl\/medium\/\d+\/[0-9a-f]{32}\/Lighthouse-Medium\.zip\?u=/);
  const dl = await call(env, link.body.url);
  assert.equal(dl.status, 200);
  assert.equal(dl.headers.get("Content-Type"), "application/zip");
  assert.equal(dl.headers.get("Content-Disposition"), 'attachment; filename="Lighthouse-Medium.zip"');
  assert.deepEqual([...dl.body], [...zip]);
  assert.match(dl.headers.get("Content-Security-Policy"), /default-src 'none'/);
  // the .mrpack's signature doesn't open the zip (and the other way round), and links expire
  const mrpack = await call(env, "/api/link", { method: "POST", body: { tier: "medium" }, headers: { Cookie: cookie, ...same } });
  assert.equal((await call(env, mrpack.body.url.replace("Lighthouse-Medium.mrpack", "Lighthouse-Medium.zip"))).status, 410);
  assert.equal((await call(env, link.body.url.replace("Lighthouse-Medium.zip", "Lighthouse-Medium.mrpack"))).status, 410);
  assert.equal((await call(env, link.body.url, { now: NOW + 10 * 60000 + 1000 })).status, 410);
  const audit = (await env.DB.prepare("SELECT kind, tier FROM client_audit ORDER BY ts").all()).results;
  assert.deepEqual(audit.map((a) => `${a.kind}:${a.tier}`), ["code:null", "launcher:medium"]);
  // client-publish reads the index back with the publish token only
  assert.equal((await call(env, "/publish/client/index.json", { host: HB })).status, 401);
  assert.equal((await call(env, "/publish/client/index.json", { host: HB, headers: { Authorization: `Bearer ${AGENT}` } })).status, 401);
  const back = await call(env, "/publish/client/index.json", { host: HB, headers: { Authorization: `Bearer ${PUBLISH}` } });
  assert.equal(back.status, 200);
  assert.equal(back.body.tiers[0].launcher.sha256, zsha);
  assert.equal((await call(env, "/publish/client/medium.json", { host: HB, headers: { Authorization: `Bearer ${PUBLISH}` } })).status, 405);
});

test("packZip writes stored entries that a zip reader accepts, in a fixed order", async () => {
  const zip = await W.packZip(plan(), async (sha, f) => `https://mirror.test/${sha}/${f}`);
  const files = unzip(zip);
  assert.equal(new TextDecoder().decode(files.get("client-overrides/options.txt")), "version:5023\nrenderDistance:12\n");
  assert.equal(W.crc32(new TextEncoder().encode("123456789")), 0xcbf43926); // the standard check value
  assert.equal(typeof inflateRawSync, "function");
});

test("v1.2: an in-house file (no upstream URL) is downloaded from the mirror only", async () => {
  const p = plan();
  p.files.push({ ...p.files[0], path: "mods/smallworld-1.0.0.jar", upstream: "", filename: "smallworld-1.0.0.jar" });
  const index = JSON.parse(new TextDecoder().decode(unzip(await W.packZip(p, async (sha, f) => `https://mirror.test/${sha}/${f}`)).get("modrinth.index.json")));
  const mod = index.files.find((f) => f.path === "mods/smallworld-1.0.0.jar");
  assert.deepEqual(mod.downloads, [`https://mirror.test/${SHA}/smallworld-1.0.0.jar`]);
});

test("publish: tokens, immutable artifacts, plans; the server's downloads need the agent token", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const key = sha256(new Uint8Array([9, 9, 9]));
  const put = (headers, body = new Uint8Array([9, 9, 9])) => call(env, `/publish/artifacts/${key}/lithium.jar`, { host: HB, method: "PUT", body, headers: { "Content-Type": "application/octet-stream", "Content-Length": String(body.length), ...headers } });
  assert.equal((await put({})).status, 401);
  assert.equal((await put({ Authorization: `Bearer ${AGENT}` })).status, 401, "the agent token can't publish");
  const first = await put({ Authorization: `Bearer ${PUBLISH}` });
  assert.deepEqual(first.body, { ok: true, existed: false });
  const again = await put({ Authorization: `Bearer ${PUBLISH}` }, new Uint8Array([6, 6]));
  assert.deepEqual(again.body, { ok: true, existed: true });
  assert.deepEqual([...env.ARTIFACTS.objects.get(`artifacts/${key}/lithium.jar`)], [9, 9, 9], "never overwritten");
  assert.equal((await call(env, `/publish/artifacts/${key}/lithium.jar`, { host: HB, method: "HEAD", headers: { Authorization: `Bearer ${PUBLISH}` } })).status, 200);
  assert.equal((await call(env, `/publish/artifacts/${key}/..%2Fx`, { host: HB, method: "PUT", body: "x", headers: { Authorization: `Bearer ${PUBLISH}` } })).status, 400);
  const p = await call(env, "/publish/client/high.json", { host: HB, method: "PUT", body: { tier: "high" }, headers: { Authorization: `Bearer ${PUBLISH}` } });
  assert.equal(p.status, 200);
  assert.equal((await call(env, "/publish/client/other.json", { host: HB, method: "PUT", body: {}, headers: { Authorization: `Bearer ${PUBLISH}` } })).status, 404);
  assert.equal((await call(env, `/artifacts/${key}/lithium.jar`, { host: HB })).status, 401);
  const dl = await call(env, `/artifacts/${key}/lithium.jar`, { host: HB, headers: { Authorization: `Bearer ${AGENT}` } });
  assert.equal(dl.status, 200);
  assert.deepEqual([...dl.body], [9, 9, 9]);
  assert.equal((await call(env, `/artifacts/${"00".repeat(32)}/x.jar`, { host: HB, headers: { Authorization: `Bearer ${AGENT}` } })).status, 404);
});

test("artifact uploads: refusals drop the body, a wrong sha256 isn't kept, a storage failure is a JSON 5xx", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const data = new Uint8Array(64).map((_, i) => i);
  const key = sha256(data);
  const name = "restic_0.18.1_linux_amd64.bz2";
  const path = `/publish/artifacts/${key}/${name}`;
  const auth = { Authorization: `Bearer ${PUBLISH}` };

  const denied = await streamingPut(env, path, data, { Authorization: `Bearer ${AGENT}` });
  assert.equal(denied.status, 401);
  assert.ok(denied.state.cancelled, "401: the body is cancelled, not left for the client to time out on");
  assert.ok(!JSON.stringify(denied.body).includes(AGENT), "the token is never echoed");

  const huge = await streamingPut(env, path, data, { ...auth, "Content-Length": String(101 * 1048576) });
  assert.equal(huge.status, 413);
  assert.ok(huge.state.cancelled, "413: the body is cancelled");
  const none = await streamingPut(env, path, data, { ...auth, "Content-Length": "" });
  assert.equal(none.status, 411);
  assert.ok(none.state.cancelled);
  const bad = await streamingPut(env, `/publish/artifacts/${key}/..%2Fx`, data, auth);
  assert.equal(bad.status, 400);
  assert.ok(bad.state.cancelled);

  env.ARTIFACTS.failPut = "R2 internal error";
  const failed = await streamingPut(env, path, data, auth);
  assert.equal(failed.status, 502);
  assert.match(failed.body.error, /storing failed: R2 internal error/);
  assert.ok(!env.ARTIFACTS.objects.has(`artifacts/${key}/${name}`));
  env.ARTIFACTS.failPut = null;

  const zero = "0".repeat(64);
  const other = await streamingPut(env, `/publish/artifacts/${zero}/x.jar`, data, auth);
  assert.equal(other.status, 422, "the body's sha256 isn't the path's");
  assert.match(other.body.error, /sha256 mismatch/);
  assert.ok(!env.ARTIFACTS.objects.has(`artifacts/${zero}/x.jar`), "a mismatch isn't kept");

  const short = await streamingPut(env, path, data, { ...auth, "Content-Length": String(data.length + 8) });
  assert.equal(short.status, 502, "fewer bytes than Content-Length: FixedLengthStream fails the put");
  assert.ok(!env.ARTIFACTS.objects.has(`artifacts/${key}/${name}`));

  const ok = await streamingPut(env, path, data, auth);
  assert.equal(ok.status, 200);
  assert.deepEqual(ok.body, { ok: true, existed: false });
  assert.equal(env.ARTIFACTS.options.at(-1).sha256, key, "R2 checks the same sha256");
  assert.deepEqual([...env.ARTIFACTS.objects.get(`artifacts/${key}/${name}`)], [...data]);

  const existed = await streamingPut(env, path, data, auth);
  assert.deepEqual(existed.body, { ok: true, existed: true });
  assert.ok(existed.state.cancelled, "existed: the body is cancelled");
});

test("the setup page: only its own files, CSP 'self', no-store HTML; other hosts don't serve it", async () => {
  W.resetIsolate();
  const env = makeEnv();
  const page = await call(env, "/");
  assert.equal(page.status, 200);
  assert.equal(page.headers.get("Cache-Control"), "no-store");
  assert.match(page.headers.get("Content-Security-Policy"), /script-src 'self'/);
  assert.equal(page.headers.get("X-Frame-Options"), "DENY");
  assert.equal((await call(env, "/fonts/inter-400.woff2")).status, 200);
  for (const p of ["/panel.js", "/index.html.bak", "/client/../panel.js", "/api/overview"]) {
    assert.equal((await call(env, p)).status, 404, p);
  }
  assert.equal((await call(env, "/", { host: HB })).status, 404);
});

test("the daily cleanup removes spent codes, old counters and old audit rows once a day", async () => {
  W.resetIsolate();
  const env = makeEnv();
  await mint(env);
  await call(env, "/api/code", { method: "POST", body: { code: "123456" }, headers: same });
  const day = Math.floor(NOW / 86400000) * 86400000 + 2 * 86400000; // midnight UTC two days later
  await W.maintainClient(env, day + 300000); // not the day's first run: nothing
  assert.equal((await env.DB.prepare("SELECT COUNT(*) AS n FROM client_codes").first()).n, 1);
  await W.maintainClient(env, day);
  assert.equal((await env.DB.prepare("SELECT COUNT(*) AS n FROM client_codes").first()).n, 0);
  assert.equal((await env.DB.prepare("SELECT COUNT(*) AS n FROM client_rate").first()).n, 0);
});

test("theme.css carries the server's colours (hex only), never the text colour", async () => {
  W.resetIsolate();
  const env = makeEnv();
  env.CLIENT = JSON.stringify({ host: HOST, title: "Server", colors: { text: "#FFFFFF", accent: "#B03A5B", secondary: "red;}", subtle: "#D9E8EE" } });
  const r = await call(env, "/theme.css");
  assert.equal(r.status, 200);
  const css = new TextDecoder().decode(r.body);
  assert.equal(css, ":root { --accent: #B03A5B; --subtle: #D9E8EE; }\n");
});
