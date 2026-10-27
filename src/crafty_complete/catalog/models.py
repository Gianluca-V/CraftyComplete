from dataclasses import dataclass, field
from typing import Literal, Protocol

ProjectType = Literal["mod", "modpack"]
Loader = Literal["fabric", "forge", "neoforge", "quilt"]
SortOrder = Literal["relevance", "downloads", "follows", "newest", "updated"]

SERVER_LOADERS: tuple[str, ...] = ("fabric", "forge", "neoforge", "quilt")


@dataclass(frozen=True)
class SearchQuery:
    text: str = ""
    project_type: ProjectType = "mod"
    loaders: tuple[str, ...] = ()
    game_version: str | None = None
    categories: tuple[str, ...] = ()
    # Hide projects whose server side is "unsupported" (client-only mods).
    server_compatible: bool = True
    sort: SortOrder = "relevance"
    page: int = 1
    page_size: int = 20


@dataclass(frozen=True)
class CatalogProject:
    id: str
    slug: str
    title: str
    description: str
    author: str
    icon_url: str | None
    project_type: str
    downloads: int
    follows: int
    loaders: list[str]
    categories: list[str]
    latest_game_version: str | None
    client_side: str
    server_side: str
    updated_at: str | None
    url: str


@dataclass(frozen=True)
class SearchPage:
    items: list[CatalogProject]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class CatalogFilters:
    loaders: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


class CatalogError(Exception):
    """A catalog provider could not answer."""


class CatalogProvider(Protocol):
    async def search(self, query: SearchQuery) -> SearchPage: ...

    async def filters(self, project_type: ProjectType) -> CatalogFilters: ...
