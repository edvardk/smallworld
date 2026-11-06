// P13 e2e: the real watchdog/worker.js as mc-agent's Worker on http://127.0.0.1:8787 (the e2e-mock runner; the
// container shares its network). D1 = node:sqlite (DB + TELEMETRY), R2 = memory, the Control object on memory
// storage. The agent has no WebSocket here (the upgrade is refused) and polls for signed commands. Test-only
// routes under /__e2e/ (loopback only, no auth): state for the checks, an outage switch, and enqueueing a
// signed command the way the panel would.
//
//   MC_SECRET_AGENT_TOKEN=... MC_SECRET_COMMAND_HMAC_KEY=... node --import ./watchdog/test/register.mjs \
//     watchdog/test/e2e-worker.mjs [port]
import http from "node:http";
import { webcrypto } from "node:crypto";

import worker from "../worker.js";
import { makePanelEnv } from "./panel-fixtures.mjs";
import { SqliteD1 } from "./d1-sqlite.mjs";

const port = Number(process.argv[2] || 8787);
// P14: a throwaway Ed25519 key pair standing in for the Discord application's (the check signs interactions
// through /__e2e/discord-sign; the Worker verifies them with the public half, exactly as in production).
const discordPair = await webcrypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign", "verify"]);
const discordPublic = Buffer.from(await webcrypto.subtle.exportKey("raw", discordPair.publicKey)).toString("hex");
const DISCORD_IDS = { app: "1300000000000000001", guild: "1300000000000000002", channel: "1300000000000000003", user: "1300000000000000004" };
const env = makePanelEnv({
  AGENT_TOKEN: process.env.MC_SECRET_AGENT_TOKEN,
  COMMAND_HMAC_KEY: process.env.MC_SECRET_COMMAND_HMAC_KEY,
  TELEMETRY: new SqliteD1(),
  EXPERIENCE: JSON.stringify({ minecraft_version: "26.3", messages: { enabled: true, per_player_minutes: 30, broadcast_minutes: 1, join_summary: true } }),
  DISCORD: JSON.stringify({ app_id: DISCORD_IDS.app, public_key: discordPublic, guild_id: DISCORD_IDS.guild, channel_ids: [DISCORD_IDS.channel], user_ids: [DISCORD_IDS.user], helper_role_id: "1300000000000000020", ephemeral: true, title: "e2e" }),
});
const followUps = []; // P14: the Discord reply edits the Worker would PATCH (no network here)
const rt = {
  connect: () => { throw new Error("no sockets"); },
  sleep: (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 2000))),
  fetch: async (url, init) => {
    if (String(url).startsWith("https://discord.com/api/v10/webhooks/")) followUps.push(JSON.parse(init.body).content);
    return new Response("[]");
  },
  waitUntil: (p) => { p.catch(() => {}); },
};
let down = false;
const seen = { experience: 0, segments: 0, refused: 0, metrics_at: [] }; // metrics_at: arrival times (ms) of /ingest/metrics

async function state() {
  const db = env.TELEMETRY;
  const tables = (await db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'exp_raw_%'").all()).results;
  const rows = [];
  for (const { name } of tables) rows.push(...(await db.prepare(`SELECT ts, id, data FROM ${name} ORDER BY ts`).all()).results);
  const results = [...env.ctx.storage.map.entries()].filter(([k]) => k.startsWith("result:")).map(([k, v]) => ({ id: k.slice(7), ...v }));
  return {
    seen,
    rows: rows.map((r) => {
      const d = JSON.parse(r.data);
      return { ts: r.ts, id: r.id, players: d.pl.length, p99: d.srv.p99, ticks: d.srv.ticks, samples: d.srv.samples, self_p99_us: d.srv.self_p99_us, verdict: d.verdicts.server.verdict, ovh: d.ovh, backlog: d.backlog, backfill: d.backfill };
    }),
    segments: env.archived.filter((o) => o.key.startsWith("telemetry/")).map((o) => o.key),
    results,
    discord_follow_ups: followUps,
  };
}

const server = http.createServer(async (req, res) => {
  try {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = Buffer.concat(chunks);
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    if (url.pathname.startsWith("/__e2e/")) {
      let out = { ok: true };
      if (url.pathname === "/__e2e/state") out = await state();
      else if (url.pathname === "/__e2e/down") down = url.searchParams.get("on") === "1";
      else if (url.pathname === "/__e2e/enqueue") {
        const r = await env.control.fetch(new Request("https://control/enqueue", { method: "POST", body }));
        out = await r.json();
      } else if (url.pathname === "/__e2e/maintain") out = await worker._internals.maintainExperience(env, Date.now(), rt);
      else if (url.pathname === "/__e2e/discord-sign") {
        // P14: sign a raw interaction body the way Discord does (timestamp + body, Ed25519)
        const timestamp = String(Math.floor(Date.now() / 1000));
        const sig = await webcrypto.subtle.sign("Ed25519", discordPair.privateKey, Buffer.concat([Buffer.from(timestamp), body]));
        out = { timestamp, signature: Buffer.from(sig).toString("hex"), ids: DISCORD_IDS };
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(out));
      return;
    }
    if (down) {
      seen.refused += 1;
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end('{"error":"outage (e2e)"}');
      return;
    }
    if (url.pathname === "/ingest/experience") seen.experience += 1;
    if (url.pathname === "/ingest/segment") seen.segments += 1;
    if (url.pathname === "/ingest/metrics") seen.metrics_at = [...seen.metrics_at.slice(-499), Date.now()];
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) if (typeof v === "string" && k !== "host") headers.set(k, v);
    const request = new Request(`https://hb.e2e.invalid${req.url}`, { method: req.method, headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : body });
    const resp = await worker._internals.handle(request, env, Date.now(), rt);
    const out = Buffer.from(await resp.arrayBuffer());
    res.writeHead(resp.status, { "Content-Type": resp.headers.get("Content-Type") || "application/json" });
    res.end(out);
  } catch (err) {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: String(err && err.stack || err) }));
  }
});
server.on("upgrade", (req, socket) => socket.destroy()); // no WebSocket: the agent polls
server.listen(port, "127.0.0.1", () => console.log(`e2e worker on 127.0.0.1:${port}`));
