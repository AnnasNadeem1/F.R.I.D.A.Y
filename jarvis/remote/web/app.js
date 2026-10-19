// Phone client for the assistant running on your PC.
// Pairing: the desktop app's QR code opens this page as /#pair=<token>; the token is
// kept in this browser only and sent with every API call.
"use strict";

const $ = (id) => document.getElementById(id);
const COLORS = { loading: "#8a8f98", off: "#4b4f57", idle: "#ff9f43", listening: "#2fe3c4",
                 thinking: "#b48cff", speaking: "#ffb86b", error: "#ff4d5e" };
const LABELS = { loading: "CONNECTING", off: "OFFLINE", idle: "READY", listening: "LISTENING",
                 thinking: "PROCESSING", speaking: "SPEAKING", error: "ERROR" };

let token = null;
try { token = localStorage.getItem("token"); } catch (e) {}
const pair = location.hash.match(/pair=([\w-]+)/);
if (pair) {
  token = pair[1];
  try { localStorage.setItem("token", token); } catch (e) {}
  history.replaceState(null, "", "/");  // don't leave the key sitting in the address bar
}

let name = "Friday";
let state = "loading";
let level = 0, targetLevel = 0;
let voiceOn = true;
try { voiceOn = localStorage.getItem("voice") !== "off"; } catch (e) {}

// ---------------------------------------------------------------- UI helpers

function setState(s) {
  state = s;
  const el = $("state");
  el.textContent = "● " + LABELS[s];
  el.style.color = COLORS[s];
}

function activity(text) { $("activity").textContent = text ? text + "..." : ""; }

function time() { return new Date().toTimeString().slice(0, 5); }

function addMsg(who, text) {
  const div = document.createElement("div");
  div.className = "msg " + (who === "you" ? "you" : "ai");
  const tag = document.createElement("div");
  tag.className = "tag";
  tag.textContent = (who === "you" ? "▸ YOU · " : "◂ " + name.toUpperCase() + " · ") + time();
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  div.append(tag, body);
  $("log").append(div);
  scrollDown();
  return body;
}

function addNote(text, cls = "note") {
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = text;
  $("log").append(div);
  scrollDown();
}

function scrollDown() { const log = $("log"); log.scrollTop = log.scrollHeight; }

function api(path, opts = {}) {
  opts.headers = Object.assign({ Authorization: "Bearer " + token }, opts.headers || {});
  return fetch(path, opts);
}

// ---------------------------------------------------------------- HUD core (canvas)

const canvas = $("core"), ctx = canvas.getContext("2d");
function drawCore(t) {
  const s = canvas.width, cx = s / 2, cy = s / 2;
  const color = COLORS[state] || COLORS.idle;
  const active = ["listening", "thinking", "speaking", "loading"].includes(state);
  level += (targetLevel - level) * (targetLevel > level ? 0.5 : 0.12);
  ctx.clearRect(0, 0, s, s);
  const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, s * 0.5);
  g.addColorStop(0, color + (active ? "46" : "26")); g.addColorStop(1, color + "00");
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, s * 0.5, 0, 7); ctx.fill();
  // tick ring
  ctx.save(); ctx.translate(cx, cy); ctx.rotate((t / 1000) * 6 * Math.PI / 180);  // 6 degrees a second
  for (let i = 0; i < 72; i++) {
    const long = i % 6 === 0, a = i * Math.PI / 36, r1 = s * 0.46, r2 = r1 - (long ? s * 0.045 : s * 0.022);
    ctx.strokeStyle = color + (long ? "aa" : "46"); ctx.lineWidth = Math.max(1, s * 0.008);
    ctx.beginPath(); ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2); ctx.stroke();
  }
  ctx.restore();
  // segment arcs
  const speed = (active ? 70 : 14) * Math.PI / 180, start = -t / 1000 * speed;
  ctx.strokeStyle = color + "e6"; ctx.lineWidth = s * 0.03; ctx.lineCap = "butt";
  for (let k = 0; k < 3; k++) {
    ctx.beginPath(); ctx.arc(cx, cy, s * 0.37, start + k * 2.0944, start + k * 2.0944 + 1.361); ctx.stroke();
  }
  ctx.strokeStyle = color + "6e"; ctx.lineWidth = Math.max(1, s * 0.01);
  for (let k = 0; k < 4; k++) {
    const a0 = t / 1000 * speed * 0.6 + k * Math.PI / 2 + 0.35;
    ctx.beginPath(); ctx.arc(cx, cy, s * 0.31, a0, a0 + 0.84); ctx.stroke();
  }
  // voice bars
  if (level > 0.015) {
    ctx.strokeStyle = color + "d2"; ctx.lineWidth = Math.max(1.2, s * 0.012); ctx.lineCap = "round";
    for (let i = 0; i < 48; i++) {
      const a = 2 * Math.PI * i / 48, ts = t / 1000;
      const wob = 0.55 + 0.45 * Math.sin(i * 1.7 + ts * 9) * Math.sin(i * 0.6 - ts * 5);
      const len = s * 0.09 * Math.min(1, level * 1.6) * Math.max(0.15, wob), r1 = s * 0.2;
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      ctx.lineTo(cx + Math.cos(a) * (r1 + len), cy + Math.sin(a) * (r1 + len)); ctx.stroke();
    }
  }
  // core
  const ts = t / 1000, pulse = 1 + (active ? 0.07 * Math.sin(ts * 6) : 0.03 * Math.sin(ts * 1.5)) + level * 0.18;
  const rc = s * 0.13 * pulse;
  const cg = ctx.createRadialGradient(cx - rc * 0.25, cy - rc * 0.25, 0, cx, cy, rc * 1.3);
  cg.addColorStop(0, "#ffffff"); cg.addColorStop(0.35, color); cg.addColorStop(1, "#1a1206");
  ctx.fillStyle = cg; ctx.beginPath(); ctx.arc(cx, cy, rc, 0, 7); ctx.fill();
  requestAnimationFrame(drawCore);
}
requestAnimationFrame(drawCore);

