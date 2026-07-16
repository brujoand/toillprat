"use strict";

// --- Tiny helpers -----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const views = {
  login: $("#login"),
  home: $("#home"),
  chat: $("#chat"),
  editor: $("#editor"),
  settings: $("#settings"),
};

function show(name) {
  for (const [key, el] of Object.entries(views)) {
    el.classList.toggle("hidden", key !== name);
  }
}

function initial(name) {
  return (name || "?").trim().charAt(0).toUpperCase();
}

// Fill an avatar element: photo if we have one, else a coloured initial.
function paintAvatar(el, char) {
  if (char.avatar) {
    el.style.background = "none";
    el.innerHTML = "";
    if (el.tagName === "IMG") {
      el.src = char.avatar;
    } else {
      const img = document.createElement("img");
      img.src = char.avatar;
      el.appendChild(img);
    }
  } else if (el.tagName === "IMG") {
    el.removeAttribute("src");
  } else {
    el.textContent = initial(char.name);
  }
}

// --- State ------------------------------------------------------------------

let characters = [];
let current = null; // character being chatted with
let editing = null; // character id being edited (null = creating)
let muted = false;
let voices = [];

// Reply audio goes through the Web Audio API, not an <audio> element. On iOS
// Safari an <audio> element flat-out refuses to play a Blob object URL, and it
// blocks play() that isn't inside a real tap — and we always play *after* an
// async fetch, so we're never inside the tap. An AudioContext sidesteps both:
// resume() it inside a tap to "unlock" it, then decode the bytes and play them
// ourselves, no media element and no autoplay gate.
let audioCtx = null;
let currentSource = null; // the clip playing right now, so mute can stop it

function ensureAudioCtx() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) audioCtx = new Ctx();
  }
  return audioCtx;
}

// Which engine speaks replies: "chatterbox" (the TTS server) or "device" (the
// browser/OS built-in speech — instant, offline). Set from /api/config at boot.
let ttsEngine = "chatterbox";
let speechPrimed = false;

// The friends speak English, but the device's default voice follows the OS
// locale — on a Norwegian iPad that reads English with a heavy accent. So pick
// an English voice explicitly. Voices can load lazily, hence voiceschanged.
const SPEECH_LANG = "en-US";
let englishVoice = null;

function pickEnglishVoice() {
  if (!window.speechSynthesis) return;
  const voices = window.speechSynthesis.getVoices() || [];
  const english = voices.filter(
    (v) => /^en[-_]?/i.test(v.lang || "") || /english/i.test(v.name || ""),
  );
  const preferring = (code) =>
    english.find((v) => (v.lang || "").toLowerCase().replace("_", "-").startsWith(code));
  englishVoice =
    preferring("en-us") ||
    preferring("en-gb") ||
    english.find((v) => v.default) ||
    english[0] ||
    null;
}

if (window.speechSynthesis) {
  pickEnglishVoice();
  window.speechSynthesis.onvoiceschanged = pickEnglishVoice;
}

function normLang(lang) {
  return (lang || "").toLowerCase().replace("_", "-");
}

// The best device voice for a friend's chosen language/accent (a BCP-47 tag).
// Exact region match wins (en-GB), then the base language (en-*), then the
// English default so a friend never falls back to the OS-locale voice.
function pickVoiceForLang(lang) {
  if (!window.speechSynthesis) return null;
  const want = normLang(lang);
  if (!want) return englishVoice;
  const voices = window.speechSynthesis.getVoices() || [];
  const base = want.split("-")[0];
  return (
    voices.find((v) => normLang(v.lang) === want) ||
    voices.find((v) => normLang(v.lang).split("-")[0] === base) ||
    englishVoice
  );
}

// Call from inside a user gesture (a tap or submit) so iOS lets audio play
// later, even when the actual playback happens after an await. Primes both
// engines: the AudioContext for Chatterbox audio, and speechSynthesis for the
// device voice (iOS blocks the first speak() unless it's warmed in a gesture).
function unlockAudio() {
  const ctx = ensureAudioCtx();
  if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  if (!speechPrimed && window.speechSynthesis) {
    speechPrimed = true;
    try {
      const warm = new SpeechSynthesisUtterance(" ");
      warm.volume = 0;
      warm.lang = SPEECH_LANG;
      if (englishVoice) warm.voice = englishVoice;
      window.speechSynthesis.speak(warm);
    } catch (_) {
      /* device speech just won't be available */
    }
  }
}

function stopAudio() {
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (!currentSource) return;
  try {
    currentSource.stop();
  } catch (_) {
    /* already ended */
  }
  currentSource = null;
}

