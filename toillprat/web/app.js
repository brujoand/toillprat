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

// Whether a reply speaks on its own, or only when you tap its 🔊. From
// /api/config at boot; changed in Settings. Tapping 🔊 always works either way.
let autoplay = true;

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
  let btn = null;
  if (isBot && withSpeaker) {
    btn = document.createElement("button");
    btn.className = "speak-btn";
    btn.textContent = "🔊";
    btn.onclick = () => speak(span.textContent, btn);
    bubble.append(btn, span);
  } else {
    bubble.appendChild(span);
  }
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
  return { span, btn };
}

async function sendMessage(text) {
  addBubble("user", text, false);
  const { span, btn } = addBubble("assistant", "", true);
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
  if (reply && autoplay) speak(reply, btn); // else it waits for a tap on 🔊
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
function speakDevice(text) {
  if (!window.speechSynthesis || typeof SpeechSynthesisUtterance === "undefined") {
    return false;
  }
  window.speechSynthesis.cancel(); // only the newest reply speaks
  const utter = new SpeechSynthesisUtterance(text);
  utter.rate = 1;
  utter.lang = SPEECH_LANG;
  if (!englishVoice) pickEnglishVoice(); // voices may have loaded since boot
  if (englishVoice) utter.voice = englishVoice;
  window.speechSynthesis.speak(utter);
  return true;
}

// Reflect "voice is being generated" on the speaker button (⏳ while working),
// so there's always a sign something is happening -- and nothing fails silently.
// Only one button shows the state at a time; starting a new one clears the last.
let speakingBtn = null;
function setSpeaking(btn, on) {
  if (on && speakingBtn && speakingBtn !== btn) setSpeaking(speakingBtn, false);
  speakingBtn = on ? btn : speakingBtn === btn ? null : speakingBtn;
  if (!btn) return;
  btn.disabled = on;
  btn.textContent = on ? "⏳" : "🔊";
  btn.classList.toggle("speaking", on);
}

// Surface the server's actual reason (e.g. no OpenRouter credits, bad voice)
// instead of a bare status code, so a silent "no sound" becomes explainable.
async function ttsErrorMessage(resp) {
  try {
    const data = await resp.json();
    if (data && data.detail) return String(data.detail);
  } catch (_) {
    /* not JSON — fall back to the status code */
  }
  return `Voice unavailable (error ${resp.status}).`;
}

// Roleplay actions in *asterisks* (e.g. "*giggles*") are stage directions, not
// speech -- strip them so they're never read aloud. The bubble still shows them.
function stripActions(text) {
  return (text || "")
    .replace(/\*[^*]*\*/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// Break a reply into sentences so we can speak (and fetch) them one at a time:
// the first short clip plays almost at once instead of waiting for the whole
// reply's audio. Sentence-ending punctuation is kept with its sentence.
function splitSentences(text) {
  const parts = text.match(/[^.!?…]+[.!?…]+|[^.!?…]+$/g) || [];
  return parts.map((s) => s.trim()).filter(Boolean);
}

// Bumping this supersedes any in-flight speech: the pipeline checks it and bails,
// so a new reply (or mute) cancels the old one cleanly.
let speakToken = 0;
function cancelSpeech() {
  speakToken++;
  stopAudio();
}

async function speak(text, btn) {
  if (muted) return;
  const clean = stripActions(text);
  if (!clean) return;

  if (ttsEngine === "device") {
    if (speakDevice(clean)) return;
    // No device speech here — fall through to the server so audio still works.
  }

  const ctx = ensureAudioCtx();
  if (!ctx) {
    showAudioError("This device can't play audio.");
    return;
  }
  // Whether we got here from an auto-reply or a tap on the speaker button, make
  // sure the context is running; a tap is what lets iOS grant this.
  if (ctx.state === "suspended") {
    try {
      await ctx.resume();
    } catch (_) {
      /* handled just below */
    }
  }
  if (ctx.state === "suspended") {
    // iOS keeps audio locked until a real tap. Auto-play after a reply isn't
    // one, so tell the user how to hear it instead of playing to silence.
    showAudioError("Tap 🔊 on a message to turn on sound.");
    return;
  }

  await speakServer(splitSentences(clean), btn, ctx);
}

// Fetch + decode one sentence's audio. Throws a friendly message on any failure
// so the pipeline can surface it; returns an AudioBuffer on success.
async function fetchClip(ctx, sentence) {
  let resp;
  try {
    resp = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sentence, voice: current && current.voice }),
    });
  } catch (_) {
    throw new Error("Couldn't reach the server for audio.");
  }
  if (!resp.ok) throw new Error(await ttsErrorMessage(resp));
  try {
    // decodeAudioData needs the raw bytes; no Blob URL, which iOS won't play.
    return await ctx.decodeAudioData(await resp.arrayBuffer());
  } catch (_) {
    throw new Error("The voice audio couldn't be played.");
  }
}

