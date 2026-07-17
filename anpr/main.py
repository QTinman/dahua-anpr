"""Application entry point.

Run with:  python -m anpr            (or: uvicorn anpr.main:app)
Options via environment variables:
    ANPR_DB    - SQLite database path        (default: anpr.db)
    ANPR_HOST  - bind address                (default: 0.0.0.0)
    ANPR_PORT  - HTTP port                   (default: 8080)
    ANPR_DEMO  - set to 1 to enable the demo event generator
    ANPR_NO_AUTH - set to 1 to disable the login entirely (trusted LAN)
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .access import AccessController
from .api import router
from .auth import SESSION_COOKIE, AuthManager
from .database import Database
from .dahua.manager import CameraManager
from .reports import ReportScheduler, RetentionCleaner
from .ws import WebSocketHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs every request (including the normal Digest-auth 401 challenges)
# at INFO, which floods the console. Only surface its warnings/errors.
logging.getLogger("httpx").setLevel(logging.WARNING)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(os.environ.get("ANPR_DB", "anpr.db"))
    counts = db.counts()
    logging.getLogger("anpr").info(
        "Using database %s (%d camera(s), %d event(s))",
        db.path, counts["cameras"], counts["events"],
    )
    hub = WebSocketHub()
    access = AccessController(db, hub)
    manager = CameraManager(db, hub, access)
    reports = ReportScheduler(db)
    retention = RetentionCleaner(db)
    auth = AuthManager(db)
    auth.purge_expired()

    app.state.db = db
    app.state.hub = hub
    app.state.manager = manager
    app.state.reports = reports
    app.state.retention = retention
    app.state.access = access
    app.state.auth = auth
    app.state.simulator = None

    await manager.start_all()
    reports.start()
    retention.start()

    if os.environ.get("ANPR_DEMO") == "1":
        from .simulator import DemoSimulator
        app.state.simulator = DemoSimulator(db, hub)
        app.state.simulator.start()
        logging.getLogger("anpr").info("Demo simulator enabled (ANPR_DEMO=1)")

    yield

    if app.state.simulator:
        await app.state.simulator.stop()
    await reports.stop()
    await retention.stop()
    await manager.stop_all()
    db.close()


app = FastAPI(title="Dahua ANPR Monitor", lifespan=lifespan)

# Endpoints reachable without a session (the login/setup flow itself).
PUBLIC_PATHS = {
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}


@app.middleware("http")
async def require_auth(request, call_next):
    """Gate the REST API behind a session cookie once a user exists.

    The HTML shell and static assets stay public so the browser can load the
    login screen; every /api/* call other than the auth flow needs a valid
    session. Auth is skipped entirely when disabled (env or setup-skip).
    """
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        auth = request.app.state.auth
        if auth.auth_required() and not auth.validate(
                request.cookies.get(SESSION_COOKIE)):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return await call_next(request)


app.include_router(router)


@app.get("/", include_in_schema=False)
async def index():
    # never cache the shell so new asset versions are always picked up
    return FileResponse(
        os.path.join(WEB_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def run() -> None:
    import uvicorn

    uvicorn.run(
        "anpr.main:app",
        host=os.environ.get("ANPR_HOST", "0.0.0.0"),
        port=int(os.environ.get("ANPR_PORT", "8080")),
    )


if __name__ == "__main__":
    run()
