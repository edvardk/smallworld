// mc-panel UI (design §5a, §8 P9/P10): the approved console mockup on real data from the Worker's API.
// One ES module, no dependencies, no build step. Everything the page shows comes from /api/* (same origin,
// behind Cloudflare Access; the Worker verifies the Access JWT on every request). Strings from the server are
// always escaped (esc) before they reach innerHTML, and no inline styles or scripts are used (CSP 'self').
// P10 actions (restart/stop/start with an in-game countdown, backup now, whitelist add/remove recorded in Git,
// rejected logins, kick/ban/pardon, plugin switches, restore) each open a confirmation dialog first, go to
// POST /api/action (the Worker checks, audits with the Access identity and signs; the agent checks again and
// runs them through fixed systemd units), and show the agent's job (countdown, steps) in the header banner.
// Panel perf: every update is coalesced into one animation frame (onFrame) and patched into the DOM (morph: only
// what changed is touched, so nothing is rebuilt, re-laid-out or re-animated on a poll); charts off screen wait until
// they scroll into view; the console appends lines in chunks the browser skips while out of view; numbers tween;
// nothing renders or polls while the tab is hidden; effects step down to "light" when frames get slow (FX).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const num = (v) => typeof v === "number" && Number.isFinite(v);
const POLL_MS = 15000;
const CONSOLE_POLL_MS = 2000;
const LIVE_RENEW_MS = 60000;

// --- small formatting helpers --------------------------------------------------------------------------------

const tpsCls = (v) => (v >= 19.5 ? "good" : v >= 17 ? "warn" : "bad");
const msptCls = (v) => (v < 35 ? "good" : v <= 45 ? "warn" : "bad");
const pctCls = (v) => (v < 70 ? "good" : v < 85 ? "warn" : "bad");
const COLORS = { good: "var(--good)", warn: "var(--warn)", bad: "var(--bad)" };

function fmtUp(s) {
  if (!num(s)) return "–";
  const d = Math.floor(s / 86400);
  const p = (n) => String(n).padStart(2, "0");
  return `${d ? `${d}d ` : ""}${p(Math.floor((s % 86400) / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(Math.floor(s % 60))}`;
}
function ago(ms) {
  if (!num(ms)) return "never";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const isoMs = (iso) => (iso ? Date.parse(iso) : null);
function clock(ms, withDate = false) {
  if (!num(ms)) return "–";
  const d = new Date(ms);
  const t = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (!withDate) return t;
  const today = new Date().toDateString() === d.toDateString();
  return today ? t : `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} ${t.slice(0, 5)}`;
}
const gb = (mb) => (num(mb) ? (mb / 1024).toFixed(1) : "–");
const kbps = (bps) => (num(bps) ? Math.round(bps / 1000) : "–");
const fix = (v, d = 1) => (num(v) ? v.toFixed(d) : "–");

// --- rendering pipeline (panel perf) ------------------------------------------------------------------------------

// One animation frame per batch of updates: a poll, console lines and the job banner arriving together paint once.
// While the page is hidden the browser runs no frames, so nothing renders until it is shown again.
const FRAME = new Map();
let frameId = 0;
function onFrame(key, fn) {
  FRAME.set(key, fn);
  if (!frameId) frameId = requestAnimationFrame(flushFrame);
}
function flushFrame() {
  frameId = 0;
  const jobs = [...FRAME.values()];
  FRAME.clear();
  for (const fn of jobs) fn();
}

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");
const calm = () => REDUCED.matches || FX.level === "light" || document.hidden;

// Number tweening for live stats: cheap (one text node per frame, only while a value changes).
const TW = new WeakMap();
const tweens = new Set();
let tweenId = 0;
function tweenNum(el, to, fmt) {
  if (!el) return;
  const prev = TW.get(el);
  const from = prev ? prev.cur : Number.NaN;
  const st = { from, to, cur: to, t0: performance.now(), fmt };
  TW.set(el, st);
  if (!num(to)) {
    tweens.delete(el);
    st.cur = Number.NaN;
    el.textContent = "–";
    return;
  }
  if (!num(from) || from === to || calm()) {
    tweens.delete(el);
    el.textContent = fmt(to);
    return;
  }
  st.cur = from;
  tweens.add(el);
  if (!tweenId) tweenId = requestAnimationFrame(stepTweens);
}
function stepTweens(now) {
  tweenId = 0;
  for (const el of tweens) {
    const st = TW.get(el);
    const p = Math.min(1, Math.max(0, (now - st.t0) / 650));
    st.cur = p >= 1 ? st.to : st.from + (st.to - st.from) * (1 - (1 - p) ** 3);
    el.textContent = st.fmt(st.cur);
    if (p >= 1) tweens.delete(el);
  }
  if (tweens.size) tweenId = requestAnimationFrame(stepTweens);
}

// DOM patching: a view is re-rendered to an HTML string as before, then morphed into the live DOM node by node.
// Nodes are matched by data-k or id (keyed: moved, not rebuilt), attributes and text are only written when they
// differ, inputs keep what the user typed, [data-keep] subtrees (gauges, the console) are left to their painters,
// and new items in a .feed slide in. style is never touched (set through the CSSOM by applyDyn).
const keyOf = (n) => (n.nodeType === 1 ? n.getAttribute("data-k") || n.id || null : null);
const sameNode = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName && keyOf(a) === keyOf(b);
function patch(parent, html) {
  const t = document.createElement("template");
  t.innerHTML = html;
  morphChildren(parent, t.content);
}
function morphChildren(parent, next) {
  let cur = parent.firstChild;
  for (let nx = next.firstChild; nx; ) {
    const after = nx.nextSibling;
    if (cur && sameNode(cur, nx)) {
      morphNode(cur, nx);
      cur = cur.nextSibling;
    } else {
      const k = keyOf(nx);
      let found = null;
      if (k) for (let c = cur; c && !found; c = c.nextSibling) if (sameNode(c, nx)) found = c;
      if (found) {
        parent.insertBefore(found, cur);
        morphNode(found, nx);
      } else {
        parent.insertBefore(nx, cur);
        if (nx.nodeType === 1 && parent.classList && parent.classList.contains("feed") && !calm()) nx.classList.add("enter");
      }
    }
    nx = after;
  }
  while (cur) {
    const n = cur.nextSibling;
    cur.remove();
    cur = n;
  }
}
function morphNode(a, b) {
  if (a.nodeType !== 1) {
    if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue;
    return;
  }
  if (a.hasAttribute("data-keep")) return;
  if (isChart(a) && !chartDue(a, b)) return;
  for (const { name } of [...a.attributes]) if (name !== "style" && !b.hasAttribute(name)) a.removeAttribute(name);
  for (const { name, value } of b.attributes) if (a.getAttribute(name) !== value) a.setAttribute(name, value);
  morphChildren(a, b);
}

// Charts redraw only while visible (IntersectionObserver) and at most once a second; an off-screen chart keeps its
// newest version pending and draws it when it scrolls into view.
const isChart = (el) => el.nodeName === "svg" && el.classList.contains("spark");
const CHART = { io: null, seen: new WeakSet(), visible: new WeakSet(), pending: new WeakMap(), last: new WeakMap() };
function chartDue(a, b) {
  if (!CHART.io) return true;
  const now = performance.now();
  if (CHART.visible.has(a) && now - (CHART.last.get(a) || 0) >= 1000) {
    CHART.last.set(a, now);
    CHART.pending.delete(a);
    return true;
  }
  const waiting = CHART.pending.has(a);
  CHART.pending.set(a, b);
  if (!waiting && CHART.visible.has(a)) setTimeout(() => drawPending(a), 1000);
  return false;
}
function drawPending(a) {
  const b = CHART.pending.get(a);
  if (!b || !a.isConnected || !CHART.visible.has(a)) return;
  CHART.pending.delete(a);
  CHART.last.set(a, performance.now());
  onFrame(a, () => {
    for (const { name } of [...a.attributes]) if (name !== "style" && !b.hasAttribute(name)) a.removeAttribute(name);
    for (const { name, value } of b.attributes) if (a.getAttribute(name) !== value) a.setAttribute(name, value);
    morphChildren(a, b);
  });
}
function watchCharts(root, fresh) {
  if (!("IntersectionObserver" in window)) return;
  if (fresh && CHART.io) {
    CHART.io.disconnect();
    CHART.io = null;
  }
  if (!CHART.io) {
    CHART.seen = new WeakSet();
    CHART.io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          CHART.visible.add(e.target);
          drawPending(e.target);
        } else CHART.visible.delete(e.target);
      }
    }, { rootMargin: "120px 0px" });
  }
  for (const el of root.querySelectorAll("svg.spark")) {
    if (CHART.seen.has(el)) continue;
    CHART.seen.add(el);
    CHART.io.observe(el);
  }
}

// Effects (html[data-fx]): "full" or "light" (no live header blur, gauge glows, card glows or motion; see
// panel.css). The Effects switch picks auto, full or light (remembered per browser); auto measures frame times
// while the page scrolls or animates and steps down to light when the 95th percentile stays over ~24 ms for a few
// seconds (a slow laptop), and back up only when frames are fast again for a while (at most twice per visit).
const FX = { mode: "auto", level: "full", auto: "full", bad: 0, good: 0, since: 0, steps: 0 };
const FX_MODES = ["auto", "full", "light"];
const MON = { deltas: [], last: 0, id: 0, until: 0 };
function applyFx() {
  FX.level = FX.mode === "auto" ? FX.auto : FX.mode;
  const root = document.documentElement;
  root.dataset.fx = FX.level;
  root.dataset.fxMode = FX.mode;
  const b = $("#fxBtn");
  if (!b) return;
  const what = FX.mode === "auto" ? `auto (now ${FX.level})` : FX.mode;
  b.setAttribute("aria-label", `Effects: ${what} (switch)`);
  b.title = `Effects: ${what}. Auto, full or light (lighter effects for slow machines).`;
  $("#fxLabel").textContent = FX.mode;
}
function initFx() {
  try {
    const m = localStorage.getItem("mc-fx");
    if (FX_MODES.includes(m)) FX.mode = m;
  } catch {
    // storage blocked: auto
  }
  applyFx();
  $("#fxBtn").addEventListener("click", () => {
    FX.mode = FX_MODES[(FX_MODES.indexOf(FX.mode) + 1) % FX_MODES.length];
    try {
      localStorage.setItem("mc-fx", FX.mode);
    } catch {
      // per-viewer convenience only
    }
    applyFx();
    toast(`Effects: ${FX.mode === "auto" ? "auto (lighter when this machine is slow)" : FX.mode}`, 2200);
  });
  const active = () => {
    MON.until = performance.now() + 700;
    if (!MON.id && !document.hidden) MON.id = requestAnimationFrame(monFrame);
  };
  document.addEventListener("scroll", active, { passive: true, capture: true });
  window.addEventListener("wheel", active, { passive: true });
  window.addEventListener("touchmove", active, { passive: true });
  return active;
}
function monFrame(now) {
  MON.id = 0;
  if (MON.last) MON.deltas.push(now - MON.last);
  MON.last = now;
  if (MON.deltas.length >= 40) judgeFrames(now);
  if (now < MON.until && !document.hidden) MON.id = requestAnimationFrame(monFrame);
  else MON.last = 0;
}
function judgeFrames(now) {
  const d = MON.deltas.sort((x, y) => x - y);
  const p95 = d[Math.floor(d.length * 0.95)];
  MON.deltas = [];
  if (p95 > 24) {
    FX.good = 0;
    if (++FX.bad >= 3 && FX.auto === "full") {
      FX.auto = "light";
      FX.since = now;
      FX.steps += 1;
      FX.bad = 0;
      applyFx();
    }
  } else if (p95 < 14) {
    FX.bad = 0;
    if (++FX.good >= 6 && FX.auto === "light" && now - FX.since > 30000 && FX.steps < 2) {
      FX.auto = "full";
      FX.good = 0;
      applyFx();
    }
  } else {
    FX.bad = 0;
    FX.good = 0;
  }
}

// --- API -------------------------------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status = 0, data = {}) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 403) {
    sessionLost();
    throw new ApiError("forbidden");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(data.error || `HTTP ${resp.status}`, resp.status, data);
  return data;
}

let lost = false;
function sessionLost() {
  if (lost) return;
  lost = true;
  toast("Your Cloudflare Access session ended. Reload the page to sign in again.", 0);
}

// --- shared state ------------------------------------------------------------------------------------------------

const S = { o: null, inv: null, lastError: null };
let current = "overview";

// --- drawing primitives (SVG presentation attributes, no inline styles: CSP) ----------------------------------------

const GC = 2 * Math.PI * 52;
const GA = GC * 0.75;
function gauge(id, sm = false) {
  // .glo: the arc's glow, a blurred copy with room around it so the blur is never clipped (panel.css)
  return `<div class="gauge${sm ? " sm" : ""}" id="g-${id}" data-keep role="img" aria-label="loading"><svg class="glo" viewBox="-16 -16 152 152" aria-hidden="true"><circle cx="60" cy="60" r="52" fill="none" stroke="currentColor" stroke-width="10" stroke-linecap="round" stroke-dasharray="0 ${GC.toFixed(1)}"/></svg><svg viewBox="0 0 120 120" aria-hidden="true">
    <circle class="trk" cx="60" cy="60" r="52" fill="none" stroke-width="10" stroke-dasharray="${GA.toFixed(1)} ${GC.toFixed(1)}" stroke-linecap="round"/>
    <circle class="val" cx="60" cy="60" r="52" fill="none" stroke="currentColor" stroke-width="10" stroke-linecap="round" stroke-dasharray="0 ${GC.toFixed(1)}"/></svg>
    <div class="ctr" aria-hidden="true"><span class="big">–</span><span class="unit"></span></div></div>`;
}
// value: the number shown (tweened with fmt), or null for "–"
function setGauge(id, frac, cls, value, fmt, unit, aria) {
  const g = document.getElementById(`g-${id}`);
  if (!g) return;
  if (!g.classList.contains(`c-${cls}`)) {
    g.classList.remove("c-good", "c-warn", "c-bad");
    g.classList.add(`c-${cls}`);
  }
  const dash = `${(GA * clamp(num(frac) ? frac : 0, 0, 1)).toFixed(1)} ${GC.toFixed(1)}`;
  for (const arc of g.querySelectorAll(".val, .glo circle")) if (arc.getAttribute("stroke-dasharray") !== dash) arc.setAttribute("stroke-dasharray", dash);
  tweenNum(g.querySelector(".big"), num(value) ? value : null, fmt);
  const u = g.querySelector(".unit");
  if (u.textContent !== unit) u.textContent = unit;
  if (g.getAttribute("aria-label") !== aria) g.setAttribute("aria-label", aria);
}

// A sparkline over [{ts, key...}] points. m: {key, min, max, line?, label, unit, fmt}; m2: a second series.
function spark(points, m, color = "var(--accent)", m2 = null, color2 = "var(--accent-2)", cls = "spark") {
  const W = 300;
  const H = cls === "spark" ? 48 : 90;
  const P = 3;
  const data = points.filter((p) => num(p[m.key]));
  if (data.length < 2) return `<svg class="${cls}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(m.label)}: not enough data yet"><text x="${W / 2}" y="${H / 2 + 4}" text-anchor="middle" font-size="11" fill="var(--muted)">waiting for samples</text></svg>`;
  const t0 = data[0].ts;
  const t1 = data[data.length - 1].ts;
  const allMax = Math.max(...data.map((p) => p[m.key]), ...(m2 ? points.filter((p) => num(p[m2.key])).map((p) => p[m2.key]) : []));
  const max = m.max ?? Math.max(1, allMax * 1.15);
  const min = m.min ?? 0;
  const x = (ts) => P + ((ts - t0) / Math.max(1, t1 - t0)) * (W - 2 * P);
  const y = (v) => H - P - ((clamp(v, min, max) - min) / (max - min || 1)) * (H - 2 * P);
  const line = (key) => points.filter((p) => num(p[key])).map((p) => `${x(p.ts).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const pts = line(m.key);
  const last = data[data.length - 1][m.key];
  const thr = m.line ? `<line x1="0" x2="${W}" y1="${y(m.line)}" y2="${y(m.line)}" stroke="var(--bad)" stroke-dasharray="4 4" stroke-width="1" opacity=".6" vector-effect="non-scaling-stroke"/>` : "";
  const second = m2 ? `<polyline points="${line(m2.key)}" fill="none" stroke="${color2}" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>` : "";
  return `<svg class="${cls}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(m.label)}${m2 ? ` and ${esc(m2.label)}` : ""}, now ${esc(m.fmt(last))}${esc(m.unit)}">
    <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>${thr}
    <polygon points="${P},${H - P} ${pts} ${(W - P).toFixed(1)},${H - P}" fill="${color}" opacity=".16"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>${second}
    <circle cx="${x(t1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="3" fill="${color}"/></svg>`;
}

// A deterministic 8x8 "skin face" per name: drawn locally, no avatar service learns who plays here.
function headSvg(name, cls = "head") {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const hue = h % 360;
  let rects = "";
  for (let i = 0; i < 64; i++) {
    const r = (h >> (i % 24)) & 3;
    const l = 30 + r * 10 + (i > 23 && i < 40 ? 20 : 0);
    rects += `<rect x="${i % 8}" y="${i >> 3}" width="1" height="1" fill="hsl(${hue} 35% ${l}%)"/>`;
  }
  rects += '<rect x="1" y="4" width="2" height="1" fill="#fff"/><rect x="5" y="4" width="2" height="1" fill="#fff"/><rect x="2" y="4" width="1" height="1" fill="#2a2a5a"/><rect x="5" y="4" width="1" height="1" fill="#2a2a5a"/>';
  return `<svg class="${cls}" viewBox="0 0 8 8" shape-rendering="crispEdges" aria-hidden="true">${rects}</svg>`;
}

// Widths/heights/colours computed at runtime go through the CSSOM (allowed by the CSP), via data attributes.
function applyDyn(root = document) {
  for (const el of root.querySelectorAll("[data-w]")) el.style.width = `${clamp(Number(el.dataset.w), 0, 100)}%`;
  for (const el of root.querySelectorAll("[data-h]")) el.style.transform = `scaleY(${clamp(Number(el.dataset.h), 0, 100) / 100})`;
  for (const el of root.querySelectorAll("[data-bg]")) el.style.background = COLORS[el.dataset.bg] || "";
}

const WARN_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18h.01"/></svg>';
const pill = (cls, text, extra = "") => `<span class="pill ${cls}${extra}">${esc(text)}</span>`;
const emptyRow = (cols, text) => `<tr><td colspan="${cols}" class="muted">${esc(text)}</td></tr>`;

let toastTimer;
function toast(msg, ms = 3600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => (t.hidden = true), ms);
}

// --- dialogs, actions and the agent's job (P10) ----------------------------------------------------------------