// Start a clip's fetch without awaiting it, so the next sentence is generating
// while the current one plays. The extra catch keeps a superseded prefetch from
// tripping an unhandled-rejection; the real await below still sees any error.
function prefetchClip(ctx, sentence) {
  if (!sentence) return null;
  const clip = fetchClip(ctx, sentence);
  clip.catch(() => {});
  return clip;
}

// Play one decoded buffer to the end (or until stopped), resolving when done.
function playBuffer(ctx, buffer) {
  return new Promise((resolve) => {
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    src.onended = () => {
      if (currentSource === src) currentSource = null;
      resolve();
    };
    currentSource = src;
    src.start(0);
  });
}

// Speak sentences in order, fetching the next while the current one plays: audio
// starts as soon as the first sentence is ready, with no gap between sentences.
async function speakServer(sentences, btn, ctx) {
  if (!sentences.length) return;
  const token = ++speakToken;
  stopAudio(); // silence whatever was playing before this reply
  setSpeaking(btn, true);
  try {
    let nextClip = prefetchClip(ctx, sentences[0]);
    for (let i = 0; i < sentences.length; i++) {
      let buffer;
      try {
        buffer = await nextClip;
      } catch (err) {
        if (token === speakToken) showAudioError(err.message || String(err));
        return;
      }
      if (token !== speakToken) return; // superseded by a newer reply / mute
      // Kick off the next fetch before playing, so it's ready when this ends.
      nextClip = prefetchClip(ctx, sentences[i + 1]);
      if (buffer && buffer.duration) await playBuffer(ctx, buffer);
      if (token !== speakToken) return;
    }
  } finally {
    setSpeaking(btn, false);
  }
}

$("#mute-btn").addEventListener("click", () => {
  muted = !muted;
  $("#mute-btn").textContent = muted ? "🔇" : "🔊";
  if (muted) cancelSpeech();
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
  $("#delete-btn").classList.toggle("hidden", !id);
  $("#paste-input").value = "";
  $(".paste-block").open = false;
  show("editor");
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
  $("#s-autoplay").value = settings.autoplay || (autoplay ? "on" : "off");
}

$("#settings-save-btn").addEventListener("click", async () => {
  const default_model = $("#s-model").value;
  const tts_engine = $("#s-tts").value;
  const autoplay_choice = $("#s-autoplay").value;
  $("#s-status").textContent = "Saving…";
  try {
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_model, tts_engine, autoplay: autoplay_choice }),
    });
  } catch (_) {
    $("#s-status").textContent = "Could not save. Try again.";
    return;
  }
  if (tts_engine !== ttsEngine) voices = []; // engine changed: refetch its voices
  ttsEngine = tts_engine; // take effect immediately, no reload
  autoplay = autoplay_choice === "on";
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
  if (config.autoplay) autoplay = config.autoplay === "on";
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
