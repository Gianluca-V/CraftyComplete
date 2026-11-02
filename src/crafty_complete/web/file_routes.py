from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.crafty.client import CraftyError
from crafty_complete.files.editor import MAX_PATH_LENGTH, MAX_TEXT_BYTES, FileEditor, FileError
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session
from crafty_complete.web.server_routes import ServerId, require_not_busy

router = APIRouter(prefix="/api/servers")


class SaveRequest(BaseModel):
    path: str = Field(max_length=MAX_PATH_LENGTH)
    # A loose bound in characters; the editor checks the encoded size (413).
    content: str = Field(max_length=4 * MAX_TEXT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _files(request: Request) -> FileEditor:
    require_crafty(request)
    return request.app.state.files


async def _run(request: Request, session: Session, operation) -> dict:
    try:
        return await operation(_files(request))
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc


@router.get("/{server_id}/files")
async def list_files(
    server_id: ServerId,
    request: Request,
    path: str = "",
    session: Session = Depends(require_session),
) -> dict:
    """Folders and files in one folder of the server ("" is the server folder)."""
    return await _run(request, session, lambda files: files.list(
        session.crafty_token, server_id, path))


@router.get("/{server_id}/files/content")
async def read_file(
    server_id: ServerId,
    path: str,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda files: files.read(
        session.crafty_token, server_id, path))


@router.put("/{server_id}/files/content", dependencies=[Depends(require_not_busy)])
async def save_file(
    server_id: ServerId,
    body: SaveRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Replace a text file; `sha256` is the hash returned when it was opened."""
    return await _run(request, session, lambda files: files.write(
        session.crafty_token, server_id, body.path, body.content, body.sha256))
