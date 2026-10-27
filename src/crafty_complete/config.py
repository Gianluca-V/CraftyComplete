from pydantic_settings import BaseSettings, SettingsConfigDict

from crafty_complete import __version__


class Settings(BaseSettings):
    """Runtime configuration, read from `CRAFTY_COMPLETE_*` environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="CRAFTY_COMPLETE_", env_file=".env", extra="ignore"
    )

    crafty_url: str | None = None
    crafty_api_token: str | None = None
    # Crafty ships a self-signed certificate by default; set to false only on a trusted network.
    crafty_verify_tls: bool = True

    modrinth_url: str = "https://api.modrinth.com/v2"
    # Modrinth asks every client to send a uniquely identifying User-Agent.
    user_agent: str = f"craftyComplete/{__version__}"
    http_timeout_seconds: float = 10.0

    @property
    def crafty_configured(self) -> bool:
        return bool(self.crafty_url and self.crafty_api_token)
