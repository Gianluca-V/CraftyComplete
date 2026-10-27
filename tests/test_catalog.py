import json

import httpx
import pytest

from crafty_complete.catalog.models import SearchQuery
from crafty_complete.catalog.modrinth import build_facets
from tests.conftest import make_logged_in_client as make_client

HIT = {
    "project_id": "P7dR8mSH",
    "project_type": "mod",
    "slug": "fabric-api",
    "author": "modmuss50",
    "title": "Fabric API",
    "description": "Core hooks.",
    "categories": ["fabric", "library", "quilt"],
    "display_categories": ["fabric", "library", "quilt"],
    "versions": ["1.20.1", "1.21.1"],
    "downloads": 1000,
    "follows": 50,
    "icon_url": "https://cdn.modrinth.com/icon.png",
    "date_modified": "2026-10-01T00:00:00Z",
    "latest_version": "abc123",
    "client_side": "optional",
    "server_side": "required",
}


def modrinth_search_handler(seen: list[httpx.Request], hits=None, total=1):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"hits": hits if hits is not None else [HIT], "offset": 0, "limit": 20,
                  "total_hits": total},
        )

    return handler


def facets_of(request: httpx.Request) -> list[list[str]]:
    return json.loads(request.url.params["facets"])


def test_build_facets_defaults_to_server_compatible_mods():
    assert build_facets(SearchQuery()) == [
        ["project_type:mod"],
        ["server_side!=unsupported"],
    ]


def test_build_facets_combines_filters():
    query = SearchQuery(
        project_type="modpack",
        loaders=("fabric", "quilt"),
        game_version="1.20.1",
        categories=("magic", "adventure"),
        server_compatible=False,
    )
    assert build_facets(query) == [
        ["project_type:modpack"],
        ["categories:fabric", "categories:quilt"],
        ["versions:1.20.1"],
        ["categories:magic"],
        ["categories:adventure"],
    ]


def test_search_forwards_query_and_maps_results(requests_seen):
    handler = modrinth_search_handler(requests_seen, total=45)
    with make_client(handler) as client:
        response = client.get(
            "/api/catalog/search",
            params={"q": "fabric api", "loader": "fabric", "game_version": "1.21.1",
                    "sort": "downloads", "page": 3, "page_size": 10},
        )

    assert response.status_code == 200
    sent = requests_seen[0]
    assert sent.url.path == "/v2/search"
    assert sent.url.params["query"] == "fabric api"
    assert sent.url.params["index"] == "downloads"
    assert sent.url.params["offset"] == "20"
    assert sent.url.params["limit"] == "10"
    assert facets_of(sent) == [
        ["project_type:mod"],
        ["categories:fabric"],
        ["versions:1.21.1"],
        ["server_side!=unsupported"],
    ]
    assert sent.headers["User-Agent"].startswith("craftyComplete/")

    body = response.json()
    assert body["total"] == 45
    assert body["page"] == 3
    assert body["page_size"] == 10
    assert body["items"] == [
        {
            "id": "P7dR8mSH",
            "slug": "fabric-api",
            "title": "Fabric API",
            "description": "Core hooks.",
            "author": "modmuss50",
            "icon_url": "https://cdn.modrinth.com/icon.png",
            "project_type": "mod",
            "downloads": 1000,
            "follows": 50,
            "loaders": ["fabric", "quilt"],
            "categories": ["library"],
            "latest_game_version": "1.21.1",
            "client_side": "optional",
            "server_side": "required",
            "updated_at": "2026-10-01T00:00:00Z",
            "url": "https://modrinth.com/mod/fabric-api",
        }
    ]


def test_search_reads_loaders_hidden_from_display_categories(requests_seen):
    modpack = {**HIT, "project_type": "modpack", "slug": "prominence-2-fabric",
               "categories": ["adventure", "fabric", "quests"],
               "display_categories": ["adventure", "quests"]}
    with make_client(modrinth_search_handler(requests_seen, hits=[modpack])) as client:
        item = client.get("/api/catalog/search", params={"type": "modpack"}).json()["items"][0]

    assert item["loaders"] == ["fabric"]
    assert item["categories"] == ["adventure", "quests"]
    assert item["url"] == "https://modrinth.com/modpack/prominence-2-fabric"


