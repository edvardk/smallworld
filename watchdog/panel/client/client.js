// The client modpack's setup page (v1.1, v1.2 stepper): one ES module, no dependencies, no third-party requests.
// Flow: a code (from the link /modpack gives, ?c=..., or typed) -> POST /api/code (a session cookie) -> GET
// /api/session (the tiers) -> four steps: computer (OS, detected, switchable), tier, install (the official
// launcher's zip: POST /api/link {tier, kind: "launcher"}; the .mrpack behind a disclosure), play. The page never
// sees the server's address; the code is removed from the address bar at once. Dark by default; the light
// choice is remembered per browser (localStorage, every access wrapped: private windows may refuse it).

const $ = (id) => document.getElementById(id);
const STEPS = ["os", "tier", "install", "play"];
const THEME_KEY = "client-theme";
let session = null;
let chosen = null;
let os = "windows";

// Where the official launcher keeps its files (its default game directory), per OS.
const MC_FOLDER = {
  windows: {
    name: "Windows",
    path: "%appdata%\\.minecraft",
    how: "Press Windows+R, paste it, press Enter.",
    unzip: "Right-click the download, Extract All…, paste that folder as the destination, Extract. Merge versions if asked.",
    store: "Got Minecraft from the Microsoft Store or the Xbox app? Same launcher, same folder.",
  },
  mac: {
    name: "macOS",
    path: "~/Library/Application Support/minecraft",
    how: "In Finder: Go, Go to Folder…, paste it.",
    unzip: "Double-click the download, then drag the versions and smallworld folders it made into that folder (merge versions if asked).",
    store: "",
  },
  linux: {
    name: "Linux",
    path: "~/.minecraft",
    how: "A hidden folder in your home folder (Ctrl+H shows hidden files).",
    unzip: "Extract the download into that folder (e.g. unzip <file> -d ~/.minecraft). Merge versions if asked.",
    store: "Installed the launcher as a Flatpak? Its folder is ~/.var/app/com.mojang.Minecraft/.minecraft.",
  },
};

function store(op, value) {
  try {
    if (op === "get") return window.localStorage.getItem(THEME_KEY);
    window.localStorage.setItem(THEME_KEY, value);
  } catch {
    // private window or blocked storage: the choice just isn't remembered
  }
  return null;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
  const light = theme === "light";
  $("themeBtn").textContent = light ? "Dark" : "Light";
  $("themeBtn").setAttribute("aria-pressed", String(light));
  $("themeBtn").setAttribute("aria-label", light ? "Switch to the dark theme" : "Switch to the light theme");
}

