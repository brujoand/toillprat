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

// Call from inside a user gesture (a tap or submit) so iOS lets audio play
// later, even when the actual playback happens after an await.
function unlockAudio() {
  const ctx = ensureAudioCtx();
  if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
}

function stopAudio() {
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

async function speak(text) {
  if (muted || !text) return;
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
  $("#delete-btn").classList.toggle("hidden", !id);
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
}

$("#settings-save-btn").addEventListener("click", async () => {
  const default_model = $("#s-model").value;
  $("#s-status").textContent = "Saving…";
  try {
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_model }),
    });
  } catch (_) {
    $("#s-status").textContent = "Could not save. Try again.";
    return;
  }
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
