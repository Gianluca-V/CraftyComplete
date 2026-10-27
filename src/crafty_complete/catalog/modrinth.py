import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from crafty_complete.catalog.models import (
    SERVER_LOADERS,
    CatalogError,
    CatalogNotFound,
    CatalogFilters,
    CatalogProject,
    ProjectType,
    SearchPage,
    SearchQuery,
)

TAG_CACHE_SECONDS = 3600
# Release game versions such as 1.20.1 or 26.3; excludes snapshots and pre-releases.
RELEASE_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")
HASH_BATCH = 200
PROJECT_BATCH = 100
MODRINTH_CDN = "https://cdn.modrinth.com/"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


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
        if response.status_code == 404:
            raise CatalogNotFound("Modrinth request failed: HTTP 404")
        if response.status_code >= 400:
            raise CatalogError(f"Modrinth request failed: HTTP {response.status_code}")
        return response.json()

    async def project(self, ref: str) -> dict[str, Any]:
        """A project by id or slug."""
        return await self._get(f"project/{quote(ref, safe='')}")

    async def version(self, version_id: str) -> dict[str, Any]:
        return await self._get(f"version/{quote(version_id, safe='')}")

    async def compatible_versions(self, project_id: str, loaders: list[str],
                                  game_version: str) -> list[dict[str, Any]]:
        """Versions of a project for these loaders and Minecraft version, newest first."""
        return await self._get(f"project/{quote(project_id, safe='')}/version", params={
            "loaders": json.dumps(loaders), "game_versions": json.dumps([game_version]),
        })

    async def project_versions(self, project_id: str) -> list[dict[str, Any]]:
        """Every version of a project, newest first."""
        return await self._get(f"project/{quote(project_id, safe='')}/version")

    async def download_to_file(self, url: str, destination: Path,
                               max_bytes: int) -> tuple[str, str]:
        """Stream `url` to `destination`; returns its (SHA-1, SHA-512) for the caller to check.

        Redirects are followed (GitHub release assets redirect to a storage host), so
        callers must verify the hashes against a trusted source.
        """
        sha1, sha512, size = hashlib.sha1(), hashlib.sha512(), 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._http.stream("GET", url, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise CatalogError(f"Download failed: HTTP {response.status_code} for {url}")
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise CatalogError(f"{url} is larger than {max_bytes} bytes")
                        sha1.update(chunk)
                        sha512.update(chunk)
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise CatalogError(f"Download failed: {exc}") from exc
        return sha1.hexdigest(), sha512.hexdigest()

    async def download(self, url: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
        """Download a file Modrinth hosts. Only its CDN is trusted."""
        if not url.startswith(MODRINTH_CDN):
            raise CatalogError(f"Refusing to download from outside Modrinth: {url}")
        data = bytearray()
        try:
            async with self._http.stream("GET", url, follow_redirects=False) as response:
                if response.status_code >= 400:
                    raise CatalogError(f"Modrinth download failed: HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise CatalogError(f"{url} is larger than {max_bytes} bytes")
        except httpx.HTTPError as exc:
            raise CatalogError(f"Modrinth download failed: {exc}") from exc
        return bytes(data)

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise CatalogError(f"Modrinth request failed: {exc}") from exc
        if response.status_code >= 400:
            raise CatalogError(f"Modrinth request failed: HTTP {response.status_code}")
        return response.json()

    async def versions_by_sha1(self, hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Modrinth versions whose files match these SHA-1 hashes; unknown hashes are absent."""
        found: dict[str, dict[str, Any]] = {}
        for batch in _chunks(hashes, HASH_BATCH):
            found.update(
                await self._post("version_files", {"hashes": batch, "algorithm": "sha1"})
            )
        return found

    async def latest_versions(self, hashes: list[str], loaders: list[str],
                              game_version: str) -> dict[str, dict[str, Any]]:
        """Newest version compatible with loaders + game version, keyed by the current SHA-1."""
        latest: dict[str, dict[str, Any]] = {}
        for batch in _chunks(hashes, HASH_BATCH):
            latest.update(await self._post("version_files/update", {
                "hashes": batch, "algorithm": "sha1",
                "loaders": loaders, "game_versions": [game_version],
            }))
        return latest

    async def projects(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for batch in _chunks(ids, PROJECT_BATCH):
            for project in await self._get("projects", params={"ids": json.dumps(batch)}):
                found[project["id"]] = project
        return found

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
