// The manager's phone. It draws the simulator's chat from /sim/state and sends what the manager writes:
// the simulator turns each message into Meta's webhook for Musha.
"use strict";

const $ = (id) => document.getElementById(id);

// Line icons on a 24 grid in the current text colour, drawn for Musha's phone
const STROKE = {
  back: '<path d="M19.5 12H5M11 5.5 4.5 12l6.5 6.5"/>',
  video: '<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="m15.5 10.2 5.3-3v9.6l-5.3-3"/>',
  call: '<path d="M6.6 3.5h-2a1.5 1.5 0 0 0-1.5 1.6 16.5 16.5 0 0 0 15.8 15.8 1.5 1.5 0 0 0 1.6-1.5v-2a1.5 1.5 0 0 0-1.2-1.5l-3.2-.7a1.5 1.5 0 0 0-1.4.4l-1.2 1.2a12 12 0 0 1-5.3-5.3l1.2-1.2a1.5 1.5 0 0 0 .4-1.4L8.1 4.7a1.5 1.5 0 0 0-1.5-1.2Z"/>',
  smile: '<circle cx="12" cy="12" r="9"/><path d="M8.2 14.2a4.6 4.6 0 0 0 7.6 0"/><circle cx="9" cy="9.8" r=".6" fill="currentColor"/><circle cx="15" cy="9.8" r=".6" fill="currentColor"/>',
  clip: '<path d="M20.5 11.5 12.4 19.6a5 5 0 0 1-7-7l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.4 8.4a1.7 1.7 0 0 1-2.4-2.4l7.7-7.7"/>',
  camera: '<path d="M4 8h3l1.6-2.2h6.8L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Z"/><circle cx="12" cy="13" r="3.4"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  trash: '<path d="M4.5 6.5h15M9.5 6.5V4.8a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.7M6.5 6.5l.9 12.6a1.5 1.5 0 0 0 1.5 1.4h6.2a1.5 1.5 0 0 0 1.5-1.4l.9-12.6M10 10.5v6M14 10.5v6"/>',
  doc: '<path d="M6 2.8h8.2L19 7.6V20a1.2 1.2 0 0 1-1.2 1.2H6A1.2 1.2 0 0 1 4.8 20V4A1.2 1.2 0 0 1 6 2.8Z"/><path d="M14 3v4.8h4.8M8.5 12.5h7M8.5 16h5"/>',
  gallery: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="9.5" r="1.8"/><path d="m3.5 17.5 5-5 4 4 2.5-2.5 5.5 5.5"/>',
  headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3.5" y="14" width="4.5" height="6.5" rx="1.5"/><rect x="16" y="14" width="4.5" height="6.5" rx="1.5"/>',
  receipt: '<path d="M6 3h12v18l-2-1.4-2 1.4-2-1.4-2 1.4-2-1.4L6 21V3Z"/><path d="M9 8h6M9 11.5h6M9 15h3.5"/>',
  box: '<path d="M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5v-9Z"/><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9"/>',
  tick: '<path d="m4.5 12.5 4 4 9-9.5"/>',
  ticks: '<path d="M2.5 12.5 6.5 16.5 14 8M10 15.5l1 1L18.5 8"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.4v.1"/>',
  reply: '<path d="M9.5 7 4.5 12l5 5M5 12h9a5.5 5.5 0 0 1 5.5 5.5V19"/>',
  wifi: '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.3a9.5 9.5 0 0 1 13 0M8.6 15.6a5 5 0 0 1 6.8 0"/><circle cx="12" cy="18.8" r=".9" fill="currentColor"/>',
};
const FILL = {
  dots: '<circle cx="12" cy="5.5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="18.5" r="1.7"/>',
  mic: '<rect x="8.6" y="2.5" width="6.8" height="12" rx="3.4"/><path d="M5.6 11.2a6.4 6.4 0 0 0 12.8 0h-1.7a4.7 4.7 0 0 1-9.4 0H5.6Zm5.55 6.85h1.7V21.5h-1.7z"/>',
  send: '<path d="M3.4 20.6 21.5 12 3.4 3.4l-.02 6.7L16 12 3.38 13.9l.02 6.7Z"/>',
  play: '<path d="M7 4.5v15l12.5-7.5L7 4.5Z"/>',
  pause: '<rect x="6" y="4.5" width="4" height="15" rx="1"/><rect x="14" y="4.5" width="4" height="15" rx="1"/>',
};
const SIZE = { back: 22, video: 22, call: 20, dots: 22, smile: 24, clip: 22, camera: 22, mic: 22, send: 21, close: 20,
  trash: 21, mark: 24, doc: 26, gallery: 26, headphones: 26, receipt: 26, box: 26 };

