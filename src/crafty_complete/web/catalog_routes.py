from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from crafty_complete.catalog.models import (
    CatalogError,
    CatalogProvider,
    Loader,
    ProjectType,
    SearchQuery,
    SortOrder,
)

router = APIRouter(prefix="/api/catalog")


def _provider(request: Request) -> CatalogProvider:
    return request.app.state.catalog


@router.get("/search")
async def search(
    request: Request,
    q: str = "",
    type: ProjectType = "mod",
    loader: Annotated[list[Loader], Query()] = [],
    game_version: str | None = None,
    category: Annotated[list[str], Query()] = [],
    server_compatible: bool = True,
    sort: SortOrder = "relevance",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    query = SearchQuery(
        text=q,
        project_type=type,
        loaders=tuple(loader),
        game_version=game_version or None,
        categories=tuple(category),
        server_compatible=server_compatible,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    try:
        return asdict(await _provider(request).search(query))
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/filters")
async def filters(request: Request, type: ProjectType = "mod") -> dict:
    try:
        return asdict(await _provider(request).filters(type))
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