// The mockup's confirmation dialog. `body` is HTML built with esc(); `typed`: the OK button stays disabled until
// the input #typedConfirm holds exactly that text. Enter in a field submits with the first button (Cancel).
function confirmDlg({ title, body, ok = "Confirm", danger = false, typed = null, onOpen, onOk }) {
  const d = $("#dlg");
  $("#dlgTitle").textContent = title;
  $("#dlgBody").innerHTML = body;
  const okBtn = $("#dlgOk");
  okBtn.textContent = ok;
  okBtn.className = `btn ${danger ? "danger solid" : "primary"}`;
  okBtn.disabled = Boolean(typed);
  if (typed) {
    const inp = $("#typedConfirm", d);
    inp.addEventListener("input", () => {
      okBtn.disabled = inp.value.trim() !== typed;
    });
  }
  if (onOpen) onOpen(d);
  d.onclose = () => {
    if (d.returnValue === "ok" && onOk) onOk(d);
  };
  d.returnValue = "";
  d.showModal();
}

// P10 review: stop, plugin switches and restore are never sent from here. They are confirmed on a separate host
// (<panel>-confirm.<zone>) behind its own Cloudflare Access application: a session of reauth_minutes and the
// one-time PIN as the only login, so a forgotten or stolen panel session can't run them. The action and its
// arguments travel in the URL fragment (never sent to a server); the confirmation page shows them, posts the
// action there and follows the result. This page sees the job through its usual polling.
const DESTRUCTIVE = new Set(["stop", "plugin", "restore", "world_create", "world_switch", "world_delete"]);
let confirmUrl = null;

async function confirmBase() {
  if (confirmUrl) return confirmUrl;
  const me = await api("/api/me");
  confirmUrl = typeof me.confirm_url === "string" && /^https:\/\/[a-z0-9.-]+\/$/.test(me.confirm_url) ? me.confirm_url : null;
  return confirmUrl;
}

async function openConfirm(action, args, what) {
  let base;
  try {
    base = await confirmBase();
  } catch (e) {
    toast(`${what}: not sent (${e.message})`, 6000);
    return null;
  }
  if (!base) {
    toast(`${what}: no confirmation host is configured (panel_confirm_url); destructive actions are off.`, 8000);
    return null;
  }
  const url = `${base}#${encodeURIComponent(JSON.stringify({ action, args, what }))}`;
  const win = window.open(url, "mc-confirm", "popup,width=560,height=720");
  confirmDlg({
    title: "Confirm in the new window",
    ok: "Done",
    body: `<p>${esc(what)} is confirmed in a separate window: sign in there with a one-time PIN (the confirmation session lasts only a few minutes), check the details and press Confirm.</p>
      <p class="note">${win ? "No window? Your browser may have blocked it: " : ""}<a class="btn" href="${esc(url)}" target="_blank" rel="noopener noreferrer" id="confirmLink">Open the confirmation page</a></p>
      <p class="note">The countdown and progress appear in the banner here once it is confirmed.</p>`,
    onOk: () => afterAction(),
  });
  afterAction();
  return null;
}

const LABELS = { selftest: "Panel → Git self-test", restart: "Restart", stop: "Stop", start: "Start", backup: "Backup", cancel: "Cancel", whitelist: "Whitelist", rejected_dismiss: "Dismiss", moderate: "Moderation", plugin: "Plugin switch", restore: "Restore", world_create: "New world", world_switch: "World switch", world_delete: "World delete" };

// POST /api/action, then wait for the agent's answer. Returns the result ({ok, output|error}) or null.
async function runAction(action, args, what = LABELS[action] || action) {
  if (DESTRUCTIVE.has(action)) return openConfirm(action, args, what);
  let q;
  try {
    q = await api("/api/action", { method: "POST", body: { action, args } });
  } catch (e) {
    if (!(e instanceof ApiError && e.message === "forbidden")) toast(`${what}: not sent (${e.message})`, 6000);
    return null;
  }
  toast(`${what}: sent to the server…`);
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, i < 5 ? 400 : 1000));
    let result = null;
    try {
      ({ result } = await api(`/api/commands/${q.id}`));
    } catch {
      continue;
    }
    if (result) {
      toast(result.ok ? `${what}: ${result.output || "done"}` : `${what} refused: ${result.error || "failed"}`, 7000);
      afterAction();
      return result;
    }
  }
  toast(`${what}: no answer from the server's agent within 30 s (it may be offline); the command expires unrun.`, 8000);
  return null;
}

// Fresh data after an action: the agent pushes its inventory right away; reload what the open tab shows.
function afterAction() {
  for (const id of Object.keys(loaded)) loaded[id] = 0;
  pollJob();
  setTimeout(() => refresh().catch(() => {}), 1500);
  setTimeout(() => refresh().catch(() => {}), 6000);
}

const JOB = { job: null, skew: 0, fast: null, announced: new Set() };

function jobActive(j) {
  return Boolean(j && (j.state === "countdown" || j.state === "running"));
}

function setJob(job, serverNow) {
  if (num(serverNow)) JOB.skew = serverNow - Date.now();
  const prev = JOB.job;
  JOB.job = job || null;
  const j = JOB.job;
  if (j && !jobActive(j) && prev && prev.id === j.id && jobActive(prev) && !JOB.announced.has(`${j.id}:${j.state}`)) {
    JOB.announced.add(`${j.id}:${j.state}`);
    toast(`${j.label || j.action}: ${j.state === "done" ? j.step || "done" : `${j.state}: ${j.step}`}`, 8000);
    afterAction();
  }
  // the Plugins and Worlds tabs disable their buttons while a job runs: repaint them when that changes
  if (jobActive(prev) !== jobActive(j) && (current === "plugins" || current === "worlds")) update();
  if (jobActive(j) && !JOB.fast) JOB.fast = setInterval(() => document.hidden || pollJob(), 2000);
  if (!jobActive(j) && JOB.fast) {
    clearInterval(JOB.fast);
    JOB.fast = null;
  }
  paintJob();
}

async function pollJob() {
  try {
    const r = await api("/api/job");
    setJob(r.job, r.now);
  } catch {
    // the banner keeps its last state
  }
}

function paintJob() {
  const el = $("#countdown");
  const j = JOB.job;
  const active = jobActive(j);
  el.classList.toggle("on", active && j.state === "countdown");
  el.classList.toggle("running", active && j.state === "running");
  if (!active) return;
  const by = j.login ? ` · started by ${j.login}` : "";
  if (j.state === "countdown") {
    const left = Math.max(0, Math.round((j.deadline - (Date.now() + JOB.skew)) / 1000));
    $("#cdText").textContent = `${j.label || "Restart"} in ${left}s`;
    $("#cdSub").textContent = `${j.players ?? 0} player(s) online see a chat and action-bar warning at 5–1 min, 30, 10 and 5–1 s${by}.`;
    $("#cdCancel").textContent = `Cancel ${(j.label || "action").toLowerCase()}`;
  } else {
    $("#cdText").textContent = `${j.label || j.action}: running`;
    $("#cdSub").textContent = `${j.step || "working"}${by}`;
  }
}

function onlineNames() {
  const s = S.o && S.o.sample;
  return s && S.o.sample_age_s <= 180 && Array.isArray(s.players_online) ? s.players_online : [];
}

function countdownBody(action, { backup = false, note = "" } = {}) {
  const names = onlineNames();
  return `<p><strong>${names.length} player(s) online</strong>${names.length ? `: ${names.map(esc).join(", ")}.` : ". With nobody online the countdown is skipped."}</p>
  <div class="field"><span class="label">Countdown</span><div class="seg" id="cdSeg" role="group" aria-label="Countdown length">
    ${[[10, "10 s"], [60, "60 s"], [300, "5 min"]].map(([v, l]) => `<button type="button" data-v="${v}" aria-pressed="${v === 60}">${l}</button>`).join("")}
  </div></div>
  <div class="field"><span class="label">Players will see</span><div class="preview" id="cdPreview"></div></div>
  ${backup ? '<label class="check"><input type="checkbox" id="cdBackup" checked> Take a backup first (restic, about 20–40 s)</label>' : ""}
  ${note}`;
}

function wireSeg(d, label) {
  let secs = 60;
  const upd = () => {
    $("#cdPreview", d).textContent = `[Server] ${label} in ${secs >= 60 ? `${secs / 60} minute${secs > 60 ? "s" : ""}` : `${secs} seconds`}. Find a safe spot!`;
  };
  $("#cdSeg", d).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    secs = Number(b.dataset.v);
    $$("#cdSeg button", d).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    upd();
  });
  upd();
  return () => secs;
}

function restartDlg() {
  let get;
  confirmDlg({
    title: "Restart the server?",
    ok: "Start countdown",
    body: countdownBody("Restart", { backup: true, note: '<ul class="note"><li>systemd restarts the service; the world is usually back in about 40 s.</li><li>The watchdog is told (maintenance window) so it doesn\'t alert.</li></ul>' }),
    onOpen: (d) => (get = wireSeg(d, "Restart")),
    onOk: (d) => runAction("restart", { countdown: get(), backup: $("#cdBackup", d).checked }),
  });
}

function stopDlg() {
  let get;
  confirmDlg({
    title: "Stop the server?",
    ok: "Start countdown",
    danger: true,
    body: countdownBody("Stop", { backup: true, note: '<ul class="note"><li>The server stays down until you press Start: self-heal, the watchdog and deploys leave it stopped.</li><li>Confirmed in a separate window with a fresh one-time PIN sign-in.</li></ul>' }),
    onOpen: (d) => (get = wireSeg(d, "Server stopping")),
    onOk: (d) => runAction("stop", { countdown: get(), backup: $("#cdBackup", d).checked, reason: "stopped from the panel" }),
  });
}

function startDlg() {
  confirmDlg({
    title: "Start the server?",
    ok: "Start",
    body: '<p>systemd starts <code class="mono">minecraft.service</code> and the admin-stopped marker is removed (self-heal and the watchdog watch it again). Loading the world takes about 40 s.</p>',
    onOk: () => runAction("start", {}),
  });
}

function backupDlg() {
  confirmDlg({
    title: "Back up now?",
    ok: "Back up now",
    body: `<p>Runs <code class="mono">mc-backup.service</code> now instead of waiting for the timer${S.o?.server.backup_schedule ? ` (${esc(S.o.server.backup_schedule)})` : ""}.</p>
      <ul class="note"><li>Sends <code class="mono">save-off</code> and <code class="mono">save-all flush</code>, snapshots to R2 with restic, then <code class="mono">save-on</code>.</li><li>Players are not interrupted. Usually 15–40 s; the Backups tab lists it afterwards.</li></ul>`,
    onOk: () => runAction("backup", {}),
  });
}

// --- header ------------------------------------------------------------------------------------------------------

function serverState(o) {
  const s = o && o.sample;
  if (!s || o.sample_age_s > 180) return ["bad", "No data"];
  if (o.watchdog && o.watchdog.admin_stopped) return ["off", "Stopped"];
  if (s.up) return ["ok live", "Online"];
  return ["bad", "Down"];
}

function paintHeader() {
  const o = S.o;
  if (!o) return;
  const s = o.sample || {};
  const fresh = o.sample && o.sample_age_s <= 180;
  const title = `${o.server.title} · console`;
  if (document.title !== title) document.title = title;
  setText($("#title"), o.server.title);
  setText($("#host"), o.server.host || "");
  // v1.3: "Smallworld vX.Y.Z · Purpur 26.3" (the deployed product version, else the Worker's)
  const mc = o.server.version || [o.server.flavour, o.server.minecraft_version].filter(Boolean).join(" ");
  setText($("#version"), [o.server.product ? `Smallworld v${o.server.product}` : "", mc].filter(Boolean).join(" · "));
  const [cls, label] = serverState(o);
  setPill($("#statePill"), `pill ${cls}`, label);
  setText($("#uptime"), fresh && s.up ? fmtUp(s.uptime_s) : "–");
  setText($("#pcount"), fresh && num(s.players) ? String(s.players) : "–");
  setText($("#pmax"), num(s.max_players) ? `/${s.max_players}` : "");
  const ht = $("#hdrTps");
  tweenNum(ht, fresh && num(s.tps) ? s.tps : null, (v) => v.toFixed(1));
  setCls(ht, num(s.tps) ? tpsCls(s.tps) : null);
  const hm = $("#hdrMspt");
  tweenNum(hm, fresh && num(s.mspt) ? s.mspt : null, (v) => v.toFixed(1));
  setCls(hm, num(s.mspt) ? msptCls(s.mspt) : null);
  setHtml($("#hdrCpu"), num(s.cpu_pct) ? `${Math.round(s.cpu_pct)}<small>%</small>` : "–");
  // the mini bars scale (transform: composited) instead of changing width (layout)
  const cb = $("#hdrCpuBar");
  cb.style.transform = `scaleX(${clamp(s.cpu_pct || 0, 0, 100) / 100})`;
  cb.style.background = COLORS[pctCls(s.cpu_pct || 0)];
  setHtml($("#hdrRam"), num(s.mem_used_mb) ? `${gb(s.mem_used_mb)}<small>/${Math.round((s.mem_total_mb || 0) / 1024)} GB</small>` : "–");
  const rb = $("#hdrRamBar");
  const memPct = num(s.mem_pct) ? s.mem_pct : 0;
  rb.style.transform = `scaleX(${clamp(memPct, 0, 100) / 100})`;
  rb.style.background = COLORS[pctCls(memPct - 10)];
  if (o.me) setText($("#me"), o.me.email);
  const stopped = Boolean(o.watchdog && o.watchdog.admin_stopped);
  setText($("#btnStop span"), stopped ? "Start" : "Stop");
  const stopCls = `btn ${stopped ? "primary" : "danger"}`;
  if ($("#btnStop").className !== stopCls) $("#btnStop").className = stopCls;
  $("#btnRestart").disabled = stopped;
  if (o.job !== undefined && !JOB.fast) setJob(o.job, o.now);
}
function setCls(el, cls) {
  for (const c of ["good", "warn", "bad"]) el.classList.toggle(`c-${c}`, c === cls);
}
function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}
const HTML = new WeakMap();
function setHtml(el, html) {
  if (!el || HTML.get(el) === html) return;
  HTML.set(el, html);
  el.innerHTML = html;
}

// --- tabs --------------------------------------------------------------------------------------------------------

const TABS = [
  ["overview", "Overview"], ["console", "Console"], ["players", "Players"], ["experience", "Experience"], ["bans", "Bans"], ["plugins", "Plugins"],
  ["backups", "Backups"], ["worlds", "Worlds"], ["logs", "Logs"], ["health", "Health"], ["audit", "Audit log"],
];

function buildTabs() {
  $("#tablist").innerHTML = TABS.map(([id, label]) => `<button class="tab" role="tab" id="tab-${id}" aria-controls="main" aria-selected="${id === current}" tabindex="${id === current ? 0 : -1}" data-id="${id}">${label}<span class="count" data-c="${id}" hidden></span></button>`).join("");
}
function updateCounts() {
  const o = S.o;
  for (const el of $$(".tab .count")) {
    const id = el.dataset.c;
    let v = null;
    let alert = false;
    if (o && id === "players" && o.sample && num(o.sample.players)) v = o.sample.players;
    if (o && id === "players" && o.inventory && o.inventory.rejected) {
      v = `${o.inventory.rejected} req`;
      alert = true;
    }
    if (o && id === "bans" && o.inventory) v = o.inventory.bans || null;
    if (o && id === "overview") {
      const n = (o.warnings || []).filter((w) => w.level !== "info").length;
      if (n) {
        v = `${n} warn`;
        alert = true;
      }
    }
    if (id === "worlds" && S.inv && S.inv.inventory && S.inv.inventory.worlds && S.inv.inventory.worlds.archives.length) v = S.inv.inventory.worlds.archives.length;
    if (o && id === "plugins" && o.inventory && o.inventory.disabled_plugins) {
      v = `${o.inventory.disabled_plugins} off`;
      alert = true;
    }
    if (el.hidden !== (v === null)) el.hidden = v === null;
    setText(el, String(v ?? ""));
    el.classList.toggle("alert", alert);
  }
}
function go(id, focus = false) {
  if (!TABS.some((t) => t[0] === id)) id = "overview";
  if (current === "console" && id !== "console") stopConsole();
  current = id;
  for (const t of $$(".tab")) {
    const on = t.dataset.id === id;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
  }
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
  try {
    localStorage.setItem("mc-tab", id);
  } catch {
    // storage blocked: fine
  }
  render();
  if (focus) $(`#tab-${id}`).focus();
}

// --- views -------------------------------------------------------------------------------------------------------

// Null-prototype maps, read only with a tab id from TABS: no inherited names can be looked up or called.
const V = Object.create(null);
const LOADERS = Object.create(null);

function banners(o) {
  const ws = (o.warnings || []).map((w) => {
    const cls = w.level === "bad" ? "bad" : w.level === "info" ? "info" : "warn";
    return `<div class="banner ${cls}" role="note">${cls === "info" ? "" : WARN_ICON}<p>${esc(w.text)}${w.ts ? ` <span class="ago">(${esc(clock(w.ts, true))})</span>` : ""}</p></div>`;
  });
  return ws.length ? `<div class="banners">${ws.join("")}</div>` : "";
}

const TREND = {
  tps: { key: "tps", label: "TPS", unit: "", min: 15, max: 20, fmt: (v) => v.toFixed(1), note: "game updates per second; 20 = full speed", cls: tpsCls },
  mspt: { key: "mspt", label: "MSPT", unit: " ms", min: 0, max: 60, line: 45, fmt: (v) => v.toFixed(1), note: "tick time; 50 ms = full load, 45 ms = lag line", cls: msptCls },
  heap: { key: "heap_used_gb", label: "JVM heap", unit: " GiB", min: 0, fmt: (v) => v.toFixed(2), note: "G1 sawtooth = healthy", cls: () => "good" },
  cpu: { key: "cpu_pct", label: "CPU", unit: " %", min: 0, max: 100, fmt: (v) => String(Math.round(v)), note: "all cores", cls: pctCls },
};
function withHeapGb(points) {
  return points.map((p) => (num(p.heap_used_mb) ? { ...p, heap_used_gb: p.heap_used_mb / 1024 } : p));
}

