"""The HTTP surface: config, the login gate, and the character/settings API.

The security rule that matters most here is
test_a_forged_identity_header_is_ignored: in the default cookie mode, sending
x-auth-* headers must NOT log you in. If that ever passes identity through, the
whole app is handed to anyone who can set a header.
"""

from __future__ import annotations

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
