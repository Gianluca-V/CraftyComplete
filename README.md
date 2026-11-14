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

Open http://localhost:8500 and log in with your Crafty account: the home page is the **Servers** list, and **Browse** searches Modrinth mods and modpacks. Open a server and press **Adopt server** to identify the mods it already has and see which ones have updates. Then:

- **Install** a mod from the browse page (pick the server under "Install to"); its missing required dependencies are installed too.
- **Update** one mod, or **Update all** unpinned mods, from the server page. **Update all** first shows which mods move to which version and asks you to confirm.
- **Version** installs another version of a mod, chosen from the versions that fit the server's loader and Minecraft version (missing required dependencies are installed too). The chosen version is not pinned, so **Update all** can move it to the newest one again.
- **Remove** deletes a mod. If other mods require it, you have to confirm. Dependencies craftyComplete installed for it that no other mod needs are listed, and removed only if you tick them. Mods you added yourself are never offered. Removing a mod of the linked modpack warns that the next modpack update adds it back. When Modrinth cannot be reached, required-by checks are skipped, no dependencies are offered and the removal needs confirmation. A jar already deleted outside craftyComplete is just dropped from the list.
- **Drop jars** on the server page (or press **Choose files**) to add your own `.jar` files to `mods/`, several at once, up to 90 MiB each. Each one is identified on Modrinth like a scan does; you get a warning for client-only mods and for a second jar of a mod already installed. A file with the same name as one in `mods/` is refused. **Search mods** filters the mod list by name or file name.
- **Check for updates** refreshes the "update available" badges; this also runs in the background. Nothing is ever updated without you pressing a button.

Changed mods load the next time the server starts: restart it from Crafty or from the server page.

The **Servers** list shows whether each server is running and how many players are online. The server page shows its state (running, stopped, starting, updating or crashed), the players online, CPU and memory use, Minecraft version, world size and port, refreshed every 10 seconds, with **Start**, **Restart**, **Stop** and **Kill** buttons. These need Crafty's Commands permission on that server. Start and Restart are refused while a modpack update is changing the server's files.

The **Players** panel on the server page lists who is online, the whitelist, the operators and the banned players, and adds or removes players from each list (whitelist, op/deop, ban/pardon) or kicks them. While the server runs, changes are sent as console commands (Crafty's Commands permission). While it is stopped, craftyComplete edits `whitelist.json`, `ops.json` and `banned-players.json` (Files permission) and the change applies on the next start; new entries get the player's UUID from Mojang, or the offline UUID when `server.properties` sets `online-mode=false`. Kick needs a running server.

The **Files** panel on the server page browses the server's folders and edits its text files in the browser, such as `server.properties`, `options.txt` or mod configs in `config/` (Crafty's Files permission). Only UTF-8 text files up to 1 MiB open; jars and other binary files are refused, and files whose path has non-ASCII characters open but cannot be saved (Crafty's upload takes the name in an HTTP header). **Save** (or Ctrl+S) replaces the file, keeping its Windows line endings if it had them. Saving is refused when the file changed on the server since you opened it (open it again to see the new content) and while a modpack update is changing the server's files. Most files are read when the server starts, so restart it to apply your changes. **Search this folder** filters the open folder by file name.

The **Backups** panel on the server page lists the backups Crafty has made of the server, newest first, with their date in the Crafty server's local time (Crafty's Backup permission). Crafty only lists backup files through its API from version 4.10.5; on older versions (such as 4.7) the panel shows the backup configurations and **Back up now**, and you see the backups in the Crafty panel. **Back up now** asks Crafty to make one with the server's default backup configuration, or the one you choose when there are several (Commands permission); create configurations in the Crafty panel. A configuration that stops the server while it runs asks you to confirm first. Crafty does not report when a backup finishes: press **Refresh** to see it. Backing up is refused while a modpack update is changing the server's files. Snapshot backups are not listed, only zip backups.

To **create a server without a modpack**, press **New server** on the Servers page. Choose Vanilla, Paper, Fabric, Forge or NeoForge and one of the Minecraft versions Crafty offers for it, then a name, port and memory, and accept the Minecraft EULA. For Fabric, Forge and NeoForge you can list Modrinth mods (slugs or links, one per line): each is checked against the loader and version before anything is created, the server is adopted, and the mods are installed with their required dependencies. A mod that fails after the server exists becomes a warning; install it from the server page. Paper plugins are not managed.

To **create a server from a modpack**, open the Modpacks tab, press **Create server** on a Fabric, Forge or NeoForge pack, choose a version, name, port and memory, and accept the Minecraft EULA. craftyComplete then, in the background:

1. downloads the `.mrpack` and every server file it lists, checking each against the pack's SHA-1/SHA-512, and drops mods Modrinth marks as client-only (nothing is created in Crafty if any of this fails);
2. asks Crafty to create the server and install the loader. Crafty installs the newest loader build it offers for that Minecraft version; you get a warning when that differs from the version the pack names;
3. creates the folders the pack needs and uploads its mods and `overrides/` (then `server-overrides/`) to the server and records it as adopted, linked to the modpack version.