function svg(name, size) {
  const s = size || SIZE[name] || 20;
  if (name === "mark") {  // Musha's own mark, the roofline M
    return `<svg width="${s}" height="${s}" viewBox="0 0 64 64" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 46V20l18 15 18-15v26"/></svg>`;
  }
  const fill = name in FILL;
  return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" aria-hidden="true" fill="${fill ? "currentColor" : "none"}"`
    + ` stroke="${fill ? "none" : "currentColor"}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">`
    + `${fill ? FILL[name] : STROKE[name] || ""}</svg>`;
}

// The doodle wallpaper: a tuck shop's day in small line drawings, tiled behind the chat
const GLYPHS = {
  house: "M4 11.5 12 4.5l8 7M6 9.8V20h12V9.8M10 20v-5h4v5",
  egg: "M12 3.2c3.4 0 6.2 6 6.2 10a6.2 6.2 0 0 1-12.4 0c0-4 2.8-10 6.2-10Z",
  bread: "M6 20V11a3.6 3.6 0 0 1-1-6.6C6 3.8 8 3.5 12 3.5s6 .3 7 .9A3.6 3.6 0 0 1 18 11v9H6ZM10 8v2.5M14 8v2.5",
  bottle: "M10 2.8h4v3.4l2 3V20a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V9.2l2-3V2.8ZM8 13h8",
  coin: "M12 3.5a8.5 8.5 0 1 1 0 17 8.5 8.5 0 0 1 0-17ZM14.6 9.3c-.6-.8-1.5-1.2-2.6-1.2-1.4 0-2.5.8-2.5 1.9 0 2.6 5 1.4 5 4.1 0 1.1-1.1 1.9-2.5 1.9-1.1 0-2-.4-2.6-1.2M12 6.6v10.8",
  chat: "M4.5 5h15a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H10l-4.5 3.5V16h-1a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1ZM8 9.5h8M8 12.5h5",
  heart: "M12 19.5s-7-4.3-7-9.8a3.9 3.9 0 0 1 7-2.4 3.9 3.9 0 0 1 7 2.4c0 5.5-7 9.8-7 9.8Z",
  star: "m12 3.8 2.5 5.1 5.6.8-4 3.9 1 5.6L12 16.6l-5.1 2.6 1-5.6-4-3.9 5.6-.8L12 3.8Z",
  leaf: "M5 19C5 10.5 10.5 5 19 5c0 8.5-5.5 14-14 14ZM5 19l7.5-7.5",
  phone: "M8.5 3h7A1.5 1.5 0 0 1 17 4.5v15a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 7 19.5v-15A1.5 1.5 0 0 1 8.5 3ZM11 18h2",
  basket: "M3.5 10h17l-2.2 9.2a1 1 0 0 1-1 .8H6.7a1 1 0 0 1-1-.8L3.5 10ZM8 10l3-5.5M16 10l-3-5.5M9 13v3.5M12 13v3.5M15 13v3.5",
  sun: "M12 8.3a3.7 3.7 0 1 1 0 7.4 3.7 3.7 0 0 1 0-7.4ZM12 2.8v2.2M12 19v2.2M2.8 12H5M19 12h2.2M5.5 5.5 7 7M17 17l1.5 1.5M5.5 18.5 7 17M17 7l1.5-1.5",
  receipt: "M6 3h12v18l-2-1.4-2 1.4-2-1.4-2 1.4-2-1.4L6 21V3ZM9 8h6M9 11.5h6M9 15h3.5",
  cup: "M5 8h11v6.5A4.5 4.5 0 0 1 11.5 19h-2A4.5 4.5 0 0 1 5 14.5V8ZM16 10h1.8a2 2 0 0 1 0 4H16M8.5 3.5v2M12 3.5v2",
  tomato: "M12 7.5c-3.8-1.8-7.5.8-7 5.6.5 4.7 3.8 6.9 7 6 3.2.9 6.5-1.3 7-6 .5-4.8-3.2-7.4-7-5.6ZM12 7.5c0-1.9.9-3.2 3-4M9.5 6.5 12 7.5l2.5-1",
  key: "M8 10.5a4 4 0 1 1 0 .1ZM11.8 11.8 20 20M16.5 16.5l2-2M18.5 18.5l1.5-1.5",
  bag: "M5.5 8h13l-1 12.5h-11L5.5 8ZM9 8V6.5a3 3 0 0 1 6 0V8",
};
const PLACES = [
  ["house", 14, 10, -8, 1.15], ["egg", 92, 22, 14, 1], ["coin", 168, 6, -12, 1.05], ["chat", 240, 26, 8, 1.1],
  ["bread", 38, 78, 10, 1.1], ["star", 120, 84, -16, .9], ["bottle", 196, 76, 18, 1.1], ["heart", 262, 104, -10, .85],
  ["leaf", 6, 146, 20, 1], ["phone", 82, 150, -14, 1], ["basket", 150, 146, 6, 1.15], ["sun", 226, 158, 0, 1.05],
  ["receipt", 32, 216, -6, 1.05], ["cup", 104, 222, 12, 1.05], ["tomato", 176, 214, -10, 1], ["key", 246, 230, 24, .95],
  ["bag", 266, 270, -8, .8], ["egg", 140, 272, -24, .75],
];
const DOTS = [[66, 8, 1.6], [146, 54, 1.3], [226, 6, 1.4], [292, 64, 1.2], [70, 124, 1.4], [132, 118, 1.1], [208, 128, 1.5],
  [290, 190, 1.4], [16, 196, 1.2], [88, 196, 1.2], [160, 194, 1.3], [222, 210, 1.1], [12, 276, 1.4], [76, 268, 1.3],
  [210, 276, 1.4], [292, 150, 1.1]];