def test_search_reports_latest_release_not_snapshot(requests_seen):
    hit = {**HIT, "versions": ["1.21.1", "26.3", "26.4-snapshot-5", "26w14a"]}
    with make_client(modrinth_search_handler(requests_seen, hits=[hit])) as client:
        item = client.get("/api/catalog/search").json()["items"][0]

    assert item["latest_game_version"] == "26.3"


def test_search_can_include_client_only_projects(requests_seen):
    with make_client(modrinth_search_handler(requests_seen)) as client:
        client.get("/api/catalog/search", params={"server_compatible": "false"})

    assert facets_of(requests_seen[0]) == [["project_type:mod"]]


def test_search_handles_missing_optional_fields(requests_seen):
    sparse = {k: v for k, v in HIT.items() if k not in ("icon_url", "versions", "follows")}
    with make_client(modrinth_search_handler(requests_seen, hits=[sparse])) as client:
        item = client.get("/api/catalog/search").json()["items"][0]

    assert item["icon_url"] is None
    assert item["latest_game_version"] is None
    assert item["follows"] == 0


@pytest.mark.parametrize(
    "params",
    [{"page_size": 0}, {"page_size": 101}, {"page": 0}, {"type": "plugin"},
     {"loader": "bukkit"}, {"sort": "random"}],
)
def test_search_rejects_invalid_parameters(params, requests_seen):
    with make_client(modrinth_search_handler(requests_seen)) as client:
        response = client.get("/api/catalog/search", params=params)

    assert response.status_code == 422
    assert requests_seen == []


def test_search_reports_modrinth_failure_as_bad_gateway():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with make_client(handler) as client:
        response = client.get("/api/catalog/search")

    assert response.status_code == 502
    assert response.json() == {"detail": "Modrinth request failed: HTTP 500"}


def test_search_keeps_the_generic_message_for_modrinth_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with make_client(handler) as client:
        response = client.get("/api/catalog/search")

    assert response.status_code == 502
    assert response.json() == {"detail": "Modrinth request failed: HTTP 404"}


def test_search_reports_unreachable_modrinth_as_bad_gateway():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    with make_client(handler) as client:
        response = client.get("/api/catalog/search")

    assert response.status_code == 502
    assert response.json()["detail"].startswith("Modrinth request failed:")


TAGS = {
    "/v2/tag/loader": [
        {"name": "fabric", "supported_project_types": ["mod", "modpack"]},
        {"name": "quilt", "supported_project_types": ["mod", "modpack"]},
        {"name": "paper", "supported_project_types": ["plugin"]},
        {"name": "forge", "supported_project_types": ["mod", "modpack"]},
    ],
    "/v2/tag/game_version": [
        {"version": "1.21.1", "version_type": "release"},
        {"version": "24w14a", "version_type": "snapshot"},
        {"version": "1.20.1", "version_type": "release"},
    ],
    "/v2/tag/category": [
        {"name": "technology", "project_type": "mod", "header": "categories"},
        {"name": "adventure", "project_type": "mod", "header": "categories"},
        {"name": "adventure", "project_type": "modpack", "header": "categories"},
        {"name": "64x", "project_type": "resourcepack", "header": "resolutions"},
        {"name": "client", "project_type": "mod", "header": "environment"},
    ],
}


def tags_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TAGS[request.url.path])

    return handler


def test_filters_lists_server_loaders_releases_and_categories(requests_seen):
    with make_client(tags_handler(requests_seen)) as client:
        response = client.get("/api/catalog/filters", params={"type": "mod"})

    assert response.status_code == 200
    assert response.json() == {
        "loaders": ["fabric", "forge", "quilt"],
        "game_versions": ["1.21.1", "1.20.1"],
        "categories": ["adventure", "technology"],
    }


def test_filters_are_cached_between_requests(requests_seen):
    with make_client(tags_handler(requests_seen)) as client:
        client.get("/api/catalog/filters", params={"type": "mod"})
        second = client.get("/api/catalog/filters", params={"type": "modpack"})

    assert len(requests_seen) == 3
    assert second.json()["categories"] == ["adventure"]


def test_index_page_renders_search_view():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("page render must not call Modrinth")

    with make_client(handler) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="search-input"' in response.text
    assert client.get("/static/app.js").status_code == 200
