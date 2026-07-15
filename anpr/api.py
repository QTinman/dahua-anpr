"""REST + WebSocket API."""

import base64
import csv
import io
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from .dahua.client import DahuaError, probe
from .models import (
    CameraCreate,
    CameraPublic,
    CameraUpdate,
    ReportSettings,
    TestConnectionRequest,
)
from .reports import write_report

router = APIRouter()


def _state(request: Request):
    return request.app.state


@router.get("/api/diagnostics")
async def diagnostics(request: Request):
    """Report which database file is in use and how much it holds.

    Handy for spotting the 'server started in a different directory and made a
    fresh empty anpr.db' situation, where the live feed keeps working but
    cameras and history look empty.
    """
    state = _state(request)
    counts = state.db.counts()
    return {
        "database_path": state.db.path,
        "cameras": counts["cameras"],
        "events": counts["events"],
        "demo_mode": state.simulator is not None,
    }


# ------------------------------------------------------------------ cameras

@router.get("/api/cameras")
async def list_cameras(request: Request):
    state = _state(request)
    result = []
    for cam in state.db.list_cameras():
        status, detail = state.manager.status_of(cam.id, cam.enabled)
        result.append(CameraPublic.from_camera(cam, status, detail))
    return result


@router.post("/api/cameras", status_code=201)
async def add_camera(request: Request, body: CameraCreate):
    state = _state(request)
    camera = state.db.add_camera(body)
    if camera.enabled:
        state.manager.start_camera(camera)
    status, detail = state.manager.status_of(camera.id, camera.enabled)
    return CameraPublic.from_camera(camera, status, detail)


@router.put("/api/cameras/{camera_id}")
async def update_camera(request: Request, camera_id: int, body: CameraUpdate):
    state = _state(request)
    if state.db.get_camera(camera_id) is None:
        raise HTTPException(404, "Camera not found")
    fields = body.model_dump(exclude_unset=True)
    # An empty password field in the form means "keep the current one".
    if fields.get("password") == "":
        fields.pop("password")
    camera = state.db.update_camera(camera_id, fields)
    await state.manager.restart_camera(camera)
    status, detail = state.manager.status_of(camera.id, camera.enabled)
    return CameraPublic.from_camera(camera, status, detail)


@router.delete("/api/cameras/{camera_id}", status_code=204)
async def delete_camera(request: Request, camera_id: int):
    state = _state(request)
    await state.manager.stop_camera(camera_id)
    if not state.db.delete_camera(camera_id):
        raise HTTPException(404, "Camera not found")


@router.api_route("/api/cameras/{camera_id}/diagnose-stream",
                  methods=["GET", "POST"])
async def diagnose_stream(request: Request, camera_id: int):
    """Report how the camera delivers events and plate pictures.

    Attaches briefly to the event and ITC snapshot streams and returns counts
    of events / image parts and a sample event, to determine the correct
    image source for a given firmware.
    """
    state = _state(request)
    camera = state.db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    from .dahua.client import DahuaClient

    client = DahuaClient(camera.host, camera.port, camera.username,
                         camera.password, camera.use_https)
    try:
        report = await client.diagnose_stream(camera.event_codes)
    except DahuaError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "report": report}


@router.post("/api/cameras/{camera_id}/sync")
async def sync_history(request: Request, camera_id: int):
    """Import the camera's stored ANPR history into the local database."""
    state = _state(request)
    camera = state.db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    try:
        summary = await state.manager.sync_history(camera)
    except DahuaError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **summary}


@router.post("/api/cameras/test")
async def test_connection(request: Request, body: TestConnectionRequest):
    state = _state(request)
    password = body.password
    if not password and body.camera_id is not None:
        existing = state.db.get_camera(body.camera_id)
        if existing:
            password = existing.password
    try:
        info = await probe(body.host, body.port, body.username, password,
                           body.use_https)
    except DahuaError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "device": info}


# ------------------------------------------------------------------- events

@router.get("/api/events")
async def search_events(
    request: Request,
    plate: str = "",
    camera_id: Optional[int] = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
):
    state = _state(request)
    limit = max(1, min(limit, 500))
    return state.db.search_events(
        plate=plate.strip(), camera_id=camera_id,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=max(0, offset),
    )


@router.get("/api/events/{event_id}/image")
async def event_image(request: Request, event_id: int):
    state = _state(request)
    image_b64 = state.db.get_event_image(event_id)
    if not image_b64:
        raise HTTPException(404, "No image for this event")
    data = base64.b64decode(image_b64)
    media_type = "image/png" if data[:8].startswith(b"\x89PNG") else "image/jpeg"
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "max-age=86400"})


@router.get("/api/events/export.csv")
async def export_csv(
    request: Request,
    plate: str = "",
    camera_id: Optional[int] = None,
    date_from: str = "",
    date_to: str = "",
):
    state = _state(request)
    rows = state.db.iter_events_for_export(
        plate=plate.strip(), camera_id=camera_id,
        date_from=date_from, date_to=date_to,
    )

    def generate():
        buffer = io.StringIO()
        writer = None
        columns = ["id", "camera_name", "plate", "plate_color", "country",
                   "vehicle_type", "vehicle_color", "vehicle_brand", "speed",
                   "direction", "lane", "event_time", "received_at"]
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        yield buffer.getvalue()
        for row in rows:
            buffer.seek(0)
            buffer.truncate()
            writer.writerow(row)
            yield buffer.getvalue()

    filename = f"anpr-export-{date.today().isoformat()}.csv"
    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ reports

@router.get("/api/settings/report")
async def get_report_settings(request: Request):
    return _state(request).reports.get_settings()


@router.put("/api/settings/report")
async def set_report_settings(request: Request, body: ReportSettings):
    _state(request).reports.set_settings(body)
    return body


@router.post("/api/reports/run")
async def run_report_now(request: Request, day: str = ""):
    """Generate a report immediately for the given day (default: today)."""
    state = _state(request)
    settings = state.reports.get_settings()
    try:
        target = date.fromisoformat(day) if day else date.today()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    try:
        path = write_report(state.db, settings.directory, target)
    except OSError as exc:
        raise HTTPException(500, f"Could not write report: {exc}")
    return {"ok": True, "path": path}


# ---------------------------------------------------------------- websocket

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    state = ws.app.state
    hub = state.hub
    await hub.connect(ws)
    try:
        # Send the current camera statuses so badges are right immediately.
        statuses = state.manager.status_snapshot()
        if state.simulator is not None:
            statuses.append({
                "type": "camera_status", "camera_id": 0,
                "camera_name": "Demo Camera", "status": "connected",
                "detail": "",
            })
        for status in statuses:
            await ws.send_json(status)
        while True:
            # We don't expect messages from the browser; this keeps the
            # connection open and detects disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)