Quilt modpacks are not supported yet. Each user can build one modpack server at a time. The build keeps using your Crafty session until it ends, even if you log out of craftyComplete; if craftyComplete stops mid-build, the job is marked as failed. If a step fails after Crafty created the server, the error names it so you can delete it in Crafty.

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
| `CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN` | — | Crafty API token the MCP server acts with (see [MCP server](#mcp-server)); not used by the web app |
| `CRAFTY_COMPLETE_MOJANG_URL` | `https://api.mojang.com` | Mojang API, used to find a player's UUID when editing the player files of a stopped server |
| `CRAFTY_COMPLETE_CRAFTY_VERIFY_TLS` | `true` | Set `false` for Crafty's default self-signed certificate on a trusted network |
| `CRAFTY_COMPLETE_MODRINTH_URL` | `https://api.modrinth.com/v2` | Modrinth API base URL |
| `CRAFTY_COMPLETE_USER_AGENT` | `craftyComplete/<version>` | User-Agent sent to Modrinth |
| `CRAFTY_COMPLETE_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for outbound HTTP calls |
| `CRAFTY_COMPLETE_DATABASE_PATH` | `crafty_complete.sqlite3` | SQLite file with adopted servers and their mods |
| `CRAFTY_COMPLETE_MODPACK_WORK_DIR` | system temp dir | Where modpacks are downloaded while a server is built; emptied after each job. Needs room for the pack and its files (several GB for large packs) |
| `CRAFTY_COMPLETE_MODPACK_INSTALL_TIMEOUT_MINUTES` | `20` | How long to wait for Crafty to install the loader |
| `CRAFTY_COMPLETE_MODPACK_POLL_SECONDS` | `5` | How often to ask Crafty whether the loader is installed |
| `CRAFTY_COMPLETE_UPLOAD_CHUNK_BYTES` | `10485760` | Files above this size go to Crafty in chunks (Crafty rejects single requests over 100 MB) |
| `CRAFTY_COMPLETE_CRAFTY_RETRY_SECONDS` | `2` | Base delay before retrying a Crafty request that failed with a network error or a server error while building a modpack server (up to 3 attempts) |
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
- `GET /api/servers/{server_id}/status` — `state` (`running`, `stopped`, `starting`, `updating`, `crashed`), `players_online`, `max_players`, `players`, `cpu_percent`, `memory`, `memory_percent`, `started`, `version`, `world_size`, `port`.
- `POST /api/servers/{server_id}/power` — JSON `{"action": "start"|"stop"|"restart"|"kill"}`. Needs Crafty's Commands permission (`403`). Start and restart answer `409` while a modpack update writes the server's files.
- `GET /api/servers/{server_id}/mods/{file_name}/versions` — versions of an installed mod for the server's loader and Minecraft version, and `current_version_id`.
- `POST /api/servers/{server_id}/mods/{file_name}/version` — JSON `{"version_id"}`: install that version (with missing dependencies). Not pinned; `latest_*` keeps the newest release, so Update all can move it back. `409` for pinned mods or the installed version, `422` for a version that does not fit the server.
- `GET /api/servers/{server_id}/mods/{file_name}/removal` — what removing a mod affects: `required_by`, `unused_dependencies` (only mods craftyComplete installed as dependencies that nothing else needs), `warnings`, `requirements_checked` (false when Modrinth cannot be reached).
- `POST /api/servers/{server_id}/mods/{file_name}/remove` — JSON `{"force"?, "remove_dependencies"?: [file names]}`. A mod other mods require, or any mod while Modrinth is unreachable, needs `force` (`409` otherwise). Only dependencies from the plan are accepted, and none that a mod you keep still requires (`409`, nothing deleted).
- `GET /api/servers/{server_id}/players` — `running`, `online`, `whitelist`, `ops`, `banned`.
- `POST /api/servers/{server_id}/players` — JSON `{"action", "name", "reason"?}` with `action` one of `whitelist_add`, `whitelist_remove`, `op`, `deop`, `ban`, `pardon`, `kick`. Names must be Minecraft names (`^[A-Za-z0-9_]{1,16}$`); a reason (one line, at most 200 characters) only applies to ban and kick. Answers `{"applied": "command"|"file", "changed"}`.
- `GET /api/servers/new/options` — server kinds (`vanilla`, `paper`, `fabric`, `forge`, `neoforge`) with the Minecraft versions Crafty's catalog offers for each.
- `POST /api/servers/new` — JSON `{"kind", "minecraft", "name", "port", "mem_min", "mem_max", "mods"?: [Modrinth ids or slugs], "agree_to_eula": true}`. Answers `202 {"job_id"}`; the job's `result.installed` lists the mods installed.
- `POST /api/servers/{server_id}/check-updates` — refresh `latest_version_*` and `update_available` from the stored hashes, without downloading jars. Returns the server like `GET .../mods`, including `updates_checked_at`.

- `GET /api/modpacks/{project}/versions` — a modpack's versions with `loaders`, `game_versions`, `version_type` and `supported` (Fabric, Forge or NeoForge).
- `POST /api/modpacks/servers` — JSON `{"project_id", "version_id"?, "name", "port", "mem_min", "mem_max", "agree_to_eula": true}` (memory in GB). Answers `202 {"job_id"}`. Refuses without EULA consent, a modpack, a Fabric/Forge/NeoForge version, a loader Crafty can install for that Minecraft version, or Crafty's server-creation permission.
- `GET /api/servers/{server_id}/modpack` — the modpack version an adopted server is linked to (`linked`, or `null`), the compatible versions for its Minecraft version and loader (`versions`, newest first), and `latest`/`update_available`. Only newer versions count as updates: a newer release wins; a server on a pre-release with no newer release is offered the newest newer pre-release.
- `POST /api/servers/{server_id}/modpack/link` — JSON `{"project_id", "version_id"}`: record which modpack version the server runs. Downloads only the `.mrpack` to learn the path and SHA-1 of every file the pack puts on a server. Answers `202 {"job_id"}`; the job's `result` lists pack mods `missing` from the server, the server's `own_mods`, pack mods the server has under the same file name as `other_builds` (for example CurseForge builds), and pack mods meant for clients only that the server has as `client_only`. A jar counts as the pack's when its SHA-1 or its path matches.
- `POST /api/servers/{server_id}/modpack/update` — JSON `{"version_id"?}` (default: the update `latest` offers). The server must be linked and stopped in Crafty (`409` otherwise). Files the pack added or changed are uploaded; files the pack left alone keep the server's copy (config edits survive); jars the pack dropped from `mods/` are removed; pack mods missing from the server are uploaded again (`restored`), unless the server has another jar of the same Modrinth project; your own mods (for example Distant Horizons) and pinned mods are left alone, except your own jar of a project the pack now ships, which is moved aside. Every replaced or removed file is moved to `.crafty-complete/backups/<time>-<old>-to-<new>/` on the server, never deleted. The job's `result` lists `added`, `updated`, `removed`, `restored`, `own_mods_kept` and `backup_folder`; start the server in Crafty afterwards. While the update writes files, scans, installs, mod updates, pins and update checks on that server answer `409` instead of waiting.
- `GET /api/jobs/{job_id}` — progress of your own job: `status` (`running`, `succeeded`, `failed`), `step`, `progress`, `warnings`, `error`, `server_id`, `result`. Jobs live in memory and are lost on restart.

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

## MCP server

`crafty-complete-mcp` is an [MCP](https://modelcontextprotocol.io) server that lets an AI agent do everything the web app does: list servers and their status, start and stop them, adopt them, search, install, update, pin, version and remove mods, manage players, read and edit text files, list and make backups, create vanilla, Paper, mod-loader and modpack servers, and link or update modpacks. It runs as a local stdio process started by the MCP client and shares the web app's code and SQLite database.

It acts with one Crafty API token (create one in the Crafty panel under your user's API keys), so Crafty enforces that user's permissions. Keep the token out of shared config files and give it to a user with only the permissions the agent needs.

Example for Claude Desktop (`claude_desktop_config.json`) or Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "crafty": {
      "command": "C:/path/to/craftyComplete/.venv/Scripts/crafty-complete-mcp.exe",
      "env": {
        "CRAFTY_COMPLETE_CRAFTY_URL": "https://192.168.1.20:8443",
        "CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN": "<Crafty API token>",
        "CRAFTY_COMPLETE_DATABASE_PATH": "C:/path/to/craftyComplete/crafty_complete.sqlite3"
      }
    }
  }
}
```

On Linux the command is `.venv/bin/crafty-complete-mcp`. Point `CRAFTY_COMPLETE_DATABASE_PATH` at the web app's database so both see the same adopted servers.

Tools that stop, restart or kill a server, remove a mod, ban or kick a player, make a backup with a configuration that stops the server, or move a server to another modpack version take two calls. The first returns a `plan` and a `confirm_token`; the agent should show the plan to you and call again with the same arguments plus the token only if you agree. A token works once, for that tool and those arguments, for 5 minutes. Creating a server requires `accept_minecraft_eula: true`, which the agent should only send after you accept the EULA. Long work returns a `job_id` to follow with `job_status`; jobs live as long as the MCP server process, one at a time. `write_file` replaces a whole text file and needs the `sha256` that `read_file` returned, so it fails instead of overwriting a file that changed since the agent read it.

The MCP server and the web app are separate processes: a modpack update running in one does not block mod changes started from the other, so avoid changing the same server from both at once.

## Tests

```bash
.venv/Scripts/python -m pytest
```