// Voice is best-effort, but not silently: if it fails, say why in a brief toast
// so "no sound" doesn't just look like the app is broken.
let audioMsgTimer = null;
function showAudioError(msg) {
  let el = $("#audio-msg");
  if (!el) {
    el = document.createElement("div");
    el.id = "audio-msg";
    el.className = "audio-msg";
    document.body.appendChild(el);
  }
  el.textContent = "🔇 " + msg;
  el.classList.add("show");
  clearTimeout(audioMsgTimer);
  audioMsgTimer = setTimeout(() => el.classList.remove("show"), 6000);
}

// --- Home grid --------------------------------------------------------------

async function loadCharacters() {
  characters = await fetch("/api/characters").then((r) => r.json());
  renderGrid();
}

function renderGrid() {
  const grid = $("#grid");
  grid.innerHTML = "";
  for (const char of characters) {
    const tile = document.createElement("button");
    tile.className = "tile";
    const av = document.createElement("div");
    av.className = "tile-avatar";
    paintAvatar(av, char);
    const nm = document.createElement("div");
    nm.className = "tile-name";
    nm.textContent = char.name;
    tile.append(av, nm);
    tile.onclick = () => openChat(char);
    grid.appendChild(tile);
  }
  // "Create" tile always last.
  const add = document.createElement("button");
  add.className = "tile add";
  add.innerHTML =
    '<div class="tile-avatar">＋</div><div class="tile-name">New friend</div>';
  add.onclick = () => openEditor(null);
  grid.appendChild(add);
}

// --- Chat -------------------------------------------------------------------

async function openChat(char) {
  unlockAudio(); // this runs inside the tile tap, so audio is primed for replies
  current = char;
  $("#chat-name").textContent = char.name;
  paintAvatar($("#chat-avatar"), char);
  const box = $("#messages");
  box.innerHTML = "";
  show("chat");
  const msgs = await fetch(
    `/api/characters/${char.id}/messages`,
  ).then((r) => r.json());
  for (const m of msgs) addBubble(m.role, m.content, m.role === "assistant");
  $("#msg-input").focus();
}

function addBubble(role, text, withSpeaker) {
  const box = $("#messages");
  const bubble = document.createElement("div");
  const isBot = role === "assistant";
  bubble.className = "bubble " + (isBot ? "bot" : "user");
  const span = document.createElement("span");
  span.textContent = text;
  if (isBot && withSpeaker) {
    const btn = document.createElement("button");
    btn.className = "speak-btn";
    btn.textContent = "🔊";
    btn.onclick = () => speak(span.textContent);
    bubble.append(btn, span);
  } else {
    bubble.appendChild(span);
  }
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
  return span;
}

async function sendMessage(text) {
  addBubble("user", text, false);
  const span = addBubble("assistant", "", true);
  span.textContent = "…";

  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character_id: current.id, message: text }),
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let reply = "";
  let first = true;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop();
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data: ")) continue;
      const evt = JSON.parse(line.slice(6));
      if (evt.error) {
        span.textContent = "😕 " + evt.error;
        return;
      }
      if (evt.delta) {
        if (first) {
          span.textContent = "";
          first = false;
        }
        reply += evt.delta;
        span.textContent = reply;
        $("#messages").scrollTop = $("#messages").scrollHeight;
      }
    }
  }
  if (reply) speak(reply);
}

$("#send-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#msg-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  unlockAudio(); // submit is a user gesture — prime audio before the async reply
  sendMessage(text);
});

// --- Text-to-speech ---------------------------------------------------------

// The OS/browser built-in voice: instant, offline, no server or GPU. The
// trade-off is a generic device voice, not a character's custom Chatterbox one.
// `lang` is the friend's chosen BCP-47 tag; empty means the English default.
function speakDevice(text, lang) {
  if (!window.speechSynthesis || typeof SpeechSynthesisUtterance === "undefined") {
    return false;
  }
  window.speechSynthesis.cancel(); // only the newest reply speaks
  if (!englishVoice) pickEnglishVoice(); // voices may have loaded since boot
  const voice = pickVoiceForLang(lang);
  const utter = new SpeechSynthesisUtterance(text);
  utter.rate = 1;
  utter.lang = lang || SPEECH_LANG;
  if (voice) utter.voice = voice;
  window.speechSynthesis.speak(utter);
  return true;
}

