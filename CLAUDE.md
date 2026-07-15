# toillprat

A small, tablet-friendly character chat app: a single FastAPI process serving a
static frontend, talking to an OpenAI-compatible LLM (OpenRouter) and an
OpenAI-compatible TTS server (Chatterbox). See `README.md` for how to run it.

## Hard rules (never violate)

- **This is a PUBLIC repo. No secrets, ever.** API keys, tokens, private URLs,
  and internal hostnames come only from environment variables at runtime — never
  a default, a comment, a test fixture, or a commit. `gitleaks` runs in
  pre-commit and CI, but it is a backstop, not a licence to be careless.
- **No knowledge of any private deployment.** Do not reference a specific
  cluster, homelab, network, namespace, internal DNS name, or infrastructure
  repo. Defaults must be generic (`http://localhost:8004`, not an in-cluster
  service address). The app must read as a standalone project someone else could
  pick up.
- **Never authenticate on a request header** unless the deployment can guarantee
  the header cannot come from a client. By default a header is set by whoever is
  talking to us, so trusting one (`X-Auth-*`, `X-Forwarded-User`, …) lets anybody
  be anybody. `test_a_forged_identity_header_is_ignored` fails if that is
  reintroduced in the default (cookie) mode — do not delete it. The one sanctioned
  exception is `TRUSTED_PROXY_AUTH=1`, which takes identity from
  `X-Auth-Sub`/`X-Auth-Email`; it is safe **only** behind a proxy that
  authenticates *and* is the sole thing able to reach the process. Only `sub` and
  `email` are trusted — a header backing an *optional* claim is one the client
  still controls.
- **Never push to `main`.** Every change lands via a branch and a PR, CI green
  before merge — **only the human merges**.
- **Conventional Commits decide the release, and a type that releases nothing
  ships nothing.** `feat` → minor, `fix`/`perf`/`revert` → patch, a
  `BREAKING CHANGE:` footer → major. Anything else (`chore`, `docs`, `refactor`,
  `test`, `ci`) mints **no version**, and with no version there is no image. **The
  squash/PR title is what semantic-release reads.** CI fails if a commit touches
  the app (`toillprat/`, `Dockerfile`, `requirements.txt`) but produces no release.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest ruff

python -m pytest -q            # tests
ruff check . && ruff format --check .
pre-commit run --all-files     # ruff, gitleaks, formatting
uvicorn toillprat.main:app --reload --port 8080
docker build --build-arg VERSION=0.0.0-dev -t toillprat .
```

## Layout

- `toillprat/auth.py` — the only source of identity. `CookieIdentity` (default,
  self-claimed name → opaque cookie) and `ProxyIdentity` (Pocket ID / OIDC via
  trusted headers). Read its module docstring before touching either.
- `toillprat/main.py` — HTTP endpoints, the LLM SSE stream, the TTS proxy, the
  character/settings storage, and the middleware that gates `/api/*` on an
  identity. Config is all env; `VERSION` is stamped in at build time.
- `toillprat/web/` — the frontend SPA (`index.html`, `app.js`, `styles.css`).
  Boots from `/api/config`, which decides whether to show the login screen.
- `tests/` — `test_auth.py` (identity units) and `test_app.py` (the HTTP surface,
  incl. the forged-header guard).

## Gotchas

- **`VERSION` is build-time, not in the tree.** `pyproject.toml` says `0.0.0` and
  the image defaults to `dev`; semantic-release decides the real version *after*
  the commit and the Dockerfile bakes it in. A deployed image saying `dev` means
  its build was wrong. CI has a test that the build-arg actually reaches the image.
- **No `latest` image tag** — deployments pin an exact version.
- **Data is shared, not per-user.** Every identity sees the same characters and
  chat history. That is intentional; partitioning by `sub` would be a real change.
- **The app reads `DATA_DIR` once at import.** Tests point it at a temp dir in
  `conftest.py` *before* importing the app — keep that ordering (hence the
  `# noqa: E402`).
- **Startup work runs in the `lifespan` handler**, not on first request; the
  demo character is seeded there. `TestClient` only runs it as a context manager,
  so `conftest` seeds/ensures dirs explicitly.
