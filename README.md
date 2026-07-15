# toillprat

A small, tablet-friendly character chat app. Pick an avatar, type a message, and
read *and hear* the reply. Characters can be created in the app or imported from
SillyTavern-style character cards (`.json` or `.png`).

It is deliberately tiny: a single FastAPI process serving a static frontend, with
two pluggable backends chosen entirely by environment variables:

- an **OpenAI-compatible LLM API** for the replies (OpenRouter by default),
  streamed to the browser token by token;
- an **OpenAI-compatible TTS server** ([Chatterbox](https://github.com/devnen/Chatterbox-TTS-Server))
  for the spoken audio.

Characters, chat history, and settings are stored as plain JSON under `DATA_DIR`
and are shared by everyone using the instance.

## Run it

```bash
docker run -p 8080:8080 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e CHATTERBOX_URL=http://your-tts-host:8004 \
  -v toillprat-data:/data \
  ghcr.io/brujoand/toillprat:1
```

Then open <http://localhost:8080>, claim a name, and start chatting. Without a
TTS server the chat still works; you just get no audio.

## Configuration

Everything is an environment variable; nothing is baked in.

| Variable             | Default                        | What it does                                                        |
| -------------------- | ------------------------------ | ------------------------------------------------------------------- |
| `OPENROUTER_API_KEY` | *(empty)*                      | API key for the LLM. Without it, chat returns a friendly "not set up" message. |
| `OPENROUTER_BASE_URL`| `https://openrouter.ai/api/v1` | Any OpenAI-compatible chat-completions endpoint.                    |
| `DEFAULT_MODEL`      | `deepseek/deepseek-v3.2-exp`   | Model used when a character has none of its own. Overridable in-app. |
| `CHATTERBOX_URL`     | `http://localhost:8004`        | OpenAI-compatible TTS server for spoken replies.                    |
| `DEFAULT_VOICE`      | `default`                      | Voice name passed to the TTS server.                                |
| `DATA_DIR`           | `/data`                        | Where characters, chats, and settings persist.                      |
| `TRUSTED_PROXY_AUTH` | *(off)*                        | See **Login** below. Only set behind a trusted, sole-access proxy.  |
| `SECURE_COOKIE`      | *(off)*                        | Set to `1` when serving over HTTPS so the session cookie is `Secure`. |

## Login

The app answers one question — *who is using it?* — in one of two ways, and the
deployment picks.

- **Cookie (default).** You claim a display name and get an opaque, HttpOnly
  session cookie. No account, no password: the right amount of security for a
  family chat toy. In this mode a request header is worth **nothing** — identity
  comes only from the cookie.
- **Proxy / OIDC (`TRUSTED_PROXY_AUTH=1`).** Identity is taken from the
  `X-Auth-Sub` / `X-Auth-Email` request headers, which an authenticating reverse
  proxy sets after an OIDC flow (e.g. [Pocket ID](https://pocket-id.org/) behind
  an ingress). This is safe **only** where that proxy is the sole thing that can
  reach the process — the app cannot verify that, so the flag is your assertion.
  Read the module docstring in `toillprat/auth.py` before turning it on.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest ruff

python -m pytest -q            # tests
ruff check . && ruff format --check .
pre-commit run --all-files     # ruff, gitleaks, formatting

uvicorn toillprat.main:app --reload --port 8080
```

## Releases

Commits follow [Conventional Commits](https://www.conventionalcommits.org/).
Merging to `main` runs [semantic-release](https://semantic-release.gitbook.io/),
which reads the commits and decides the next version. Only if it publishes a
release does the image get built and pushed to GHCR, tagged with that exact
version — there is no `latest` tag, so a deployment always names the version it
wants.

## License

[MIT](LICENSE)
