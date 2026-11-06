// The confirmation page (P10 review), served only on <panel>-confirm.<zone>: its own Cloudflare Access application
// with a session of panel.access.reauth_minutes and the one-time PIN as the only login. The panel opens it with
// the action in the URL fragment (never sent to a server); this page shows what will happen, posts the action to
// its own host's /api/action (the Worker accepts stop, plugin switches and restores only here, with this app's
// Access token no older than reauth_minutes) and follows the agent's answer. One ES module, no dependencies, no
// inline code (CSP 'self'). p11-review: a restore's typed confirmation is typed HERE, never taken from the
// fragment (a crafted link could pre-fill it); v1.2: so are the world library's ("create <name>", "switch to
// <name>", "delete <name>").

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ACTIONS = new Set(["stop", "plugin", "restore", "world_create", "world_switch", "world_delete"]);

function parseFragment() {
  try {
    const data = JSON.parse(decodeURIComponent(location.hash.slice(1)));
    if (!data || typeof data !== "object" || !ACTIONS.has(data.action)) return null;
    const args = data.args && typeof data.args === "object" && !Array.isArray(data.args) ? { ...data.args } : {};
    delete args.confirm; // typed on this page only
    return { action: data.action, args, what: typeof data.what === "string" ? data.what.slice(0, 80) : data.action };
  } catch {
    return null;
  }
}

// The text the Worker wants in args.confirm (actionArgs), or null for actions without one.
function typedPhrase(action, args) {
  const name = String(args.name || "");
  if (action === "restore") return `restore ${String(args.snapshot || "").slice(0, 8)}`;
  if (action === "world_create") return `create ${name}`;
  if (action === "world_switch") return `switch to ${name}`;
  if (action === "world_delete") return `delete ${name}`;
  return null;
}
const typedField = (phrase) => `<div class="field"><label class="label" for="cTyped">Type <span class="mono typed">${esc(phrase)}</span> to confirm</label><input class="input mono" id="cTyped" type="text" autocomplete="off" spellcheck="false" autocapitalize="off"></div>`;
const countdown = (s) => (s ? (s >= 60 ? `${s / 60} min` : `${s} s`) : "no countdown");

function describe({ action, args }) {
  const cd = Number.isInteger(args.countdown) ? args.countdown : 60;
  const backup = args.backup === true ? "<li>A backup runs first.</li>" : "";
  if (action === "stop") {
    return { title: "Stop the server", body: `<ul><li>In-game countdown: <strong>${esc(countdown(cd))}</strong> (skipped when nobody is online).</li>${backup}<li>The server stays down until someone presses Start: self-heal, the watchdog and deploys leave it stopped.</li></ul>` };
  }
  if (action === "plugin") {
    const on = args.enabled === true;
    return { title: `${on ? "Enable" : "Disable"} plugin ${args.name || "?"}`, body: `<ul><li>Moves the locked jar ${on ? "back to <code class=\"mono\">plugins/</code>" : "to <code class=\"mono\">plugins/disabled/</code>"} and restarts the server.</li><li>Countdown: <strong>${esc(countdown(cd))}</strong>.</li></ul>` };
  }
  const name = String(args.name || "?");
  const archiveFirst = '<li>The active world is archived first (a restic snapshot tagged <code class="mono">world-archive</code>, kept outside the backup retention): that is the safety snapshot, and it lets you switch back later.</li>';
  if (action === "world_create") {
    const seed = args.seed ? `seed <code class="mono">${esc(args.seed)}</code>` : "a random seed";
    return { title: `Create the world ${name}`, body: `<ul><li>A new world <strong>${esc(name)}</strong>: ${seed}, type <code class="mono">${esc(args.type)}</code>, preset <code class="mono">${esc(args.preset)}</code>.</li>${archiveFirst}<li>Countdown: <strong>${esc(countdown(cd))}</strong>; then the server stops, and starts again generating the new world.</li><li>Each world keeps its own inventories, ender chests and XP; the whitelist, ops and bans are shared.</li></ul>${typedField(typedPhrase(action, args))}` };
  }
  if (action === "world_switch") {
    return { title: `Switch to the world ${name}`, body: `<ul><li>The archived world <strong>${esc(name)}</strong> replaces the active one on the server.</li>${archiveFirst}<li>Countdown: <strong>${esc(countdown(cd))}</strong>; then a few minutes of downtime while the archive is fetched from R2.</li><li>A world saved by an older Minecraft is upgraded when it loads (one way); a newer one is refused.</li></ul>${typedField(typedPhrase(action, args))}` };
  }
  if (action === "world_delete") {
    return { title: `Delete the world ${name}`, body: `<ul><li>Forgets the archive snapshots of <strong>${esc(name)}</strong> (the active world can never be deleted). The server isn't touched.</li><li>Snapshots younger than the bucket lock are forgotten once it allows; after that the world is gone for good.</li></ul>${typedField(typedPhrase(action, args))}` };
  }
  const id = String(args.snapshot || "");
  return {
    title: `Restore snapshot ${id.slice(0, 8)}`,
    body: `<ul><li>Replaces the live world with snapshot <code class="mono">${esc(id.slice(0, 8))}</code>; a safety snapshot (tag <code class="mono">pre-restore</code>) of the current world is taken first.</li><li>Countdown: <strong>${esc(countdown(cd))}</strong>; then the server stops for a few minutes.</li></ul>${typedField(typedPhrase(action, args))}`,
  };
}

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw Object.assign(new Error(data.error || `HTTP ${resp.status}`), { status: resp.status, data });
  return data;
}

