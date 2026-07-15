"""Application entry point.

Run with:  python -m anpr            (or: uvicorn anpr.main:app)
Options via environment variables:
    ANPR_DB    - SQLite database path        (default: anpr.db)
    ANPR_HOST  - bind address                (default: 0.0.0.0)
    ANPR_PORT  - HTTP port                   (default: 8080)
    ANPR_DEMO  - set to 1 to enable the demo event generator
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .database import Database
from .dahua.manager import CameraManager
from .reports import ReportScheduler
from .ws import WebSocketHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(os.environ.get("ANPR_DB", "anpr.db"))
    hub = WebSocketHub()
    manager = CameraManager(db, hub)
    reports = ReportScheduler(db)

    app.state.db = db
    app.state.hub = hub
    app.state.manager = manager
    app.state.reports = reports
    app.state.simulator = None

    await manager.start_all()
    reports.start()

    if os.environ.get("ANPR_DEMO") == "1":
        from .simulator import DemoSimulator
        app.state.simulator = DemoSimulator(db, hub)
        app.state.simulator.start()
        logging.getLogger("anpr").info("Demo simulator enabled (ANPR_DEMO=1)")

    yield

    if app.state.simulator:
        await app.state.simulator.stop()
    await reports.stop()
    await manager.stop_all()
    db.close()


app = FastAPI(title="Dahua ANPR Monitor", lifespan=lifespan)
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
