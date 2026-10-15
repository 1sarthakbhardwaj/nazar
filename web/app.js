// Nazar dashboard: a calm, consumer-grade view of the engine (app.py). Technical details live in the
// developer drawer (press D); the main screen only shows the camera, a status, and alerts in plain words.
"use strict";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);

const TITLES = {
  person_fell: "Fall detected", object_fell: "Something fell", liquid_on_floor: "Spill on the floor",
  robbery_or_theft: "Possible theft", fight: "Fight detected", vandalism: "Damage detected", other: "Something's wrong",
};
const PHASES = {
  ready: { text: "Paused", sub: "Monitoring is off", tone: "idle" },
  watching: { text: "Monitoring", sub: "All quiet", tone: "ok" },
  collecting: { text: "Movement", sub: "Taking a closer look", tone: "warn" },
  analyzing: { text: "Reviewing", sub: "AI is checking what happened", tone: "ai" },
};

const S = {
  status: {}, phase: "ready", armed: false, alert: null, since: null,
  reviewed: 0, alerts: 0, speeds: [], pending: null, sound: true, audio: null, timers: {},
};

const post = (name, body) =>
  fetch(`/api/${name}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .catch(() => toast("Can't reach the camera service"));
const clockTime = (d = new Date()) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

// ------------------------------------------------------------ engine events

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => handle(JSON.parse(e.data));
}

function handle(ev) {
  switch (ev.type) {
    case "status": return applyStatus(ev);
    case "phase": return onPhase(ev);
    case "motion": return void ($("dMotion").textContent = `${ev.value.toFixed(1)}%`);
    case "sheet": return onReviewStart(ev);
    case "verdict": return onVerdict(ev);
    case "alert": return onAlert(ev);
    case "log": return devlog(ev.text, ev.kind);
    case "restart": return clearAlert();
  }
}

function applyStatus(s) {
  S.status = s;
  document.title = s.app || "Nazar";
  $("appName").textContent = s.app || "Nazar";
  $("roomName").textContent = s.camera;
  $("live").classList.toggle("hidden", s.source.kind === "file");
  $("dSource").textContent = s.source.name + (s.source.start ? ` from ${s.source.start}s` : "");
  $("dModel").textContent = `${s.model} · ${s.input}`;
  $("dQuota").textContent = `${s.used}/${s.cap}` + (s.free && s.free.remaining != null ? ` · ${s.free.remaining} free left` : "");
  if (document.activeElement !== $("sens")) {
    $("sens").value = s.trigger;
    $("sensVal").textContent = `${s.trigger}%`;
  }
  onPhase({ phase: s.phase, armed: s.armed });
}

function onPhase(ev) {
  if (ev.armed && !S.armed) {
    S.since = new Date();
    addItem("Monitoring started", `${S.status.camera || "Camera"} is being watched`, "ok");
  } else if (!ev.armed && S.armed) {
    addItem("Monitoring paused", "", "muted");
  }
  S.phase = ev.phase;
  S.armed = ev.armed;
  render();
}

function onReviewStart(ev) {
  clearAlert();
  S.pending = addItem("Movement noticed", "Reviewing", "ai", true);
  $("momentTitle").textContent = "Reviewing this moment";
  $("momentImg").src = ev.url;
  $("momentCard").classList.remove("hidden");
  $("sweep").classList.remove("hidden");
}

function settle(title, sub, tone) {
  $("sweep").classList.add("hidden");
  if (S.pending) {
    updateItem(S.pending, title, sub, tone);
    S.pending = null;
  } else {
    addItem(title, sub, tone);
  }
}

function onVerdict(ev) {
  if (!ev.ok) return settle("Couldn't review this moment", "", "muted");
  S.reviewed += 1;
  $("momentTitle").textContent = "Looks normal";
  settle("Everything looks normal", tidy(ev.why), "ok");
  render();
}

function onAlert(ev) {
  S.reviewed += 1;
  S.alerts += 1;
  S.speeds.push(ev.after);
  const title = ev.title || TITLES[ev.rule] || TITLES.other;
  settle(title, tidy(ev.why), "alert");
  const sent = (ev.deliveries || []).some((d) => d.ok);
  if (sent) addItem("Alert sent to your phone", "Telegram", "tg");
  $("momentTitle").textContent = "What happened";
  if (ev.sheet) $("momentImg").src = ev.sheet;

  $("alertTitle").textContent = title;
  $("alertSub").textContent = `${S.status.camera} · ${clockTime()}`;
  $("alertWhy").textContent = ev.why ? `“${tidy(ev.why)}”` : "";
  $("alertSpeed").textContent = `${Math.max(1, Math.round(ev.after))}s`;
  $("alertSent").classList.toggle("hidden", !sent);
  $("alert").classList.remove("hidden");
  $("feed").classList.add("alerting");
  S.alert = title;
  chime();
  clearTimeout(S.timers.alert);
  S.timers.alert = setTimeout(clearAlert, 60000);
  render();
}

function clearAlert() {
  clearTimeout(S.timers.alert);
  $("alert").classList.add("hidden");
  $("feed").classList.remove("alerting");
  if (S.alert) {
    S.alert = null;
    render();
  }
}

// "person fell onto floor and stayed down." -> "Person fell onto floor and stayed down"
const tidy = (s) => (s || "").trim().replace(/[.\s]+$/, "").replace(/^./, (c) => c.toUpperCase());

// ------------------------------------------------------------ rendering

function render() {
  const p = PHASES[S.phase] || PHASES.ready;
  const tone = S.alert ? "alert" : p.tone;
  const text = S.alert || p.text;
  const sub = S.alert ? "Alert sent to your phone" : S.phase === "watching" && S.since ? `Since ${clockTime(S.since)}` : p.sub;
  $("chip").className = `chip tone-${tone}`;
  $("chipText").textContent = text;
  $("statusDot").className = `status-dot tone-${tone}`;
  $("statusBig").textContent = text;
  $("statusSub").textContent = sub;
  $("monitor").setAttribute("aria-pressed", String(S.armed));
  $("statReviewed").textContent = S.reviewed;
  $("statAlerts").textContent = S.alerts;
  $("statSpeed").textContent = S.speeds.length
    ? `${Math.round(S.speeds.reduce((a, b) => a + b, 0) / S.speeds.length)}s` : "–";
}

function addItem(title, sub, tone, working = false) {
  const li = document.createElement("li");
  li.innerHTML = '<span class="dot"></span><div><div class="title"></div><div class="sub"></div></div><span class="when"></span>';
  li.querySelector(".when").textContent = clockTime();
  updateItem(li, title, sub, tone, working);
  $("none")?.remove();
  $("timeline").prepend(li);
  while ($("timeline").children.length > 30) $("timeline").lastChild.remove();
  return li;
}

function updateItem(li, title, sub, tone, working = false) {
  li.className = `item ${tone}`;
  li.querySelector(".title").textContent = title;
  const subEl = li.querySelector(".sub");
  subEl.textContent = sub || "";
  subEl.classList.toggle("dots", working);
  subEl.classList.toggle("hidden", !sub);
}

function devlog(text, kind) {
  const li = document.createElement("li");
  li.className = kind || "";
  li.textContent = `${new Date().toLocaleTimeString("en-GB")}  ${text}`;
  $("devlog").prepend(li);
  while ($("devlog").children.length > 80) $("devlog").lastChild.remove();
}

function toast(text) {
  $("toast").textContent = text;
  $("toast").classList.remove("hidden");
  clearTimeout(S.timers.toast);
  S.timers.toast = setTimeout(() => $("toast").classList.add("hidden"), 2200);
}

function tick() {
  $("clock").textContent = clockTime();
}

// a soft two-note chime, made in the browser
function unlockAudio() {
  try {
    S.audio = S.audio || new (window.AudioContext || window.webkitAudioContext)();
    if (S.audio.state === "suspended") S.audio.resume();
  } catch {}
}

function chime() {
  if (!S.sound || !S.audio) return;
  const a = S.audio, t0 = a.currentTime;
  [[0, 880], [0.16, 1318.5], [0.6, 880], [0.76, 1318.5]].forEach(([dt, f]) => {
    const o = a.createOscillator(), g = a.createGain();
    o.type = "sine";
    o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0 + dt);
    g.gain.exponentialRampToValueAtTime(0.22, t0 + dt + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.5);
    o.connect(g).connect(a.destination);
    o.start(t0 + dt);
    o.stop(t0 + dt + 0.55);
  });
}

// ------------------------------------------------------------ controls

function toggleMonitoring() {
  const now = performance.now();
  if (now - (S.lastToggle || 0) < 800) return;  // a double press or key repeat shouldn't flip it straight back
  S.lastToggle = now;
  unlockAudio();
  if (S.armed) return post("disarm");
  if (!S.status.key_set) return toast("Add your OpenRouter key first");
  clearAlert();
  post("arm");
}

// a clean slate for the next recording take: clip back to the start, timeline and stats cleared
function freshTake() {
  clearAlert();
  Object.assign(S, { reviewed: 0, alerts: 0, speeds: [], pending: null, since: S.armed ? new Date() : null });
  $("timeline").innerHTML = '<li class="none" id="none">Nothing yet today</li>';
  $("momentCard").classList.add("hidden");
  render();
  post("restart");
}

function toggleLayout() {
  const reel = $("app").classList.toggle("reel");
  $("app").classList.toggle("wide", !reel);
  try { localStorage.setItem("layout", reel ? "reel" : "wide"); } catch {}
}

const stream = $("stream");
// browsers don't reliably fire "load" for a live MJPEG image, so watch for its first frame instead
const firstFrame = setInterval(() => {
  if (stream.naturalWidth > 0) {
    $("empty").classList.add("hidden");
    clearInterval(firstFrame);
  }
}, 200);
stream.addEventListener("error", () => setTimeout(() => (stream.src = `/stream.mjpg?${Date.now()}`), 2000));
stream.src = "/stream.mjpg";

$("monitor").addEventListener("click", toggleMonitoring);
$("dCheck").addEventListener("click", () => { unlockAudio(); post("check"); });
$("dReplay").addEventListener("click", freshTake);
$("dLayout").addEventListener("click", toggleLayout);
document.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => setTimeout(() => b.blur(), 0)));
let sensTimer;
$("sens").addEventListener("input", () => {
  $("sensVal").textContent = `${$("sens").value}%`;
  clearTimeout(sensTimer);
  sensTimer = setTimeout(() => post("trigger", { value: Number($("sens").value) }), 150);
});

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || e.target.tagName === "INPUT") return;
  if (e.repeat) return e.code === "Space" && e.preventDefault();  // holding a key: act once
  const k = e.key.toLowerCase();
  if (e.code === "Space") { e.preventDefault(); toggleMonitoring(); }
  else if (k === "d") $("drawer").classList.toggle("hidden");
  else if (k === "c") { unlockAudio(); post("check"); }
  else if (k === "r") freshTake();
  else if (k === "p") post("pause");
  else if (k === "v") toggleLayout();
  else if (k === "h") $("app").classList.toggle("clean");
  else if (k === "f") document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  else if (k === "s") { S.sound = !S.sound; toast(S.sound ? "Sound on" : "Sound off"); }
  else if (k === "escape") { $("drawer").classList.add("hidden"); clearAlert(); }
});

try {
  if ((params.get("layout") || localStorage.getItem("layout")) === "reel") toggleLayout();
} catch {}
connect();
tick();
setInterval(tick, 1000);
render();
