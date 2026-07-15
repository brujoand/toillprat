"""The HTTP surface: config, the login gate, and the character/settings API.

The security rule that matters most here is
test_a_forged_identity_header_is_ignored: in the default cookie mode, sending
x-auth-* headers must NOT log you in. If that ever passes identity through, the
whole app is handed to anyone who can set a header.
"""

from __future__ import annotations

import asyncio

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


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"ok": True}


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