async function speak(text) {
  if (muted || !text) return;

  if (ttsEngine === "device") {
    if (speakDevice(text, current && current.speech_lang)) return;
    // No device speech here — fall through to the server so audio still works.
  }

  const ctx = ensureAudioCtx();
  if (!ctx) return;
  // Whether we got here from an auto-reply or a tap on the speaker button, make
  // sure the context is running; a tap is what lets iOS grant this.
  if (ctx.state === "suspended") {
    try {
      await ctx.resume();
    } catch (_) {
      /* stays suspended — playback below just won't make sound */
    }
  }

  let resp;
  try {
    resp = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: current && current.voice }),
    });
  } catch (_) {
    showAudioError("Couldn't reach the server for audio.");
    return;
  }
  if (!resp.ok) {
    showAudioError(`Voice unavailable (error ${resp.status}).`);
    return;
  }

  let buffer;
  try {
    // decodeAudioData needs the raw bytes; no Blob URL, which iOS won't play.
    buffer = await ctx.decodeAudioData(await resp.arrayBuffer());
  } catch (_) {
    showAudioError("The voice audio couldn't be played.");
    return;
  }

  stopAudio(); // only the newest reply speaks
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  src.onended = () => {
    if (currentSource === src) currentSource = null;
  };
  currentSource = src;
  src.start(0);
}

$("#mute-btn").addEventListener("click", () => {
  muted = !muted;
  $("#mute-btn").textContent = muted ? "🔇" : "🔊";
  if (muted) stopAudio();
});

// --- Editor -----------------------------------------------------------------

async function openEditor(id) {
  editing = id;
  await ensureVoices();
  const char = id ? characters.find((c) => c.id === id) : {};
  $("#editor-title").textContent = id ? "Edit friend" : "New friend";
  $("#f-name").value = char.name || "";
  $("#f-greeting").value = char.greeting || "";
  $("#f-persona").value = char.persona || "";
  $("#f-example").value = char.example_dialogue || "";
  paintAvatar($("#avatar-preview"), char);
  $("#avatar-preview").dataset.url = char.avatar || "";
  renderVoiceOptions(char.voice);
  renderLanguageOptions(char.speech_lang || "");
  $("#delete-btn").classList.toggle("hidden", !id);
  $("#paste-input").value = "";
  $(".paste-block").open = false;
  show("editor");
}

// A readable label for a BCP-47 tag, e.g. "en-GB" -> "English (United Kingdom)".
function langLabel(lang) {
  try {
    const [base, region] = lang.split("-");
    // English labels, to match the app's English UI regardless of OS locale.
    let label = new Intl.DisplayNames(["en"], { type: "language" }).of(base) || base;
    if (region) {
      const rn = new Intl.DisplayNames(["en"], { type: "region" }).of(region);
      if (rn) label += ` (${rn})`;
    }
    return label;
  } catch (_) {
    return lang;
  }
}

// Fill the language/accent dropdown from the voices this device actually has,
// so every option is one that will really work here. "Default (English)" first.
function renderLanguageOptions(selected) {
  const sel = $("#f-speech-lang");
  sel.innerHTML = "";
  const voiceList = window.speechSynthesis
    ? window.speechSynthesis.getVoices() || []
    : [];
  // De-dupe case-insensitively but keep each tag's canonical casing (en-GB).
  const langs = new Map(); // normalised key -> {tag, label}
  for (const v of voiceList) {
    const tag = (v.lang || "").replace("_", "-");
    const key = tag.toLowerCase();
    if (tag && !langs.has(key)) langs.set(key, { tag, label: langLabel(tag) });
  }
  // Keep a previously-saved choice selectable even if this device lacks it.
  if (selected && !langs.has(normLang(selected))) {
    langs.set(normLang(selected), { tag: selected, label: langLabel(selected) });
  }
  const options = [{ tag: "", label: "Default (English)" }, ...langs.values()];
  options.sort((a, b) => (a.tag === "" ? -1 : b.tag === "" ? 1 : a.label.localeCompare(b.label)));
  for (const { tag, label } of options) {
    const opt = document.createElement("option");
    opt.value = tag;
    opt.textContent = tag ? `${label} — ${tag}` : label;
    if (normLang(tag) === normLang(selected)) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function ensureVoices() {
  if (voices.length) return;
  try {
    const data = await fetch("/api/voices").then((r) => r.json());
    const raw = data.voices || data.data || data || [];
    voices = raw
      .map((v) => (typeof v === "string" ? v : v.id || v.name || v.voice))
      .filter(Boolean);
  } catch (_) {
    voices = [];
  }
}

function renderVoiceOptions(selected) {
  const sel = $("#f-voice");
  sel.innerHTML = "";
  const list = voices.length ? voices : [selected || "default"];
  for (const v of list) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v.replace(/\.wav$/i, "");
    if (v === selected) opt.selected = true;
    sel.appendChild(opt);
  }
}

$("#avatar-input").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $("#avatar-preview").dataset.url = reader.result;
    paintAvatar($("#avatar-preview"), { avatar: reader.result });
  };
  reader.readAsDataURL(file);
});

