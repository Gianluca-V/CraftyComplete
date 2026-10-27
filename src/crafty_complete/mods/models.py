from dataclasses import asdict, dataclass, field
from typing import Literal

ModState = Literal["identified", "unknown"]


@dataclass
class InstalledMod:
    file_name: str
    sha1: str
    state: ModState
    project_id: str | None = None
    slug: str | None = None
    title: str | None = None
    icon_url: str | None = None
    version_id: str | None = None
    version_number: str | None = None
    server_side: str | None = None
    latest_version_id: str | None = None
    latest_version_number: str | None = None
    pinned: bool = False

    @property
    def update_available(self) -> bool:
        return bool(self.latest_version_id and self.latest_version_id != self.version_id)

    @property
    def pin_key(self) -> str:
        """What a pin follows across rescans: the project if known, otherwise the exact file."""
        return f"project:{self.project_id}" if self.project_id else f"sha1:{self.sha1}"

    def to_dict(self) -> dict:
        data = asdict(self)
        pinned = data.pop("pinned")
        return {**data, "update_available": self.update_available, "pinned": pinned}


@dataclass
class AdoptedServer:
    server_id: str
    loader: str | None = None
    game_version: str | None = None
    scanned_at: str | None = None
    updates_checked_at: str | None = None
    mods: list[InstalledMod] = field(default_factory=list)

    @property
    def adopted(self) -> bool:
        return self.scanned_at is not None

    def to_dict(self) -> dict:
        mods = sorted(self.mods, key=lambda mod: mod.file_name.casefold())
        return {
            "server_id": self.server_id,
            "adopted": self.adopted,
            "loader": self.loader,
            "game_version": self.game_version,
            "scanned_at": self.scanned_at,
            "updates_checked_at": self.updates_checked_at,
            "mods": [mod.to_dict() for mod in mods],
            "counts": {
                "identified": sum(mod.state == "identified" for mod in mods),
                "unknown": sum(mod.state == "unknown" for mod in mods),
                "updates": sum(mod.update_available and not mod.pinned for mod in mods),
                "pinned": sum(mod.pinned for mod in mods),
            },
        }
