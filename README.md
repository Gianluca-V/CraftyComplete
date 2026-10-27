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

Open http://localhost:8500, log in with your Crafty account and browse Modrinth mods and modpacks. Under **Servers**, open a server and press **Adopt server** to identify the mods it already has and see which ones have updates. Then:

- **Install** a mod from the browse page (pick the server under "Install to"); its missing required dependencies are installed too.
- **Update** one mod, or **Update all** unpinned mods, from the server page.
- **Check for updates** refreshes the "update available" badges; this also runs in the background. Nothing is ever updated without you pressing a button.

Changed mods load the next time the server starts: restart it from Crafty.

To **create a server from a modpack**, open the Modpacks tab, press **Create server** on a Fabric, Forge or NeoForge pack, choose a version, name, port and memory, and accept the Minecraft EULA. craftyComplete then, in the background:

1. downloads the `.mrpack` and every server file it lists, checking each against the pack's SHA-1/SHA-512, and drops mods Modrinth marks as client-only (nothing is created in Crafty if any of this fails);
2. asks Crafty to create the server and install the loader. Crafty installs the newest loader build it offers for that Minecraft version; you get a warning when that differs from the version the pack names;
3. uploads the pack's mods and `overrides/` (then `server-overrides/`) to the server and records it as adopted, linked to the modpack version.

Quilt modpacks are not supported yet. If a step fails after Crafty created the server, the error names it so you can delete it in Crafty.

## Authentication and permissions

- Users log in with their **Crafty** username, password and, if enabled, 2FA code. craftyComplete has no accounts of its own.
- Every call to Crafty uses the logged-in user's own Crafty token, so Crafty enforces that user's permissions (visible servers, files, server creation, …).
- The browser only receives an opaque session cookie (`HttpOnly`, `SameSite=Strict`). Crafty tokens stay in the server's memory and are never written to disk, so restarting craftyComplete logs everyone out.
- Sessions end after `SESSION_IDLE_MINUTES` without activity, after `SESSION_MAX_HOURS` in total, on logout, or as soon as Crafty rejects the token.
- Logging out ends the craftyComplete session only; it does not log you out of the Crafty panel.
- Sessions live in one process, so run a single uvicorn worker (the default). With `--workers` above 1, users would be logged out whenever a request lands on another worker.
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
| `CRAFTY_COMPLETE_DATABASE_PATH` | `crafty_complete.sqlite3` | SQLite file with adopted servers and their mods |
| `CRAFTY_COMPLETE_MODPACK_WORK_DIR` | system temp dir | Where modpacks are downloaded while a server is built; emptied after each job. Needs room for the pack and its files (several GB for large packs) |
| `CRAFTY_COMPLETE_MODPACK_INSTALL_TIMEOUT_MINUTES` | `20` | How long to wait for Crafty to install the loader |
| `CRAFTY_COMPLETE_MODPACK_POLL_SECONDS` | `5` | How often to ask Crafty whether the loader is installed |
| `CRAFTY_COMPLETE_UPLOAD_CHUNK_BYTES` | `10485760` | Files above this size go to Crafty in chunks (Crafty rejects single requests over 100 MB) |
| `CRAFTY_COMPLETE_UPDATE_CHECK_HOURS` | `6` | How often to look for mod updates in the background; `0` turns it off. It only marks updates as available |
| `CRAFTY_COMPLETE_SESSION_IDLE_MINUTES` | `60` | Log out after this much inactivity |
| `CRAFTY_COMPLETE_SESSION_MAX_HOURS` | `12` | Maximum session lifetime |
| `CRAFTY_COMPLETE_SESSION_COOKIE_SECURE` | auto | `true`/`false` forces the cookie's `Secure` flag; by default it is set when the request arrived over HTTPS. Behind a reverse proxy that terminates TLS, set it to `true` (or run uvicorn with `--proxy-headers`). |

## API

Everything except `/api/health`, `/login` and `/api/auth/login` requires a session; without one, APIs answer `401` and pages redirect to `/login`. FastAPI's interactive docs (`/docs`, `/redoc`, `/openapi.json`) are disabled.