function tile(stroke, opacity) {
  const marks = PLACES.map(([g, x, y, r, s]) =>
    `<path transform="translate(${x} ${y}) rotate(${r} 12 12) scale(${s})" d="${GLYPHS[g]}"/>`).join("");
  const dots = DOTS.map(([x, y, r]) => `<circle cx="${x}" cy="${y}" r="${r}"/>`).join("");
  const art = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">`
    + `<g fill="none" stroke="${stroke}" stroke-opacity="${opacity}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${marks}</g>`
    + `<g fill="${stroke}" fill-opacity="${opacity}">${dots}</g></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(art)}")`;
}

document.documentElement.style.setProperty("--wa-wall-light", tile("#5c4a2e", 0.15));
document.documentElement.style.setProperty("--wa-wall-dark", tile("#e9edef", 0.06));

// A viewer's own choices: the theme and the wire, kept in this browser when it allows
function kept(name) {
  try { return localStorage.getItem(name); } catch { return null; }
}
function keep(name, value) {
  try { localStorage.setItem(name, value); } catch { /* the page works the same without it */ }
}

const params = new URLSearchParams(location.search);
const startTheme = params.get("theme") || kept("sim-theme");
if (startTheme === "light" || startTheme === "dark") document.documentElement.dataset.theme = startTheme;
if (params.has("film") || kept("sim-film") === "1") document.body.classList.add("film");