function status(text, cls = "") {
  const el = $("#cStatus");
  el.textContent = text;
  el.className = `note ${cls}`.trim();
}

async function confirm(req) {
  const ok = $("#cOk");
  ok.disabled = true;
  status("Sending to the server…");
  let q;
  try {
    q = await api("/api/action", { method: "POST", body: { action: req.action, args: req.args } });
  } catch (e) {
    status(e.data && e.data.reauth ? `${e.message}. Close this window, sign out of Cloudflare Access and try again.` : `Not sent: ${e.message}`, "bad");
    return;
  }
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, i < 5 ? 400 : 1000));
    let result = null;
    try {
      ({ result } = await api(`/api/commands/${q.id}`));
    } catch {
      continue;
    }
    if (result) {
      status(result.ok ? `${req.what}: ${result.output || "done"}. You can close this window; the panel shows the progress.` : `${req.what} refused: ${result.error || "failed"}`, result.ok ? "good" : "bad");
      $("#cCancel").textContent = "Close";
      return;
    }
  }
  status("No answer from the server's agent within 30 s (it may be offline); the command expires unrun.", "bad");
}

async function main() {
  const req = parseFragment();
  $("#cCancel").addEventListener("click", () => window.close());
  if (!req) {
    $("#cTitle").textContent = "Nothing to confirm";
    $("#cWho").textContent = "Open this page from the panel's Stop, plugin switch, Restore or Worlds buttons.";
    return;
  }
  const d = describe(req);
  $("#cTitle").textContent = `${d.title}?`;
  $("#cBody").innerHTML = d.body;
  try {
    const me = await api("/api/me");
    const mins = Math.max(0, Math.round((me.login_age_s || 0) / 60));
    $("#cWho").textContent = `Signed in as ${me.email} (confirmation sign-in ${mins} min ago; valid for ${me.reauth_minutes} min).`;
  } catch (e) {
    $("#cWho").textContent = `Not signed in for confirmations (${e.message}). Reload this window to sign in.`;
    return;
  }
  const ok = $("#cOk");
  ok.textContent = d.title;
  const typed = $("#cTyped");
  if (typed) {
    const want = typedPhrase(req.action, req.args);
    const sync = () => {
      ok.disabled = typed.value !== want;
    };
    typed.addEventListener("input", sync);
    sync();
    typed.focus();
  } else {
    ok.disabled = false;
  }
  ok.addEventListener(
    "click",
    () => confirm(typed ? { ...req, args: { ...req.args, confirm: typed.value } } : req),
    { once: true },
  );
}

main();
