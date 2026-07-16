"""toillprat — a dead-simple, tablet-friendly character chat.

Pick an avatar, type, read + hear the reply. It talks to two backends, both
chosen by env so the app itself is portable and carries no infrastructure:

  * an OpenAI-compatible LLM API (OpenRouter by default), streamed as SSE
  * an OpenAI-compatible TTS server (Chatterbox) for spoken replies

Characters (create / import) and per-character chat history persist to DATA_DIR
and are shared by everyone using this instance. See auth.py for who "everyone"
is and how a Pocket ID / OIDC proxy plugs in.
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import uuid
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    COOKIE_NAME,
    SESSION_TTL,
    CookieIdentity,
    Identity,
    InvalidName,
    ProxyIdentity,
    Sessions,
)

# --- Configuration (all overridable via env) --------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
WEB_DIR = Path(__file__).resolve().parent / "web"

OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "deepseek/deepseek-v3.2-exp")

# Points at nothing in particular by default; set it to wherever your TTS server
# lives. No default reveals or assumes any particular deployment.
CHATTERBOX_URL = os.environ.get("CHATTERBOX_URL", "http://localhost:8004").rstrip("/")
# A character with no voice of its own uses this. Empty (the default) means "let
# the TTS server pick" -- we resolve it to the first voice the server offers, so
# audio works out of the box. There is deliberately no literal "default" voice:
# Chatterbox has no such thing and 404s on it, which silently killed all speech.
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "").strip()

# Voice values that mean "none chosen" -- treated as unset when resolving. The
# "default" string is legacy: older builds hardcoded it and persisted it onto
# characters, so we still map it back to "unset" rather than send it to the TTS.
_UNSET_VOICES = {"", "default"}

# Stamped into the image at build time by the Dockerfile's ARG, from the same
# version semantic-release tagged the image with. "dev" when run from a checkout.
VERSION = os.environ.get("APP_VERSION", "dev")

# Set SECURE_COOKIE=1 when serving over HTTPS so the session cookie is marked
# Secure. Off by default because a Secure cookie is silently dropped over plain
# HTTP, which would make http://localhost unusable.
SECURE_COOKIE = os.environ.get("SECURE_COOKIE") == "1"

# Take identity from a trusting proxy's headers instead of our own cookie. Safe
# ONLY where that proxy is the sole thing that can reach this process -- read
# auth.py's module docstring before setting it.
TRUSTED_PROXY_AUTH = os.environ.get("TRUSTED_PROXY_AUTH") == "1"

CHARACTERS_DIR = DATA_DIR / "characters"
CHATS_DIR = DATA_DIR / "chats"
SETTINGS_PATH = DATA_DIR / "settings.json"

# App-wide settings changeable from the UI (persisted to DATA_DIR). An empty
# string means "fall back to the env default".
#
# tts_engine picks who speaks the replies:
#   "chatterbox" -- the TTS server (custom voices, but a network + GPU round-trip)
#   "device"     -- the browser/OS built-in speech (instant, offline, no server)
DEFAULT_SETTINGS = {
    "default_model": "",
    "default_voice": "",
    "tts_engine": "chatterbox",
}
TTS_ENGINES = {"chatterbox", "device"}

# Endpoints reachable without an identity: the config the frontend boots from,
# and the login/logout flow itself. Everything else under /api/ requires one.
PUBLIC_API_PATHS = {"/api/config", "/api/login", "/api/logout"}

sessions = Sessions()  # unused, and permanently empty, in proxy mode
IDENTITY: Identity = ProxyIdentity() if TRUSTED_PROXY_AUTH else CookieIdentity(sessions)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _ensure_dirs()
    _seed_demo_character()
    yield


app = FastAPI(title="toillprat", lifespan=lifespan)


@app.middleware("http")
async def require_identity(request: Request, call_next):
    """Gate the API on an identity, so a login actually means something.

    Static files and the SPA shell are served freely -- the page loads, asks
    /api/config who it is, and shows a login screen if the answer is nobody.
    """
    path = request.url.path
    if (
        path.startswith("/api/")
        and path not in PUBLIC_API_PATHS
        and IDENTITY.identify(request) is None
    ):
        return JSONResponse({"error": "login_required"}, status_code=401)
    return await call_next(request)


# --- Storage helpers --------------------------------------------------------


def _ensure_dirs() -> None:
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    CHATS_DIR.mkdir(parents=True, exist_ok=True)


def _char_path(char_id: str) -> Path:
    return CHARACTERS_DIR / f"{char_id}.json"


def _chat_path(char_id: str) -> Path:
    return CHATS_DIR / f"{char_id}.json"


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "character"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def load_character(char_id: str) -> dict:
    path = _char_path(char_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="character not found")
    return json.loads(path.read_text())


def list_characters() -> list[dict]:
    return sorted(
        (json.loads(p.read_text()) for p in CHARACTERS_DIR.glob("*.json")),
        key=lambda c: c.get("name", "").lower(),
    )


def save_character(char: dict) -> dict:
    _char_path(char["id"]).write_text(json.dumps(char, indent=2))
    return char


def normalize_character(data: dict, char_id: str | None = None) -> dict:
    """Coerce arbitrary input (form or imported card) into our schema."""
    name = (data.get("name") or "Character").strip()
    return {
        "id": char_id or data.get("id") or _slug(name),
        "name": name,
        "avatar": data.get("avatar") or "",  # data: URL, stored inline
        "greeting": (data.get("greeting") or "").strip(),
        "persona": (data.get("persona") or "").strip(),
        "example_dialogue": (data.get("example_dialogue") or "").strip(),
        "voice": (data.get("voice") or "").strip(),  # "" = resolve at TTS time
        "model": (data.get("model") or "").strip(),  # "" = use the default model
    }


def load_history(char_id: str) -> list[dict]:
    path = _chat_path(char_id)
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_history(char_id: str, messages: list[dict]) -> None:
    _chat_path(char_id).write_text(json.dumps(messages, indent=2))


# --- App settings (UI-configurable, persisted) ------------------------------


def load_settings() -> dict:
    data = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError:
            data = {}
    result = {key: data.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
    if result["tts_engine"] not in TTS_ENGINES:
        result["tts_engine"] = DEFAULT_SETTINGS["tts_engine"]
    return result


def save_settings(data: dict) -> dict:
    """Persist only the keys present in `data`; leave the rest untouched."""
    current = load_settings()
    for key in DEFAULT_SETTINGS:
        if key in data:
            current[key] = (data.get(key) or "").strip()
    if current.get("tts_engine") not in TTS_ENGINES:
        current["tts_engine"] = DEFAULT_SETTINGS["tts_engine"]
    SETTINGS_PATH.write_text(json.dumps(current, indent=2))
    return current


def effective_model(char: dict | None = None) -> str:
    """Resolve which model to use: per-character > settings default > env."""
    if char and char.get("model"):
        return char["model"]
    return load_settings().get("default_model") or DEFAULT_MODEL


# --- Character-card import (SillyTavern + Character.AI style) ----------------


def _card_to_character(card: dict) -> dict:
    """Map an imported character card to our schema.

    Handles both a SillyTavern v2 card (fields under a `data` wrapper, using
    `first_mes` / `mes_example`) and a flat v1 or Character.AI-style export
    (`greeting`, `description`, `definition`, `title`). Character.AI splits the
    persona across a short `description` and a long `definition`; keep both.
    """
    data = card.get("data") if isinstance(card.get("data"), dict) else card

    def pick(*keys: str) -> str:
        for source in (data, card):
            if not isinstance(source, dict):
                continue
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    description = pick("description", "personality")
    definition = pick("definition")
    persona = "\n\n".join(part for part in (description, definition) if part)
    return normalize_character(
        {
            "name": pick("name", "title", "char_name"),
            "greeting": pick("first_mes", "greeting", "char_greeting"),
            "persona": persona,
            "example_dialogue": pick("mes_example", "example_dialogue"),
        }
    )


def _extract_card_from_png(raw: bytes) -> dict:
    """Read the base64 JSON embedded in a PNG tEXt/zTXt chunk keyword 'chara'."""
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos : pos + 4])
        ctype = raw[pos + 4 : pos + 8]
        body = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length  # 4 len + 4 type + data + 4 crc
        keyword, _, value = body.partition(b"\x00")
        if keyword == b"chara":
            if ctype == b"tEXt":
                return json.loads(base64.b64decode(value))
            if ctype == b"zTXt":  # 1 compression-method byte, then zlib data
                return json.loads(base64.b64decode(zlib.decompress(value[1:])))
        if ctype == b"IEND":
            break
    raise ValueError("no character card found in PNG")


# --- Startup seed: a demo character so the grid is never empty --------------


def _seed_demo_character() -> None:
    if any(CHARACTERS_DIR.glob("*.json")):
        return
    demo = normalize_character(
        {
            "name": "Robo",
            "greeting": "Beep boop! Hi there, I'm Robo the friendly robot. "
            "What's your name?",
            "persona": "You are Robo, a cheerful, curious robot who loves making "
            "friends. You speak simply and kindly, ask fun questions, and are "
            "always encouraging. Keep replies short.",
        }
    )
    save_character(demo)


# --- Identity: who is using this instance -----------------------------------


@app.get("/api/config")
def api_config(request: Request) -> JSONResponse:
    player = IDENTITY.identify(request)
    return JSONResponse(
        {
            "version": VERSION,
            "auth_mode": IDENTITY.mode,
            "login_enabled": IDENTITY.login_enabled,
            "me": player.name if player else None,
            # Which voice engine to speak replies with; the frontend needs it up
            # front, before the first reply, not just on the settings screen.
            "tts_engine": load_settings()["tts_engine"],
        }
    )


@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    if not IDENTITY.login_enabled:
        raise HTTPException(status_code=404, detail="login is handled by the proxy")
    body = await request.json()
    try:
        token, player = sessions.login(body.get("name", ""))
    except InvalidName as exc:
        return JSONResponse(exc.as_dict(), status_code=400)
    resp = JSONResponse({"name": player.name})
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIE,
    )
    return resp


@app.post("/api/logout")
def api_logout(request: Request) -> JSONResponse:
    sessions.logout(request.cookies.get(COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- Character CRUD ---------------------------------------------------------


@app.get("/api/characters")
def api_list_characters() -> list[dict]:
    return list_characters()


@app.get("/api/characters/{char_id}")
def api_get_character(char_id: str) -> dict:
    return load_character(char_id)


@app.post("/api/characters")
async def api_create_character(request: Request) -> dict:
    data = await request.json()
    return save_character(normalize_character(data))


@app.put("/api/characters/{char_id}")
async def api_update_character(char_id: str, request: Request) -> dict:
    load_character(char_id)  # 404 if missing
    data = await request.json()
    return save_character(normalize_character(data, char_id=char_id))


@app.delete("/api/characters/{char_id}")
def api_delete_character(char_id: str) -> JSONResponse:
    _char_path(char_id).unlink(missing_ok=True)
    _chat_path(char_id).unlink(missing_ok=True)
    return JSONResponse({"ok": True})


@app.post("/api/characters/import")
async def api_import_character(file: UploadFile) -> dict:
    raw = await file.read()
    name = (file.filename or "").lower()
    try:
        if name.endswith(".png") or raw[:8] == b"\x89PNG\r\n\x1a\n":
            card = _extract_card_from_png(raw)
        else:
            card = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        raise HTTPException(
            status_code=400,
            detail=f"Could not read that character file: {exc}",
        ) from exc
    return save_character(_card_to_character(card))


@app.post("/api/characters/parse")
async def api_parse_character(request: Request) -> dict:
    """Turn pasted text into character fields for the editor -- without saving.

    Character.AI can't be exported, so the realistic path is to copy a friend's
    details off the page and paste them here. Pasted JSON (an exported card) is
    mapped like a file import; anything else is treated as the persona, leaving
    the rest for the user to fill in. Nothing is persisted -- the form is.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="nothing to parse")
    try:
        card = json.loads(text)
    except json.JSONDecodeError:
        card = None
    if isinstance(card, dict):
        char = _card_to_character(card)
    else:
        # Not a card: use the raw text as the persona, name left for the user.
        char = {"name": "", "greeting": "", "persona": text, "example_dialogue": ""}
    keys = ("name", "greeting", "persona", "example_dialogue")
    return {key: char[key] for key in keys}