function theme() {
  return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

// Words and numbers
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// WhatsApp's own marks: *bold*, _italic_, ~struck~
function fmt(s) {
  return esc(s)
    .replace(/(^|[\s(])\*(?!\s)([^*\n]+?)\*(?=$|[\s).,!?:;])/g, "$1<strong>$2</strong>")
    .replace(/(^|[\s(])_(?!\s)([^_\n]+?)_(?=$|[\s).,!?:;])/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])~(?!\s)([^~\n]+?)~(?=$|[\s).,!?:;])/g, "$1<s>$2</s>");
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const length = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const bytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} kB`);
function day(t) {
  const d = new Date(t * 1000), today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
}
function summary(l) {
  if (l.kind === "image") return l.text ? `Photo: ${l.text}` : "Photo";
  if (l.kind === "audio") return l.voice ? `Voice message (${length(l.seconds || 0)})` : "Audio";
  if (l.kind === "document") return l.filename || "Document";
  return l.text;
}

// The chat
let state = null;
let version = -1;
let replyTo = null;
const asked = new Set();  // Musha's lines the phone has reported as shown
const nodes = new Map();  // key -> {el, sig}
const players = new Map();  // media url -> Audio

function ticks(s) {
  const [name, cls, label] = {
    sending: ["clock", "", "Sending"], sent: ["tick", "", "Sent"], delivered: ["ticks", "", "Delivered"],
    read: ["ticks", " ph-ticks-read", "Read"], failed: ["alert", " ph-ticks-failed", "Failed"],
  }[s] || ["clock", "", "Sending"];
  return `<span class="ph-ticks${cls}" role="img" aria-label="${label}">${svg(name, name === "clock" || name === "alert" ? 13 : 16)}</span>`;
}

function bars(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return Array.from({ length: 30 }, (_, i) => {
    h = (h * 1103515245 + 12345) >>> 0;
    const wave = 0.45 + 0.35 * Math.sin(i / 2.3);
    return Math.round(18 + 82 * Math.min(1, wave * (0.55 + (h % 1000) / 1000 * 0.75)));
  });
}

function voice(l) {
  const initial = esc((state.me.name || "F")[0]);
  const lines = bars(l.id).map((v) => `<span style="height:${v}%"></span>`).join("");
  return `<div class="ph-voice" data-src="${esc(l.media)}" data-seconds="${l.seconds || 0}">`
    + `<span class="ph-voice-face" aria-hidden="true">${initial}<i>${svg("mic", 12)}</i></span>`
    + `<button type="button" class="ph-voice-play" aria-label="Play the voice message">${svg("play", 20)}</button>`
    + `<span class="ph-voice-bars" aria-hidden="true">${lines}</span>`
    + `<span class="ph-voice-time">${length(l.seconds || 0)}</span></div>`;
}

function bubble(l, first, byId) {
  const mine = l.side === "me";
  let html = "";
  const quoted = l.reply_to && byId.get(l.reply_to);
  if (quoted) {
    const who = quoted.side === "me" ? "You" : state.business.name;
    html += `<div class="ph-quote"><strong>${esc(who)}</strong><span>${esc(summary(quoted))}</span></div>`;
  }
  if (l.kind === "image") {
    html += `<button type="button" class="ph-photo" data-zoom="${esc(l.media)}" aria-label="Open the photo">`
      + `<img src="${esc(l.media)}" alt="${esc(l.text || "Photo")}"></button>`;
    if (l.text) html += `<p class="ph-caption">${fmt(l.text)}</p>`;
  } else if (l.kind === "document") {
    const ext = ((l.filename.split(".").pop() || "") || l.mime.split("/").pop() || "file").slice(0, 4).toUpperCase();
    html += `<a class="ph-doc" href="${esc(l.media)}" target="_blank" rel="noopener"><span class="ph-doc-icon">${esc(ext)}</span>`
      + `<span><strong>${esc(l.filename || "Document")}</strong><small>${bytes(l.size)}, ${esc(ext)}</small></span></a>`;
    if (l.text) html += `<p class="ph-caption">${fmt(l.text)}</p>`;
  } else if (l.kind === "audio") {
    html += voice(l);
  } else {
    html += `<p class="ph-text">${fmt(l.text)}</p>`;
  }
  if (l.state === "failed" && l.note) html += `<p class="ph-why">${esc(l.note)}</p>`;
  html += `<span class="ph-meta">${clock(l.at)}${mine ? ticks(l.state) : ""}</span>`;
  if (!mine) html += `<button type="button" class="ph-reply" data-reply="${esc(l.id)}" aria-label="Reply to this message">${svg("reply", 16)}</button>`;
  const cls = `ph-b ${mine ? "ph-out" : "ph-in"}${first ? " ph-first" : ""}${l.state === "failed" ? " ph-failed" : ""}`
    + `${l.kind === "audio" ? " ph-b-voice" : ""}`;
  return { cls, html };
}

function place(container, items, anchor) {
  // Keep each node that still says the same, and put the list in order after the anchor
  const wanted = new Set(items.map((i) => i.key));
  for (const [key, n] of nodes) {
    if (!wanted.has(key)) {
      n.el.remove();
      nodes.delete(key);
    }
  }
  let ref = anchor.nextSibling;
  for (const item of items) {
    let n = nodes.get(item.key);
    if (!n || n.sig !== item.sig) {
      const el = item.make();
      if (n) {
        el.classList.add("ph-settled");
        n.el.replaceWith(el);
      }
      n = { el, sig: item.sig };
      nodes.set(item.key, n);
    }
    if (n.el !== ref) container.insertBefore(n.el, ref);
    else ref = ref.nextSibling;
  }
}

function render() {
  const b = state.business;
  $("who").textContent = b.name;
  $("sub").textContent = state.typing ? "typing..." : b.subtitle;
  $("notice").innerHTML = `${esc(b.name)}'s WhatsApp Business ${esc(b.subtitle)}, <span class="ph-nowrap">${esc(b.display)}</span>.`;

  const thread = $("thread");
  const atEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  const before = nodes.size;
  const byId = new Map(state.lines.map((l) => [l.id, l]));
  const items = [];
  let lastDay = "", lastSide = "";
  for (const l of state.lines) {
    const d = day(l.at);
    if (d !== lastDay) {
      items.push({ key: `day:${d}`, sig: d, make: () => {
        const el = document.createElement("div");
        el.className = "ph-day";
        el.innerHTML = `<span>${esc(d)}</span>`;
        return el;
      } });
      lastDay = d;
      lastSide = "";
    }
    const first = l.side !== lastSide;
    lastSide = l.side;
    const { cls, html } = bubble(l, first, byId);
    items.push({ key: l.id, sig: cls + html, make: () => {
      const el = document.createElement("div");
      el.className = cls;
      el.dataset.id = l.id;
      el.innerHTML = html;
      return el;
    } });
  }
  if (state.typing) {
    items.push({ key: "typing", sig: "typing", make: () => {
      const el = document.createElement("div");
      el.className = "ph-b ph-in ph-first ph-typing";
      el.setAttribute("aria-label", `${b.name} is typing`);
      el.innerHTML = "<span></span><span></span><span></span>";
      return el;
    } });
  }
  place($("lines"), items, $("notice"));
  if (atEnd || nodes.size > before) thread.scrollTop = thread.scrollHeight;
  renderWire();
  markSeen();
}

