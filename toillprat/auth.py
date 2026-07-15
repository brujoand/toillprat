"""Who is using toillprat. Two ways, and the deployment picks.

**CookieIdentity is the default.** You claim a display name, the server hands back
an opaque token, and that token is who you are until it expires. No password, no
account: the right amount of security for a family chat toy and no more. The
cookie is the only credential, so it is unguessable, HttpOnly so page scripts
cannot read it, and SameSite=Lax so another origin cannot ride along on it.

In this mode a request header is worth NOTHING. Whoever is talking to us can set
any header they like, so treating one as proof of identity would let anybody be
anybody. There is a test that fails if that is reintroduced.

**ProxyIdentity is opt-in, and trusts exactly two headers.** It exists for a
deployment where the process is unreachable except through an authenticating
proxy -- e.g. behind an ingress that terminates a Pocket ID / OIDC flow and, via
a network policy, is the only client that can open a socket to us. There, and
only there, the header is not client-supplied: it is worth exactly as much as the
proxy's own authentication.

The code cannot check that precondition. The flag is an operator's assertion, and
if it is wrong the server is handed to anyone who can send a header.
"""

from __future__ import annotations

import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from starlette.requests import HTTPConnection

COOKIE_NAME = "toillprat_session"
SESSION_TTL = 12 * 60 * 60
MAX_NAME_LENGTH = 24
MAX_SESSIONS = 10_000

# Only claims that are in EVERY token may back a trusted header.
#
# An OIDC proxy's claim-to-header mapping typically OVERWRITES the header when the
# claim is present and LEAVES IT ALONE when the claim is missing -- so a header
# backing an OPTIONAL claim is a header the client still controls. `sub` and
# `email` are guaranteed by the flow; `name` is not, and must never be trusted.
SUB_HEADER = "x-auth-sub"
EMAIL_HEADER = "x-auth-email"


class InvalidName(Exception):
    """Raised when a display name is unusable.

    The message is a stable CODE and never a sentence: the browser owns the prose,
    because the server does not know what language the user reads.
    """

    def __init__(self, code: str, /, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.params = params

    def as_dict(self) -> dict:
        return {"code": self.code, "params": self.params}


@dataclass
class Player:
    sub: str
    name: str


@dataclass
class Sessions:
    """Live logins, keyed by cookie token."""

    players: dict[str, Player] = field(default_factory=dict)
    seen: dict[str, float] = field(default_factory=dict)

    def login(self, raw_name: str) -> tuple[str, Player]:
        name = clean_name(raw_name)
        self.reap()
        if len(self.players) >= MAX_SESSIONS:
            raise InvalidName("name.too_many_players")

        token = secrets.token_urlsafe(32)
        # The id is not the cookie: a credential must never be something we would
        # ever hand back out.
        player = Player(sub=f"u-{secrets.token_urlsafe(8)}", name=name)
        self.players[token] = player
        self.seen[token] = time.monotonic()
        return token, player

    def resolve(self, token: str | None) -> Player | None:
        if not token:
            return None
        player = self.players.get(token)
        if player is None:
            return None
        self.seen[token] = time.monotonic()
        return player

    def logout(self, token: str | None) -> None:
        if token:
            self.players.pop(token, None)
            self.seen.pop(token, None)

    def reap(self) -> int:
        now = time.monotonic()
        stale = [t for t, last in self.seen.items() if now - last > SESSION_TTL]
        for token in stale:
            self.players.pop(token, None)
            self.seen.pop(token, None)
        return len(stale)


def clean_name(raw: str) -> str:
    """Normalise a claimed display name, or reject it.

    Names are rendered into the page, so anything that is not a printable,
    non-controlling character is refused outright rather than escaped and
    forgotten about.
    """
    if not isinstance(raw, str):
        raise InvalidName("name.not_text")

    name = " ".join(raw.split())  # collapse runs of whitespace, strip the ends
    if not name:
        raise InvalidName("name.empty")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidName("name.too_long", max=MAX_NAME_LENGTH)
    if any(not ch.isprintable() for ch in name):
        raise InvalidName("name.unprintable")
    return name


def display_name(email: str) -> str:
    """A usable name from a verified address: its local part, made safe to render.

    Unlike clean_name this NEVER raises. There is no login form to bounce a user
    back to -- the proxy has already said who they are -- so a strange address
    must degrade into something we can print, not 401 someone out of the building.
    """
    local = unicodedata.normalize("NFC", email).partition("@")[0]
    name = " ".join(local.split())
    name = "".join(ch for ch in name if ch.isprintable())
    return name[:MAX_NAME_LENGTH] or "friend"


class Identity(Protocol):
    """How this deployment answers "who is this?"."""

    mode: str  # "cookie" | "proxy" -- the frontend asks, so it knows what to render
    login_enabled: bool

    def identify(self, conn: HTTPConnection) -> Player | None: ...


class CookieIdentity:
    """The default. A self-declared name, exchanged for an opaque cookie."""

    mode = "cookie"
    login_enabled = True

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions

    def identify(self, conn: HTTPConnection) -> Player | None:
        return self.sessions.resolve(conn.cookies.get(COOKIE_NAME))


class ProxyIdentity:
    """Identity from headers that only a trusted proxy can set.

    Safe if and only if the deployment guarantees the proxy is the ONLY thing that
    can reach this process. Read the module docstring before turning this on.
    """

    mode = "proxy"
    login_enabled = False

    def identify(self, conn: HTTPConnection) -> Player | None:
        sub = conn.headers.get(SUB_HEADER, "").strip()
        email = conn.headers.get(EMAIL_HEADER, "").strip()

        # Both, or nobody. No falling back to a cookie and no falling back to a
        # third header: a fallback is a downgrade, and the downgrade is the
        # attack. A comma means two headers were folded into one, which is not a
        # request we are willing to guess about either.
        if not sub or not email or "," in sub or "," in email:
            return None

        # Namespaced so the two identity spaces can never be confusable. And
        # never, ever use the email as the sub.
        return Player(sub=f"oidc:{sub}", name=display_name(email))
