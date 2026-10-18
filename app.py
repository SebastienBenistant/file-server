from __future__ import annotations

import hmac
import json
import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Mapping

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


logger = logging.getLogger("file_server")
BASE_DIR = Path(__file__).resolve().parent
ROOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class Settings:
    roots: Mapping[str, Path]
    token: str | None = None
    max_upload_bytes: int = 100 * 1024 * 1024


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if environ is None else environ
    roots_value = values.get("FILE_SERVER_ROOTS", "{}")
    try:
        configured_roots = json.loads(roots_value)
    except json.JSONDecodeError as exc:
        raise ValueError("FILE_SERVER_ROOTS must contain a JSON object") from exc
    if not isinstance(configured_roots, dict):
        raise ValueError("FILE_SERVER_ROOTS must contain a JSON object")

    roots: dict[str, Path] = {}
    for root_id, root_path in configured_roots.items():
        if (
            not isinstance(root_id, str)
            or ROOT_ID_PATTERN.fullmatch(root_id) is None
            or not isinstance(root_path, str)
            or not root_path
        ):
            raise ValueError(
                "FILE_SERVER_ROOTS keys must be simple IDs and values must be paths"
            )
        roots[root_id] = Path(root_path).expanduser().resolve()

    try:
        max_upload_bytes = int(values.get("FILE_SERVER_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    except ValueError as exc:
        raise ValueError("FILE_SERVER_MAX_UPLOAD_BYTES must be a positive integer") from exc
    if max_upload_bytes < 1:
        raise ValueError("FILE_SERVER_MAX_UPLOAD_BYTES must be a positive integer")

    token = values.get("FILE_SERVER_TOKEN") or None
    return Settings(roots=roots, token=token, max_upload_bytes=max_upload_bytes)


class CreateDirectoryRequest(BaseModel):
    path: str = ""
    name: str


def _is_within_root(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate))))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _validate_relative_path(relative_path: str) -> tuple[str, ...]:
    if (
        not isinstance(relative_path, str)
        or "\\" in relative_path
        or relative_path.startswith("/")
        or PureWindowsPath(relative_path).is_absolute()
        or PureWindowsPath(relative_path).drive
    ):
        raise HTTPException(status_code=400, detail="Path must be relative to its root")
    if not relative_path:
        return ()
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise HTTPException(status_code=400, detail="Invalid relative path")
    return parts


def _validate_filename(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or PureWindowsPath(name).is_absolute()
        or PureWindowsPath(name).drive
        or any(ord(character) < 32 for character in name)
        or name.startswith(".file-server-upload-")
    ):
        raise HTTPException(status_code=400, detail="Invalid file or directory name")