function markSeen() {
  if (!state || document.visibilityState !== "visible") return;
  const ids = state.lines.filter((l) => l.side === "them" && l.state !== "read" && !asked.has(l.id)).map((l) => l.id);
  if (!ids.length) return;
  ids.forEach((id) => asked.add(id));
  post("/sim/seen", { ids });
}
document.addEventListener("visibilitychange", markSeen);

// The wire
const wired = new Set();

function renderWire() {
  const v = state.verified;
  const check = v === true ? "Webhook verified." : v === false ? "The webhook check failed." : "Verify the webhook from the phone's menu.";
  const window24 = state.window ? "The 24-hour window is open."
    : "The 24-hour window opens with the manager's next message.";
  $("wireLine").textContent = `${check} ${window24}`;
  const list = $("wireList");
  if (!state.wire.length && !list.children.length) {
    list.innerHTML = '<li class="wire-empty">Calls show here as they cross.</li>';
  }
  for (const w of state.wire) {
    if (wired.has(w.n)) continue;
    wired.add(w.n);
    list.querySelector(".wire-empty")?.remove();
    const li = document.createElement("li");
    const meta = w.way === "webhook";
    const headers = Object.entries(w.headers || {}).map(([k, val]) => `${k}: ${val}`).join("\n");
    const body = (headers ? `<b>Headers</b>${esc(headers)}\n` : "")
      + (w.request ? `<b>${meta ? "Payload" : "Request"}</b>${esc(w.request)}\n` : "")
      + (w.response ? `<b>Answer</b>${esc(w.response)}` : "");
    li.innerHTML = `<details><summary>`
      + `<span class="wire-way${meta ? " in" : ""}">${meta ? "Meta → Musha" : "Musha → Meta"}</span>`
      + `<span class="wire-path">${esc(w.method)} ${esc(w.path)}</span>`
      + `<span class="wire-status${w.status >= 200 && w.status < 300 ? "" : " bad"}">${w.status || "none"}</span>`
      + `<span class="wire-what">${esc(w.what)}, ${new Date(w.at * 1000).toLocaleTimeString()}</span>`
      + `</summary><div class="wire-body">${body}</div></details>`;
    list.prepend(li);
  }
  while (list.children.length > 80) list.lastElementChild.remove();
}