- `GET /api/health` — service status and Crafty connectivity (`not_configured`, `connected`, `unauthorized`, `forbidden`, `not_found`, `unreachable`, `error`).
- `POST /api/auth/login` — JSON body `{"username", "password", "totp"?}`. Answers `401` for wrong credentials or 2FA code, `403` for disabled accounts or accounts Crafty refuses (for example when it enforces 2FA setup), `429` during Crafty's login cooldown, `502` when Crafty is unreachable, `503` when `CRAFTY_COMPLETE_CRAFTY_URL` is unset.
- `POST /api/auth/logout` — ends the session (`204`). Must be sent as JSON (`415` otherwise). Logging in again also ends the previous session.
- `GET /api/me` — the logged-in user and their Crafty-wide permissions (`server_creation`, `user_config`, `roles_config`).
- `GET /api/servers` — the Crafty servers this user can see: `server_id`, `server_name`, `type`.
- `POST /api/servers/{server_id}/scan` — adopt the server (first call) or rescan it. JSON body `{"loader"?, "game_version"?}` overrides the detected loader (`fabric`, `forge`, `neoforge`, `quilt`) and Minecraft version; otherwise the previous values are kept, or they are inferred from the identified mods. Reads every `mods/*.jar` through Crafty, identifies it on Modrinth by SHA-1 and checks for a newer compatible version. Nothing is updated. Answers `403` without Crafty file access to the server, `404` when the server has no `mods` folder, `502` when Modrinth fails (nothing is saved then).
- `GET /api/servers/{server_id}/mods` — the stored result of the last scan (`adopted: false` before the first one). Each mod has `state` (`identified` or `unknown`), current and latest version, `update_available`, `pinned` and Modrinth's `server_side`; `counts.updates` excludes pinned mods.
- `PATCH /api/servers/{server_id}/mods/{file_name}` — JSON `{"pinned": true|false}`. A pinned mod is never offered for update; the pin follows the Modrinth project (or the exact file, for unknown jars) across rescans.
- `POST /api/servers/{server_id}/mods` — JSON `{"project_id": "<Modrinth id or slug>"}`. Installs the newest compatible release for the server's loader and Minecraft version (Quilt servers also accept Fabric mods), plus missing required dependencies. Refuses client-only projects and modpacks (`422`), projects with no compatible version (`422`), projects already installed, mods declared incompatible with an installed one, or a different file already using the target name (`409`), and servers not adopted yet (`409`). Dependencies that are client-only or have no compatible version, and files Modrinth does not host, are reported under `skipped` for you to handle by hand.
- `POST /api/servers/{server_id}/mods/{file_name}/update` — update one mod to its latest compatible version (`409` when pinned, unknown or already up to date).
- `POST /api/servers/{server_id}/update-all` — update every unpinned mod that has an update; failures are listed under `failed` and do not stop the rest.
- `POST /api/servers/{server_id}/check-updates` — refresh `latest_version_*` and `update_available` from the stored hashes, without downloading jars. Returns the server like `GET .../mods`, including `updates_checked_at`.

- `GET /api/modpacks/{project}/versions` — a modpack's versions with `loaders`, `game_versions`, `version_type` and `supported` (Fabric, Forge or NeoForge).
- `POST /api/modpacks/servers` — JSON `{"project_id", "version_id"?, "name", "port", "mem_min", "mem_max", "agree_to_eula": true}` (memory in GB). Answers `202 {"job_id"}`. Refuses without EULA consent, a modpack, a Fabric/Forge/NeoForge version, a loader Crafty can install for that Minecraft version, or Crafty's server-creation permission.
- `GET /api/jobs/{job_id}` — progress of your own job: `status` (`running`, `succeeded`, `failed`), `step`, `progress`, `warnings`, `error`, `server_id`. Jobs live in memory and are lost on restart.

Install and update responses contain `installed`, `updated`, `skipped`, `failed`, `restart_required` and the refreshed `server`. Every download must come from `https://cdn.modrinth.com/` and match Modrinth's published SHA-1 and SHA-512, or nothing is written. A replaced jar is deleted only after its new version has been uploaded and recorded; if Crafty cannot delete it (for example while the server runs and holds the file), both jars stay listed and the error says what to remove. Updating again only retries the delete. File names Crafty cannot receive (non-ASCII) are skipped for manual upload. The bodyless `POST` endpoints still require `Content-Type: application/json` (`415` otherwise).

Server endpoints check, with your own Crafty token, that you have Crafty's **Files** permission on the server before reading or changing anything stored about it (the Backup permission alone is not enough). The check asks Crafty to download a file that does not exist, `.crafty-complete-permission-check`, which Crafty answers with "not found" only when the permission is granted.
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
