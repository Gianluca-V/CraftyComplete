# craftyComplete

Mod and modpack companion for [Crafty Controller 4](https://gitlab.com/crafty-controller/crafty-4).
It runs as a separate service and talks to Crafty through its REST API v2.

> Early development. Serve it over HTTPS (for example behind a reverse proxy) whenever it is reachable from outside your machine: the login form sends Crafty passwords to it.

## Run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn crafty_complete.main:app --port 8500
```

Open http://localhost:8500, log in with your Crafty account and browse Modrinth mods and modpacks.

## Authentication and permissions

- Users log in with their **Crafty** username, password and, if enabled, 2FA code. craftyComplete has no accounts of its own.
- Every call to Crafty uses the logged-in user's own Crafty token, so Crafty enforces that user's permissions (visible servers, files, server creation, …).
- The browser only receives an opaque session cookie (`HttpOnly`, `SameSite=Strict`). Crafty tokens stay in the server's memory and are never written to disk, so restarting craftyComplete logs everyone out.
- Sessions end after `SESSION_IDLE_MINUTES` without activity, after `SESSION_MAX_HOURS` in total, on logout, or as soon as Crafty rejects the token.
- Logging out ends the craftyComplete session only; it does not log you out of the Crafty panel.
- Crafty's login cooldown counts failed attempts per IP address. All logins arrive from craftyComplete's address, so repeated failures by one user also delay logins for everyone else for a few minutes.

## Configuration

Environment variables (or a `.env` file in the working directory):

| Variable | Default | Description |
|----------|---------|-------------|
| `CRAFTY_COMPLETE_CRAFTY_URL` | — | Crafty base URL, e.g. `https://192.168.1.20:8443`. Required for login. |
| `CRAFTY_COMPLETE_CRAFTY_API_TOKEN` | — | Optional service token, used only by `/api/health` |
| `CRAFTY_COMPLETE_CRAFTY_VERIFY_TLS` | `true` | Set `false` for Crafty's default self-signed certificate on a trusted network |
| `CRAFTY_COMPLETE_MODRINTH_URL` | `https://api.modrinth.com/v2` | Modrinth API base URL |
| `CRAFTY_COMPLETE_USER_AGENT` | `craftyComplete/<version>` | User-Agent sent to Modrinth |
| `CRAFTY_COMPLETE_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for outbound HTTP calls |
| `CRAFTY_COMPLETE_SESSION_IDLE_MINUTES` | `60` | Log out after this much inactivity |
| `CRAFTY_COMPLETE_SESSION_MAX_HOURS` | `12` | Maximum session lifetime |
| `CRAFTY_COMPLETE_SESSION_COOKIE_SECURE` | auto | `true`/`false` forces the cookie's `Secure` flag; by default it is set when the request arrived over HTTPS |

## API

Everything except `/api/health`, `/login` and `/api/auth/login` requires a session; without one, APIs answer `401` and pages redirect to `/login`.

- `GET /api/health` — service status and Crafty connectivity (`not_configured`, `connected`, `unauthorized`, `unreachable`, `error`).
- `POST /api/auth/login` — JSON body `{"username", "password", "totp"?}`. Answers `401` for wrong credentials or 2FA code, `403` for disabled accounts, `429` during Crafty's login cooldown, `502` when Crafty is unreachable, `503` when `CRAFTY_COMPLETE_CRAFTY_URL` is unset.
- `POST /api/auth/logout` — ends the session (`204`).
- `GET /api/me` — the logged-in user and their Crafty-wide permissions (`server_creation`, `user_config`, `roles_config`).
- `GET /api/servers` — the Crafty servers this user can see: `server_id`, `server_name`, `type`.
- `GET /api/catalog/search` — search Modrinth. Query parameters:
  - `q` — free text
  - `type` — `mod` (default) or `modpack`
  - `loader` — repeatable: `fabric`, `forge`, `neoforge`, `quilt`
  - `game_version` — e.g. `1.20.1`
  - `category` — repeatable, e.g. `magic`
  - `server_compatible` — `true` (default) hides client-only projects
  - `sort` — `relevance`, `downloads`, `follows`, `newest`, `updated`
  - `page` (≥ 1), `page_size` (1–100, default 20)

  Returns `502` when Modrinth fails.
- `GET /api/catalog/filters?type=mod|modpack` — loaders, release game versions and categories for the filter sidebar (cached for one hour).

## Tests

```bash
.venv/Scripts/python -m pytest
```