function healthItems(o) {
  const s = o.sample || {};
  const w = o.watchdog || { checks: {}, probes: {} };
  const fresh = o.sample && o.sample_age_s <= 180;
  const heapPct = num(s.heap_used_mb) && s.heap_max_mb ? (100 * s.heap_used_mb) / s.heap_max_mb : null;
  const backup = w.checks.backup || {};
  const probes = Object.entries(w.probes || {});
  const probeOk = probes.length && probes.every(([, p]) => p.ok);
  return [
    ["Server process & RCON", !fresh ? "bad" : w.admin_stopped ? "off" : s.up ? "ok" : "bad", !fresh ? "No data" : w.admin_stopped ? "Stopped" : s.up ? "Healthy" : "Down", fresh ? `agent sample ${Number(o.sample_age_s) | 0} s old · uptime ${fmtUp(s.uptime_s)}` : "the agent hasn't reported for over 3 minutes"],
    ["Tick rate", num(s.tps) ? (tpsCls(s.tps) === "good" ? "ok" : tpsCls(s.tps)) : "off", num(s.tps) ? `${s.tps.toFixed(1)} TPS` : "–", num(s.mspt) ? `MSPT ${s.mspt.toFixed(1)} · server load ${Math.round((s.mspt / 50) * 100)}%` : "no reading"],
    ["Heap & GC", num(heapPct) ? (pctCls(heapPct) === "good" ? "ok" : pctCls(heapPct)) : "off", num(heapPct) ? `${Math.round(heapPct)}%` : "–", num(s.gc_max_pause_ms) ? `longest GC pause since the last sample ${s.gc_max_pause_ms} ms` : "no GC line yet"],
    ["Disk", num(s.disk_pct) ? (s.disk_pct < 85 ? "ok" : "bad") : "off", num(s.disk_pct) ? `${Math.round(s.disk_pct)}% used` : "–", num(s.disk_used_gb) ? `${s.disk_used_gb} of ${s.disk_total_gb} GiB · alert at 85%` : ""],
    ["CPU steal", num(s.steal_pct) ? (s.steal_pct < 5 ? "ok" : "warn") : "off", num(s.steal_pct) ? `${s.steal_pct.toFixed(1)}%` : "–", "below 5% is fine on a shared host"],
    ["Backups", backup.state === "ok" ? "ok" : backup.state === "unknown" || !backup.state ? "off" : "bad", backup.last_ok ? ago(isoMs(backup.last_ok)) : "none yet", `watchdog heartbeat: ${backup.state || "unknown"}${o.server.backup_schedule ? ` · schedule ${o.server.backup_schedule}` : ""}`],
    ["Watchdog (Cloudflare)", !probes.length ? "off" : probeOk ? "ok" : "bad", !probes.length ? "No targets" : probeOk ? "Reachable" : "Failing", probes.map(([f, p]) => `${f === "v4" ? "IPv4" : "IPv6"} ${p.ok ? "ok" : p.ok === false ? `failing (${p.failed_runs} runs)` : "not yet"}`).join(" · ") + (w.last_run_age_s !== null && w.last_run_age_s !== undefined ? ` · cron ${w.last_run_age_s} s ago` : "")],
    ["Agent link", fresh && s.ws ? "ok" : fresh ? "warn" : "bad", fresh && s.ws ? "WebSocket" : fresh ? "Polling" : "Silent", fresh ? (s.ws ? "commands arrive instantly" : "commands arrive within 15 s") : "no sample for over 3 minutes"],
  ];
}

V.overview = () => {
  const o = S.o;
  if (!o) return `<div class="empty">Loading…</div>`;
  const s = o.sample || {};
  const fresh = o.sample && o.sample_age_s <= 180;
  const players = fresh ? s.players_online || [] : [];
  const maxP = s.max_players || o.server.max_players || 20;
  const cores = Array.from({ length: Math.min(32, s.cpu_cores || 0) }, (_, i) => s[`cpu${i}_pct`]);
  const ops = new Set((o.inventory && o.inventory.op_names) || []);
  const hist = withHeapGb(o.history || []);
  const tps = hist.filter((p) => num(p.tps));
  const avgOf = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null);
  const last = (n) => tps.slice(-n).map((p) => p.tps);
  const md = hist.filter((p) => num(p.mspt)).map((p) => p.mspt);
  const sorted = [...md].sort((a, b) => a - b);
  const jvmPct = num(s.jvm_mem_mb) && s.mem_total_mb ? (100 * s.jvm_mem_mb) / s.mem_total_mb : 0;
  const otherPct = num(s.mem_used_mb) && s.mem_total_mb ? Math.max(0, (100 * (s.mem_used_mb - (s.jvm_mem_mb || 0))) / s.mem_total_mb) : 0;
  const heapPct = num(s.heap_used_mb) && s.heap_max_mb ? (100 * s.heap_used_mb) / s.heap_max_mb : 0;
  return `${banners(o)}
  <div class="hero">
    <section class="panel hcard glow-good" aria-labelledby="h-tps">
      <div class="row1"><h2 id="h-tps">Tick rate</h2><span class="pill ok plain" id="tpsPill" data-keep>–</span></div>
      <div class="gauge-wrap">${gauge("tps")}<div class="gside">
        <div class="kvs"><span class="muted">now</span><span>${fix(s.tps, 2)}</span><span class="muted">5 min</span><span>${fix(s.tps5 ?? avgOf(last(5)), 2)}</span><span class="muted">15 min</span><span>${fix(s.tps15 ?? avgOf(last(15)), 2)}</span></div>
        <div class="note">20 TPS is full speed. Players notice below about 18.</div></div></div>
    </section>
    <section class="panel hcard glow-acc" aria-labelledby="h-mspt">
      <div class="row1"><h2 id="h-mspt">Tick time</h2><span class="pill ok plain" id="msptPill" data-keep>–</span></div>
      <div class="gauge-wrap">${gauge("mspt")}<div class="gside">
        <div class="kvs"><span class="muted">median 2 h</span><span>${md.length ? sorted[Math.floor((md.length - 1) / 2)].toFixed(1) : "–"}</span><span class="muted">95th %ile</span><span>${md.length ? sorted[Math.floor((md.length - 1) * 0.95)].toFixed(1) : "–"}</span><span class="muted">max</span><span>${md.length ? sorted[md.length - 1].toFixed(1) : "–"}</span></div>
        <div class="thr" aria-hidden="true"><i class="g"></i><i class="w"></i><i class="b"></i></div>
        <div class="note">Good under 35 ms · tight 35–45 · lag above 45 (budget 50).</div></div></div>
    </section>
    <section class="panel hcard glow-grass" aria-labelledby="h-pc">
      <div class="row1"><h2 id="h-pc">Players</h2><button class="btn sm" type="button" data-go="players">Details</button></div>
      <div class="players-big">${fresh && num(s.players) ? s.players : "–"}<small> / ${maxP}</small></div>
      <div class="avatars">${players.map((n) => `<div class="av">${headSvg(n)}<span>${esc(n)}</span>${ops.has(n) ? '<span class="tag op">OP</span>' : ""}</div>`).join("")}<div class="av slot"><div class="ph" aria-hidden="true"></div><span>${Math.max(0, maxP - players.length)} free</span></div></div>
    </section>
  </div>

  <div class="vitals" aria-label="System vitals">
    <section class="panel tile" aria-labelledby="h-cpu">
      <div class="row1"><h2 id="h-cpu">CPU</h2><span class="mchip${s.steal_pct > 2 ? " c-warn" : ""}" id="steal">steal ${fix(s.steal_pct)}%</span></div>
      <div class="gauge-wrap">${gauge("cpu", true)}
        <div class="cores flex1${cores.length > 8 ? " many" : ""}" aria-label="Per-core usage">${cores.map((c, i) => `<div class="core"><div class="col"><i data-h="${num(c) ? c : 0}" data-bg="${pctCls(c || 0)}"></i></div><span>vCPU${i}</span><span>${num(c) ? Math.round(c) : "–"}%</span></div>`).join("") || '<span class="note">per-core data after the next sample</span>'}</div></div>
      <div class="chiprow"><span class="mchip">load ${fix(s.load1, 2)} · ${fix(s.load5, 2)} · ${fix(s.load15, 2)}</span><span class="mchip">${s.cpu_cores || "?"} vCPU</span></div>
    </section>
    <section class="panel tile" aria-labelledby="h-mem">
      <div class="row1"><h2 id="h-mem">Memory</h2><span class="bignum">${gb(s.mem_used_mb)}<small> / ${num(s.mem_total_mb) ? Math.round(s.mem_total_mb / 1024) : "–"} GB</small></span></div>
      <div class="meter"><div class="mtop"><span>System</span><span>${gb(s.mem_used_mb)} / ${gb(s.mem_total_mb)} GB</span></div>
        <div class="seg-bar" role="img" aria-label="System memory: Minecraft process ${Math.round(jvmPct)}%, other ${Math.round(otherPct)}%"><i class="memjvm" data-w="${jvmPct}"></i><i class="memother" data-w="${otherPct}"></i></div>
        <div class="chiprow note"><span><span class="legend-acc">■</span> Minecraft (Java) ${gb(s.jvm_mem_mb)} GB</span><span><span class="legend-mut">■</span> OS, agent, Tailscale</span></div></div>
      <div class="meter"><div class="mtop"><span>JVM heap</span><span>${num(s.heap_used_mb) ? (s.heap_used_mb / 1024).toFixed(2) : "–"} / ${num(s.heap_max_mb) ? (s.heap_max_mb / 1024).toFixed(1) : "–"} GiB</span></div><div class="seg-bar"><i data-w="${heapPct}" data-bg="${pctCls(heapPct)}"></i></div></div>
      <div class="chiprow"><span class="mchip">longest GC pause ${num(s.gc_max_pause_ms) ? `${s.gc_max_pause_ms} ms` : "–"}</span></div>
    </section>
    <section class="panel tile" aria-labelledby="h-disk">
      <div class="row1"><h2 id="h-disk">Disk</h2><span class="mchip">server volume</span></div>
      <div class="gauge-wrap">${gauge("disk", true)}<div class="gside flex1"><div class="kvs">
        <span class="muted">used</span><span>${num(s.disk_used_gb) ? `${s.disk_used_gb} / ${s.disk_total_gb} GiB` : "–"}</span><span class="muted">free</span><span>${num(s.disk_used_gb) ? `${(s.disk_total_gb - s.disk_used_gb).toFixed(1)} GiB` : "–"}</span><span class="muted">alert at</span><span>85%</span></div></div></div>
    </section>
    <section class="panel tile" aria-labelledby="h-net">
      <div class="row1"><h2 id="h-net">Network</h2><span class="mchip">all interfaces</span></div>
      <div class="net"><div><span class="label lbl-in">▼ In</span><div class="bignum">${kbps(s.net_rx_bps)}<small> kB/s</small></div></div>
        <div><span class="label lbl-out">▲ Out</span><div class="bignum">${kbps(s.net_tx_bps)}<small> kB/s</small></div></div></div>
      ${spark(hist.map((p) => ({ ...p, tx: num(p.net_tx_bps) ? p.net_tx_bps / 1000 : null, rx: num(p.net_rx_bps) ? p.net_rx_bps / 1000 : null })), { key: "tx", label: "Out", unit: " kB/s", fmt: (v) => String(Math.round(v)) }, "var(--accent)", { key: "rx", label: "In" })}
    </section>
  </div>

  ${experienceCard(o)}

  <section class="panel" aria-labelledby="h-perf">
    <header><h2 id="h-perf">Live trends</h2><button class="btn sm" type="button" data-go="health">History ↗</button></header>
    <div class="metrics">${Object.entries(TREND).map(([k, m]) => {
      const pts = hist.filter((p) => num(p[m.key]));
      const lastV = pts.length ? pts[pts.length - 1][m.key] : null;
      const c = num(lastV) ? m.cls(lastV) : "good";
      return `<div class="metric"><div class="top"><span class="label">${m.label}</span><span class="val c-${c}">${num(lastV) ? m.fmt(lastV) : "–"}<span class="muted unit-sm">${m.unit}</span></span></div>${spark(hist, m, COLORS[c])}<div class="axis"><span>−2 h</span><span>${m.note}</span><span>now</span></div></div>`;
    }).join("")}</div>
  </section>

  <div class="grid2">
    <section class="panel" aria-labelledby="h-health">
      <header><h2 id="h-health">Health checks</h2><span class="note">agent every minute · watchdog every 5 min</span></header>
      <ul class="health">${healthItems(o).map(([n, c, v, d]) => `<li><span>${esc(n)}</span>${pill(c, v)}<span class="d">${esc(d)}</span></li>`).join("")}</ul>
    </section>
    <section class="panel" aria-labelledby="h-recent">
      <header><h2 id="h-recent">Recent actions</h2><button class="btn sm" type="button" data-go="audit">Audit log</button></header>
      <div class="body stack feed">${(o.audit || []).map((a) => `<div data-k="${esc(`${a.ts}:${a.source}:${a.action || a.unit || ""}`)}"><span class="mono muted">${esc(clock(a.ts, true))}</span> · ${auditLine(a)}</div>`).join("") || '<span class="muted">Nothing yet.</span>'}</div>
    </section>
  </div>`;
};

// A readable summary of an audited panel action's arguments (P10), e.g. "add Felix_2011", "countdown 60 s".
function actionDetail(action, raw) {
  let a;
  try {
    a = JSON.parse(raw);
  } catch {
    return raw;
  }
  if (!a || typeof a !== "object") return raw;
  const cd = num(a.countdown) ? (a.countdown ? `countdown ${a.countdown} s` : "no countdown") : "";
  switch (action) {
    case "console":
      return a.command ?? raw;
    case "restart":
    case "stop":
      return [cd, a.backup ? "backup first" : "", a.reason && a.reason !== "stopped from the panel" ? a.reason : ""].filter(Boolean).join(", ");
    case "whitelist":
      return `${a.op} ${a.name}`;
    case "rejected_dismiss":
      return `dismiss ${a.name}`;
    case "moderate":
      return `${a.verb} ${a.target}${a.reason ? `: ${a.reason}` : ""}`;
    case "plugin":
      return `${a.name} ${a.enabled ? "on" : "off"}, ${cd}`;
    case "restore":
      return `snapshot ${String(a.snapshot || "").slice(0, 8)}, ${cd}`;
    case "world_create":
      return `${a.name}: ${a.seed ? `seed ${a.seed}` : "random seed"}, ${a.type}, ${a.preset}, ${cd}`;
    case "world_switch":
      return `to ${a.name}, ${cd}`;
    case "world_delete":
      return `${a.name}`;
    default:
      return Object.keys(a).length ? raw : "";
  }
}

function auditLine(a) {
  if (a.source === "panel") {
    const detail = actionDetail(a.action, a.args);
    return `<strong>${esc(a.action)}</strong> <span class="muted">${esc(detail)}</span> <span class="ago">${esc(a.login)}</span>`;
  }
  return `<strong>${esc(a.unit.replace(/\.service$/, ""))}</strong> <span class="muted">${esc(a.msg)}</span>`;
}

// the overview's pills carry data-keep (painted here, not by the patch): written only when they change
function setPill(el, cls, text) {
  if (el.className !== cls) el.className = cls;
  if (el.textContent !== text) el.textContent = text;
}

function paintOverview() {
  const o = S.o;
  if (!o || !$("#g-tps")) return;
  const s = o.sample || {};
  const fresh = o.sample && o.sample_age_s <= 180;
  const on = fresh && s.up;
  const f1 = (v) => v.toFixed(1);
  const pct = (v) => `${Math.round(v)}%`;
  if (on && num(s.tps)) setGauge("tps", (s.tps - 15) / 5, tpsCls(s.tps), s.tps, f1, "ticks/s", `TPS ${s.tps.toFixed(1)} of 20`);
  else setGauge("tps", 0, "warn", null, f1, on ? "no reading" : "offline", "No tick rate");
  if (on && num(s.mspt)) setGauge("mspt", s.mspt / 60, msptCls(s.mspt), s.mspt, f1, "ms / tick", `MSPT ${s.mspt.toFixed(1)} milliseconds`);
  else setGauge("mspt", 0, "warn", null, f1, on ? "no reading" : "offline", "No tick time");
  const tc = on && num(s.tps) ? tpsCls(s.tps) : "warn";
  setPill($("#tpsPill"), `pill plain ${tc === "good" ? "ok" : tc}`, !on ? "Offline" : tc === "good" ? "Smooth" : tc === "warn" ? "Slowing" : "Lagging");
  const mc = on && num(s.mspt) ? msptCls(s.mspt) : "warn";
  setPill($("#msptPill"), `pill plain ${mc === "good" ? "ok" : mc}`, !on ? "Offline" : mc === "good" ? "Headroom" : mc === "warn" ? "Tight" : "Over budget");
  if (num(s.cpu_pct)) setGauge("cpu", s.cpu_pct / 100, pctCls(s.cpu_pct), s.cpu_pct, pct, "avg", `CPU ${Math.round(s.cpu_pct)} percent`);
  if (num(s.disk_pct)) setGauge("disk", s.disk_pct / 100, pctCls(s.disk_pct), s.disk_pct, pct, "used", `Disk ${Math.round(s.disk_pct)} percent used`);
}

// --- console -----------------------------------------------------------------------------------------------------

const CON = { lines: [], after: 0, timer: null, renew: null, live: false, filter: "all", query: "", hist: [], hIdx: 0, pending: [], full: false, shown: 0 };
const CHUNK = 60; // console lines per chunk (each chunk is skipped by the browser while out of view)
const KINDS = { all: "All", INFO: "Info", WARN: "Warn", ERROR: "Error", CHAT: "Chat", JOIN: "Joins", CMD: "Commands" };
const CMDS = [
  ["list", "Who is online"], ["say <message>", "Broadcast to chat"], ["tell <player> <message>", "Private message"],
  ["kick <player> [reason]", "Disconnect a player"], ["ban <player> [reason]", "Ban by account"], ["ban-ip <ip|player> [reason]", "Ban by IP"],
  ["pardon <player>", "Unban account"], ["pardon-ip <ip>", "Unban IP"], ["banlist", "Show bans"], ["whitelist list", "Show whitelist"],
  ["save-all flush", "Write world to disk"], ["time set <day|night|noon>", "Set time"], ["time query daytime", "Current time"],
  ["weather <clear|rain|thunder>", "Set weather"], ["difficulty", "Current difficulty"], ["gamerule <rule>", "Read a gamerule"],
  ["tps", "Paper tick rate"], ["mspt", "Paper tick times"], ["spark health", "CPU, memory, GC summary"], ["spark tps", "spark tick rate"],
  ["chunky progress", "Pregen progress"], ["chunky pause", "Pause pregen"], ["chunky continue", "Resume pregen"],
  ["worldborder get", "Current border size"], ["seed", "World seed"], ["plugins", "Loaded plugins"],
];

