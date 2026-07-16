"""The HTTP surface: config, the login gate, and the character/settings API.

The security rule that matters most here is
test_a_forged_identity_header_is_ignored: in the default cookie mode, sending
x-auth-* headers must NOT log you in. If that ever passes identity through, the
whole app is handed to anyone who can set a header.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib

from toillprat import main


def test_config_reports_cookie_mode_and_nobody_before_login(client):
    body = client.get("/api/config").json()
    assert body["auth_mode"] == "cookie"
    assert body["login_enabled"] is True
    assert body["me"] is None
    assert body["version"] == "dev"


def test_the_api_is_gated_until_you_log_in(client):
    assert client.get("/api/characters").status_code == 401


def test_a_forged_identity_header_is_ignored(client):
    # No cookie, but a forged proxy header. Cookie mode must not honour it.
    resp = client.get(
        "/api/characters",
        headers={"x-auth-sub": "evil", "x-auth-email": "evil@example.com"},
    )
    assert resp.status_code == 401
    assert (
        client.get("/api/config", headers={"x-auth-sub": "evil"}).json()["me"] is None
    )


def test_login_then_the_api_opens_and_config_knows_me(auth_client):
    assert auth_client.get("/api/config").json()["me"] == "tester"
    assert auth_client.get("/api/characters").status_code == 200


def test_logout_closes_the_api_again(auth_client):
    auth_client.post("/api/logout")
    assert auth_client.get("/api/characters").status_code == 401


def test_a_bad_name_does_not_log_you_in(client):
    resp = client.post("/api/login", json={"name": "   "})
    assert resp.status_code == 400
    assert resp.json()["code"] == "name.empty"
    assert client.get("/api/characters").status_code == 401


def test_character_create_list_and_delete_roundtrip(auth_client):
    created = auth_client.post(
        "/api/characters",
        json={"name": "Sparkle", "persona": "a friendly unicorn"},
    ).json()
    char_id = created["id"]

    names = [c["name"] for c in auth_client.get("/api/characters").json()]
    assert "Sparkle" in names

    auth_client.delete(f"/api/characters/{char_id}")
    names = [c["name"] for c in auth_client.get("/api/characters").json()]
    assert "Sparkle" not in names


def test_a_new_characters_first_message_is_its_greeting(auth_client):
    created = auth_client.post(
        "/api/characters",
        json={"name": "Robo", "greeting": "Beep boop!"},
    ).json()
    history = auth_client.get(f"/api/characters/{created['id']}/messages").json()
    assert history == [{"role": "assistant", "content": "Beep boop!"}]


def test_settings_persist_the_default_model(auth_client):
    auth_client.put("/api/settings", json={"default_model": "some/model"})
    body = auth_client.get("/api/settings").json()
    assert body["default_model"] == "some/model"
    assert body["effective_model"] == "some/model"


def test_effective_model_prefers_character_then_settings_then_env():
    assert main.effective_model({"model": "char/model"}) == "char/model"
    main.save_settings({"default_model": "settings/model"})
    assert main.effective_model({"model": ""}) == "settings/model"
    main.save_settings({"default_model": ""})
    assert main.effective_model(None) == main.DEFAULT_MODEL


def test_tts_engine_defaults_to_chatterbox_in_config_and_settings(auth_client):
    assert auth_client.get("/api/config").json()["tts_engine"] == "chatterbox"
    assert auth_client.get("/api/settings").json()["tts_engine"] == "chatterbox"


def test_choosing_the_device_voice_persists_and_shows_up_at_boot(auth_client):
    auth_client.put("/api/settings", json={"tts_engine": "device"})
    assert auth_client.get("/api/settings").json()["tts_engine"] == "device"
    # The frontend reads the engine from /api/config on boot, so it must be there.
    assert auth_client.get("/api/config").json()["tts_engine"] == "device"


def test_an_unknown_tts_engine_falls_back_to_chatterbox(auth_client):
    auth_client.put("/api/settings", json={"tts_engine": "banana"})
    assert auth_client.get("/api/settings").json()["tts_engine"] == "chatterbox"


def test_saving_the_model_leaves_the_tts_engine_untouched(auth_client):
    auth_client.put("/api/settings", json={"tts_engine": "device"})
    auth_client.put("/api/settings", json={"default_model": "a/b"})
    body = auth_client.get("/api/settings").json()
    assert body["default_model"] == "a/b"
    assert body["tts_engine"] == "device"


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"ok": True}


# --- Static assets revalidate, so a new deploy is seen without clearing cache -


def test_the_spa_shell_tells_the_browser_to_revalidate(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_static_assets_revalidate_and_still_get_a_cheap_304(client):
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    etag = resp.headers.get("etag")
    assert etag  # revalidation needs a validator to compare against
    # An unchanged asset should come back as a 304, not the whole file again.
    again = client.get("/app.js", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_api_responses_are_not_forced_to_no_cache(client):
    # The no-cache policy is for the static SPA, not the JSON API.
    assert "cache-control" not in {k.lower() for k in client.get("/api/config").headers}


# --- Editing an existing friend ---------------------------------------------


def test_editing_a_friend_updates_it_in_place(auth_client):
    created = auth_client.post(
        "/api/characters", json={"name": "Ziggy", "persona": "a shy dragon"}
    ).json()
    char_id = created["id"]
    auth_client.put(
        f"/api/characters/{char_id}",
        json={"name": "Ziggy", "persona": "a bold dragon", "greeting": "Rawr!"},
    )
    again = auth_client.get(f"/api/characters/{char_id}").json()
    assert again["id"] == char_id  # same friend, not a new one
    assert again["persona"] == "a bold dragon"
    assert again["greeting"] == "Rawr!"


# --- Import: SillyTavern + Character.AI card shapes --------------------------


def test_import_maps_a_sillytavern_v2_card():
    card = {
        "spec": "chara_card_v2",
        "data": {
            "name": "Nova",
            "first_mes": "Hi, I'm Nova.",
            "description": "A curious astronaut.",
            "mes_example": "{{char}}: To the stars!",
        },
    }
    char = main._card_to_character(card)
    assert char["name"] == "Nova"
    assert char["greeting"] == "Hi, I'm Nova."
    assert char["persona"] == "A curious astronaut."
    assert char["example_dialogue"] == "{{char}}: To the stars!"


def test_import_maps_a_character_ai_style_export():
    # Character.AI uses greeting/definition/title and splits persona across a
    # short description and a long definition -- both should be kept.
    card = {
        "name": "Pip",
        "title": "the tiny inventor",
        "greeting": "Hello! Want to build something?",
        "description": "A cheerful gadgeteer.",
        "definition": "{{char}} loves tinkering and explains ideas simply.",
    }
    char = main._card_to_character(card)
    assert char["name"] == "Pip"
    assert char["greeting"] == "Hello! Want to build something?"
    assert "A cheerful gadgeteer." in char["persona"]
    assert "loves tinkering" in char["persona"]


def _png_with_chara(card: dict, compressed: bool) -> bytes:
    """A minimal PNG carrying a SillyTavern 'chara' tEXt or zTXt chunk."""
    payload = base64.b64encode(json.dumps(card).encode())
    if compressed:
        body = b"chara\x00" + b"\x00" + zlib.compress(payload)  # method byte + data
        ctype = b"zTXt"
    else:
        body = b"chara\x00" + payload
        ctype = b"tEXt"
    chunk = struct.pack(">I", len(body)) + ctype + body + b"\x00\x00\x00\x00"
    iend = struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk + iend


def test_import_reads_a_compressed_ztxt_png_card():
    card = {"name": "Zed", "first_mes": "beep", "description": "a robot"}
    parsed = main._extract_card_from_png(_png_with_chara(card, compressed=True))
    assert parsed["name"] == "Zed"
    # And the plain tEXt path still works.
    parsed2 = main._extract_card_from_png(_png_with_chara(card, compressed=False))
    assert parsed2["name"] == "Zed"


# --- Paste box: /api/characters/parse fills the editor, saves nothing --------


def test_parse_maps_pasted_json_without_saving(auth_client):
    before = len(auth_client.get("/api/characters").json())
    fields = auth_client.post(
        "/api/characters/parse",
        json={"text": json.dumps({"name": "Milo", "greeting": "Hey!"})},
    ).json()
    assert fields["name"] == "Milo"
    assert fields["greeting"] == "Hey!"
    # Parsing must not create a character.
    assert len(auth_client.get("/api/characters").json()) == before


def test_parse_treats_plain_text_as_persona(auth_client):
    fields = auth_client.post(
        "/api/characters/parse",
        json={"text": "A grumpy but kind old wizard."},
    ).json()
    assert fields["persona"] == "A grumpy but kind old wizard."
    assert fields["name"] == ""  # left for the user to fill


def test_parse_rejects_empty_text(auth_client):
    resp = auth_client.post("/api/characters/parse", json={"text": "  "})
    assert resp.status_code == 400


# --- Model listing respects the key's governance (allowed-models) -----------
#
# /api/models must show only the models the key may actually use. On OpenRouter
# that means the caller-scoped /models/user catalogue (which applies account
# guardrails), not the full public /models. It falls back to /models only when
# the scoped endpoint isn't there (no key, or a plain OpenAI-compatible server).


def test_models_endpoint_prefers_the_governed_catalogue(auth_client, monkeypatch):
    calls = []

    async def fake_fetch(client, url, headers):
        calls.append(url)
        if url.endswith("/models/user"):
            return [{"id": "vendor/allowed", "name": "Allowed"}]
        return [{"id": "vendor/everything", "name": "Everything"}]

    monkeypatch.setattr(main, "_fetch_model_catalogue", fake_fetch)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-test")

    ids = [m["id"] for m in auth_client.get("/api/models").json()["models"]]
    assert ids == ["vendor/allowed"]
    # The full public catalogue is never consulted when the scoped one answers.
    assert calls == [f"{main.OPENROUTER_BASE_URL}/models/user"]


def test_models_endpoint_falls_back_to_public_when_scope_is_missing(
    auth_client, monkeypatch
):
    async def fake_fetch(client, url, headers):
        # A plain OpenAI-compatible server has no /models/user -> None (404).
        if url.endswith("/models/user"):
            return None
        return [{"id": "oai/m", "name": "M"}]

    monkeypatch.setattr(main, "_fetch_model_catalogue", fake_fetch)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-test")

    ids = [m["id"] for m in auth_client.get("/api/models").json()["models"]]
    assert ids == ["oai/m"]


def test_models_endpoint_without_a_key_uses_the_public_catalogue(
    auth_client, monkeypatch
):
    calls = []

    async def fake_fetch(client, url, headers):
        calls.append(url)
        return [{"id": "pub/m", "name": "M"}]

    monkeypatch.setattr(main, "_fetch_model_catalogue", fake_fetch)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")

    ids = [m["id"] for m in auth_client.get("/api/models").json()["models"]]
    assert ids == ["pub/m"]
    # No key -> the scoped endpoint (which needs auth) is never attempted.
    assert calls == [f"{main.OPENROUTER_BASE_URL}/models"]


def test_an_empty_governed_catalogue_is_not_widened(auth_client, monkeypatch):
    async def fake_fetch(client, url, headers):
        if url.endswith("/models/user"):
            return []  # governance answered "nothing" -- honour it
        return [{"id": "vendor/everything", "name": "Everything"}]

    monkeypatch.setattr(main, "_fetch_model_catalogue", fake_fetch)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-test")

    assert auth_client.get("/api/models").json()["models"] == []


# --- Voice output: never send the TTS a voice it will 404 on ----------------
#
# Chatterbox has no voice literally named "default" and no fallback -- it 404s,
# which silently killed all speech. An unset/legacy-"default" voice must resolve
# to a real one; an explicit choice must pass through untouched.


def test_an_explicit_voice_is_left_untouched(monkeypatch):
    async def no_voices():
        return []

    monkeypatch.setattr(main, "_fetch_voices", no_voices)
    monkeypatch.setattr(main, "DEFAULT_VOICE", "")
    assert asyncio.run(main._resolve_voice("Emily.wav")) == "Emily.wav"


def test_an_unset_voice_resolves_to_the_first_available(monkeypatch):
    async def voices():
        return ["Alice.wav", "Bob.wav"]

    monkeypatch.setattr(main, "_fetch_voices", voices)
    monkeypatch.setattr(main, "DEFAULT_VOICE", "")
    assert asyncio.run(main._resolve_voice("")) == "Alice.wav"
    # The legacy "default" sentinel is treated as unset, not sent verbatim.
    assert asyncio.run(main._resolve_voice("default")) == "Alice.wav"


def test_a_configured_default_voice_wins_over_the_server_list(monkeypatch):
    async def voices():
        return ["Alice.wav"]

    monkeypatch.setattr(main, "_fetch_voices", voices)
    monkeypatch.setattr(main, "DEFAULT_VOICE", "Custom.wav")
    assert asyncio.run(main._resolve_voice("")) == "Custom.wav"


def test_new_characters_do_not_persist_the_bogus_default_voice():
    assert main.normalize_character({"name": "Robo"})["voice"] == ""


def test_tts_sends_a_resolved_voice_not_the_bogus_default(auth_client, monkeypatch):
    captured = {}

    class _FakeResp:
        content = b"AUDIO"
        headers = {"content-type": "audio/mpeg"}

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["voice"] = json["voice"]
            return _FakeResp()

    async def voices():
        return ["Alice.wav"]

    monkeypatch.setattr(main, "_fetch_voices", voices)
    monkeypatch.setattr(main, "DEFAULT_VOICE", "")
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)

    resp = auth_client.post("/api/tts", json={"text": "hi", "voice": "default"})
    assert resp.status_code == 200
    assert captured["voice"] == "Alice.wav"


# --- OpenRouter as a hosted TTS engine (no voice server of your own) ---------
#
# Selecting "openrouter" speaks replies through OpenRouter's /audio/speech using
# the same key the chat already uses. Voices are the configured model's presets,
# read from OpenRouter's catalogue; an unknown/leftover voice falls back safely.


def test_openrouter_is_a_valid_tts_engine(auth_client):
    auth_client.put("/api/settings", json={"tts_engine": "openrouter"})
    assert auth_client.get("/api/settings").json()["tts_engine"] == "openrouter"
    # The frontend reads the engine from /api/config on boot, so it must be there.
    assert auth_client.get("/api/config").json()["tts_engine"] == "openrouter"


def test_openrouter_voice_resolution(monkeypatch):
    async def known():
        return ["af_heart", "am_puck"]

    monkeypatch.setattr(main, "_fetch_openrouter_voices", known)
    monkeypatch.setattr(main, "OPENROUTER_TTS_VOICE", "af_heart")
    # A voice the model lists passes through untouched.
    assert asyncio.run(main._resolve_openrouter_voice("am_puck")) == "am_puck"
    # Unset / legacy "default" -> the configured default voice.
    assert asyncio.run(main._resolve_openrouter_voice("")) == "af_heart"
    assert asyncio.run(main._resolve_openrouter_voice("default")) == "af_heart"
    # A leftover voice from another engine the model doesn't know -> default.
    assert asyncio.run(main._resolve_openrouter_voice("Emily.wav")) == "af_heart"


def test_openrouter_voice_resolution_trusts_explicit_when_catalogue_unreadable(
    monkeypatch,
):
    async def none():
        return []

    monkeypatch.setattr(main, "_fetch_openrouter_voices", none)
    monkeypatch.setattr(main, "OPENROUTER_TTS_VOICE", "af_heart")
    # Can't validate against a list -> trust the explicit choice, don't override.
    assert asyncio.run(main._resolve_openrouter_voice("am_puck")) == "am_puck"


def test_voices_endpoint_lists_the_models_presets_when_openrouter_is_selected(
    auth_client, monkeypatch
):
    async def presets():
        return ["af_heart", "am_puck"]

    monkeypatch.setattr(main, "_fetch_openrouter_voices", presets)
    auth_client.put("/api/settings", json={"tts_engine": "openrouter"})
    assert auth_client.get("/api/voices").json()["voices"] == ["af_heart", "am_puck"]


def test_openrouter_tts_posts_to_openrouter_with_the_key(auth_client, monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 200
        content = b"AUDIO"
        headers = {"content-type": "audio/mpeg"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    async def voices():
        return ["af_heart"]

    monkeypatch.setattr(main, "_fetch_openrouter_voices", voices)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)
    auth_client.put("/api/settings", json={"tts_engine": "openrouter"})

    resp = auth_client.post("/api/tts", json={"text": "hi", "voice": "af_heart"})
    assert resp.status_code == 200
    assert captured["url"] == f"{main.OPENROUTER_BASE_URL}/audio/speech"
    assert captured["json"]["model"] == main.OPENROUTER_TTS_MODEL
    assert captured["json"]["voice"] == "af_heart"
    # PCM in, so decodeAudioData gets one WAV container and plays the whole reply,
    # not just the first sentence of a concatenated-MP3 response.
    assert captured["json"]["response_format"] == "pcm"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    # The raw PCM comes back wrapped in a WAV header the browser can decode.
    assert resp.content[:4] == b"RIFF"
    assert resp.content.endswith(b"AUDIO")


def test_openrouter_tts_surfaces_the_services_error_reason(auth_client, monkeypatch):
    # A silent "no sound" is the bug we're fixing: OpenRouter's own reason (here,
    # no credits) must reach the response body so the UI can show it.
    class _FakeResp:
        status_code = 402

        def json(self):
            return {"error": {"message": "Insufficient credits"}}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _FakeResp()

    async def voices():
        return ["af_heart"]

    monkeypatch.setattr(main, "_fetch_openrouter_voices", voices)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)
    auth_client.put("/api/settings", json={"tts_engine": "openrouter"})

    resp = auth_client.post("/api/tts", json={"text": "hi"})
    assert resp.status_code == 502
    assert "Insufficient credits" in resp.json()["detail"]


def test_openrouter_tts_without_a_key_fails_clearly(auth_client, monkeypatch):
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")
    auth_client.put("/api/settings", json={"tts_engine": "openrouter"})
    resp = auth_client.post("/api/tts", json={"text": "hi"})
    assert resp.status_code == 502


def test_pcm_is_wrapped_into_a_valid_wav():
    # Hand-rolled header, so prove a real WAV parser accepts it and the samples
    # survive intact -- otherwise the browser would decode silence or noise.
    import io
    import wave

    pcm = b"\x01\x02\x03\x04" * 500  # 1000 16-bit mono samples
    data = main._pcm_to_wav(pcm, 24000)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2  # 16-bit
        assert w.readframes(w.getnframes()) == pcm