$("#save-btn").addEventListener("click", async () => {
  const payload = {
    name: $("#f-name").value.trim() || "Friend",
    greeting: $("#f-greeting").value,
    persona: $("#f-persona").value,
    example_dialogue: $("#f-example").value,
    voice: $("#f-voice").value,
    speech_lang: $("#f-speech-lang").value,
    avatar: $("#avatar-preview").dataset.url || "",
  };
  const url = editing ? `/api/characters/${editing}` : "/api/characters";
  await fetch(url, {
    method: editing ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadCharacters();
  show("home");
});

$("#delete-btn").addEventListener("click", async () => {
  if (!editing) return;
  if (!confirm("Delete this friend?")) return;
  await fetch(`/api/characters/${editing}`, { method: "DELETE" });
  await loadCharacters();
  show("home");
});

$("#import-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch("/api/characters/import", {
    method: "POST",
    body: form,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(err.detail || "Could not import that file.");
    return;
  }
  await loadCharacters();
  show("home");
});

// Paste a friend's details (or an exported JSON) and fill the editor fields.
// Only overwrites a field when the paste actually yielded something for it, so
// half-filled pastes don't wipe what's already typed.
$("#paste-fill-btn").addEventListener("click", async () => {
  const text = $("#paste-input").value.trim();
  if (!text) return;
  let data = null;
  try {
    const resp = await fetch("/api/characters/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (resp.ok) data = await resp.json();
  } catch (_) {
    /* fall through to the alert */
  }
  if (!data) {
    alert("Couldn't read that. Paste the details, or an exported character JSON.");
    return;
  }
  const fields = {
    "#f-name": data.name,
    "#f-greeting": data.greeting,
    "#f-persona": data.persona,
    "#f-example": data.example_dialogue,
  };
  for (const [sel, value] of Object.entries(fields)) {
    if (value) $(sel).value = value;
  }
  $("#paste-input").value = "";
  $(".paste-block").open = false;
});

// --- Settings ---------------------------------------------------------------

let modelsLoaded = false;
let modelList = [];

async function ensureModels() {
  if (modelsLoaded) return;
  try {
    const data = await fetch("/api/models").then((r) => r.json());
    modelList = data.models || [];
  } catch (_) {
    modelList = [];
  }
  modelsLoaded = true;
}

function renderModelOptions(selected) {
  const sel = $("#s-model");
  sel.innerHTML = "";
  const list = modelList.slice();
  // Keep the current choice selectable even if the catalogue didn't include it.
  if (selected && !list.some((m) => m.id === selected)) {
    list.unshift({ id: selected, name: selected });
  }
  if (!list.length) {
    const opt = document.createElement("option");
    opt.value = selected || "";
    opt.textContent = selected || "(could not load models)";
    sel.appendChild(opt);
    return;
  }
  for (const m of list) {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.name === m.id ? m.id : `${m.name} — ${m.id}`;
    if (m.id === selected) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function openSettings() {
  show("settings");
  $("#s-status").textContent = "";
  const settings = await fetch("/api/settings")
    .then((r) => r.json())
    .catch(() => ({}));
  await ensureModels();
  renderModelOptions(settings.default_model || settings.effective_model || "");
  $("#s-tts").value = settings.tts_engine || ttsEngine;
}

$("#settings-save-btn").addEventListener("click", async () => {
  const default_model = $("#s-model").value;
  const tts_engine = $("#s-tts").value;
  $("#s-status").textContent = "Saving…";
  try {
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_model, tts_engine }),
    });
  } catch (_) {
    $("#s-status").textContent = "Could not save. Try again.";
    return;
  }
  ttsEngine = tts_engine; // take effect immediately, no reload
  show("home");
});

// --- Nav + chat reset -------------------------------------------------------

document.body.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "home") show("home");
  if (action === "reset") resetChat();
  if (action === "settings") openSettings();
  if (action === "edit" && current) openEditor(current.id);
});

async function resetChat() {
  if (!current || !confirm("Start this chat over?")) return;
  await fetch(`/api/characters/${current.id}/messages`, { method: "DELETE" });
  openChat(current);
}

// --- Login + boot -----------------------------------------------------------

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#login-name").value.trim();
  if (!name) return;
  $("#login-status").textContent = "";
  let resp;
  try {
    resp = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch (_) {
    $("#login-status").textContent = "Could not reach the server. Try again.";
    return;
  }
  if (!resp.ok) {
    $("#login-status").textContent = "That name won't work — try another.";
    return;
  }
  boot();
});

// Ask the server who we are and which build this is, then either show the login
// screen (cookie mode, not logged in) or go straight to the characters.
async function boot() {
  let config = {};
  try {
    config = await fetch("/api/config").then((r) => r.json());
  } catch (_) {
    config = {};
  }
  if (config.tts_engine) ttsEngine = config.tts_engine;
  const badge = $("#version");
  if (config.version) {
    badge.textContent = "v" + config.version;
    badge.hidden = false;
  }
  if (config.login_enabled && !config.me) {
    show("login");
    $("#login-name").focus();
    return;
  }
  show("home");
  loadCharacters();
}

boot();