// Talking to the simulator
async function post(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    return r.ok ? await r.json() : { error: (await r.json().catch(() => ({}))).detail || `The simulator answered ${r.status}.` };
  } catch {
    return { error: "The simulator is out of reach." };
  }
}

let toastTimer = 0;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3800);
}

async function poll() {
  for (;;) {
    try {
      const r = await fetch(`/sim/state?after=${version}`, { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      state = await r.json();
      version = state.version;
      render();
    } catch {
      await new Promise((done) => setTimeout(done, 1500));
    }
  }
}

// Replying to one of Musha's lines
function setReply(id) {
  const l = state?.lines.find((x) => x.id === id);
  if (!l) return;
  replyTo = l;
  $("replytoText").textContent = summary(l);
  $("replyto").hidden = false;
  $("text").focus();
}
function takeReply() {
  const id = replyTo ? replyTo.id : "";
  replyTo = null;
  $("replyto").hidden = true;
  return id;
}
$("replytoClose").addEventListener("click", takeReply);

// The bar
const input = $("text");
const round = $("round");

function syncRound() {
  const [name, label] = recorder ? ["send", "Send the voice message"]
    : input.value.trim() ? ["send", "Send"] : ["mic", "Record a voice message"];
  if (round.dataset.icon !== name) {
    round.dataset.icon = name;
    round.innerHTML = svg(name);
  }
  round.setAttribute("aria-label", label);
}
input.addEventListener("input", syncRound);

async function sendText() {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  syncRound();
  const r = await post("/sim/text", { text, reply_to: takeReply() });
  if (r.error) toast(r.error);
}

$("compose").addEventListener("submit", (e) => {
  e.preventDefault();
  sendText();
});

round.addEventListener("click", () => {
  if (recorder) stopRecording(true);
  else if (input.value.trim()) sendText();
  else startRecording();
});

async function upload(blob, o) {
  const q = new URLSearchParams({ caption: o.caption || "", filename: o.filename || "", voice: o.voice ? "true" : "false",
    seconds: String(o.seconds || 0), reply_to: o.reply || "" });
  try {
    const r = await fetch(`/sim/media?${q}`, { method: "POST", headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob });
    if (!r.ok) toast((await r.json().catch(() => ({}))).detail || `The simulator answered ${r.status}.`);
  } catch {
    toast("The simulator is out of reach.");
  }
}

// Voice messages: tap the mic to record, tap send to send
let recorder = null;
let recording = { stream: null, chunks: [], start: 0, timer: 0 };

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    toast("This browser records through Audio in the attach menu.");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast("Allow the microphone in the browser to record a voice message.");
    return;
  }
  const type = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find((t) => MediaRecorder.isTypeSupported(t));
  recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  recording = { stream, chunks: [], start: Date.now(), timer: 0 };
  recorder.ondataavailable = (e) => { if (e.data.size) recording.chunks.push(e.data); };
  recorder.start();
  $("field").hidden = true;
  $("recording").hidden = false;
  round.classList.add("live");
  $("recTime").textContent = "0:00";
  recording.timer = setInterval(() => { $("recTime").textContent = length((Date.now() - recording.start) / 1000); }, 250);
  syncRound();
}

function stopRecording(send) {
  const r = recorder, rec = recording;
  const seconds = Math.max(1, Math.round((Date.now() - rec.start) / 1000));
  recorder = null;
  clearInterval(rec.timer);
  $("recording").hidden = true;
  $("field").hidden = false;
  round.classList.remove("live");
  syncRound();
  r.onstop = () => {
    rec.stream.getTracks().forEach((t) => t.stop());
    if (send && rec.chunks.length) {
      const blob = new Blob(rec.chunks, { type: r.mimeType || "audio/webm" });
      upload(blob, { voice: true, seconds, reply: takeReply() });
    }
  };
  r.stop();
}
$("trash").addEventListener("click", () => stopRecording(false));

