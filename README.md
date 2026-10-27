# craftyComplete

Mod and modpack companion for [Crafty Controller 4](https://gitlab.com/crafty-controller/crafty-4).
It runs as a separate service and talks to Crafty through its REST API v2.

> Early development. Do not expose it outside a trusted network yet: authentication with Crafty users is not implemented.

## Run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn crafty_complete.main:app --port 8500
```

## Configuration

Environment variables (or a `.env` file in the working directory):

| Variable | Default | Description |
|----------|---------|-------------|
| `CRAFTY_COMPLETE_CRAFTY_URL` | — | Crafty base URL, e.g. `https://192.168.1.20:8443` |
| `CRAFTY_COMPLETE_CRAFTY_API_TOKEN` | — | API token created in Crafty (user settings → API keys) |
| `CRAFTY_COMPLETE_CRAFTY_VERIFY_TLS` | `true` | Set `false` for Crafty's default self-signed certificate on a trusted network |
| `CRAFTY_COMPLETE_MODRINTH_URL` | `https://api.modrinth.com/v2` | Modrinth API base URL |
| `CRAFTY_COMPLETE_USER_AGENT` | `craftyComplete/<version>` | User-Agent sent to Modrinth |
| `CRAFTY_COMPLETE_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for outbound HTTP calls |

## API

- `GET /api/health` — service status and Crafty connectivity (`not_configured`, `connected`, `unauthorized`, `unreachable`, `error`).

## Tests

```bash
.venv/Scripts/python -m pytest
```
