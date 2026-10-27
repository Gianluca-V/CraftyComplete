import json
import re
import time
from typing import Any

import httpx

from crafty_complete.catalog.models import (
    SERVER_LOADERS,
    CatalogError,
    CatalogFilters,
    CatalogProject,
    ProjectType,
    SearchPage,
    SearchQuery,
)

TAG_CACHE_SECONDS = 3600
# Release game versions such as 1.20.1 or 26.3; excludes snapshots and pre-releases.
RELEASE_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")


def build_facets(query: SearchQuery) -> list[list[str]]:
    """Modrinth facets: inner lists are OR-ed, outer list is AND-ed."""
    facets = [[f"project_type:{query.project_type}"]]
    if query.loaders:
        facets.append([f"categories:{loader}" for loader in query.loaders])
    if query.game_version:
        facets.append([f"versions:{query.game_version}"])
    facets.extend([f"categories:{category}"] for category in query.categories)
    if query.server_compatible:
        facets.append(["server_side!=unsupported"])
    return facets


def _latest_release(game_versions: list[str]) -> str | None:
    releases = [v for v in game_versions if RELEASE_VERSION.match(v)]
    return releases[-1] if releases else None


def _to_project(hit: dict[str, Any]) -> CatalogProject:
    # Modpacks keep their loader in `categories` but not in `display_categories`.
    loaders = [c for c in hit.get("categories") or [] if c in SERVER_LOADERS]
    categories = hit.get("display_categories") or hit.get("categories") or []
    project_type = hit.get("project_type", "mod")
    return CatalogProject(
        id=hit["project_id"],
        slug=hit["slug"],
        title=hit["title"],
        description=hit.get("description", ""),
        author=hit.get("author", ""),
        icon_url=hit.get("icon_url") or None,
        project_type=project_type,
        downloads=hit.get("downloads", 0),
        follows=hit.get("follows", 0),
        loaders=loaders,
        categories=[c for c in categories if c not in loaders],
        latest_game_version=_latest_release(hit.get("versions") or []),
        client_side=hit.get("client_side", "unknown"),
        server_side=hit.get("server_side", "unknown"),
        updated_at=hit.get("date_modified"),
        url=f"https://modrinth.com/{project_type}/{hit['slug']}",
    )


class ModrinthProvider:
    """Catalog provider backed by the public Modrinth API v2."""

    def __init__(self, http: httpx.AsyncClient):
        self._http = http
        self._tags: dict[str, Any] = {}
        self._tags_fetched_at = 0.0

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise CatalogError(f"Modrinth request failed: {exc}") from exc
        if response.status_code >= 400:
            raise CatalogError(f"Modrinth request failed: HTTP {response.status_code}")
        return response.json()

    async def search(self, query: SearchQuery) -> SearchPage:
        body = await self._get(
            "search",
            params={
                "query": query.text,
                "facets": json.dumps(build_facets(query)),
                "index": query.sort,
                "offset": (query.page - 1) * query.page_size,
                "limit": query.page_size,
            },
        )
        return SearchPage(
            items=[_to_project(hit) for hit in body.get("hits", [])],
            total=body.get("total_hits", 0),
            page=query.page,
            page_size=query.page_size,
        )

    async def _load_tags(self) -> dict[str, Any]:
        if not self._tags or time.monotonic() - self._tags_fetched_at > TAG_CACHE_SECONDS:
            self._tags = {
                "loader": await self._get("tag/loader"),
                "game_version": await self._get("tag/game_version"),
                "category": await self._get("tag/category"),
            }
            self._tags_fetched_at = time.monotonic()
        return self._tags

    async def filters(self, project_type: ProjectType) -> CatalogFilters:
        tags = await self._load_tags()
        return CatalogFilters(
            loaders=sorted(
                tag["name"]
                for tag in tags["loader"]
                if tag["name"] in SERVER_LOADERS
                and project_type in tag.get("supported_project_types", [])
            ),
            # Modrinth lists game versions newest first.
            game_versions=[
                tag["version"]
                for tag in tags["game_version"]
                if tag.get("version_type") == "release"
            ],
            categories=sorted(
                tag["name"]
                for tag in tags["category"]
                if tag.get("project_type") == project_type
                and tag.get("header") == "categories"
            ),
        )