async function api(path, body) {
  const init = body === undefined ? { credentials: "same-origin" } : {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const resp = await fetch(path, init);
  let data = {};
  try {
    data = await resp.json();
  } catch {
    data = {};
  }
  return { ok: resp.ok, status: resp.status, data };
}

// What the visitor's computer is (a guess: the visitor can switch it in step 1).
function platform() {
  const ua = navigator.userAgent || "";
  const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
  if (/Android|iPhone|iPad|iPod/i.test(ua) || (navigator.userAgentData && navigator.userAgentData.mobile)) return "phone";
  if (/Win/i.test(p) || /Windows/i.test(ua)) return "windows";
  if (/Mac/i.test(p) || /Mac OS X/i.test(ua)) return "mac";
  if (/CrOS/i.test(ua)) return "chromebook";
  if (/Linux/i.test(p) || /Linux/i.test(ua)) return "linux";
  return "";
}

// --- the stepper ---------------------------------------------------------------------------------------------

function go(step, focus = false) {
  for (const s of STEPS) {
    const on = s === step;
    $(`step-${s}`).hidden = !on;
    const tab = $(`tab-${s}`);
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  }
  if (focus) $(`tab-${step}`).focus();
}

function onTabKey(e) {
  const i = STEPS.indexOf(e.currentTarget.id.slice(4));
  const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: STEPS.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  go(STEPS[(next + STEPS.length) % STEPS.length], true);
}

// Radio groups made of buttons: click or arrow keys.
function radios(box, select) {
  const items = () => [...box.querySelectorAll('[role="radio"]')];
  box.addEventListener("click", (e) => {
    const el = e.target.closest('[role="radio"]');
    if (el) select(el);
  });
  box.addEventListener("keydown", (e) => {
    const list = items();
    const i = list.indexOf(document.activeElement);
    const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (i < 0 || !d) return;
    e.preventDefault();
    const el = list[(i + d + list.length) % list.length];
    el.focus();
    select(el);
  });
}

function mark(box, el) {
  for (const r of box.querySelectorAll('[role="radio"]')) {
    const on = r === el;
    r.setAttribute("aria-checked", String(on));
    r.tabIndex = on ? 0 : -1;
  }
}

// --- step 1: the computer ------------------------------------------------------------------------------------

function chooseOs(value) {
  os = MC_FOLDER[value] ? value : "windows";
  mark($("osChoices"), $("osChoices").querySelector(`[data-os="${os}"]`));
  const f = MC_FOLDER[os];
  $("installTitle").textContent = `Install on ${f.name}`;
  $("osPath").textContent = f.path;
  $("osHow").textContent = f.how;
  $("unzipStep").textContent = f.unzip;
  $("storeNote").textContent = f.store;
  $("storeNote").hidden = !f.store;
}

function detectOs() {
  const seen = platform();
  const note = $("osNote");
  if (seen === "phone") {
    note.textContent = "This looks like a phone or tablet. The client is for computers: open this page on yours (type /modpack there for a new code).";
  } else if (seen === "chromebook") {
    note.textContent = "Chromebooks need Linux turned on first (Settings, Advanced, Developers): then pick Linux.";
  }
  note.hidden = !note.textContent;
  for (const el of $("osChoices").querySelectorAll(".choice")) {
    el.querySelector(".hint").textContent = el.dataset.os === seen ? "detected" : "";
  }
  chooseOs(MC_FOLDER[seen] ? seen : seen === "chromebook" ? "linux" : "windows");
}

// --- step 2: the tier ----------------------------------------------------------------------------------------

function renderTiers(tiers) {
  const box = $("tiers");
  box.textContent = "";
  for (const t of tiers) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `choice tier${t.recommended ? " rec" : ""}`;
    b.setAttribute("role", "radio");
    b.dataset.tier = t.id;
    const title = document.createElement("b");
    title.textContent = t.title;
    if (t.recommended) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "Recommended";
      title.append(" ", badge);
    }
    const line = document.createElement("span");
    line.className = "line";
    line.textContent = t.description;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${t.memory_gb || 4} GB memory · ${t.launcher && t.launcher.size_mb ? t.launcher.size_mb : t.size_mb} MB`;
    b.append(title, line, meta);
    b.title = t.example || "";
    box.append(b);
  }
  const rec = tiers.find((t) => t.recommended) || tiers[0];
  if (rec) chooseTier(rec.id);
}

function chooseTier(id) {
  chosen = session.tiers.find((t) => t.id === id) || null;
  mark($("tiers"), $("tiers").querySelector(`[data-tier="${id}"]`));
  if (!chosen) return;
  const la = chosen.launcher || {};
  const ready = Boolean(la.sha256);
  $("zipBtn").disabled = !ready;
  $("zipBtn").textContent = `Download ${chosen.title} (.zip)`;
  $("zipInfo").textContent = ready ? `${la.size_mb} MB` : "Not ready yet: use the Modrinth App file below for now.";
  $("profileId").textContent = la.profile_id || "fabric-loader-…";
  $("gameDir").textContent = la.dir || "smallworld-…";
  $("gameDir2").textContent = la.dir || "smallworld-…";
  const gb = chosen.memory_gb || 4;
  $("xmx").textContent = `-Xmx${gb}G`;
  $("memGb").textContent = String(gb);
  $("dlBtn").disabled = false;
  $("dlBtn").textContent = `Download ${chosen.title} (.mrpack)`;
  $("dlInfo").textContent = `${chosen.size_mb} MB`;
  $("includes").textContent = `${chosen.title}: ${chosen.includes.join(", ")}.`;
  renderExtras(chosen.missing || []);
}

// The files the zip may not contain (their licences): each with its official download link and folder.
function renderExtras(missing) {
  const list = $("extras");
  list.textContent = "";
  $("extraFold").hidden = missing.length === 0;
  $("extraCount").textContent = `(${missing.length})`;
  const dir = (chosen.launcher && chosen.launcher.dir) || "smallworld";
  for (const m of missing) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = String(m.url || "");
    a.rel = "noopener noreferrer";
    a.textContent = m.name;
    const into = document.createElement("kbd");
    into.textContent = `${dir}/${m.folder}`;
    li.append(a, m.optional ? " (optional) into " : " into ", into);
    list.append(li);
  }
}

// --- downloads, copy buttons -----------------------------------------------------------------------------------

async function fetchLink(body, errEl) {
  errEl.hidden = true;
  const r = await api("/api/link", body);
  if (!r.ok) {
    if (r.status === 401) return start(true);
    errEl.textContent = r.data.error || "That didn't work. Reload the page and try again.";
    errEl.hidden = false;
    return null;
  }
  window.location.assign(r.data.url);
  return null;
}

async function copy(button, text) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch {
    ok = false; // no clipboard permission: the text stays visible to copy by hand
  }
  button.textContent = ok ? "Copied" : "Select it";
  setTimeout(() => {
    button.textContent = "Copy";
  }, 1500);
}

// --- session -------------------------------------------------------------------------------------------------

async function start(expired = false) {
  const r = await api("/api/session");
  if (!r.ok) {
    session = null;
    $("setup").hidden = true;
    $("logout").hidden = true;
    $("codeCard").hidden = false;
    if (expired) {
      $("codeErr").textContent = "Your session ended. Type /modpack in game for a new code.";
      $("codeErr").hidden = false;
    }
    if (r.status === 503) {
      $("codeErr").textContent = "The client isn't ready yet. Try again later.";
      $("codeErr").hidden = false;
    }
    return;
  }
  session = r.data;
  document.title = `${session.title}: get the client`;
  $("title").textContent = `Get the ${session.title} client`;
  $("hello").textContent = `Hi ${session.name}! Four steps, about ten minutes.`;
  $("serverName").textContent = session.title;
  $("codeCard").hidden = true;
  $("setup").hidden = false;
  $("logout").hidden = false;
  if (session.modrinth_app_url) $("appLink").href = session.modrinth_app_url;
  detectOs();
  renderTiers(session.tiers);
  go("os");
  const credit = session.shader_credit ? `${session.shader_credit} ` : "";
  const pending = session.pending && session.pending.length ? `Coming once they have a release for Minecraft ${session.minecraft_version}: ${session.pending.join(", ")}. ` : "";
  $("credits").textContent = `${credit}${pending}Every mod's name, version and licence is in CREDITS.txt inside the download. Xaero's Minimap and World Map are by xaero96, downloaded from Modrinth.`;
}

async function useCode(code) {
  $("codeErr").hidden = true;
  const r = await api("/api/code", { code });
  if (!r.ok) {
    $("codeErr").textContent = r.data.error || "That code didn't work. Type /modpack in game for a new one.";
    $("codeErr").hidden = false;
    $("codeCard").hidden = false;
    return;
  }
  await start();
}

async function init() {
  applyTheme(store("get"));
  $("themeBtn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    applyTheme(next);
    store("set", next);
  });
  for (const s of STEPS) {
    $(`tab-${s}`).addEventListener("click", () => go(s));
    $(`tab-${s}`).addEventListener("keydown", onTabKey);
  }
  for (const b of document.querySelectorAll("[data-go]")) {
    b.addEventListener("click", () => {
      go(b.dataset.go);
      $(`step-${b.dataset.go}`).querySelector("h2").scrollIntoView({ block: "nearest" });
    });
  }
  radios($("osChoices"), (el) => chooseOs(el.dataset.os));
  radios($("tiers"), (el) => chooseTier(el.dataset.tier));
  $("codeForm").addEventListener("submit", (e) => {
    e.preventDefault();
    useCode($("code").value);
  });
  $("zipBtn").addEventListener("click", () => fetchLink({ tier: chosen.id, kind: "launcher" }, $("zipErr")));
  $("dlBtn").addEventListener("click", () => fetchLink({ tier: chosen.id }, $("dlErr")));
  $("copyPath").addEventListener("click", (e) => copy(e.currentTarget, $("osPath").textContent));
  $("copyXmx").addEventListener("click", (e) => copy(e.currentTarget, $("xmx").textContent));
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", {});
    start();
  });
  const params = new URLSearchParams(window.location.search);
  const code = params.get("c");
  if (code) {
    history.replaceState(null, "", window.location.pathname); // the code leaves the address bar at once
    await useCode(code);
    return;
  }
  await start();
}

init();