function classify(msg, prio) {
  const m = /^\[(\d\d:\d\d:\d\d) (INFO|WARN|ERROR|FATAL)\]:? ?(.*)$/s.exec(msg);
  const text = m ? m[3] : msg;
  let lv = m ? (m[2] === "FATAL" ? "ERROR" : m[2]) : prio <= 3 ? "ERROR" : prio === 4 ? "WARN" : "INFO";
  if (!m && prio <= 3) lv = "ERROR";
  let kind = lv;
  if (/^<[^>]{1,16}> |^\[Server\] |^\[Not Secure\] </.test(text)) kind = "CHAT";
  else if (/ (joined|left) the game$| logged in with entity id | lost connection: |not white-listed/.test(text)) kind = "JOIN";
  return { lv, kind, text };
}
function addLines(rows, source) {
  for (const r of rows) {
    const c = classify(r.msg, r.prio ?? 6);
    const l = { t: clock(r.ts), ...c, src: source };
    CON.lines.push(l);
    CON.pending.push(l);
  }
  if (CON.lines.length > 1500) CON.lines.splice(0, CON.lines.length - 1500);
}
function conMatches(l) {
  if (CON.filter !== "all" && l.kind !== CON.filter && !(CON.filter === l.lv && l.kind !== "CHAT")) return false;
  return !CON.query || l.text.toLowerCase().includes(CON.query);
}
function lineHtml(l, enter = false) {
  return `<div class="ln ${l.lv} ${l.kind}${enter ? " enter" : ""}"><span class="t">[${esc(l.t)}]</span> <span class="lv">${l.kind === "CMD" ? "&gt;" : l.kind === "SYS" ? "·" : esc(l.lv)}</span> <span class="msg">${esc(l.text)}</span></div>`;
}
// Console updates are batched into one frame: new lines are appended to the last chunk (nothing already shown is
// rebuilt); a filter or search change redraws it once. At most ~1500 lines stay in the DOM.
function drawConsole(full = true) {
  if (full) CON.full = true;
  onFrame("console", paintConsole);
}
function paintConsole() {
  const c = $("#console");
  const full = CON.full;
  CON.full = false;
  const add = CON.pending;
  CON.pending = [];
  if (!c) return;
  const stick = !c.dataset.drawn || c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
  if (full || !c.querySelector(".chunk")) {
    const lines = CON.lines.filter(conMatches);
    const chunks = [];
    for (let i = 0; i < lines.length; i += CHUNK) chunks.push(`<div class="chunk">${lines.slice(i, i + CHUNK).map((l) => lineHtml(l)).join("")}</div>`);
    c.innerHTML = chunks.join("") || '<div class="ln t">No lines match this filter.</div>';
    CON.shown = lines.length;
  } else {
    const fresh = add.filter(conMatches);
    const animate = fresh.length <= 12 && !calm();
    let chunk = c.lastElementChild;
    for (const l of fresh) {
      if (chunk.childElementCount >= CHUNK) {
        chunk = document.createElement("div");
        chunk.className = "chunk";
        c.append(chunk);
      }
      chunk.insertAdjacentHTML("beforeend", lineHtml(l, animate));
    }
    CON.shown += fresh.length;
    while (CON.shown > 1500 + CHUNK && c.firstElementChild !== chunk) {
      CON.shown -= c.firstElementChild.childElementCount;
      c.firstElementChild.remove();
    }
  }
  if (stick) c.scrollTop = c.scrollHeight;
  c.dataset.drawn = "1";
}
function sysLine(text, kind = "SYS", lv = "INFO") {
  const l = { t: clock(Date.now()), lv, kind, text, src: "ui" };
  CON.lines.push(l);
  CON.pending.push(l);
  drawConsole(false);
}

V.console = () => `
  <section class="panel" aria-labelledby="h-con">
    <header><h2 id="h-con">Console <span class="pill off livebadge" id="liveBadge" data-keep>connecting</span></h2>
      <div class="filters" role="group" aria-label="Filter log lines">
        ${Object.entries(KINDS).map(([k, l]) => `<button type="button" class="chip" data-f="${k}" aria-pressed="${k === CON.filter}">${l}</button>`).join("")}
        <input class="search" id="logSearch" type="search" placeholder="Search console" value="${esc(CON.query)}" aria-label="Search console">
      </div></header>
    <div class="console" id="console" data-keep role="log" aria-live="polite" tabindex="0" aria-label="Server console output"></div>
    <form class="cmdbar" id="cmdForm" autocomplete="off">
      <div class="prompt"><span aria-hidden="true">&gt;</span><input id="cmdInput" aria-label="Command (sent via RCON)" placeholder="Type a command, e.g. list or say hello" aria-autocomplete="list" aria-controls="suggest" maxlength="256"></div>
      <button class="btn primary" type="submit">Send</button>
      <ul class="suggest" id="suggest" data-keep role="listbox" hidden></ul>
    </form>
    <div class="hint"><kbd>↑</kbd><kbd>↓</kbd> history · <kbd>Tab</kbd> complete · commands are signed by the panel, checked against the agent's console allowlist and written to the audit log. <code class="mono">stop</code>, <code class="mono">reload</code>, <code class="mono">op</code> and <code class="mono">execute</code> are refused (use the buttons: they warn players first); whitelist changes go through the Players tab.</div>
  </section>`;

async function startConsole() {
  CON.lines = [];
  CON.pending = [];
  CON.after = 0;
  drawConsole();
  try {
    const first = await api("/api/console");
    addLines(first.backlog || [], "d1");
    if ((first.backlog || []).length) sysLine("Lines above are from the stored log (up to 5 minutes behind). Live lines follow.");
    CON.after = Number(first.next) || 0;
    addLines(first.lines || [], "live");
    drawConsole();
  } catch (e) {
    sysLine(`Couldn't load the console: ${e.message}`, "SYS", "WARN");
  }
  await subscribe();
  CON.renew = setInterval(() => {
    if (!document.hidden) subscribe();
  }, LIVE_RENEW_MS);
  CON.timer = setInterval(pollConsole, CONSOLE_POLL_MS);
}
async function subscribe() {
  try {
    const r = await api("/api/console/live", { method: "POST", body: {} });
    setLive(Boolean(r.live), r.reason);
  } catch (e) {
    setLive(false, e.message);
  }
}
function setLive(live, reason) {
  CON.live = live;
  const b = $("#liveBadge");
  if (!b) return;
  b.className = `pill livebadge ${live ? "ok live" : "off"}`;
  b.textContent = live ? "live" : "stored log";
  b.title = live ? "Streaming from the server" : `Not live${reason ? `: ${reason}` : ""}; showing the stored log`;
}
async function pollConsole() {
  if (document.hidden) return;
  try {
    const r = await api(`/api/console?after=${Number(CON.after) || 0}`);
    if (r.lines && r.lines.length) {
      addLines(r.lines, "live");
      drawConsole(false);
    }
    CON.after = Number(r.next) || CON.after;
    if (r.live !== CON.live) setLive(r.live);
  } catch {
    // shown by the badge on the next subscribe
  }
}
function stopConsole() {
  clearInterval(CON.timer);
  clearInterval(CON.renew);
  CON.timer = CON.renew = null;
}
async function runCommand(raw) {
  const cmd = raw.trim().replace(/^\/+/, "");
  if (!cmd) return;
  CON.hist.push(cmd);
  CON.hIdx = CON.hist.length;
  sysLine(cmd, "CMD", "INFO");
  try {
    const q = await api("/api/console/command", { method: "POST", body: { command: cmd } });
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, i < 5 ? 400 : 1000));
      const { result } = await api(`/api/commands/${q.id}`);
      if (result) {
        if (result.ok) for (const line of String(result.output || "(no output)").split("\n")) sysLine(line, "RCON", "INFO");
        else sysLine(result.error || "refused", "SYS", "WARN");
        loadAuditSoon();
        return;
      }
    }
    sysLine("No answer from the server's agent within 30 s (it may be offline). The command expires unrun.", "SYS", "WARN");
  } catch (e) {
    sysLine(`Not sent: ${e.message}`, "SYS", "WARN");
  }
}
function loadAuditSoon() {
  setTimeout(() => refresh().catch(() => {}), 500);
}

function wireConsole() {
  startConsole();
  const inp = $("#cmdInput");
  const sug = $("#suggest");
  let sel = -1;
  let items = [];
  const showSug = () => {
    const v = inp.value.trim().toLowerCase();
    items = v ? CMDS.filter(([c]) => c.startsWith(v) || (c.split(" ")[0].startsWith(v.split(" ")[0]) && v.split(" ").length === 1)).slice(0, 7) : [];
    if (!items.length || CMDS.some(([c]) => c === v)) {
      sug.hidden = true;
      inp.removeAttribute("aria-activedescendant");
      return;
    }
    sel = -1;
    sug.innerHTML = items.map(([c, h], i) => `<li role="option" id="sg${i}" data-i="${i}" aria-selected="false"><code>${esc(c)}</code><span class="h">${esc(h)}</span></li>`).join("");
    sug.hidden = false;
  };
  const pick = (i) => {
    const c = items[i][0];
    inp.value = c.split(" <")[0].split(" [")[0] + (c.includes("<") || c.includes("[") ? " " : "");
    sug.hidden = true;
    inp.focus();
  };
  inp.addEventListener("input", showSug);
  inp.addEventListener("keydown", (e) => {
    if (!sug.hidden && items.length) {
      if (e.key === "Tab") {
        e.preventDefault();
        pick(Math.max(sel, 0));
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        sel = (sel + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
        $$("li", sug).forEach((li, i) => li.setAttribute("aria-selected", String(i === sel)));
        inp.setAttribute("aria-activedescendant", `sg${sel}`);
        return;
      }
      if (e.key === "Enter" && sel >= 0) {
        e.preventDefault();
        pick(sel);
        return;
      }
      if (e.key === "Escape") {
        sug.hidden = true;
        return;
      }
    }
    if (e.key === "ArrowUp" && CON.hIdx > 0) {
      e.preventDefault();
      inp.value = CON.hist[--CON.hIdx];
    } else if (e.key === "ArrowDown" && CON.hist.length) {
      e.preventDefault();
      CON.hIdx = Math.min(CON.hist.length, CON.hIdx + 1);
      inp.value = CON.hist[CON.hIdx] ?? "";
    }
  });
  sug.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (li) {
      e.preventDefault();
      pick(Number(li.dataset.i));
    }
  });
  $("#cmdForm").addEventListener("submit", (e) => {
    e.preventDefault();
    sug.hidden = true;
    runCommand(inp.value);
    inp.value = "";
  });
  $("#logSearch").addEventListener("input", (e) => {
    CON.query = e.target.value.toLowerCase();
    drawConsole();
  });
}

// --- players, bans, plugins (the agent's inventory, every 10 min) ----------------------------------------------

LOADERS.players = LOADERS.bans = LOADERS.plugins = LOADERS.worlds = async () => {
  S.inv = await api("/api/inventory");
};
const invAge = () => (S.inv && S.inv.inventory ? `snapshot ${ago(S.inv.inventory.received || S.inv.inventory.ts)}` : "no snapshot from the agent yet");
const EMPTY_INV = { whitelist: [], ops: [], bans: [], ip_bans: [], plugins: [], pending: [], rejected: [], plugins_lock: [], plugins_disabled: [], whitelist_sync: null };
const inventory = () => ({ ...EMPTY_INV, ...((S.inv && S.inv.inventory) || {}) });

function pendingPanel(inv) {
  const sync = inv.whitelist_sync || {};
  const pr = sync.pr && sync.pr.number ? (sync.pr.url ? `pull request <a href="${esc(sync.pr.url)}" rel="noreferrer" target="_blank">#${esc(sync.pr.number)}</a>` : `pull request #${esc(sync.pr.number)}`) : "";
  const rows = inv.pending.map((e) => `<li>${e.op === "add" ? pill("ok", "add") : pill("bad", "remove")} <strong>${esc(e.name)}</strong> <span class="note">${esc(ago(e.ts * (e.ts < 1e12 ? 1000 : 1)))} · ${esc(e.login || "")}${e.pr > 0 ? ` · PR #${esc(e.pr)}` : e.pr < 0 ? " · PR closed unmerged" : " · waiting to be batched"}</span></li>`);
  const status = !sync.configured
    ? "No panel GitHub token on the server: changes stay live and pending (deploys keep them) until they expire after 24 h. Create the token (TF_VAR_panel_github_token) and apply."
    : sync.error
      ? `Last attempt failed: ${sync.error}`
      : pr ? "" : "Changes are batched for a minute, then committed to players.yml in a pull request that merges by itself.";
  return `<section class="panel" aria-labelledby="h-pend">
    <header><h2 id="h-pend">Whitelist → Git</h2><span class="note">${inv.pending.length} pending${pr ? ` · ${pr}` : ""}</span></header>
    <div class="body stack">${rows.length ? `<ul class="pendlist">${rows.join("")}</ul>` : '<span class="muted">Everything is in players.yml.</span>'}
      ${status ? `<p class="note p0">${esc(status)}</p>` : ""}</div>
  </section>`;
}

V.players = () => {
  if (!S.inv) return '<div class="empty">Loading…</div>';
  const inv = inventory();
  const online = new Set(S.inv.online || []);
  const ops = new Map(inv.ops.map((o) => [o.name, o.level]));
  const known = new Set(inv.whitelist.map((w) => w.name.toLowerCase()));
  const pendingNames = new Set(inv.pending.map((e) => e.name.toLowerCase()));
  const onlineRows = [...online].map((n) => `<tr><td><div class="player">${headSvg(n)}<span>${esc(n)}</span>${ops.has(n) ? '<span class="tag op">OP</span>' : ""}</div></td><td>${pill("ok", "Online")}</td><td class="hide-sm muted">${known.has(n.toLowerCase()) ? "whitelisted" : "not on the whitelist snapshot"}</td><td class="actions-cell"><button class="btn sm" type="button" data-act="kick" data-name="${esc(n)}">Kick</button><button class="btn sm danger" type="button" data-act="ban" data-name="${esc(n)}">Ban</button></td></tr>`);
  const rejected = inv.rejected.filter((r) => !known.has(r.name.toLowerCase()));
  return `
  <section class="panel" aria-labelledby="h-pl">
    <header><h2 id="h-pl">Online players</h2><span class="note">${online.size} online · from the latest sample</span></header>
    <div class="tablewrap"><table><thead><tr><th>Player</th><th>Status</th><th class="hide-sm">Whitelist</th><th><span class="sr">Actions</span></th></tr></thead><tbody>
      ${onlineRows.join("") || emptyRow(4, "Nobody online.")}
    </tbody></table></div>
  </section>
  <div class="grid2">
    <section class="panel" aria-labelledby="h-wadd">
      <header><h2 id="h-wadd">Add a player</h2>${pill("ok plain", "Whitelist → Git")}</header>
      <form class="body" id="wlForm" autocomplete="off">
        <div class="row"><div class="field flex1"><label class="label" for="wlName">Java username</label><input class="input mono" id="wlName" placeholder="e.g. Felix_2011" maxlength="16" pattern="[A-Za-z0-9_]{3,16}"></div><button class="btn primary" type="submit" id="wlAdd" disabled>Add to whitelist</button></div>
        <div class="lookup" id="wlLookup" aria-live="polite">Names are checked against Mojang so typos don't end up on the list.</div>
        <p class="note p0">Applies live (<code class="mono">whitelist add</code>), then lands in <code class="mono">players.yml</code> through a pull request that merges by itself.</p>
      </form>
    </section>
    <section class="panel" aria-labelledby="h-req">
      <header><h2 id="h-req">Rejected logins</h2><span class="note">not whitelisted · last ${esc(inv.rejected.length)}</span></header>
      <div class="body reqs">${rejected.map((r) => `<div class="player">${headSvg(r.name)}<div class="who"><div><strong>${esc(r.name)}</strong> <span class="tag">${esc(r.count)}×</span></div><div class="note">last ${esc(ago(r.last))}${r.ip ? ` · from ${esc(r.ip)}` : ""}${r.uuid ? "" : " · UUID looked up on approve"}</div></div><button class="btn sm primary" type="button" data-req="ok" data-name="${esc(r.name)}">Whitelist</button><button class="btn sm" type="button" data-req="no" data-name="${esc(r.name)}">Dismiss</button></div>`).join("") || '<span class="muted">Nobody was turned away recently.</span>'}</div>
    </section>
  </div>
  ${inv.pending.length || (inv.whitelist_sync && (inv.whitelist_sync.error || !inv.whitelist_sync.configured)) ? pendingPanel(inv) : ""}
  <section class="panel" aria-labelledby="h-wl">
    <header><h2 id="h-wl">Whitelisted players</h2><span class="note">${inv.whitelist.length} players · <code class="mono">whitelist.json</code> · ${esc(invAge())}</span></header>
    <div class="tablewrap"><table><thead><tr><th>Player</th><th class="hide-sm">UUID</th><th>Status</th><th><span class="sr">Actions</span></th></tr></thead><tbody>
      ${inv.whitelist.map((w) => `<tr><td><div class="player">${headSvg(w.name)}${esc(w.name)}${ops.has(w.name) ? '<span class="tag op">OP</span>' : ""}${pendingNames.has(w.name.toLowerCase()) ? ' <span class="tag">not in Git yet</span>' : w.unmanaged ? ' <span class="tag" title="Kept by the last deploy although players.yml doesn\'t list this player: mcctl players add NAME">not in Git</span>' : ""}</div></td><td class="mono hide-sm muted small">${esc(w.uuid || "")}</td><td>${online.has(w.name) ? pill("ok", "Online") : pill("off", "Offline")}</td><td class="actions-cell">${ops.has(w.name) ? '<span class="note">op (Git)</span>' : `<button class="btn sm danger" type="button" data-wrm="${esc(w.name)}">Remove</button>`}</td></tr>`).join("") || emptyRow(4, "The whitelist is empty.")}
    </tbody></table></div>
  </section>
  <section class="panel" aria-labelledby="h-ops">
    <header><h2 id="h-ops">Operators</h2><span class="note">from <code class="mono">players.yml</code> in Git</span></header>
    <div class="body stack">${inv.ops.map((o) => `<div class="player">${headSvg(o.name)}<strong>${esc(o.name)}</strong><span class="tag op">level ${esc(o.level ?? "?")}</span></div>`).join("") || '<span class="muted">No operators.</span>'}
    <p class="note p0">The console refuses <code class="mono">op</code>/<code class="mono">deop</code> and the panel never changes operators: change them with a PR.</p></div>
  </section>`;
};

function wirePlayers() {
  const form = $("#wlForm");
  if (!once(form)) return;
  const inp = $("#wlName");
  const out = $("#wlLookup");
  const btn = $("#wlAdd");
  let timer;
  let found = null;
  let unverified = false;
  inp.addEventListener("input", () => {
    clearTimeout(timer);
    found = null;
    btn.disabled = true;
    const v = inp.value.trim();
    const inv = inventory();
    if (!v) {
      out.className = "lookup";
      out.textContent = "Names are checked against Mojang so typos don't end up on the list.";
      return;
    }
    if (!/^[A-Za-z0-9_]{3,16}$/.test(v)) {
      out.className = "lookup bad";
      out.textContent = "Java names are 3–16 characters: letters, digits and _.";
      return;
    }
    if (inv.whitelist.some((w) => w.name.toLowerCase() === v.toLowerCase())) {
      out.className = "lookup bad";
      out.textContent = `${v} is already whitelisted.`;
      return;
    }
    out.className = "lookup";
    out.textContent = "Looking up on Mojang…";
    timer = setTimeout(async () => {
      try {
        const r = await api(`/api/mojang?name=${encodeURIComponent(v)}`);
        if (inp.value.trim() !== v) return;
        if (r.found) {
          found = r.name;
          unverified = false;
          out.className = "lookup ok";
          out.textContent = `✓ Found Java account ${r.name} · UUID ${r.uuid}${r.via === "server" ? " · checked by the server" : ""}`;
          btn.disabled = false;
        } else if (r.unverified) {
          // Mojang didn't answer the Worker (it refuses Cloudflare's addresses at times): the server checks the
          // name itself when it adds it, as Discord's /whitelist add does.
          found = v;
          unverified = true;
          out.className = "lookup";
          out.textContent = `Couldn't check "${v}" with Mojang from here. You can still add it: the server looks the name up itself and refuses one that doesn't exist.`;
          btn.disabled = false;
        } else {
          out.className = "lookup bad";
          out.textContent = `No Java account named "${v}". Check the spelling, or it may be a Bedrock name.`;
        }
      } catch (e) {
        out.className = "lookup bad";
        out.textContent = `Couldn't check the name (${e.message}).`;
      }
    }, 400);
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!found) return;
    const name = found;
    confirmDlg({
      title: `Whitelist ${name}?`,
      ok: "Add to whitelist",
      body: `<p>${esc(name)} can join right away (<code class="mono">whitelist add ${esc(name)}</code>). The change is recorded in <code class="mono">players.yml</code> through a pull request that merges by itself; until then deploys keep it.</p>${unverified ? '<p class="note">Not checked with Mojang: the server refuses the add if no Java account has this name.</p>' : ""}`,
      onOk: async () => {
        inp.value = "";
        btn.disabled = true;
        out.className = "lookup";
        out.textContent = "";
        await runAction("whitelist", { op: "add", name }, `Whitelist add ${name}`);
      },
    });
  });
}

