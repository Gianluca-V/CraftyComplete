from pydantic_settings import BaseSettings, SettingsConfigDict

from crafty_complete import __version__


class Settings(BaseSettings):
    """Runtime configuration, read from `CRAFTY_COMPLETE_*` environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="CRAFTY_COMPLETE_", env_file=".env", extra="ignore"
    )

    crafty_url: str | None = None
    # Optional service token, used only by /api/health. Users act with their own tokens.
    crafty_api_token: str | None = None
    # Crafty ships a self-signed certificate by default; set to false only on a trusted network.
    crafty_verify_tls: bool = True

    modrinth_url: str = "https://api.modrinth.com/v2"
    # Modrinth asks every client to send a uniquely identifying User-Agent.
    user_agent: str = f"craftyComplete/{__version__}"
    http_timeout_seconds: float = 10.0

    # SQLite file holding adopted servers and their mods.
    database_path: str = "crafty_complete.sqlite3"
    # How often to look for mod updates in the background (0 disables it). Never installs.
    update_check_hours: float = 6

    # Where modpacks are downloaded while a server is built (default: the system temp dir).
    modpack_work_dir: str | None = None
    # How long to wait for Crafty to install a server's loader, and how often to ask.
    modpack_install_timeout_minutes: float = 20
    modpack_poll_seconds: float = 5
    # Files above this size are uploaded to Crafty in chunks (its request limit is 100 MB).
    upload_chunk_bytes: int = 10 * 1024 * 1024

    session_idle_minutes: float = 60
    session_max_hours: float = 12
    # None: mark the cookie Secure only when the request arrived over HTTPS.
    session_cookie_secure: bool | None = None

    @property
    def crafty_configured(self) -> bool:
        return bool(self.crafty_url and self.crafty_api_token)
