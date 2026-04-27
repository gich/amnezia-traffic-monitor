import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as dbmod
from . import queries as q
from .config import load_config


_BASE = Path(__file__).resolve().parent


def _fmt_bytes(n: int | None) -> str:
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} TB"


def _fmt_handshake(unix_ts: int | None) -> str:
    if not unix_ts:
        return "never"
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="Amnezia Traffic Monitor", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=_BASE / "static"), name="static")

    templates = Jinja2Templates(directory=_BASE / "templates")
    templates.env.filters["bytes"] = _fmt_bytes
    templates.env.filters["handshake"] = _fmt_handshake

    def get_conn():
        conn = dbmod.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "users": q.list_users_with_totals(conn),
                "unassigned": q.list_unassigned_peers_aggregate(conn),
            },
        )

    @app.get("/peers", response_class=HTMLResponse)
    def peers(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return templates.TemplateResponse(
            request,
            "peers.html",
            {"peers": q.list_all_peers_with_totals(conn)},
        )

    @app.get("/user/{user_id}", response_class=HTMLResponse)
    def user_detail(user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        user = q.get_user(conn, user_id)
        if not user:
            raise HTTPException(404, "user not found")
        return templates.TemplateResponse(
            request,
            "user.html",
            {
                "user": user,
                "peers": q.list_peers_for_user(conn, user_id),
            },
        )

    @app.get("/peer/{peer_id}", response_class=HTMLResponse)
    def peer_detail(peer_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        peer = q.get_peer(conn, peer_id)
        if not peer:
            raise HTTPException(404, "peer not found")
        return templates.TemplateResponse(
            request,
            "peer.html",
            {"peer": peer},
        )

    @app.get("/api/peer/{peer_id}/timeseries")
    def api_peer_ts(
        peer_id: int,
        window: str = "24h",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            return q.peer_timeseries(conn, peer_id, window)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/user/{user_id}/timeseries")
    def api_user_ts(
        user_id: int,
        window: str = "24h",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            return q.user_timeseries(conn, user_id, window)
        except ValueError as e:
            raise HTTPException(400, str(e))

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    app = create_app(cfg.db.path)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="info")


if __name__ == "__main__":
    main()