V.bans = () => {
  if (!S.inv) return '<div class="empty">Loading…</div>';
  const inv = inventory();
  const rows = [
    ...inv.bans.map((b) => ({ target: b.name, type: "player", reason: b.reason, when: b.created, by: b.source, expires: b.expires })),
    ...inv.ip_bans.map((b) => ({ target: b.ip, type: "ip", reason: b.reason, when: b.created, by: b.source, expires: b.expires })),
  ];
  return `
  <section class="panel" aria-labelledby="h-bans">
    <header><h2 id="h-bans">Banned players</h2><button class="btn sm danger" type="button" id="banNew">Ban a player or IP…</button></header>
    <div class="tablewrap"><table><thead><tr><th>Target</th><th>Type</th><th>Reason</th><th class="hide-sm">Banned</th><th class="hide-sm">Expires</th><th><span class="sr">Actions</span></th></tr></thead><tbody>
      ${rows.map((b) => `<tr><td class="${b.type === "ip" ? "mono" : ""}">${b.type === "player" ? `<div class="player">${headSvg(b.target || "?")}${esc(b.target)}</div>` : esc(b.target)}</td><td><span class="tag">${b.type === "ip" ? "IP" : "Account"}</span></td><td>${esc(b.reason || "")}</td><td class="hide-sm muted">${esc((b.when || "").slice(0, 16))}${b.by ? ` by ${esc(b.by)}` : ""}</td><td class="hide-sm muted">${esc(b.expires || "")}</td><td class="actions-cell"><button class="btn sm" type="button" data-pardon="${esc(b.target || "")}" data-kind="${b.type === "ip" ? "ip" : "player"}">Pardon</button></td></tr>`).join("") || emptyRow(6, "No bans.")}
    </tbody></table></div>
    <div class="body note">${esc(invAge())}. On a whitelisted server, removing someone from the whitelist is usually enough. Account bans also kick and keep a reason on record; IP bans catch alt accounts but can hit a whole household. Every ban, kick and pardon is audit-logged.</div>
  </section>`;
};

function pluginRows(inv) {
  const jars = new Map(inv.plugins.map((p) => [p.file, p]));
  if (!inv.plugins_lock.length) {
    return inv.plugins.map((p) => ({ name: p.name, version: "", file: p.file, enabled: p.enabled === true, locked: false }));
  }
  return inv.plugins_lock.map((l) => {
    const jar = jars.get(l.filename);
    const off = inv.plugins_disabled.includes(l.name);
    return { name: l.name, version: l.version, file: l.filename, enabled: !off, loaded: jar ? jar.enabled === true : null, locked: true };
  });
}

V.plugins = () => {
  if (!S.inv) return '<div class="empty">Loading…</div>';
  const inv = inventory();
  const rows = pluginRows(inv);
  const off = rows.filter((p) => p.locked && !p.enabled);
  const busy = jobActive(JOB.job);
  return `
  ${off.length ? `<div class="banner warn" role="note">${WARN_ICON}<p><strong>Server differs from Git.</strong> ${off.map((p) => esc(p.name)).join(", ")} ${off.length === 1 ? "is" : "are"} switched off on the server (jar in <code>plugins/disabled/</code>) but <code>plugins.lock.yml</code> still lists ${off.length === 1 ? "it" : "them"}. Deploys keep ${off.length === 1 ? "it" : "them"} off; to make it permanent, remove ${off.length === 1 ? "it" : "them"} from <code>plugins.lock.yml</code> in a PR. ${off.map((p) => `<button class="btn sm" type="button" data-plg="${esc(p.name)}" data-on="1">Re-enable ${esc(p.name)}…</button>`).join("")}</p></div>` : '<div class="banner info" role="note"><p>Plugin versions and hashes come from <code>plugins.lock.yml</code>; update them with a PR. This page can only switch a locked plugin off (or back on), which needs a restart.</p></div>'}
  <section class="panel" aria-labelledby="h-plg">
    <header><h2 id="h-plg">Plugins</h2><span class="note">${esc(invAge())}</span></header>
    <div class="tablewrap"><table><thead><tr><th>Plugin</th><th class="hide-sm">Jar</th><th>Status</th><th>Enabled</th></tr></thead><tbody>
      ${rows.map((p) => {
        const status = p.locked && p.loaded === null ? pill("bad", "Jar missing") : p.enabled === (p.loaded ?? p.enabled) ? (p.enabled ? pill("ok", "Loaded") : pill("off", "Disabled")) : pill("warn", p.enabled ? "On after restart" : "Off after restart");
        return `<tr><td><strong>${esc(p.name)}</strong><div class="note">${esc(p.version)}</div></td><td class="mono hide-sm muted small">${esc(p.file)}</td><td>${status}</td><td><button class="switch" role="switch" aria-checked="${p.enabled === true ? "true" : "false"}" aria-label="${p.enabled ? "Disable" : "Enable"} ${esc(p.name)}" data-plg="${esc(p.name)}" data-on="${p.enabled ? "0" : "1"}"${p.locked && !busy ? "" : " disabled"}></button></td></tr>`;
      }).join("") || emptyRow(4, "No plugin jars.")}
    </tbody></table></div>
  </section>`;
};

// --- backups -----------------------------------------------------------------------------------------------------

