"""REST + WebSocket API."""

import base64
import csv
import io
from datetime import date as _date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from .dahua.client import DahuaError, probe
from .models import (
    AccessSettings,
    AccessSettingsPublic,
    CameraCreate,
    CameraPublic,
    CameraUpdate,
    ReportSettings,
    RetentionSettings,
    TestConnectionRequest,
    WhitelistCreate,
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


@router.api_route("/api/cameras/{camera_id}/diagnose-rtsp",
                  methods=["GET", "POST"])
async def diagnose_rtsp(request: Request, camera_id: int):
    """Report the RTSP SDP and media tracks, to locate the metadata track."""
    state = _state(request)
    camera = state.db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    from .dahua.rtsp import RtspError, RtspMetadataClient

    client = RtspMetadataClient(camera.host, camera.rtsp_port, camera.username,
                                camera.password, camera.channel)
    try:
        report = await client.describe_all()
    except RtspError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "report": report}


@router.api_route("/api/cameras/{camera_id}/sample-event",
                  methods=["GET", "POST"])
async def sample_event(request: Request, camera_id: int, seconds: int = 60):
    """Watch the HTTP eventManager stream and return the first traffic event's
    full data (to inspect fields such as capture direction)."""
    state = _state(request)
    camera = state.db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    from .dahua.client import DahuaClient

    seconds = max(5, min(seconds, 180))
    client = DahuaClient(camera.host, camera.port, camera.username,
                         camera.password, camera.use_https)
    try:
        return {"ok": True, **await client.sample_event(camera.event_codes, seconds)}
    except DahuaError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.api_route("/api/cameras/{camera_id}/sample-onvif",
                  methods=["GET", "POST"])
async def sample_onvif(request: Request, camera_id: int):
    """Return a few raw ONVIF metadata documents (images redacted)."""
    state = _state(request)
    camera = state.db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    from .dahua.rtsp import RtspError, RtspMetadataClient

    client = RtspMetadataClient(camera.host, camera.rtsp_port, camera.username,
                                camera.password, camera.channel)
    try:
        return {"ok": True, **await client.sample_metadata()}
    except RtspError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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


@router.get("/api/events/day")
async def events_day(request: Request, date: str):
    """All of a day's events, oldest first, for calendar playback."""
    try:
        target = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    events = _state(request).db.events_for_day(target.isoformat())
    return {"date": target.isoformat(), "total": len(events), "events": events}


@router.get("/api/events/calendar")
async def events_calendar(request: Request, month: str):
    """Per-day event counts for a YYYY-MM month, to annotate the calendar."""
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(400, "month must be YYYY-MM")
    try:
        _date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    return {"month": month, "counts": _state(request).db.event_day_counts(month)}


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

    filename = f"anpr-export-{_date.today().isoformat()}.csv"
    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- whitelist

@router.get("/api/whitelist")
async def list_whitelist(request: Request):
    return _state(request).db.list_whitelist()


@router.post("/api/whitelist", status_code=201)
async def add_whitelist(request: Request, body: WhitelistCreate):
    if not body.plate.strip():
        raise HTTPException(400, "plate is required")
    return _state(request).db.add_whitelist(
        body.plate, body.label, datetime.now().isoformat(timespec="seconds"))


@router.delete("/api/whitelist/{entry_id}", status_code=204)
async def delete_whitelist(request: Request, entry_id: int):
    if not _state(request).db.delete_whitelist(entry_id):
        raise HTTPException(404, "Whitelist entry not found")


# ----------------------------------------------------------- access control

@router.get("/api/settings/access")
async def get_access_settings(request: Request):
    return AccessSettingsPublic.from_settings(_state(request).access.get_settings())


@router.put("/api/settings/access")
async def set_access_settings(request: Request, body: AccessSettings):
    access = _state(request).access
    # An empty SMTP password means "keep the stored one".
    if not body.smtp_password:
        body.smtp_password = access.get_settings().smtp_password
    access.save_settings(body)
    return AccessSettingsPublic.from_settings(body)


@router.get("/api/access/log")
async def access_log(request: Request, limit: int = 200):
    limit = max(1, min(limit, 1000))
    return _state(request).db.list_access_log(limit)


@router.post("/api/access/test-gate")
async def test_gate(request: Request, camera_id: int):
    state = _state(request)
    camera = state.db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(404, "Camera not found")
    try:
        await state.access.test_gate(camera, state.access.get_settings())
    except DahuaError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True}


@router.post("/api/access/test-email")
async def test_email(request: Request, body: AccessSettings):
    access = _state(request).access
    if not body.smtp_password:
        body.smtp_password = access.get_settings().smtp_password
    try:
        await access.send_test_email(body)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True}


# ------------------------------------------------------------------ reports

@router.get("/api/settings/report")
async def get_report_settings(request: Request):
    return _state(request).reports.get_settings()


@router.put("/api/settings/report")
async def set_report_settings(request: Request, body: ReportSettings):
    _state(request).reports.set_settings(body)
    return body


@router.get("/api/settings/retention")
async def get_retention_settings(request: Request):
    return _state(request).retention.get_settings()


@router.put("/api/settings/retention")
async def set_retention_settings(request: Request, body: RetentionSettings):
    _state(request).retention.set_settings(body)
    return body


@router.post("/api/retention/run")
async def run_retention_now(request: Request):
    purged = _state(request).retention.run_now()
    return {"ok": True, "purged": purged}


@router.post("/api/reports/run")
async def run_report_now(request: Request, day: str = ""):
    """Generate a report immediately for the given day (default: today)."""
    state = _state(request)
    settings = state.reports.get_settings()
    try:
        target = _date.fromisoformat(day) if day else _date.today()
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
