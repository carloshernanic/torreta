"""API HTTP local do Torreta.

Só escuta em 127.0.0.1 e exige um token gerado a cada execução, então nenhuma
página aberta no navegador consegue controlar o cliente (diferente do esquema
de credenciais fixas em users.conf do uTorrent Web).
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, integration
from .engine import Engine, EngineError

STATIC = Path(__file__).parent / "static"


class AddBody(BaseModel):
    uri: str
    sequential: bool | None = None


class SequentialBody(BaseModel):
    on: bool


class PrioritiesBody(BaseModel):
    priorities: dict[int, int]


class SettingsBody(BaseModel):
    download_dir: str | None = None
    listen_port: int | None = None
    download_limit_kib: int | None = None
    upload_limit_kib: int | None = None
    sequential_default: bool | None = None
    extra_trackers: bool | None = None
    tracker_list: list[str] | str | None = None
    encryption: str | None = None
    seed_ratio_limit: float | None = None
    slow_detect: bool | None = None
    slow_speed_kib: int | None = None
    slow_interval_s: int | None = None
    watch_dir: str | None = None
    prevent_sleep: bool | None = None


def create_app(
    engine: Engine,
    token: str,
    pick_folder: Callable[[], str | None] | None = None,
    focus: Callable[[], None] | None = None,
) -> FastAPI:
    app = FastAPI(title="Torreta", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pick_folder = pick_folder
    app.state.focus = focus

    def auth(request: Request) -> None:
        got = request.headers.get("x-torreta-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(got, token):
            raise HTTPException(401, "token inválido")

    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @app.exception_handler(EngineError)
    async def _engine_error(_: Request, exc: EngineError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @api.get("/state")
    def state() -> dict[str, Any]:
        torrents = engine.snapshot()
        return {
            "version": __version__,
            "torrents": torrents,
            "totals": {
                "down_rate": sum(t["down_rate"] for t in torrents),
                "up_rate": sum(t["up_rate"] for t in torrents),
            },
            "settings": engine.cfg.to_dict(),
            "listen_port_effective": engine.listen_port,
            "can_pick_folder": app.state.pick_folder is not None,
            "can_register": sys.platform == "win32",
            "registered": integration.is_registered(),
        }

    @api.post("/torrents")
    def add(body: AddBody) -> dict[str, str]:
        return {"id": engine.add_uri(body.uri, body.sequential)}

    @api.post("/torrents/file")
    async def add_file(file: UploadFile = File(...), sequential: str | None = Form(None)) -> dict[str, str]:
        data = await file.read()
        seq = None if sequential in (None, "", "null") else sequential == "true"
        return {"id": engine.add_torrent_bytes(data, seq)}

    @api.post("/torrents/{tid}/pause")
    def pause(tid: str) -> dict:
        engine.pause(tid)
        return {"ok": True}

    @api.post("/torrents/{tid}/resume")
    def resume(tid: str) -> dict:
        engine.resume(tid)
        return {"ok": True}

    @api.post("/torrents/{tid}/recheck")
    def recheck(tid: str) -> dict:
        engine.recheck(tid)
        return {"ok": True}

    @api.post("/torrents/{tid}/reannounce")
    def reannounce(tid: str) -> dict:
        engine.reannounce(tid)
        return {"ok": True}

    @api.post("/torrents/{tid}/open")
    def open_folder(tid: str) -> dict:
        engine.open_folder(tid)
        return {"ok": True}

    @api.post("/torrents/{tid}/sequential")
    def sequential(tid: str, body: SequentialBody) -> dict:
        engine.set_sequential(tid, body.on)
        return {"ok": True}

    @api.delete("/torrents/{tid}")
    def remove(tid: str, delete_files: bool = False) -> dict:
        engine.remove(tid, delete_files)
        return {"ok": True}

    @api.get("/torrents/{tid}/files")
    def files(tid: str) -> dict:
        return {"files": engine.files(tid)}

    @api.put("/torrents/{tid}/files")
    def set_files(tid: str, body: PrioritiesBody) -> dict:
        engine.set_file_priorities(tid, body.priorities)
        return {"ok": True}

    @api.put("/settings")
    def settings(body: SettingsBody) -> dict:
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            engine.cfg.update(**changes)
        except ValueError as e:
            raise EngineError(str(e)) from e
        engine.cfg.save()
        engine.apply_config()
        if changes.get("extra_trackers") or "tracker_list" in changes:
            engine.apply_extra_trackers_to_all()
        return engine.cfg.to_dict()

    @api.post("/pick-folder")
    def pick_folder_route() -> dict:
        if app.state.pick_folder is None:
            raise HTTPException(404, "sem janela nativa")
        return {"path": app.state.pick_folder()}

    @api.post("/focus")
    def focus_route() -> dict:
        if app.state.focus is not None:
            app.state.focus()
        return {"ok": True}

    @api.post("/register")
    def register() -> dict:
        try:
            return {"message": integration.register_windows(), "registered": integration.is_registered()}
        except (RuntimeError, OSError) as e:
            raise EngineError(str(e)) from e

    @api.post("/unregister")
    def unregister() -> dict:
        try:
            return {"message": integration.unregister_windows(), "registered": integration.is_registered()}
        except (RuntimeError, OSError) as e:
            raise EngineError(str(e)) from e

    app.include_router(api)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
