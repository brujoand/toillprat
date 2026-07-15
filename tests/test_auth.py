"""Identity: names, the session store, and the two ways to answer "who is this?".

The one rule this file exists to defend: in cookie mode, identity comes from the
cookie and NEVER from a request header. See test_app.py for the HTTP-level proof.
"""

from __future__ import annotations

import pytest

from toillprat.auth import (
    MAX_NAME_LENGTH,
    InvalidName,
    ProxyIdentity,
    Sessions,
    clean_name,
    display_name,
)


class FakeConn:
    """Enough of an HTTPConnection for ProxyIdentity: just .headers.get."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


# -- names -------------------------------------------------------------------


def test_a_name_is_trimmed_and_collapsed():
    assert clean_name("  ada   lovelace  ") == "ada lovelace"


@pytest.mark.parametrize(
    "raw", ["", "   ", "\t\n", "x" * 25, "nul\x00byte", "bell\x07", None, 42]
)
def test_an_unusable_name_is_rejected(raw):
    with pytest.raises(InvalidName):
        clean_name(raw)


def test_the_error_code_is_a_stable_code_not_prose():
    try:
        clean_name("x" * 99)
    except InvalidName as exc:
        assert exc.code == "name.too_long"
        assert exc.as_dict() == {
            "code": "name.too_long",
            "params": {"max": MAX_NAME_LENGTH},
        }


# -- sessions ----------------------------------------------------------------


def test_logins_get_distinct_seats_even_with_the_same_name():
    store = Sessions()
    first_token, first = store.login("alice")
    second_token, second = store.login("alice")

    assert first_token != second_token
    assert first.sub != second.sub


def test_the_cookie_is_not_the_identity():
    # `sub` may be shown/stored; the cookie is the credential. They must differ.
    store = Sessions()
    token, player = store.login("alice")
    assert token != player.sub


def test_an_unknown_or_missing_token_resolves_to_nobody():
    store = Sessions()
    assert store.resolve(None) is None
    assert store.resolve("nope") is None


def test_logout_forgets_the_session():
    store = Sessions()
    token, _ = store.login("alice")
    store.logout(token)
    assert store.resolve(token) is None


# -- proxy identity (Pocket ID / OIDC) ---------------------------------------


def test_proxy_identity_needs_both_headers():
    proxy = ProxyIdentity()
    assert proxy.identify(FakeConn({})) is None
    assert proxy.identify(FakeConn({"x-auth-sub": "abc"})) is None
    assert proxy.identify(FakeConn({"x-auth-email": "a@b.c"})) is None


def test_proxy_identity_builds_a_namespaced_player():
    proxy = ProxyIdentity()
    player = proxy.identify(FakeConn({"x-auth-sub": "abc", "x-auth-email": "kid@home"}))
    assert player is not None
    # Namespaced sub, and NEVER the email used as the identity key.
    assert player.sub == "oidc:abc"
    assert player.name == "kid"


def test_proxy_identity_rejects_folded_headers():
    proxy = ProxyIdentity()
    conn = FakeConn({"x-auth-sub": "a,b", "x-auth-email": "kid@home"})
    assert proxy.identify(conn) is None


def test_display_name_never_raises_and_degrades_gracefully():
    assert display_name("kid@example.com") == "kid"
    assert display_name("@nothing") == "friend"
    assert len(display_name("x" * 99 + "@e")) <= MAX_NAME_LENGTH