// The attach menu and the camera
const attach = $("attach");
const clip = $("clip");
// The sample photos, each with its file name and the caption the preview starts with. The caption says what
// the photo shows, so Musha files the goods as goods even before the receipt is in (routes/whatsapp.py).
const SAMPLES = { receipt: ["Receipt.jpg", "Receipt"], goods: ["Goods.jpg", "Goods"] };

function closeAttach() {
  attach.hidden = true;
  clip.setAttribute("aria-expanded", "false");
}
clip.addEventListener("click", () => {
  const open = attach.hidden;
  attach.hidden = !open;
  clip.setAttribute("aria-expanded", String(open));
  if (open) attach.querySelector("button")?.focus();
});
$("cam").addEventListener("click", () => $("pickCamera").click());

attach.addEventListener("click", async (e) => {
  const item = e.target.closest("[data-pick]");
  if (!item) return;
  closeAttach();
  const pick = item.dataset.pick;
  if (Object.hasOwn(SAMPLES, pick)) {
    const [name, caption] = SAMPLES[pick];
    try {
      const r = await fetch(`/sim/sample/${pick}`, { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      openPreview(await r.blob(), name, caption);
    } catch {
      toast("The sample photo stopped on its way. Try again.");
    }
    return;
  }
  $({ doc: "pickDoc", camera: "pickCamera", photo: "pickPhoto", audio: "pickAudio" }[pick]).click();
});

for (const id of ["pickDoc", "pickCamera", "pickPhoto", "pickAudio"]) {
  $(id).addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    if (f.type.startsWith("audio/")) upload(f, { filename: f.name, reply: takeReply() });
    else openPreview(f, f.name);
  });
}

// A file on its way, with a caption
let pending = null;
let previewUrl = "";

function openPreview(blob, name, caption = "") {
  pending = { blob, name };
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(blob);
  const ext = (name.split(".").pop() || "file").slice(0, 4).toUpperCase();
  $("previewBody").innerHTML = blob.type.startsWith("image/")
    ? `<img src="${previewUrl}" alt="The photo to send">`
    : `<div class="ph-preview-doc"><span class="ph-doc-icon">${esc(ext)}</span><strong>${esc(name)}</strong><small>${bytes(blob.size)}</small></div>`;
  $("previewName").textContent = name;
  $("caption").value = caption;
  $("preview").hidden = false;
  $("caption").focus();
}
function closePreview() {
  $("preview").hidden = true;
  pending = null;
}
$("previewClose").addEventListener("click", closePreview);
$("previewForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!pending) return;
  const { blob, name } = pending;
  const caption = $("caption").value.trim();
  closePreview();
  upload(blob, { caption, filename: name, reply: takeReply() });
});

// The thread's own buttons: a photo opens, a voice message plays, a line takes a reply
$("lines").addEventListener("click", (e) => {
  const zoom = e.target.closest("[data-zoom]");
  if (zoom) {
    $("zoomImg").src = zoom.dataset.zoom;
    $("zoom").hidden = false;
    $("zoomClose").focus();
    return;
  }
  const reply = e.target.closest("[data-reply]");
  if (reply) {
    setReply(reply.dataset.reply);
    return;
  }
  const play = e.target.closest(".ph-voice-play");
  if (play) playVoice(play.closest(".ph-voice"));
});
$("lines").addEventListener("dblclick", (e) => {
  const b = e.target.closest(".ph-in[data-id]");
  if (b) setReply(b.dataset.id);
});
$("zoomClose").addEventListener("click", () => { $("zoom").hidden = true; });