LOADERS.backups = async () => {
  S.backups = await api("/api/backups");
};
const mib = (bytes) => (num(bytes) ? `${(bytes / 1048576).toFixed(bytes < 1073741824 ? 1 : 0)} MiB` : "–");
V.backups = () => {
  const b = S.backups;
  if (!b) return '<div class="empty">Loading…</div>';
  const restic = b.source === "restic" ? b.restic : [];
  const rows = restic.length
    ? restic.map((s) => `<tr><td class="mono">${esc(s.short_id)}</td><td>${esc(clock(s.ts, true))}</td><td class="num">${esc(ago(s.ts))}</td><td class="hide-sm">${(s.tags || "").split(",").filter(Boolean).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ")}</td><td class="num hide-sm">${esc(mib(s.added))}</td><td class="actions-cell"><button class="btn sm danger" type="button" data-restore="${esc(s.id)}">Restore…</button></td></tr>`)
    : b.snapshots.map((s) => `<tr><td class="mono">${esc(s.id)}</td><td>${esc(clock(s.ts, true))}</td><td class="num">${esc(ago(s.ts))}</td><td class="hide-sm"></td><td class="num hide-sm">${esc(s.added || "–")}</td><td class="actions-cell"></td></tr>`);
  return `
  ${b.failures.length ? `<div class="banner bad" role="note">${WARN_ICON}<p><strong>Backup errors in the log:</strong> ${esc(b.failures[0].msg)} <span class="ago">(${esc(clock(b.failures[0].ts, true))})</span></p></div>` : ""}
  <div class="grid2">
    <section class="panel" aria-labelledby="h-bk">
      <header><h2 id="h-bk">Backup schedule</h2><button class="btn sm primary" type="button" data-click="btnBackup">Back up now</button></header>
      <div class="body"><dl class="kv"><dt>Destination</dt><dd>R2 · restic repository (bucket lock)</dd><dt>Schedule</dt><dd>${esc(S.o?.server.backup_schedule || "–")}</dd><dt>Last success</dt><dd>${b.last_ok ? `${esc(clock(isoMs(b.last_ok), true))} (${esc(ago(isoMs(b.last_ok)))})` : "none yet"}</dd><dt>Last failure</dt><dd>${b.last_fail ? esc(clock(isoMs(b.last_fail), true)) : "none"}</dd><dt>Running</dt><dd>${b.running_since ? `since ${esc(clock(isoMs(b.running_since)))}` : "no"}</dd></dl></div>
    </section>
    <section class="panel" aria-labelledby="h-rs">
      <header><h2 id="h-rs">Restoring</h2></header>
      <div class="body note stack"><p class="p0">A restore replaces the whole live world with a snapshot. Before anything changes, a <strong>safety snapshot</strong> of the current world is taken (tag <code class="mono">pre-restore</code>, kept by the bucket lock like every snapshot), so a restore can itself be undone.</p><p class="p0">You type the snapshot's id to confirm and need a recent sign-in. Then: countdown, stop, fetch, apply, start; the header shows each step.</p></div>
    </section>
  </div>
  <section class="panel" aria-labelledby="h-snap">
    <header><h2 id="h-snap">Snapshots</h2><span class="note">${restic.length ? `${restic.length} in the repository (restic's list, refreshed after every backup)` : `${b.snapshots.length} seen in the last days' log`}</span></header>
    <div class="tablewrap"><table><thead><tr><th>Snapshot</th><th>Taken</th><th>Age</th><th class="hide-sm">Tags</th><th class="hide-sm">Added</th><th><span class="sr">Actions</span></th></tr></thead><tbody>
      ${rows.join("") || emptyRow(6, "No snapshots yet.")}
    </tbody></table></div>
  </section>`;
};

// --- worlds (v1.2 world library) ---------------------------------------------------------------------------------
//
// Only the active world is on the server; every other world is a restic snapshot in the R2 repository (tags
// world-archive, world:<name>, dv:<DataVersion>), kept outside the backup retention. Create, switch and delete
// are confirmed on the confirmation host like a restore (never from Discord or in-game); the agent's job shows
// the countdown and each step in the banner. server.yml's world.active/world.library follow by pull request.

const WORLD_TYPES = [
  ["default", "Default"], ["large_biomes", "Large biomes"], ["amplified", "Amplified"], ["single_biome", "Single biome (plains)"],
  ["flat_classic", "Superflat: classic"], ["flat_void", "Superflat: the void"], ["flat_redstone", "Superflat: redstone ready"],
];
const WORLD_PRESETS = [
  ["survival_smp", "Survival SMP (normal)"], ["creative_build", "Creative build (peaceful, creative for all)"], ["hardcore", "Hardcore (hard, one life)"],
  ["farm_test", "Farm test (normal, creative for all)"], ["peaceful", "Peaceful survival"],
];
const WORLD_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const EMPTY_WORLDS = { active: "", since: "", data_version: null, archives: [], library: [], deleted: [], archive_bytes: 0, storage_note: false, pending: null };
const worldsInv = () => ({ ...EMPTY_WORLDS, ...(inventory().worlds || {}) });
const worldSize = (bytes) => (num(bytes) ? (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : mib(bytes)) : "–");
const optLabel = (list, v) => (list.find((x) => x[0] === v) || [v, v || "–"])[1];

function dvPill(dv, current) {
  if (!num(dv) || !num(current)) return pill("off", num(dv) ? `DataVersion ${dv}` : "version unknown");
  if (dv > current) return pill("bad", "newer: can't load");
  if (dv < current) return pill("warn", "older: upgrades on load");
  return pill("ok", "same version");
}

function worldSettings(e) {
  if (!e) return "settings not in server.yml";
  const border = e.border_radius ? Object.values(e.border_radius).map((r) => (r === null ? "–" : r)).join(" / ") : "server default";
  return `${e.seed ? `seed ${e.seed}` : "random seed"} · ${optLabel(WORLD_TYPES, e.type)} · ${e.preset ? optLabel(WORLD_PRESETS, e.preset) : "preset: as is"} · border ${border}${e.pregen === false ? " · no pre-generation" : ""}`;
}

V.worlds = () => {
  if (!S.inv) return '<div class="empty">Loading…</div>';
  const w = worldsInv();
  if (!w.active) return '<div class="banner info" role="note"><p>No world library data from the agent yet (it needs the v1.2 agent and a backup to list the archives).</p></div>';
  const lib = new Map(w.library.map((e) => [e.name, e]));
  const busy = jobActive(JOB.job);
  const p = w.pending;
  const pr = p && p.pr ? (p.url ? `pull request <a href="${esc(p.url)}" rel="noreferrer" target="_blank">#${esc(p.pr)}</a>` : `pull request #${esc(p.pr)}`) : "a pull request (opening shortly)";
  const onlyGit = w.library.filter((e) => e.name !== w.active && !w.archives.some((a) => a.name === e.name));
  const rows = w.archives.map((a) => {
    const newer = num(a.data_version) && num(w.data_version) && a.data_version > w.data_version;
    return `<tr><td><strong>${esc(a.name)}</strong><div class="note">${esc(worldSettings(lib.get(a.name)))}</div></td><td>${esc(a.time ? ago(isoMs(a.time)) : "–")}<div class="note mono">${esc(a.short_id)}${num(a.count) && a.count > 1 ? ` · ${esc(a.count)} archives` : ""}</div></td><td class="num">${esc(worldSize(a.size))}</td><td class="hide-sm">${dvPill(a.data_version, w.data_version)}</td><td class="actions-cell"><button class="btn sm primary" type="button" data-wsw="${esc(a.name)}"${newer || busy ? " disabled" : ""}>Switch…</button> <button class="btn sm danger" type="button" data-wdel="${esc(a.name)}"${busy ? " disabled" : ""}>Delete…</button></td></tr>`;
  });
  return `
  ${p ? `<div class="banner ${p.error ? "warn" : "info"}" role="note">${p.error ? WARN_ICON : ""}<p><strong>Git follows:</strong> ${esc(p.what || "a world change")} goes to <code>server.yml</code> (<code>world.active</code>, <code>world.library</code>) by ${pr}. ${p.error ? esc(p.error) : "Deploys honour the live world meanwhile."}</p></div>` : ""}
  ${w.storage_note ? `<div class="banner warn" role="note">${WARN_ICON}<p><strong>Archives use ${esc(worldSize(w.archive_bytes))} in R2.</strong> R2's free tier includes 10 GB of storage; beyond it storage is billed per GB-month (restic deduplicates, so similar worlds cost less than their sizes add up to). Deleting worlds you no longer need frees it once the bucket lock allows.</p></div>` : ""}
  <div class="grid2">
    <section class="panel" aria-labelledby="h-wact">
      <header><h2 id="h-wact">Active world</h2><button class="btn sm primary" type="button" id="wCreate"${busy ? " disabled" : ""}>New world…</button></header>
      <div class="body"><dl class="kv"><dt>World</dt><dd>${esc(w.active)}</dd><dt>Since</dt><dd>${w.since ? esc(clock(isoMs(w.since), true)) : "before the world library"}</dd><dt>DataVersion</dt><dd>${num(w.data_version) ? esc(w.data_version) : "–"}</dd><dt>Settings</dt><dd class="wrap">${esc(worldSettings(lib.get(w.active)))}</dd></dl></div>
    </section>
    <section class="panel" aria-labelledby="h-whow">
      <header><h2 id="h-whow">How it works</h2></header>
      <div class="body note stack"><p class="p0">Only the active world is on the server. A switch warns the players (countdown), stops the server, <strong>archives the active world</strong> (a restic snapshot in R2 tagged <code class="mono">world-archive</code>, kept outside the backup retention: the safety snapshot), puts the other world in place and starts again.</p><p class="p0">Each world has its own inventories, ender chests and XP; the whitelist, operators and bans are shared. Confirmed in a separate window with a one-time PIN, never from Discord or in-game.</p></div>
    </section>
  </div>
  <section class="panel" aria-labelledby="h-warch">
    <header><h2 id="h-warch">Archived worlds</h2><span class="note">${esc(w.archives.length)} in R2 · ${esc(worldSize(w.archive_bytes))}</span></header>
    <div class="tablewrap"><table><thead><tr><th>World</th><th>Archived</th><th class="num">Size</th><th class="hide-sm">Version</th><th><span class="sr">Actions</span></th></tr></thead><tbody>
      ${rows.join("") || emptyRow(5, "No archived worlds yet. Create one: the active world is archived first.")}
    </tbody></table></div>
    ${onlyGit.length ? `<div class="body note">In server.yml without an archive: ${onlyGit.map((e) => esc(e.name)).join(", ")}${w.deleted.length ? `. Deleted (forgotten once the bucket lock allows): ${w.deleted.map(esc).join(", ")}` : ""}.</div>` : w.deleted.length ? `<div class="body note">Deleted (forgotten once the bucket lock allows): ${w.deleted.map(esc).join(", ")}.</div>` : ""}
  </section>`;
};

// v1.2 seed catalogue: worlds/seeds.yml, each claim measured with /locate on the pinned server build
// (scripts/seed_check.py) into /seeds.json, a static asset next to this file. Only seeds whose every claim held
// are offered; picking one only fills the seed (and the default world type the claims were measured with): the
// Worker and the agent see a plain world_create, nothing new.
const SEED_RE = /^-?[0-9]{1,19}$/;
let seedsPromise = null;
function seedCatalogue() {
  if (!seedsPromise) {
    seedsPromise = api("/seeds.json")
      .then((doc) => {
        const ok = (e) => e && typeof e.id === "string" && typeof e.name === "string" && typeof e.seed === "string" && SEED_RE.test(e.seed) && e.verified === true && Array.isArray(e.claims);
        const seeds = Array.isArray(doc.seeds) ? doc.seeds.filter(ok).slice(0, 50) : [];
        return { version: typeof doc.minecraft_version === "string" ? doc.minecraft_version : "", build: doc.server && doc.server.build, seeds };
      })
      .catch(() => {
        seedsPromise = null; // try again next time the dialog opens
        return { version: "", build: null, seeds: [] };
      });
  }
  return seedsPromise;
}

const DIM_NOTE = { nether: " (Nether)", end: " (End)" };
function seedInfo(e, cat) {
  const running = S.o && S.o.server ? S.o.server.minecraft_version : "";
  const badge = pill("ok", `verified on ${cat.version}`);
  const differs = running && running !== cat.version ? ` <span class="note">This server runs ${esc(running)}: the world may generate differently.</span>` : "";
  const claims = e.claims.map((c) => `<li>${esc(String(c.label || c.id).replace(/^./, (x) => x.toUpperCase()))}${esc(DIM_NOTE[c.dimension] || "")}: <strong>${esc(c.distance)}</strong> blocks from spawn <span class="note">(claimed within ${esc(c.within)})</span></li>`).join("");
  const src = typeof e.source === "string" && /^https:\/\//.test(e.source) ? ` · <a href="${esc(e.source)}" rel="noreferrer" target="_blank">source</a>` : "";
  return `<p class="p0">${badge}${differs}</p><p class="p0">${esc(e.description || "")}</p><ul class="plain">${claims}</ul><p class="p0 note">Seed <span class="mono">${esc(e.seed)}</span>, measured with <span class="mono">/locate</span> from the world spawn on server build ${esc(cat.build ?? "?")}, default world type${src}.</p>`;
}

async function worldCreateDlg() {
  const w = worldsInv();
  const cat = await seedCatalogue();
  let get;
  const opts = (list, sel) => list.map(([v, l]) => `<option value="${esc(v)}"${v === sel ? " selected" : ""}>${esc(l)}</option>`).join("");
  confirmDlg({
    title: "Create a new world?",
    ok: "Continue to confirmation",
    danger: true,
    body: `<p>The active world <strong>${esc(w.active)}</strong> is archived first; then the server starts with the new world.</p>
      ${cat.seeds.length ? `<div class="field"><label class="label" for="wCat">Start from</label><select class="input" id="wCat"><option value="">Your own seed (or a random one)</option>${cat.seeds.map((e) => `<option value="${esc(e.id)}">${esc(e.name)} · verified on ${esc(cat.version)}</option>`).join("")}</select></div>
      <div class="seedinfo stack" id="wCatInfo" hidden></div>` : ""}
      <div class="row"><div class="field flex1"><label class="label" for="wName">Name</label><input class="input mono" id="wName" maxlength="32" placeholder="e.g. creative-2026" autocomplete="off" spellcheck="false"></div>
      <div class="field flex1"><label class="label" for="wSeed">Seed (empty = random)</label><input class="input mono" id="wSeed" maxlength="20" inputmode="numeric" autocomplete="off"></div></div>
      <div class="row"><div class="field flex1"><label class="label" for="wType">World type</label><select class="input" id="wType">${opts(WORLD_TYPES, "default")}</select></div>
      <div class="field flex1"><label class="label" for="wPreset">Server preset</label><select class="input" id="wPreset">${opts(WORLD_PRESETS, "survival_smp")}</select></div></div>
      <div class="row"><div class="field flex1"><label class="label" for="wBorderO">Border: overworld</label><input class="input mono" id="wBorderO" inputmode="numeric" placeholder="server default"></div>
      <div class="field flex1"><label class="label" for="wBorderN">nether</label><input class="input mono" id="wBorderN" inputmode="numeric" placeholder="default"></div>
      <div class="field flex1"><label class="label" for="wBorderE">end</label><input class="input mono" id="wBorderE" inputmode="numeric" placeholder="default"></div></div>
      <label class="check"><input type="checkbox" id="wPregen" checked> Pre-generate inside the border while nobody is online (Chunky)</label>
      <p class="note" id="wHint">Names: 1–32 lowercase letters, digits, _ or -.</p>
      ${countdownBody("World switch")}`,
    onOpen: (d) => {
      get = wireSeg(d, "World switch");
      const sel = $("#wCat", d);
      if (!sel) return;
      const info = $("#wCatInfo", d);
      const seedIn = $("#wSeed", d);
      const typeIn = $("#wType", d);
      const picked = () => cat.seeds.find((e) => e.id === sel.value);
      const show = () => {
        const e = picked();
        info.hidden = !e;
        info.innerHTML = e ? seedInfo(e, cat) + (typeIn.value !== "default" ? '<p class="p0 note">The claims were measured with the default world type: another type generates a different world.</p>' : "") : "";
      };
      sel.addEventListener("change", () => {
        const e = picked();
        if (e) {
          seedIn.value = e.seed;
          typeIn.value = "default";
        }
        show();
      });
      typeIn.addEventListener("change", show);
      seedIn.addEventListener("input", () => {
        const e = picked();
        if (e && seedIn.value.trim() !== e.seed) {
          sel.value = ""; // an edited seed is no longer the catalogue's
          show();
        }
      });
    },
    onOk: (d) => {
      const name = $("#wName", d).value.trim();
      const seed = $("#wSeed", d).value.trim();
      if (!WORLD_NAME_RE.test(name)) return toast("World name: 1–32 lowercase letters, digits, _ or -.", 6000);
      if (name === w.active || w.archives.some((a) => a.name === name) || w.deleted.includes(name)) return toast(`A world called ${name} exists (or was deleted recently): pick another name.`, 6000);
      if (!/^(-?[0-9]{1,19})?$/.test(seed)) return toast("The seed must be a whole number (or empty).", 6000);
      const ids = { overworld: "#wBorderO", nether: "#wBorderN", end: "#wBorderE" };
      const raw = Object.fromEntries(Object.entries(ids).map(([k, id]) => [k, $(id, d).value.trim()]));
      if (Object.values(raw).some((v) => v && !/^[0-9]{1,8}$/.test(v))) return toast("A border radius is a whole number of blocks (or empty).", 6000);
      const border = Object.values(raw).some(Boolean) ? Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, v ? Number(v) : null])) : null;
      return runAction("world_create", { name, seed, type: $("#wType", d).value, preset: $("#wPreset", d).value, border_radius: border, pregen: $("#wPregen", d).checked, countdown: get() }, `Create world ${name}`);
    },
  });
}

function worldSwitchDlg(name) {
  const w = worldsInv();
  const a = w.archives.find((x) => x.name === name);
  if (!a) return;
  const older = num(a.data_version) && num(w.data_version) && a.data_version < w.data_version;
  let get;
  confirmDlg({
    title: `Switch to ${name}?`,
    ok: "Continue to confirmation",
    danger: true,
    body: `<p>The archived world <strong>${esc(name)}</strong> (archived ${esc(a.time ? ago(isoMs(a.time)) : "?")}, ${esc(worldSize(a.size))}) replaces <strong>${esc(w.active)}</strong>, which is archived first.</p>
      ${older ? `<div class="banner warn" role="note">${WARN_ICON}<p>${esc(name)} was saved by an older Minecraft (DataVersion ${esc(a.data_version)}; the server runs ${esc(w.data_version)}). It is upgraded when it loads, one way: the archive keeps the old copy.</p></div>` : ""}
      ${countdownBody("World switch", { note: '<ul class="note"><li>Then a few minutes of downtime while the archive is fetched from R2.</li><li>Confirmed in a separate window with a fresh one-time PIN sign-in.</li></ul>' })}`,
    onOpen: (d) => (get = wireSeg(d, "World switch")),
    onOk: () => runAction("world_switch", { name, countdown: get() }, `Switch to ${name}`),
  });
}

function worldDeleteDlg(name) {
  const a = worldsInv().archives.find((x) => x.name === name);
  if (!a) return;
  confirmDlg({
    title: `Delete the world ${name}?`,
    ok: "Continue to confirmation",
    danger: true,
    body: `<p>Forgets every archive snapshot of <strong>${esc(name)}</strong> (${esc(worldSize(a.size))}). The server and the active world aren't touched.</p>
      <ul class="note"><li>Snapshots younger than the bucket lock are forgotten once it allows (days); after that the world is gone for good.</li><li>To keep a copy, restore its snapshot <code class="mono">${esc(a.short_id)}</code> somewhere first (<code class="mono">mcctl restore</code>).</li></ul>`,
    onOk: () => runAction("world_delete", { name }, `Delete world ${name}`),
  });
}

function restoreDlg(id) {
  const s = ((S.backups && S.backups.restic) || []).find((x) => x.id === id);
  if (!s) return;
  const word = `restore ${s.short_id}`;
  let get;
  confirmDlg({
    title: `Restore snapshot ${s.short_id}?`,
    ok: "Continue to confirmation",
    danger: true,
    typed: word,
    body: `<p>This replaces the live world with the snapshot from <strong>${esc(clock(s.ts, true))}</strong> (${esc(ago(s.ts))}).</p>
      <ul><li>A safety snapshot of the current world is taken first (tag <code class="mono">pre-restore</code>), so this can be undone.</li><li>Then the server <strong>stops</strong>; everything built since the snapshot is gone from the live world.</li><li>Expect a few minutes of downtime; the watchdog is paused.</li></ul>
      ${countdownBody("Restore")}
      <div class="field"><label class="label" for="typedConfirm">Type <span class="mono typed">${esc(word)}</span> to confirm</label><input class="input mono" id="typedConfirm" autocomplete="off" spellcheck="false"></div>`,
    onOpen: (d) => (get = wireSeg(d, "Restore")),
    onOk: (d) => runAction("restore", { snapshot: s.id, confirm: $("#typedConfirm", d).value.trim(), countdown: get() }, `Restore ${s.short_id}`),
  });
}

function pluginDlg(name, on) {
  const p = pluginRows(inventory()).find((x) => x.name === name);
  if (!p) return;
  let get;
  confirmDlg({
    title: `${on ? "Enable" : "Disable"} ${name}?`,
    ok: "Start restart countdown",
    danger: !on,
    body: `<p>Plugins can't be switched safely while the server runs, so this moves <code class="mono">${esc(p.file)}</code> ${on ? "back to <code class=\"mono\">plugins/</code>" : "to <code class=\"mono\">plugins/disabled/</code>"} and restarts with a countdown.</p>
      ${countdownBody("Restart", { note: `<ul class="note"><li>${on ? "This brings the server back in line with <code class=\"mono\">plugins.lock.yml</code>." : "The server will then differ from <code class=\"mono\">plugins.lock.yml</code>; a banner reminds you, and deploys keep it off."}</li><li>Confirmed in a separate window with a fresh one-time PIN sign-in.</li></ul>` })}`,
    onOpen: (d) => (get = wireSeg(d, "Restart")),
    onOk: () => runAction("plugin", { name, enabled: on, countdown: get() }, `${on ? "Enable" : "Disable"} ${name}`),
  });
}

function moderateDlg(verb, name) {
  const ban = verb === "ban";
  const whitelisted = inventory().whitelist.some((w) => w.name === name);
  confirmDlg({
    title: `${ban ? "Ban" : "Kick"} ${name}?`,
    ok: ban ? "Ban" : "Kick",
    danger: ban,
    body: `<div class="field"><label class="label" for="reason">Reason (shown to the player)</label><input class="input" id="reason" maxlength="100" value="${ban ? "Griefing" : "Please rejoin in a few minutes"}"></div>
      ${ban && whitelisted ? '<label class="check"><input type="checkbox" id="alsoWl" checked> Also remove from the whitelist (recorded in Git)</label>' : ""}
      ${ban ? '<p class="note">Account ban (<code class="mono">ban</code>). IP bans are on the Bans tab.</p>' : ""}`,
    onOk: async (d) => {
      const reason = $("#reason", d).value;
      const also = $("#alsoWl", d)?.checked;
      await runAction("moderate", { verb, target: name, reason }, `${ban ? "Ban" : "Kick"} ${name}`);
      if (also) await runAction("whitelist", { op: "remove", name }, `Whitelist remove ${name}`);
    },
  });
}

function removeDlg(name) {
  const on = onlineNames().includes(name);
  confirmDlg({
    title: `Remove ${name} from the whitelist?`,
    ok: "Remove",
    danger: true,
    body: `<p>${esc(name)} won't be able to join again. ${on ? "<strong>They are online now</strong> and stay connected until they leave, unless you also kick them." : ""}</p>
      ${on ? '<label class="check"><input type="checkbox" id="alsoKick" checked> Also kick them now</label>' : ""}
      <p class="note">Recorded in <code class="mono">players.yml</code> through a pull request that merges by itself; until then deploys keep the removal.</p>`,
    onOk: async (d) => {
      await runAction("whitelist", { op: "remove", name }, `Whitelist remove ${name}`);
      if ($("#alsoKick", d)?.checked) await runAction("moderate", { verb: "kick", target: name, reason: "Removed from the whitelist" }, `Kick ${name}`);
    },
  });
}

function banNewDlg() {
  confirmDlg({
    title: "Ban a player or IP",
    ok: "Ban",
    danger: true,
    body: `<div class="seg" id="banType" role="group" aria-label="Ban type"><button type="button" aria-pressed="true" data-v="ban">Account</button><button type="button" aria-pressed="false" data-v="ban-ip">IP address</button></div>
      <div class="field"><label class="label" for="banTarget">Username or IP</label><input class="input mono" id="banTarget" placeholder="griefer_99 or 203.0.113.5" maxlength="45"></div>
      <div class="field"><label class="label" for="banReason">Reason</label><input class="input" id="banReason" maxlength="100" value="Griefing"></div>`,
    onOpen: (d) =>
      $("#banType", d).addEventListener("click", (ev) => {
        const x = ev.target.closest("button");
        if (x) $$("#banType button", d).forEach((y) => y.setAttribute("aria-pressed", String(y === x)));
      }),
    onOk: (d) => {
      const target = $("#banTarget", d).value.trim();
      if (!target) return toast("Nothing banned: enter a username or IP.");
      const verb = $('#banType [aria-pressed="true"]', d).dataset.v;
      return runAction("moderate", { verb, target, reason: $("#banReason", d).value }, `Ban ${target}`);
    },
  });
}

function pardonDlg(target, kind) {
  const verb = kind === "ip" ? "pardon-ip" : "pardon";
  confirmDlg({
    title: `Pardon ${target}?`,
    ok: "Pardon",
    body: `<p>Runs <code class="mono">${esc(verb)} ${esc(target)}</code>. ${kind === "ip" ? "" : "They still need to be on the whitelist to join."}</p>`,
    onOk: () => runAction("moderate", { verb, target }, `Pardon ${target}`),
  });
}

function rejectedDlg(name, approve) {
  if (!approve) return runAction("rejected_dismiss", { name }, `Dismiss ${name}`);
  return confirmDlg({
    title: `Whitelist ${name}?`,
    ok: "Add to whitelist",
    body: `<p>${esc(name)} tried to join and was turned away. Adding them applies live and is recorded in <code class="mono">players.yml</code> through a pull request that merges by itself.</p>`,
    onOk: () => runAction("whitelist", { op: "add", name }, `Whitelist add ${name}`),
  });
}

// --- logs --------------------------------------------------------------------------------------------------------

const LOG = { q: "", unit: "", level: "all", audit: false, day: "", before: 0, pages: [], data: null, archives: null };
const UNITS = ["minecraft.service", "mc-backup.service", "mc-health.service", "mc-agent.service", "mc-prune.service", "mc-restore-apply.service"];
LOADERS.logs = async () => {
  const p = new URLSearchParams({ q: LOG.q, unit: LOG.unit, level: LOG.level, limit: "200" });
  if (LOG.audit) p.set("audit", "1");
  if (LOG.day) p.set("day", LOG.day);
  if (LOG.before) p.set("before", String(LOG.before));
  LOG.data = await api(`/api/logs?${p}`);
  const day = LOG.data.day || LOG.day;
  LOG.archives = day ? await api(`/api/archives?day=${encodeURIComponent(day)}`).catch(() => null) : null;
};
V.logs = () => {
  const d = LOG.data;
  if (!d) return '<div class="empty">Loading…</div>';
  return `
  <section class="panel" aria-labelledby="h-logs">
    <header><h2 id="h-logs">Logs</h2><span class="note">minecraft and mc-* units · searchable ${esc(d.days.length)} day(s) in D1</span></header>
    <form class="toolbar" id="logForm" role="search">
      <input class="search" id="lq" type="search" placeholder="Search text" aria-label="Search text" value="${esc(LOG.q)}" maxlength="100">
      <select class="input" id="lunit" aria-label="Unit"><option value="">All units</option>${UNITS.map((u) => `<option value="${u}"${u === LOG.unit ? " selected" : ""}>${u.replace(".service", "")}</option>`).join("")}</select>
      <select class="input" id="llevel" aria-label="Level">${[["all", "All levels"], ["info", "Info+"], ["warn", "Warnings+"], ["error", "Errors"]].map(([v, l]) => `<option value="${v}"${v === LOG.level ? " selected" : ""}>${l}</option>`).join("")}</select>
      <select class="input" id="lday" aria-label="Day">${d.days.map((x) => `<option value="${esc(x)}"${x === d.day ? " selected" : ""}>${esc(`${x.slice(0, 4)}-${x.slice(4, 6)}-${x.slice(6)}`)}</option>`).join("") || '<option value="">no days</option>'}</select>
      <label class="note"><input type="checkbox" id="laudit"${LOG.audit ? " checked" : ""}> audit entries only</label>
      <button class="btn sm primary" type="submit">Search</button>
    </form>
    <div class="tablewrap"><table class="loglist"><thead><tr><th>Time</th><th class="hide-sm">Unit</th><th>Message</th></tr></thead><tbody>
      ${d.rows.map((r) => `<tr class="prio-${Number(r.prio) | 0}"><td class="nowrap">${esc(clock(r.ts))}</td><td class="hide-sm nowrap muted">${esc(r.unit.replace(".service", ""))}${r.audit ? ' <span class="tag audit-tag">audit</span>' : ""}</td><td class="msgcell">${esc(r.msg)}</td></tr>`).join("") || emptyRow(3, "No matching lines on this day.")}
    </tbody></table></div>
    <div class="pager"><button class="btn sm" type="button" id="lnewer"${LOG.pages.length ? "" : " disabled"}>Newer</button><span class="note">${d.rows.length} line(s)</span><button class="btn sm" type="button" id="lolder"${d.next_before ? "" : " disabled"}>Older</button></div>
  </section>
  <section class="panel" aria-labelledby="h-arch">
    <header><h2 id="h-arch">Raw journal archives (R2)</h2><span class="note">gzipped NDJSON, kept per <code class="mono">monitoring.logs.r2_days</code></span></header>
    <div class="tablewrap"><table><thead><tr><th>Object</th><th>Size</th><th class="hide-sm">Uploaded</th></tr></thead><tbody>
      ${(LOG.archives?.objects || []).map((o) => `<tr><td class="mono small">${esc(o.key)}</td><td class="num">${esc(Math.round(o.size / 1024))} KiB</td><td class="hide-sm muted">${esc(o.uploaded ? clock(Date.parse(o.uploaded), true) : "")}</td></tr>`).join("") || emptyRow(3, "No archives for this day.")}
    </tbody></table></div>
    <div class="body note">Download with <code class="mono">mcctl logs --since</code> (needs your Cloudflare credentials; the panel never serves raw archives).</div>
  </section>`;
};
function wireLogs() {
  if (!once($("#logForm"))) return;
  $("#logForm").addEventListener("submit", (e) => {
    e.preventDefault();
    LOG.q = $("#lq").value;
    LOG.unit = $("#lunit").value;
    LOG.level = $("#llevel").value;
    LOG.audit = $("#laudit").checked;
    const day = $("#lday").value;
    LOG.day = day && day !== LOG.data?.days?.[0] ? day : day;
    LOG.before = 0;
    LOG.pages = [];
    load("logs");
  });
  $("#lolder").addEventListener("click", () => {
    LOG.pages.push(LOG.before);
    LOG.before = LOG.data.next_before;
    load("logs");
  });
  $("#lnewer").addEventListener("click", () => {
    LOG.before = LOG.pages.pop() || 0;
    load("logs");
  });
}

// --- health (history charts + watchdog) ---------------------------------------------------------------------------

const H = { range: "24h", data: null };
LOADERS.health = async () => {
  const [m, w] = await Promise.all([api(`/api/metrics?range=${H.range}`), api("/api/health")]);
  H.data = m;
  H.watchdog = w;
};
const CHARTS = [
  [{ key: "tps", label: "TPS", unit: "", min: 0, max: 20, fmt: (v) => v.toFixed(1) }],
  [{ key: "mspt", label: "MSPT", unit: " ms", min: 0, line: 50, fmt: (v) => v.toFixed(1) }],
  [{ key: "players", label: "Players", unit: "", min: 0, fmt: (v) => String(Math.round(v)) }],
  [{ key: "heap_used_gb", label: "JVM heap", unit: " GiB", min: 0, fmt: (v) => v.toFixed(2) }],
  [{ key: "cpu_pct", label: "CPU", unit: " %", min: 0, max: 100, fmt: (v) => String(Math.round(v)) }, { key: "steal_pct", label: "steal" }],
  [{ key: "mem_used_gb", label: "Memory used", unit: " GB", min: 0, fmt: (v) => v.toFixed(1) }],
  [{ key: "disk_pct", label: "Disk", unit: " %", min: 0, max: 100, fmt: (v) => String(Math.round(v)) }],
  [{ key: "tx", label: "Network out", unit: " kB/s", min: 0, fmt: (v) => String(Math.round(v)) }, { key: "rx", label: "in" }],
];
V.health = () => {
  if (!H.data) return '<div class="empty">Loading…</div>';
  const pts = H.data.points.map((p) => ({
    ...p,
    heap_used_gb: num(p.heap_used_mb) ? p.heap_used_mb / 1024 : null,
    mem_used_gb: num(p.mem_used_mb) ? p.mem_used_mb / 1024 : null,
    tx: num(p.net_tx_bps) ? p.net_tx_bps / 1000 : null,
    rx: num(p.net_rx_bps) ? p.net_rx_bps / 1000 : null,
  }));
  const w = H.watchdog;
  const label = { "2h": "2 hours", "24h": "24 hours", "7d": "7 days", "30d": "30 days", "90d": "90 days" }[H.range];
  return `
  <section class="panel" aria-labelledby="h-hist">
    <header><h2 id="h-hist">History</h2><div class="seg rangeseg" role="group" aria-label="Time range">${["2h", "24h", "7d", "30d", "90d"].map((r) => `<button type="button" data-range="${r}" aria-pressed="${r === H.range}">${r}</button>`).join("")}</div></header>
    <div class="metrics">${CHARTS.map(([m, m2]) => {
      const vals = pts.filter((p) => num(p[m.key]));
      const lastV = vals.length ? vals[vals.length - 1][m.key] : null;
      return `<div class="metric"><div class="top"><span class="label">${esc(m.label)}${m2 ? ` <span class="legend-mut">+ ${esc(m2.label)}</span>` : ""}</span><span class="val">${num(lastV) ? m.fmt(lastV) : "–"}<span class="muted unit-sm">${m.unit}</span></span></div>${spark(pts, m, "var(--accent)", m2 || null, "var(--accent-2)", "spark big-spark")}<div class="axis"><span>−${label}</span><span>${H.data.step_ms ? `${Math.round(H.data.step_ms / 60000)}-min means` : "every sample"}</span><span>now</span></div></div>`;
    }).join("")}</div>
  </section>
  <div class="grid2">
    <section class="panel" aria-labelledby="h-wd">
      <header><h2 id="h-wd">Watchdog (Cloudflare)</h2>${pill(w.ok ? "ok" : "bad", w.ok ? "Healthy" : "Attention")}</header>
      <div class="body"><dl class="kv">
        <dt>Last cron run</dt><dd>${w.last_run ? `${esc(clock(isoMs(w.last_run), true))} (${esc(w.last_run_age_s)} s ago)` : "never"}</dd>
        ${Object.entries(w.probes).map(([f, p]) => `<dt>${f === "v4" ? "IPv4" : "IPv6"} status ping</dt><dd class="wrap">${p.ok ? "ok" : p.ok === false ? `failing ${esc(p.failed_runs)} runs: ${esc(p.error)}` : "not yet"}${p.version ? ` · ${esc(p.version)}` : ""}</dd>`).join("")}
        ${Object.entries(w.checks).map(([n, c]) => `<dt>${esc(n)} heartbeat</dt><dd>${esc(c.state)}${c.last_ok ? ` · ok ${esc(ago(isoMs(c.last_ok)))}` : ""}</dd>`).join("")}
        <dt>Maintenance</dt><dd class="wrap">${w.maintenance ? `until ${esc(clock(isoMs(w.maintenance.until), true))}: ${esc(w.maintenance.reason)}` : "none"}</dd>
        <dt>Admin-stopped</dt><dd>${w.admin_stopped ? "yes" : "no"}</dd>
      </dl></div>
    </section>
    <section class="panel" aria-labelledby="h-hc">
      <header><h2 id="h-hc">Checks now</h2><span class="note">from the latest sample</span></header>
      <ul class="health">${S.o ? healthItems(S.o).map(([n, c, v, d]) => `<li><span>${esc(n)}</span>${pill(c, v)}<span class="d">${esc(d)}</span></li>`).join("") : ""}</ul>
    </section>
  </div>
  ${S.o ? versionsPanel(S.o) : ""}`;
};

// --- v1.3: versions everywhere and the Panel -> Git self-test (Health tab) ----------------------------------------

function versionsPanel(o) {
  const v = o.versions || { server: null, worker: null, problems: [] };
  const sv = v.server;
  const wv = v.worker;
  const short = (c) => (c ? ` · ${c.slice(0, 12)}` : "");
  const bad = (v.problems || []).some((p) => p.level === "warn");
  const t = o.selftest;
  const tPill = !t ? pill("off", "Not run yet") : !t.configured ? pill("off", "Not set up") : t.ok ? pill("ok", "Passed") : pill("bad", "Failing");
  const steps = t && t.steps ? t.steps.map((x) => `<li><span>${esc(x.step)}</span>${pill(x.ok ? "ok" : "bad", x.ok ? "ok" : "failed")}<span class="d">${esc(x.detail)}</span></li>`).join("") : "";
  return `<div class="grid2">
    <section class="panel" aria-labelledby="h-ver">
      <header><h2 id="h-ver">Versions</h2>${pill(bad ? "warn" : sv && wv ? "ok" : "off", bad ? "Mismatch" : sv && wv ? "In step" : "Partly known")}</header>
      <div class="body"><dl class="kv">
        <dt>Server (deployed)</dt><dd class="wrap">${sv ? `v${esc(sv.version)}${esc(short(sv.commit))}` : "unknown (deployed before v1.3, or no agent report yet)"}</dd>
        <dt>Worker (applied)</dt><dd class="wrap">${wv ? `v${esc(wv.version)}` : "unknown (applied before v1.3: `mcctl render` + `mcctl apply`)"}</dd>
        <dt>Config repo pin</dt><dd class="wrap">${wv && wv.pin_sha ? `${esc(wv.pin_version || "")}${esc(short(wv.pin_sha))} (at the last apply)` : "unknown"}</dd>
      </dl>
      ${(v.problems || []).map((p) => `<p class="note">${pill(p.level === "warn" ? "warn" : "off", p.level === "warn" ? "Fix" : "Note")} ${esc(p.text)}</p>`).join("")}</div>
    </section>
    <section class="panel" aria-labelledby="h-st">
      <header><h2 id="h-st">Panel → Git self-test</h2>${tPill}</header>
      <div class="body">
        <p class="note">${t && t.ts ? `Last run ${esc(ago(t.ts))} (${t.trigger === "panel" ? "from the panel" : "daily"})${t.failures ? ` · ${esc(t.failures)} failure(s) in a row` : ""}` : "Runs daily on the server; nothing is created on GitHub."}${t && t.error ? ` · ${esc(t.error)}` : ""}</p>
        ${steps ? `<ul class="health">${steps}</ul>` : ""}
        <button class="btn sm" type="button" id="selftestRun">Run the self-test now</button>
      </div>
    </section>
  </div>`;
}

function selftestDlg() {
  confirmDlg({
    title: "Run the Panel → Git self-test?",
    ok: "Run it",
    body: `<p>The server asks this Worker for a GitHub token, checks it, and reads the config repo (the repository, main and <code class="mono">players.yml</code>, the pull requests) the way a panel pull request would.</p>
      <p class="note">Read-only: no branch, commit or pull request is created. At most once a minute; the result appears here.</p>`,
    onOk: () => runAction("selftest", {}),
  });
}

// --- experience (P13): verdicts, remedies, history with annotations, overhead, backlog ------------------------

const XP = { range: "24h", data: null, hist: null, board: null, player: null, playerId: null };
const VERDICT = { good: ["ok", "Good"], fair: ["warn", "Fair"], poor: ["bad", "Poor"] };
const vpill = (v) => pill((VERDICT[v] || ["off"])[0], (VERDICT[v] || [null, "–"])[1]);
const XP_RANGES = ["24h", "7d", "30d", "180d", "2y"];
const XP_LABEL = { "24h": "24 hours", "7d": "7 days", "30d": "30 days", "180d": "180 days", "2y": "2 years" };
const KIND = { config: "server.yml", plugin: "plugin", client_mod: "client mod", host: "host", action: "try" };
const fmtMs = (v) => (num(v) ? `${v < 10 ? v.toFixed(1) : Math.round(v)} ms` : "–");
const hours = (ms) => (num(ms) ? (ms / 3600000 < 10 ? (ms / 3600000).toFixed(1) : Math.round(ms / 3600000)) : "–");
const XP_CHARTS = [
  [{ key: "p99", label: "Tick p99", unit: " ms", min: 0, line: 50, fmt: (v) => v.toFixed(1) }, { key: "p999", label: "p99.9" }],
  [{ key: "max", label: "Slowest tick", unit: " ms", min: 0, line: 100, fmt: (v) => String(Math.round(v)) }],
  [{ key: "tps", label: "TPS (lowest)", unit: "", min: 0, max: 20, fmt: (v) => v.toFixed(1) }],
  [{ key: "players", label: "Players", unit: "", min: 0, fmt: (v) => String(Math.round(v)) }],
  [{ key: "ping_p50", label: "Ping (median player)", unit: " ms", min: 0, line: 150, fmt: (v) => String(Math.round(v)) }],
  [{ key: "poor", label: "Players with a poor experience", unit: "", min: 0, fmt: (v) => String(Math.round(v)) }, { key: "fair", label: "fair" }],
  [{ key: "entities", label: "Entities loaded", unit: "", min: 0, fmt: (v) => String(Math.round(v)) }],
  [{ key: "self_p99_us", label: "mc-telemetry cost per tick (p99)", unit: " µs", min: 0, line: 100, fmt: (v) => v.toFixed(1) }],
];

// A chart over a fixed time axis [from, to]: data gaps shaded, annotations (deploys, restarts, panel actions) as
// vertical lines with a tooltip. SVG presentation attributes only (CSP).
function xpChart(points, m, m2, from, to, notes, gaps) {
  const W = 300;
  const H = 90;
  const P = 3;
  const data = points.filter((p) => num(p[m.key]));
  const x = (ts) => P + ((ts - from) / Math.max(1, to - from)) * (W - 2 * P);
  const vals = [...data.map((p) => p[m.key]), ...(m2 ? points.filter((p) => num(p[m2.key])).map((p) => p[m2.key]) : [])];
  const max = m.max ?? Math.max(1, (m.line || 0) * 1.1, ...vals.map((v) => v * 1.15));
  const min = m.min ?? 0;
  const y = (v) => H - P - ((clamp(v, min, max) - min) / (max - min || 1)) * (H - 2 * P);
  const line = (key) => points.filter((p) => num(p[key])).map((p) => `${x(p.ts).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const gapRects = gaps.map((g) => `<rect x="${x(g.from).toFixed(1)}" y="0" width="${Math.max(1, x(g.to) - x(g.from)).toFixed(1)}" height="${H}" fill="var(--muted)" opacity=".12"><title>no data ${esc(clock(g.from, true))} – ${esc(clock(g.to, true))}</title></rect>`).join("");
  const noteLines = notes.map((n) => `<line x1="${x(n.ts).toFixed(1)}" x2="${x(n.ts).toFixed(1)}" y1="0" y2="${H}" stroke="var(--accent-2)" stroke-width="1.5" stroke-dasharray="3 2" vector-effect="non-scaling-stroke"><title>${esc(clock(n.ts, true))} ${esc(n.kind)}: ${esc(n.text)}</title></line>`).join("");
  const thr = m.line ? `<line x1="0" x2="${W}" y1="${y(m.line)}" y2="${y(m.line)}" stroke="var(--bad)" stroke-dasharray="4 4" stroke-width="1" opacity=".6" vector-effect="non-scaling-stroke"/>` : "";
  const body = data.length >= 2
    ? `<polyline points="${line(m.key)}" fill="none" stroke="var(--accent)" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>${m2 ? `<polyline points="${line(m2.key)}" fill="none" stroke="var(--accent-2)" stroke-width="1.4" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>` : ""}`
    : `<text x="${W / 2}" y="${H / 2 + 4}" text-anchor="middle" font-size="11" fill="var(--muted)">waiting for data</text>`;
  const last = data.length ? data[data.length - 1][m.key] : null;
  return `<svg class="spark big-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(m.label)}${num(last) ? `, latest ${esc(m.fmt(last))}${esc(m.unit)}` : ""}${notes.length ? `, ${notes.length} annotations` : ""}">${gapRects}<line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>${thr}${noteLines}${body}</svg>`;
}

LOADERS.experience = async () => {
  const [d, h, b] = await Promise.all([api("/api/experience"), api(`/api/experience/history?range=${XP.range}`), api("/api/experience/leaderboard")]);
  XP.data = d;
  XP.hist = h;
  XP.board = b.players;
  if (XP.playerId) XP.player = await api(`/api/experience/player/${XP.playerId}`);
};

function xpPlayer() {
  const p = XP.player;
  if (!p) return "";
  const now = Date.now();
  const pts = (p.pings || []).map((x) => ({ ts: x.ts, ping: x.ping, ping_max: x.ping_max }));
  const st = p.stats || {};
  return `<section class="panel" aria-labelledby="h-xpp">
    <header><h2 id="h-xpp">${headSvg(p.name, "head sm")} ${esc(p.name)}</h2><span>${vpill(p.verdict)} <button class="btn sm" type="button" data-xpp="">Close</button></span></header>
    <div class="body"><dl class="kv">
      <dt>Now</dt><dd class="wrap">${esc(p.headline || "–")}${(p.reasons || []).length ? `: ${esc(p.reasons.join(" · "))}` : ""}</dd>
      <dt>Played</dt><dd>${hours(p.play_ms)} h in ${esc(p.sessions)} sessions · first seen ${p.first_seen ? esc(clock(p.first_seen, true)) : "–"}</dd>
      <dt>Stats</dt><dd>${num(st.play_ticks) ? `${hours(st.play_ticks * 50)} h in-game` : "–"} · deaths ${esc(st.deaths ?? "–")} · walked ${num(st.walk_cm) ? `${(st.walk_cm / 100000).toFixed(1)} km` : "–"} · flown ${num(st.fly_cm) ? `${(st.fly_cm / 100000).toFixed(1)} km` : "–"}</dd>
      <dt>Client</dt><dd>${esc(p.brand || "–")}</dd>
    </dl></div>
    <div class="metric"><div class="top"><span class="label">Ping, last 24 h <span class="legend-mut">+ worst</span></span></div>${xpChart(pts, { key: "ping", label: "Ping", unit: " ms", min: 0, line: 150, fmt: (v) => String(Math.round(v)) }, { key: "ping_max", label: "worst" }, now - 86400000, now, [], [])}<div class="axis"><span>−24 h</span><span>150 ms = noticeable</span><span>now</span></div></div>
    <div class="grid2">
      <div class="tablewrap"><table><thead><tr><th>Joined</th><th>Length</th></tr></thead><tbody>${(p.session_list || []).map((x) => `<tr><td class="mono nowrap">${esc(clock(x.join_ts, true))}</td><td>${x.leave_ts ? `${Math.max(1, Math.round((x.leave_ts - x.join_ts) / 60000))} min` : "online"}</td></tr>`).join("") || emptyRow(2, "No sessions yet.")}</tbody></table></div>
      <div class="tablewrap"><table><thead><tr><th>When</th><th>What</th></tr></thead><tbody>${(p.events || []).map((e) => `<tr><td class="mono nowrap">${esc(clock(e.ts, true))}</td><td class="small">${e.kind === "message" ? `message: “${esc(e.text)}”` : `${esc(e.from)} → ${esc(e.to)}${(e.reasons || []).length ? `: ${esc(e.reasons[0])}` : ""}`}</td></tr>`).join("") || emptyRow(2, "No verdict changes or messages yet.")}</tbody></table></div>
    </div>
  </section>`;
}

function xpOverhead(d) {
  const rows = d.overhead || [];
  return `<section class="panel" aria-labelledby="h-xpo">
    <header><h2 id="h-xpo">Product overhead vs Minecraft</h2><span class="note">systemd cgroup accounting, last 24 h · flag: &gt; 1% CPU or &gt; 100 MB</span></header>
    <div class="tablewrap"><table><thead><tr><th>Unit</th><th>CPU (avg)</th><th>Memory (peak)</th><th></th></tr></thead><tbody>
      ${rows.map((o) => `<tr><td class="mono">${esc(o.unit.replace(".service", ""))}</td><td class="num">${num(o.cpu_pct) ? `${o.cpu_pct < 1 ? o.cpu_pct.toFixed(2) : o.cpu_pct.toFixed(1)}%` : "–"}</td><td class="num">${num(o.mem_mb) ? `${Math.round(o.mem_mb)} MB` : "–"}</td><td>${o.unit === "minecraft.service" ? pill("off", "the game") : o.flag ? pill("warn", "Over budget") : pill("ok", "Negligible")}</td></tr>`).join("") || emptyRow(4, "No overhead data yet.")}
    </tbody></table></div>
    ${d.plugin ? `<div class="body note">mc-telemetry on the main thread: p99 ${fix(d.plugin.self_p99_us)} µs per tick, worst ${fix(d.plugin.self_max_us)} µs (budget: server.yml experience.plugin_budget_us; ${esc(d.plugin.budget_skips ?? 0)} ticks deferred work, ${esc(d.plugin.lost_windows ?? 0)} windows skipped)</div>` : ""}
  </section>`;
}

// The busiest loaded chunks of the latest minute (the plugin's chunk scan): where a farm or pen is crowded.
function xpChunks(d) {
  const rows = d.busiest || [];
  const limit = num(d.chunk_limit) ? d.chunk_limit : 0;
  const heavy = rows.filter((c) => c.heavy).length;
  return `<section class="panel" aria-labelledby="h-xpc">
    <header><h2 id="h-xpc">Busiest chunks</h2>${heavy ? pill("warn", `${heavy} over ${limit}`) : pill("ok", limit ? `all under ${limit}` : "alert off")}</header>
    <div class="tablewrap"><table><thead><tr><th>Where</th><th>Entities</th><th>Villagers</th><th>Items</th><th>Tile entities</th></tr></thead><tbody>
      ${rows.map((c) => `<tr><td class="small"><span class="mono">${esc(c.w)} ${esc(c.cx * 16)}, ${esc(c.cz * 16)}</span><div class="note">chunk ${esc(c.cx)}, ${esc(c.cz)}</div></td><td class="num">${c.heavy ? pill("warn", String(c.ent)) : esc(c.ent)}</td><td class="num">${esc(c.vil)}</td><td class="num">${esc(c.items)}</td><td class="num">${esc(c.tiles)}</td></tr>`).join("") || emptyRow(5, "No chunk data yet.")}
    </tbody></table></div>
    <div class="body note">Latest minute, highest count per chunk. Crowded chunks: spread the pen or farm, add floor space; vanilla cramming kills mobs above 24 per block.</div>
  </section>`;
}

function xpBacklog(d, h) {
  const b = d.backlog || {};
  const gaps = (h && h.gaps) || [];
  return `<section class="panel" aria-labelledby="h-xpb">
    <header><h2 id="h-xpb">Delivery &amp; data gaps</h2>${num(b.rows) && b.rows > 0 ? pill("warn", `${b.rows} rows queued`) : pill("ok", "Up to date")}</header>
    <div class="body"><dl class="kv">
      <dt>Backfill backlog</dt><dd>${num(b.segments) ? `${b.segments} segment(s), ${b.rows} rows${b.oldest ? `, oldest ${esc(b.oldest)}` : ""}` : "–"} · sent only while nobody is online</dd>
      <dt>On the box</dt><dd>${num(b.stored_segments) ? `${b.stored_segments} segments, ${mib(b.stored_bytes)}` : "–"} (30 days, disk-capped)</dd>
      <dt>Gaps (${esc(XP_LABEL[XP.range])})</dt><dd class="wrap">${gaps.length ? gaps.slice(-8).map((g) => `${esc(clock(g.from, true))} – ${esc(clock(g.to, true))}`).join(" · ") : "none"}</dd>
    </dl></div>
  </section>`;
}

V.experience = () => {
  const d = XP.data;
  const h = XP.hist;
  if (!d || !h) return '<div class="empty">Loading…</div>';
  const srv = d.server;
  const notes = h.annotations || [];
  const gaps = h.gaps || [];
  return `
  ${!d.fresh ? '<div class="banners"><div class="banner warn" role="note"><p>No live experience data: the plugin, mc-agent or the Worker isn’t reporting (history below still works).</p></div></div>' : ""}
  ${xpPlayer()}
  <div class="grid2">
    <section class="panel" aria-labelledby="h-xps">
      <header><h2 id="h-xps">Server</h2>${srv ? vpill(srv.verdict) : pill("off", "No data")}</header>
      <div class="body"><p><strong>${esc(srv ? srv.headline : "–")}</strong></p>
        <ul class="plain">${srv && srv.reasons.length ? srv.reasons.map((r) => `<li>${esc(r)}</li>`).join("") : "<li class=\"muted\">Nothing to report.</li>"}</ul>
        ${d.srv ? `<div class="chiprow"><span class="mchip">tick p50 ${fmtMs(d.srv.p50)}</span><span class="mchip">p99 ${fmtMs(d.srv.p99)}</span><span class="mchip">p99.9 ${fmtMs(d.srv.p999)}</span><span class="mchip">max ${fmtMs(d.srv.max)}</span><span class="mchip">${esc(d.srv.entities ?? "–")} entities</span><span class="mchip">${esc(d.srv.chunks ?? "–")} chunks</span></div>` : ""}
      </div>
    </section>
    <section class="panel" aria-labelledby="h-xpr">
      <header><h2 id="h-xpr">What would help</h2><span class="note">recommendations only · plugins and mods checked for Minecraft ${esc((S.o && S.o.server.minecraft_version) || "")}</span></header>
      <ul class="health">${(d.remedies || []).map((r) => `<li><span>${esc(r.text)}</span><span class="tag">${esc(KIND[r.kind] || r.kind)}</span><span class="d">${r.field ? `<span class="mono">${esc(r.field)}</span> · ` : ""}${r.project ? (r.available === true ? `available for this version${r.version ? ` (${esc(r.version)})` : ""}` : r.available === false ? "not available for this Minecraft version yet: don't install" : "availability not checked yet") : ""}</span></li>`).join("") || '<li><span class="muted">Nothing needed right now.</span></li>'}</ul>
    </section>
  </div>
  <section class="panel" aria-labelledby="h-xpl">
    <header><h2 id="h-xpl">Players now</h2><span class="note">verdict per player · /lag in game shows the same · click for history</span></header>
    <div class="tablewrap"><table><thead><tr><th>Player</th><th>Experience</th><th>Why</th><th>Ping</th><th>Render</th><th>Client</th></tr></thead><tbody>
      ${(d.players || []).map((p) => `<tr><td class="nowrap"><button class="linkbtn" type="button" data-xpp="${esc(p.uuid)}">${headSvg(p.name, "head sm")} ${esc(p.name)}</button>${p.tips_on === false ? ' <span class="tag">tips off</span>' : ""}</td><td>${vpill(p.verdict)}</td><td class="small">${esc((p.reasons || []).join(" · ") || p.headline || "")}${Array.isArray(p.tips) && p.tips.length ? `<div class="note">Tip: ${esc(p.tips[0])}</div>` : ""}</td><td class="num">${fmtMs(p.ping)}</td><td class="num">${esc(p.view ?? "–")} / ${esc(p.send ?? "–")}</td><td class="small">${esc(p.brand || "–")}</td></tr>`).join("") || emptyRow(6, "Nobody online.")}
    </tbody></table></div>
  </section>
  <section class="panel" aria-labelledby="h-xph">
    <header><h2 id="h-xph">History</h2><div class="seg rangeseg" role="group" aria-label="Time range">${XP_RANGES.map((r) => `<button type="button" data-xprange="${r}" aria-pressed="${r === XP.range}">${r}</button>`).join("")}</div></header>
    <div class="metrics">${XP_CHARTS.map(([m, m2]) => `<div class="metric"><div class="top"><span class="label">${esc(m.label)}${m2 ? ` <span class="legend-mut">+ ${esc(m2.label)}</span>` : ""}</span></div>${xpChart(h.points, m, m2 || null, h.from, h.to, notes, gaps)}<div class="axis"><span>−${esc(XP_LABEL[XP.range])}</span><span>${h.source === "1h" ? "hourly" : "per minute"}${notes.length ? ` · ${notes.length} events` : ""}</span><span>now</span></div></div>`).join("")}</div>
    ${notes.length ? `<div class="body note">Events: ${notes.slice(-6).map((n) => `${esc(clock(n.ts, true))} ${esc(n.kind)} (${esc(n.text)})`).join(" · ")}</div>` : ""}
  </section>
  ${xpChunks(d)}
  <div class="grid2">
    ${xpOverhead(d)}
    ${xpBacklog(d, h)}
  </div>
  <div class="grid2">
    <section class="panel" aria-labelledby="h-xpm">
      <header><h2 id="h-xpm">Messages sent</h2><span class="note">private, rate-limited · audited</span></header>
      <div class="tablewrap"><table><thead><tr><th>When</th><th>To</th><th>Message</th></tr></thead><tbody>
        ${(d.messages || []).map((m) => `<tr><td class="mono nowrap">${esc(clock(m.ts, true))}</td><td>${esc(m.name || "everyone")}</td><td class="small">${esc(m.text)}</td></tr>`).join("") || emptyRow(3, "No messages sent.")}
      </tbody></table></div>
    </section>
    <section class="panel" aria-labelledby="h-xpg">
      <header><h2 id="h-xpg">Lag spikes</h2><span class="note">ticks over 100 ms, with what was going on</span></header>
      <div class="tablewrap"><table><thead><tr><th>When</th><th>Tick</th><th>Context</th></tr></thead><tbody>
        ${(d.lag || []).map((l) => `<tr><td class="mono nowrap">${esc(clock(l.ts, true))}</td><td class="num">${fmtMs(l.ms)}</td><td class="small">${esc([...(l.near || []).slice(0, 2).map((n) => `${n.n} near ${n.ent} entities`), ...(l.running || []).map((r) => `${r} running`), ...(l.worlds || []).slice(0, 1).map((w) => `${w.n}: ${w.entities} entities`)].join(" · "))}</td></tr>`).join("") || emptyRow(3, "No spikes recorded.")}
      </tbody></table></div>
    </section>
  </div>
  <section class="panel" aria-labelledby="h-xpt">
    <header><h2 id="h-xpt">Leaderboard</h2><span class="note">playtime from sessions</span></header>
    <div class="tablewrap"><table><thead><tr><th>Player</th><th>Played</th><th>Sessions</th><th>Last seen</th><th>Deaths</th></tr></thead><tbody>
      ${(XP.board || []).slice(0, 20).map((p) => `<tr><td class="nowrap"><button class="linkbtn" type="button" data-xpp="${esc(p.uuid)}">${headSvg(p.name, "head sm")} ${esc(p.name)}</button></td><td class="num">${hours(p.play_ms)} h</td><td class="num">${esc(p.sessions)}</td><td class="mono nowrap">${p.last_seen ? esc(ago(p.last_seen)) : "–"}</td><td class="num">${esc((p.stats || {}).deaths ?? "–")}</td></tr>`).join("") || emptyRow(5, "Nobody has played yet.")}
    </tbody></table></div>
  </section>`;
};

// The overview's summary card (from /api/overview's experience field).
function experienceCard(o) {
  const x = o.experience;
  if (!x) return "";
  const counts = x.counts || {};
  return `<section class="panel" aria-labelledby="h-xpc">
    <header><h2 id="h-xpc">Player experience</h2><span>${x.fresh ? vpill(x.verdict) : pill("off", "No data")} <button class="btn sm" type="button" data-go="experience">Details</button></span></header>
    <div class="body"><p><strong>${esc(x.headline || "–")}</strong>${num(x.p99) ? ` · tick p99 ${fmtMs(x.p99)}, worst ${fmtMs(x.max)}` : ""}</p>
      <div class="chiprow"><span class="mchip">${esc(counts.good ?? 0)} good</span><span class="mchip">${esc(counts.fair ?? 0)} fair</span><span class="mchip">${esc(counts.poor ?? 0)} poor</span>${num(x.backlog_rows) && x.backlog_rows > 0 ? `<span class="mchip">${esc(x.backlog_rows)} rows waiting to upload</span>` : ""}</div></div>
  </section>`;
}

// --- audit -------------------------------------------------------------------------------------------------------

LOADERS.audit = async () => {
  S.audit = (await api("/api/audit")).entries;
};
V.audit = () => {
  if (!S.audit) return '<div class="empty">Loading…</div>';
  return `
  <section class="panel" aria-labelledby="h-au">
    <header><h2 id="h-au">Audit log</h2><span class="note">panel commands (identity from Cloudflare Access) and the server's own audit entries (agent, self-heal, deploys) · append-only on the box</span></header>
    <div class="tablewrap"><table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th><th>Result</th></tr></thead><tbody>
      ${S.audit.map((a) => {
        if (a.source === "panel") {
          const detail = actionDetail(a.action, a.args);
          const ok = a.ok === 1 ? pill("ok", "OK") : a.result === "queued" ? pill("off", "Queued") : pill("bad", "Refused");
          return `<tr><td class="mono num nowrap">${esc(clock(a.ts, true))}</td><td class="nowrap">${esc(a.login)}</td><td><strong>${esc(a.action)}</strong></td><td class="mono small">${esc(detail)}${a.ok === 0 ? `<div class="note">${esc(a.result)}</div>` : ""}</td><td>${ok}</td></tr>`;
        }
        return `<tr><td class="mono num nowrap">${esc(clock(a.ts, true))}</td><td class="nowrap">${esc(a.unit.replace(".service", ""))}</td><td><strong>server</strong></td><td class="mono small">${esc(a.msg)}</td><td>${/ok=False|failed|refused/i.test(a.msg) ? pill("bad", "Failed") : pill("ok", "Logged")}</td></tr>`;
      }).join("") || emptyRow(5, "No audit entries yet.")}
    </tbody></table></div>
  </section>`;
};

// --- render + data flow -------------------------------------------------------------------------------------------

// A tab switch builds the view afresh (and plays its rise animation); every later update of the same tab is a
// patch in the next animation frame (update()).
function render() {
  paintView(true);
  if (LOADERS[current] && !loading.has(current) && needsLoad(current)) load(current);
}
function update() {
  onFrame("view", () => paintView(false));
}
function paintView(full) {
  const main = $("#main");
  main.setAttribute("role", "tabpanel");
  main.setAttribute("aria-labelledby", `tab-${current}`);
  const view = main.firstElementChild;
  const fresh = full || !view || view.dataset.tab !== current;
  const paint = Object.hasOwn(V, current) ? V[current] : V.overview;
  if (fresh) main.innerHTML = `<div class="view" data-tab="${current}">${paint()}</div>`;
  else patch(view, paint());
  applyDyn(main);
  watchCharts(main, fresh);
  if (current === "overview") paintOverview();
  if (current === "console" && fresh) wireConsole();
  if (current === "logs" && LOG.data) wireLogs();
  if (current === "players") wirePlayers();
}
// wire*() run after every paint; each element gets its listeners once
const WIRED = new WeakSet();
function once(el) {
  if (!el || WIRED.has(el)) return false;
  WIRED.add(el);
  return true;
}
const loaded = Object.create(null);
const loading = new Set();
const needsLoad = (id) => !loaded[id] || Date.now() - loaded[id] > POLL_MS;
async function load(id) {
  if (!Object.hasOwn(LOADERS, id)) return;
  loading.add(id);
  try {
    await LOADERS[id]();
    loaded[id] = Date.now();
  } catch (e) {
    if (!(e instanceof ApiError && e.message === "forbidden")) toast(`Couldn't load ${id}: ${e.message}`);
    loaded[id] = Date.now();
  } finally {
    loading.delete(id);
  }
  if (current === id && !(id === "players" && $("#wlName")?.value)) update();
}

async function refresh() {
  S.o = await api("/api/overview");
  onFrame("header", () => {
    paintHeader();
    updateCounts();
  });
  if (current === "overview") {
    update();
  } else if (current !== "console" && LOADERS[current] && needsLoad(current) && !(current === "players" && $("#wlName")?.value)) {
    load(current);
  } else if (current === "health" && $("#h-hc")) {
    update();
  }
}

function tick() {
  if (document.hidden || lost) return;
  refresh().catch((e) => {
    if (!(e instanceof ApiError && e.message === "forbidden")) $("#statePill").textContent = "Unreachable";
  });
}

// --- theme ---------------------------------------------------------------------------------------------------------

function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  $("#themeBtn").setAttribute("aria-label", `Colour theme: ${t || "system"} (switch)`);
}
function initTheme() {
  let t = null;
  try {
    t = localStorage.getItem("mc-theme");
  } catch {
    t = null;
  }
  applyTheme(t);
  $("#themeBtn").addEventListener("click", () => {
    const order = [null, "light", "dark"];
    const cur = document.documentElement.dataset.theme || null;
    const next = order[(order.indexOf(cur) + 1) % order.length];
    applyTheme(next);
    try {
      if (next) localStorage.setItem("mc-theme", next);
      else localStorage.removeItem("mc-theme");
    } catch {
      // per-viewer convenience only
    }
    if (current === "overview" || current === "health") update();
  });
}

// --- boot ------------------------------------------------------------------------------------------------------------

function boot() {
  initTheme();
  const active = initFx();
  let start = location.hash.slice(1);
  if (!start) {
    try {
      start = localStorage.getItem("mc-tab") || "overview";
    } catch {
      start = "overview";
    }
  }
  current = TABS.some((t) => t[0] === start) ? start : "overview";
  buildTabs();
  $("#tablist").addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (b) go(b.dataset.id);
  });
  $("#tablist").addEventListener("keydown", (e) => {
    const keys = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" };
    if (!(e.key in keys)) return;
    e.preventDefault();
    const i = TABS.findIndex((t) => t[0] === current);
    const k = keys[e.key];
    const n = k === "first" ? 0 : k === "last" ? TABS.length - 1 : (i + k + TABS.length) % TABS.length;
    go(TABS[n][0], true);
  });
  $("#tablist").addEventListener("click", active); // a tab switch animates: measure it too
  $("#main").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const d = b.dataset;
    if (d.go) go(d.go);
    else if (d.click) $(`#${d.click}`).click();
    else if (d.act) moderateDlg(d.act, d.name);
    else if (d.wrm) removeDlg(d.wrm);
    else if (d.req) rejectedDlg(d.name, d.req === "ok");
    else if (d.pardon !== undefined) pardonDlg(d.pardon, d.kind);
    else if (d.plg) pluginDlg(d.plg, d.on === "1");
    else if (d.restore) restoreDlg(d.restore);
    else if (d.wsw) worldSwitchDlg(d.wsw);
    else if (d.wdel) worldDeleteDlg(d.wdel);
    else if (b.id === "wCreate") worldCreateDlg();
    else if (b.id === "banNew") banNewDlg();
    else if (b.id === "selftestRun") selftestDlg();
    else if (d.f) {
      CON.filter = d.f;
      $$(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.f === CON.filter)));
      drawConsole();
    } else if (d.range) {
      H.range = d.range;
      load("health");
    } else if (d.xprange) {
      XP.range = d.xprange;
      load("experience");
    } else if (d.xpp !== undefined) {
      XP.playerId = d.xpp || null;
      XP.player = null;
      if (!XP.playerId) update();
      else load("experience").then(() => requestAnimationFrame(() => window.scrollTo(0, 0)));
    }
  });
  $("#btnRestart").addEventListener("click", restartDlg);
  $("#btnStop").addEventListener("click", () => (S.o && S.o.watchdog && S.o.watchdog.admin_stopped ? startDlg() : stopDlg()));
  $("#btnBackup").addEventListener("click", backupDlg);
  $("#cdCancel").addEventListener("click", () => runAction("cancel", {}, "Cancel"));
  confirmBase().catch(() => {}); // early, so opening the confirmation window keeps the click's user activation
  setInterval(() => document.hidden || paintJob(), 1000);
  window.addEventListener("hashchange", () => go(location.hash.slice(1)));
  // hidden: no polls, timers or frames (each one checks document.hidden); shown again: catch up at once
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    tick();
    pollJob();
    if (current === "console" && CON.timer) {
      subscribe();
      pollConsole();
    }
  });
  render();
  tick();
  setInterval(tick, POLL_MS);
  setInterval(() => {
    // the uptime counter runs between polls
    const s = S.o && S.o.sample;
    if (!document.hidden && s && s.up && num(s.uptime_s) && S.o.sample_age_s <= 180) {
      setText($("#uptime"), fmtUp(s.uptime_s + (Date.now() - s.ts) / 1000));
    }
  }, 1000);
}

boot();