// ---------------------------------------------------------------- audio out (queued sentences)

let audioCtx = null, analyser = null, playQueue = [], playing = null, playGen = 0;
function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser(); analyser.fftSize = 512; analyser.connect(audioCtx.destination);
    meter();
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
}
function meter() {
  const buf = new Float32Array(analyser.fftSize);
  (function loop() {
    analyser.getFloatTimeDomainData(buf);
    let sum = 0; for (const v of buf) sum += v * v;
    const rms = Math.sqrt(sum / buf.length);
    if (playing || recorder) targetLevel = Math.min(1, rms * 6);
    else targetLevel = 0;
    requestAnimationFrame(loop);
  })();
}
async function queueAudio(url) {
  if (!voiceOn) return;
  const gen = playGen;
  const res = await api(url);
  if (!res.ok || gen !== playGen) return;
  const buf = await audioCtx.decodeAudioData(await res.arrayBuffer());
  if (gen !== playGen) return;
  playQueue.push(buf);
  if (!playing) playNext();
}
function playNext() {
  const buf = playQueue.shift();
  if (!buf) { playing = null; if (state === "speaking") setState("idle"); return; }
  const src = audioCtx.createBufferSource();
  src.buffer = buf; src.connect(analyser);
  src.onended = () => { if (playing === src) playNext(); };
  playing = src; setState("speaking"); src.start();
}
function stopAudio() {
  playGen++; playQueue = [];
  if (playing) { const p = playing; playing = null; try { p.stop(); } catch (e) {} }
}

// ---------------------------------------------------------------- a conversational turn (SSE over fetch)

let busy = false;
async function converse(path, body, headers) {
  busy = true; setState("thinking");
  let replyEl = null, failed = false;
  try {
    const res = await api(path, { method: "POST", body, headers });
    if (res.status === 401) { showPairing(); return; }
    if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.error || res.statusText); }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = (raw.match(/^event: (.*)$/m) || [])[1], data = JSON.parse((raw.match(/^data: (.*)$/m) || [, "{}"])[1]);
        if (ev === "transcript") addMsg("you", data.text);
        else if (ev === "delta") { if (!replyEl) replyEl = addMsg("ai", ""); replyEl.textContent += data.text; scrollDown(); }
        else if (ev === "audio") queueAudio(data.url);
        else if (ev === "activity") activity(data.text);
        else if (ev === "error") { failed = true; addNote("⚠ " + data.message, "err msg"); }
      }
    }
  } catch (e) {
    failed = true; addNote("⚠ " + (e.message || "Couldn't reach your PC. Is it on, with Tailscale connected?"), "err msg");
  } finally {
    busy = false; activity("");
    if (!playing && !playQueue.length) setState(failed ? "error" : "idle");
  }
}

// ---------------------------------------------------------------- recording (hold to talk)