def _raise_filesystem_error(error: OSError) -> None:
    if isinstance(error, PermissionError):
        raise HTTPException(status_code=403, detail="Permission denied") from error
    if isinstance(error, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Path not found") from error
    if isinstance(error, NotADirectoryError):
        raise HTTPException(status_code=400, detail="A path component is not a directory") from error
    logger.exception("Filesystem operation failed")
    raise HTTPException(status_code=500, detail="Filesystem operation failed") from error


def _resolve_path(root: Path, relative_path: str, *, strict: bool = True) -> Path:
    parts = _validate_relative_path(relative_path)
    try:
        resolved = root.joinpath(*parts).resolve(strict=strict)
    except (OSError, RuntimeError) as error:
        if isinstance(error, FileNotFoundError):
            raise HTTPException(status_code=404, detail="Path not found") from error
        if isinstance(error, PermissionError):
            raise HTTPException(status_code=403, detail="Permission denied") from error
        raise HTTPException(status_code=400, detail="Invalid filesystem path") from error
    if not _is_within_root(root, resolved):
        raise HTTPException(status_code=403, detail="Path is outside the configured root")
    return resolved


def _require_directory(root: Path, relative_path: str) -> Path:
    if not root.is_dir():
        raise HTTPException(status_code=503, detail="Configured root is unavailable")
    directory = _resolve_path(root, relative_path)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    return directory


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings or load_settings()
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app = FastAPI(title="File Server")
    app.state.settings = configuration

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if configuration.token is None:
            return
        expected = f"Bearer {configuration.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def get_root(root_id: str) -> Path:
        root = configuration.roots.get(root_id)
        if root is None:
            raise HTTPException(status_code=404, detail="Configured root not found")
        if not root.is_dir():
            raise HTTPException(status_code=503, detail="Configured root is unavailable")
        return root

    def list_entries(root: Path, relative_path: str) -> list[dict[str, object]]:
        directory = _require_directory(root, relative_path)
        entries: list[dict[str, object]] = []
        try:
            for entry in directory.iterdir():
                if entry.name.startswith(".file-server-upload-"):
                    continue
                try:
                    resolved = entry.resolve(strict=True)
                    if not _is_within_root(root, resolved):
                        continue
                    metadata = resolved.stat()
                except (FileNotFoundError, RuntimeError):
                    continue
                except PermissionError as error:
                    _raise_filesystem_error(error)
                if stat.S_ISDIR(metadata.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(metadata.st_mode):
                    kind = "file"
                else:
                    continue
                item: dict[str, object] = {
                    "name": entry.name,
                    "type": kind,
                    "modified": metadata.st_mtime,
                }
                if kind == "file":
                    item["size"] = metadata.st_size
                entries.append(item)
        except OSError as error:
            _raise_filesystem_error(error)
        entries.sort(key=lambda item: (item["type"] != "directory", str(item["name"]).casefold()))
        return entries

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, bool]:
        return {"authentication_required": configuration.token is not None}

    @app.get("/api/roots", dependencies=[Depends(authorize)])
    async def roots() -> dict[str, list[dict[str, str]]]:
        return {
            "roots": [
                {"id": root_id, "name": root_id}
                for root_id in configuration.roots
            ]
        }

    @app.get("/api/roots/{root_id}/entries", dependencies=[Depends(authorize)])
    async def entries(
        root_id: str,
        path: str = Query(default=""),
    ) -> dict[str, object]:
        root = get_root(root_id)
        return {"root": root_id, "path": path, "entries": list_entries(root, path)}

    @app.get("/ui/entries", response_class=HTMLResponse, dependencies=[Depends(authorize)])
    async def entries_html(
        request: Request,
        root_id: str,
        path: str = Query(default=""),
    ) -> HTMLResponse:
        root = get_root(root_id)
        entry_list = list_entries(root, path)
        components = _validate_relative_path(path)
        breadcrumbs = [{"label": root_id, "path": ""}]
        for index, component in enumerate(components):
            breadcrumbs.append(
                {"label": component, "path": "/".join(components[: index + 1])}
            )
        return templates.TemplateResponse(
            request=request,
            name="entries.html",
            context={
                "root_id": root_id,
                "path": path,
                "entries": entry_list,
                "breadcrumbs": breadcrumbs,
            },
        )

    @app.get("/api/roots/{root_id}/download", dependencies=[Depends(authorize)])
    async def download(root_id: str, path: str = Query(...)) -> FileResponse:
        root = get_root(root_id)
        requested_parts = _validate_relative_path(path)
        if not requested_parts:
            raise HTTPException(status_code=400, detail="Path must identify a file")
        file_path = _resolve_path(root, path)
        try:
            metadata = file_path.stat()
        except OSError as error:
            _raise_filesystem_error(error)
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=400, detail="Path is not a regular file")
        return FileResponse(file_path, filename=requested_parts[-1])

    @app.post(
        "/api/roots/{root_id}/directories",
        status_code=201,
        dependencies=[Depends(authorize)],
    )
    async def create_directory(root_id: str, body: CreateDirectoryRequest) -> dict[str, str]:
        root = get_root(root_id)
        _validate_filename(body.name)
        parent = _require_directory(root, body.path)
        target = parent / body.name
        try:
            target.mkdir()
        except FileExistsError as error:
            raise HTTPException(status_code=409, detail="A file or directory with that name already exists") from error
        except OSError as error:
            _raise_filesystem_error(error)
        return {"name": body.name, "path": "/".join((*_validate_relative_path(body.path), body.name))}

    @app.post(
        "/api/roots/{root_id}/uploads",
        status_code=201,
        dependencies=[Depends(authorize)],
    )
    async def upload_files(
        root_id: str,
        path: str = Query(default=""),
        files: list[UploadFile] = File(...),
    ) -> dict[str, list[str]]:
        root = get_root(root_id)
        destination = _require_directory(root, path)
        if not files:
            raise HTTPException(status_code=400, detail="At least one file is required")

        names = [upload.filename or "" for upload in files]
        for name in names:
            _validate_filename(name)
        if len(set(names)) != len(names):
            raise HTTPException(status_code=409, detail="The upload contains duplicate filenames")
        for name in names:
            if os.path.lexists(destination / name):
                raise HTTPException(status_code=409, detail=f"A file named '{name}' already exists")

        staged: list[tuple[Path, Path, str]] = []
        created: list[Path] = []
        try:
            for upload, name in zip(files, names):
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".file-server-upload-",
                    dir=destination,
                )
                temporary_path = Path(temporary_name)
                size = 0
                try:
                    with os.fdopen(descriptor, "wb") as temporary_file:
                        while chunk := await upload.read(CHUNK_SIZE):
                            size += len(chunk)
                            if size > configuration.max_upload_bytes:
                                raise HTTPException(
                                    status_code=413,
                                    detail=f"'{name}' exceeds the per-file upload limit",
                                )
                            temporary_file.write(chunk)
                except BaseException:
                    if temporary_path.exists():
                        temporary_path.unlink()
                    raise
                staged.append((temporary_path, destination / name, name))

            for temporary_path, target, _ in staged:
                try:
                    os.link(temporary_path, target)
                except FileExistsError as error:
                    raise HTTPException(
                        status_code=409,
                        detail=f"A file named '{target.name}' already exists",
                    ) from error
                created.append(target)
        except HTTPException:
            for target in created:
                target.unlink(missing_ok=True)
            raise
        except OSError as error:
            for target in created:
                target.unlink(missing_ok=True)
            _raise_filesystem_error(error)
        finally:
            for temporary_path, _, _ in staged:
                temporary_path.unlink(missing_ok=True)
            for upload in files:
                await upload.close()

        return {"uploaded": names}

    return app


app = create_app()