# --- Chat history -----------------------------------------------------------


@app.get("/api/characters/{char_id}/messages")
def api_get_messages(char_id: str) -> list[dict]:
    load_character(char_id)
    history = load_history(char_id)
    if not history:
        char = load_character(char_id)
        if char.get("greeting"):
            history = [{"role": "assistant", "content": char["greeting"]}]
    return history


@app.delete("/api/characters/{char_id}/messages")
def api_reset_messages(char_id: str) -> JSONResponse:
    _chat_path(char_id).unlink(missing_ok=True)
    return JSONResponse({"ok": True})


# --- LLM chat (streamed via SSE) --------------------------------------------


def _system_prompt(char: dict) -> str:
    parts = [
        f"You are {char['name']}. Always stay fully in character as "
        f"{char['name']}; never break character or mention being an AI.",
    ]
    if char.get("persona"):
        parts.append(char["persona"])
    if char.get("example_dialogue"):
        parts.append("Example of how you talk:\n" + char["example_dialogue"])
    return "\n\n".join(parts)


@app.post("/api/chat")
async def api_chat(request: Request) -> StreamingResponse:
    body = await request.json()
    char = load_character(body["character_id"])
    user_text = (body.get("message") or "").strip()

    history = load_history(char["id"])
    if not history and char.get("greeting"):
        history = [{"role": "assistant", "content": char["greeting"]}]
    if user_text:
        history.append({"role": "user", "content": user_text})

    messages = [{"role": "system", "content": _system_prompt(char)}, *history]
    payload = {
        "model": effective_model(char),
        "messages": messages,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "toillprat",
    }

    async def event_stream():
        reply = ""
        if not OPENROUTER_API_KEY:
            yield _sse({"error": "This app isn't set up yet (no LLM key)."})
            return
        try:
            async with (
                httpx.AsyncClient(timeout=120) as client,
                client.stream(
                    "POST",
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp,
            ):
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    yield _sse({"error": f"LLM error {resp.status_code}: {detail}"})
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"]
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    chunk = delta.get("content")
                    if chunk:
                        reply += chunk
                        yield _sse({"delta": chunk})
        except httpx.HTTPError as exc:
            yield _sse({"error": f"Could not reach the LLM: {exc}"})
            return

        # Persist the exchange only once we have a complete reply.
        if reply:
            history.append({"role": "assistant", "content": reply})
            save_history(char["id"], history)
        yield _sse({"done": True})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# --- Settings + model list --------------------------------------------------


@app.get("/api/settings")
def api_get_settings() -> JSONResponse:
    settings = load_settings()
    return JSONResponse(
        {
            "default_model": settings["default_model"],
            "default_voice": settings["default_voice"],
            "tts_engine": settings["tts_engine"],
            # What chat uses when a character has no model of its own.
            "effective_model": settings["default_model"] or DEFAULT_MODEL,
            "env_default_model": DEFAULT_MODEL,
        }
    )


@app.put("/api/settings")
async def api_put_settings(request: Request) -> JSONResponse:
    data = await request.json()
    return JSONResponse(save_settings(data))


async def _fetch_model_catalogue(
    client: httpx.AsyncClient, url: str, headers: dict
) -> list | None:
    """The `data` list from an OpenAI-style /models response; None on failure.

    None (an error) is distinct from [] (the server answered with no models),
    so the caller can tell a missing endpoint from an empty allowlist.
    """
    try:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


@app.get("/api/models")
async def api_models() -> JSONResponse:
    """List models as a simple id/name list, honouring the key's governance.

    Prefer OpenRouter's caller-scoped catalogue (/models/user), which returns
    only the models the key is allowed to use -- account guardrails, provider,
    and privacy settings all applied. Fall back to the full public /models when
    there's no key, or when the provider is a plain OpenAI-compatible server
    with no such endpoint (a 404 there). An empty-but-successful /models/user is
    the governance saying "these and no more", so it is used as-is, not widened.
    """
    headers = {"X-Title": "toillprat"}
    auth = (
        {"Authorization": f"Bearer {OPENROUTER_API_KEY}"} if OPENROUTER_API_KEY else {}
    )
    async with httpx.AsyncClient(timeout=15) as client:
        raw = None
        if OPENROUTER_API_KEY:
            raw = await _fetch_model_catalogue(
                client, f"{OPENROUTER_BASE_URL}/models/user", {**headers, **auth}
            )
        if raw is None:
            raw = await _fetch_model_catalogue(
                client, f"{OPENROUTER_BASE_URL}/models", {**headers, **auth}
            )
    if raw is None:
        return JSONResponse({"models": []})
    models = sorted(
        (
            {"id": m["id"], "name": m.get("name") or m["id"]}
            for m in raw
            if isinstance(m, dict) and m.get("id")
        ),
        key=lambda m: m["name"].lower(),
    )
    return JSONResponse({"models": models})


# --- TTS + voices (proxy to the TTS server) ---------------------------------


async def _fetch_voices() -> list[str]:
    """The TTS server's voice names (e.g. ["Emily.wav", ...]); [] if unreachable.

    Chatterbox answers /v1/audio/voices with {"status": "ok", "voices": [...]},
    but be liberal about the shape so any OpenAI-compatible server works.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{CHATTERBOX_URL}/v1/audio/voices")
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return []
    raw = data.get("voices") if isinstance(data, dict) else data
    return [v for v in raw if isinstance(v, str)] if isinstance(raw, list) else []


async def _resolve_voice(requested: str | None) -> str:
    """Turn a character's stored voice into one the TTS server will accept.

    An explicit choice is honoured as-is (it may be a reference-audio clone the
    voices listing doesn't enumerate). Only an unset/legacy-"default" voice is
    resolved: to DEFAULT_VOICE if configured, else the first voice on offer.
    """
    requested = (requested or "").strip()
    if requested.lower() not in _UNSET_VOICES:
        return requested
    if DEFAULT_VOICE:
        return DEFAULT_VOICE
    voices = await _fetch_voices()
    return voices[0] if voices else requested


@app.get("/api/voices")
async def api_voices() -> JSONResponse:
    voices = await _fetch_voices()
    if not voices and DEFAULT_VOICE:
        # TTS server down / lists nothing — offer the configured default only.
        voices = [DEFAULT_VOICE]
    return JSONResponse({"voices": voices})


@app.post("/api/tts")
async def api_tts(request: Request) -> Response:
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="no text to speak")
    payload = {
        "model": "chatterbox",
        "input": text,
        "voice": await _resolve_voice(body.get("voice")),
        "response_format": "mp3",
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{CHATTERBOX_URL}/v1/audio/speech", json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}") from exc
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "audio/mpeg"),
    )


# --- Static SPA (mounted last so /api/* wins) -------------------------------


# "no-cache" means "revalidate before reusing", NOT "don't cache". Paired with
# the ETag that FileResponse/StaticFiles already send, the browser gets a cheap
# 304 when nothing changed but always picks up a fresh deploy immediately --
# without which a device (iOS Safari especially) keeps serving a stale SPA and
# never sees new features until someone clears its cache by hand.
NO_CACHE = "no-cache"


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that tells the browser to revalidate every asset."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = NO_CACHE
        return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": NO_CACHE})


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


app.mount("/", RevalidatingStaticFiles(directory=str(WEB_DIR)), name="web")