function playVoice(box) {
  const src = box.dataset.src;
  let a = players.get(src);
  if (!a) {
    a = new Audio(src);
    players.set(src, a);
  }
  const paint = () => {
    const el = document.querySelector(`.ph-voice[data-src="${CSS.escape(src)}"]`);
    if (!el) return;
    const total = Number.isFinite(a.duration) && a.duration > 0 ? a.duration : Number(el.dataset.seconds) || 1;
    const on = Math.round((a.currentTime / total) * 30);
    el.querySelectorAll(".ph-voice-bars span").forEach((s, i) => s.classList.toggle("on", i < on));
    el.querySelector(".ph-voice-time").textContent = length(a.paused && a.currentTime === 0 ? total : a.currentTime);
    const button = el.querySelector(".ph-voice-play");
    button.innerHTML = svg(a.paused ? "play" : "pause", 20);
    button.setAttribute("aria-label", a.paused ? "Play the voice message" : "Pause the voice message");
  };
  if (!a.wired) {
    a.wired = true;
    a.addEventListener("timeupdate", paint);
    a.addEventListener("pause", paint);
    a.addEventListener("play", paint);
    a.addEventListener("ended", () => { a.currentTime = 0; paint(); });
  }
  if (a.paused) {
    players.forEach((other) => { if (other !== a) other.pause(); });
    a.play().catch(() => toast("This browser plays that recording from a file."));
  } else {
    a.pause();
  }
}

// The three dots
const more = $("more");
const moreMenu = $("moreMenu");

function closeMore() {
  moreMenu.hidden = true;
  more.setAttribute("aria-expanded", "false");
}
function labelMenu() {
  moreMenu.querySelector('[data-act="theme"]').textContent = theme() === "dark" ? "Light theme" : "Dark theme";
  moreMenu.querySelector('[data-act="wire"]').textContent = document.body.classList.contains("film") ? "Show the wire" : "Hide the wire";
}
more.addEventListener("click", () => {
  const open = moreMenu.hidden;
  labelMenu();
  moreMenu.hidden = !open;
  more.setAttribute("aria-expanded", String(open));
  if (open) moreMenu.querySelector("button").focus();
});
moreMenu.addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act) return;
  closeMore();
  if (act === "theme") {
    const next = theme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    keep("sim-theme", next);
  } else if (act === "wire") {
    const film = document.body.classList.toggle("film");
    keep("sim-film", film ? "1" : "0");
  } else if (act === "clear") {
    await post("/sim/clear");
  } else if (act === "verify") {
    const r = await post("/sim/verify");
    toast(r.ok ? "Musha answered Meta's webhook check." : r.status ? `Musha's webhook answered ${r.status}.`
      : r.error || "Musha's webhook is out of reach.");
  }
});

document.addEventListener("click", (e) => {
  if (!moreMenu.hidden && !e.target.closest("#moreMenu, #more")) closeMore();
  if (!attach.hidden && !e.target.closest("#attach, #clip")) closeAttach();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("zoom").hidden) $("zoom").hidden = true;
  else if (!$("preview").hidden) closePreview();
  else if (!moreMenu.hidden) closeMore();
  else if (!attach.hidden) closeAttach();
  else if (recorder) stopRecording(false);
  else if (replyTo) takeReply();
});

// The status bar
function tickClock() {
  $("clock").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).replace(/\s?[AP]M$/i, "");
}
$("sys").innerHTML = '<svg width="17" height="11" viewBox="0 0 17 11" aria-hidden="true" fill="currentColor">'
  + '<rect x="0" y="7" width="3" height="4" rx="1"/><rect x="4.5" y="5" width="3" height="6" rx="1"/>'
  + '<rect x="9" y="2.5" width="3" height="8.5" rx="1"/><rect x="13.5" y="0" width="3" height="11" rx="1"/></svg>'
  + svg("wifi", 16)
  + '<svg width="25" height="12" viewBox="0 0 25 12" aria-hidden="true"><rect x=".5" y=".5" width="21" height="11" rx="3.2" fill="none" stroke="currentColor" opacity=".45"/>'
  + '<rect x="2" y="2" width="15" height="8" rx="2" fill="currentColor"/><path d="M23 4v4c.8-.3 1.3-1.1 1.3-2S23.8 4.3 23 4Z" fill="currentColor" opacity=".45"/></svg>';
tickClock();
setInterval(tickClock, 15000);

document.querySelectorAll("[data-icon]").forEach((el) => { el.innerHTML = svg(el.dataset.icon); });
syncRound();
poll();