let recorder = null, chunks = [], micStream = null, micSource = null;
async function startRec() {
  if (recorder || busy) return;
  ensureAudio(); stopAudio();  // talking over her stops her, like saying her name on the PC
  try {
    micStream = micStream || await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) { addNote("⚠ Microphone permission is needed to talk.", "err msg"); return; }
  micSource = audioCtx.createMediaStreamSource(micStream);
  const tap = audioCtx.createAnalyser(); tap.fftSize = 512; micSource.connect(tap);
  const buf = new Float32Array(512);
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "";
  recorder = new MediaRecorder(micStream, mime ? { mimeType: mime } : {});
  chunks = [];
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = () => {
    micSource.disconnect();
    const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    recorder = null; $("talk").classList.remove("rec"); $("live").textContent = "";
    if (blob.size > 2000) converse("/api/voice", blob, { "Content-Type": blob.type });
    else setState("idle");
  };
  recorder.start();
  $("talk").classList.add("rec"); setState("listening"); $("live").textContent = "◉  listening...";
  (function vu() {
    if (!recorder) return;
    tap.getFloatTimeDomainData(buf); let s = 0; for (const v of buf) s += v * v;
    targetLevel = Math.min(1, Math.sqrt(s / buf.length) * 8);
    requestAnimationFrame(vu);
  })();
}
function stopRec() { if (recorder && recorder.state === "recording") recorder.stop(); }

const talk = $("talk");
talk.addEventListener("pointerdown", (e) => { e.preventDefault(); talk.setPointerCapture(e.pointerId); startRec(); });
talk.addEventListener("pointerup", stopRec);
talk.addEventListener("pointercancel", stopRec);
talk.addEventListener("contextmenu", (e) => e.preventDefault());

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text || busy) return;
  $("text").value = ""; ensureAudio(); stopAudio();
  addMsg("you", text);
  converse("/api/message", JSON.stringify({ text }), { "Content-Type": "application/json" });
});

$("voiceToggle").addEventListener("click", () => {
  voiceOn = !voiceOn;
  try { localStorage.setItem("voice", voiceOn ? "on" : "off"); } catch (e) {}
  $("voiceToggle").textContent = "VOICE: " + (voiceOn ? "ON" : "OFF");
  if (!voiceOn) stopAudio();
});
$("voiceToggle").textContent = "VOICE: " + (voiceOn ? "ON" : "OFF");

// ---------------------------------------------------------------- notifications (web push)

let pushKey = null;
function urlB64ToBytes(s) {
  const pad = "=".repeat((4 - s.length % 4) % 4), b = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}
async function enablePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    addNote("⚠ Notifications need Chrome over HTTPS (open the Tailscale address).", "err msg"); return;
  }
  const perm = await Notification.requestPermission();
  if (perm !== "granted") { addNote("Notifications were not allowed.", "note"); return; }
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToBytes(pushKey) });
  await api("/api/push/subscribe", { method: "POST", body: JSON.stringify(sub), headers: { "Content-Type": "application/json" } });
  await api("/api/push/test", { method: "POST" });
  $("notify").textContent = "NOTIFICATIONS: ON";
}
$("notify").addEventListener("click", () => enablePush().catch((e) => addNote("⚠ " + e.message, "err msg")));

// ---------------------------------------------------------------- startup

function showPairing() {
  $("pair").style.display = "block"; $("main").style.display = "none"; $("controls").style.display = "none";
  setState("off");
}

async function hello() {
  if (!token) return showPairing();
  try {
    const res = await api("/api/hello");
    const info = await res.json();
    name = info.name || name;
    $("title").textContent = name.toLowerCase() === "friday" ? "F.R.I.D.A.Y." : name.toUpperCase();
    document.title = $("title").textContent;
    $("text").placeholder = "›  Ask " + name + "...";
    if (!info.paired) return showPairing();
    pushKey = info.push_key;
    talk.disabled = false;
    setState(info.ready ? "idle" : "loading");
    if (!info.ready) setTimeout(hello, 3000);
    if (!$("log").children.length) addNote("CONNECTED TO YOUR PC. HOLD THE BUTTON AND TALK, OR TYPE BELOW.");
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").then(async (reg) => {
        if (Notification.permission === "granted" && await reg.pushManager.getSubscription()) {
          $("notify").textContent = "NOTIFICATIONS: ON";
        }
      }).catch(() => {});
    }
  } catch (e) {
    setState("off"); addNote("⚠ Couldn't reach your PC. Is it on, with Tailscale connected?", "err msg");
    setTimeout(hello, 5000);
  }
}
hello();
