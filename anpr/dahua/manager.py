"""Camera manager: one background task per enabled camera.

Each worker attaches to its camera's event stream, normalises ANPR events,
persists them and pushes them to connected browsers. Connections are
retried forever with exponential backoff, so cameras may be added, powered
off and back on without operator intervention.
"""

import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from ..database import Database
from ..models import AnprEvent, Camera
from ..ws import WebSocketHub
from .client import DahuaClient, DahuaError
from .parser import (
    extract_image_b64,
    is_traffic_code,
    normalize_traffic_event,
    normalize_traffic_record,
)

log = logging.getLogger("anpr.manager")

RETRY_MIN_SECONDS = 5
RETRY_MAX_SECONDS = 60
# How long after a text event a jpeg part is still considered "its" image.
IMAGE_MATCH_WINDOW = 3.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class CameraWorker:
    def __init__(self, camera: Camera, db: Database, hub: WebSocketHub):
        self.camera = camera
        self.db = db
        self.hub = hub
        self.status = "connecting"
        self.status_detail = ""
        self._task: Optional[asyncio.Task] = None
        # Most recent stored event still waiting for its jpeg part.
        self._pending_image_event: Optional[int] = None
        self._pending_image_at: float = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"camera-{self.camera.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _set_status(self, status: str, detail: str = "") -> None:
        if status == self.status and detail == self.status_detail:
            return
        self.status = status
        self.status_detail = detail
        await self.hub.broadcast({
            "type": "camera_status",
            "camera_id": self.camera.id,
            "camera_name": self.camera.name,
            "status": status,
            "detail": detail,
        })

    async def _run(self) -> None:
        cam = self.camera
        client = DahuaClient(cam.host, cam.port, cam.username, cam.password,
                             cam.use_https)
        subscribed = [c.strip() for c in cam.event_codes.split(",") if c.strip()]
        retry = RETRY_MIN_SECONDS
        while True:
            await self._set_status("connecting")
            try:
                async for part in client.stream_events(cam.event_codes):
                    if self.status != "connected":
                        retry = RETRY_MIN_SECONDS
                        await self._set_status("connected")
                    if part.kind == "event" and part.event:
                        await self._handle_event(part.event, subscribed, client)
                    elif part.kind == "image" and part.image:
                        await self._handle_image(part.image)
            except asyncio.CancelledError:
                raise
            except DahuaError as exc:
                await self._set_status("error", str(exc))
            except Exception as exc:  # defensive: never let a worker die
                log.exception("Camera %s worker error", cam.name)
                await self._set_status("error", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(retry)
            retry = min(retry * 2, RETRY_MAX_SECONDS)

    async def _handle_event(self, event: dict, subscribed: list,
                            client: DahuaClient) -> None:
        code = event.get("code", "")
        action = (event.get("action") or "").lower()
        if not is_traffic_code(code, subscribed):
            return
        # TrafficJunction fires Start/Pulse/Stop; the plate payload arrives
        # with Start or Pulse. Ignore Stop to avoid duplicate rows.
        if action == "stop":
            return
        data = event.get("data") or {}
        fields = normalize_traffic_event(code, data)
        # Keep every traffic/ANPR capture (including plate-less ones such as
        # "Unlicensed") and anything that carries a plate. This lets a camera
        # subscribed to "All" also record manual-snapshot captures while still
        # dropping non-ANPR noise (VideoMotion, etc.) that has no plate.
        if not fields["plate"] and not code.startswith("Traffic"):
            return

        anpr = AnprEvent(
            camera_id=self.camera.id,
            camera_name=self.camera.name,
            received_at=_now_iso(),
            **fields,
        )
        # Prefer a picture embedded in the event metadata itself.
        embedded = extract_image_b64(data)
        if embedded:
            anpr.image_b64 = embedded

        event_id = self.db.add_event(anpr)
        has_image = embedded is not None
        if not has_image:
            # No inline picture: wait for a separate jpeg multipart part.
            self._pending_image_event = event_id
            self._pending_image_at = asyncio.get_event_loop().time()

        payload = anpr.model_dump()
        payload["id"] = event_id
        payload["has_image"] = has_image
        payload.pop("image_b64", None)
        await self.hub.broadcast({"type": "anpr_event", "event": payload})

        # Only pull a live snapshot when the event carried no picture at all.
        if not has_image and self.camera.snapshot_on_event:
            asyncio.create_task(self._snapshot_fallback(event_id, client))

    async def _handle_image(self, image: bytes) -> None:
        """A jpeg multipart part: attach it to the most recent event."""
        event_id = self._pending_image_event
        loop_now = asyncio.get_event_loop().time()
        if event_id is None or loop_now - self._pending_image_at > IMAGE_MATCH_WINDOW:
            return
        self._pending_image_event = None
        self._store_image(event_id, image)
        await self.hub.broadcast({
            "type": "event_image", "event_id": event_id,
        })

    async def _snapshot_fallback(self, event_id: int, client: DahuaClient) -> None:
        """If no embedded image arrived shortly after the event, pull one."""
        await asyncio.sleep(IMAGE_MATCH_WINDOW)
        if self.db.get_event_image(event_id):
            return
        image = await client.snapshot(self.camera.channel)
        if image:
            self._store_image(event_id, image)
            await self.hub.broadcast({
                "type": "event_image", "event_id": event_id,
            })

    def _store_image(self, event_id: int, image: bytes) -> None:
        self.db.set_event_image(event_id, base64.b64encode(image).decode("ascii"))


class CameraManager:
    def __init__(self, db: Database, hub: WebSocketHub):
        self.db = db
        self.hub = hub
        self._workers: Dict[int, CameraWorker] = {}

    async def start_all(self) -> None:
        for camera in self.db.list_cameras():
            if camera.enabled:
                self.start_camera(camera)

    def start_camera(self, camera: Camera) -> None:
        worker = CameraWorker(camera, self.db, self.hub)
        self._workers[camera.id] = worker
        worker.start()

    async def stop_camera(self, camera_id: int) -> None:
        worker = self._workers.pop(camera_id, None)
        if worker:
            await worker.stop()

    async def restart_camera(self, camera: Camera) -> None:
        await self.stop_camera(camera.id)
        if camera.enabled:
            self.start_camera(camera)

    async def stop_all(self) -> None:
        for camera_id in list(self._workers):
            await self.stop_camera(camera_id)

    def status_of(self, camera_id: int, enabled: bool) -> tuple:
        worker = self._workers.get(camera_id)
        if worker is None:
            return ("disabled", "") if not enabled else ("stopped", "")
        return worker.status, worker.status_detail

    async def sync_history(self, camera: Camera, max_records: int = 500) -> dict:
        """Import the camera's stored ANPR records that we don't already have.

        Returns a summary dict: how many records the camera returned, how many
        were new (imported) and how many were duplicates already present.
        """
        client = DahuaClient(camera.host, camera.port, camera.username,
                             camera.password, camera.use_https)
        records = await client.find_traffic_records(max_records=max_records)
        imported = 0
        duplicates = 0
        for rec in records:
            fields = normalize_traffic_record(rec)
            if not fields["plate"] and not fields["event_time"]:
                continue
            if self.db.event_exists(camera.id, fields["plate"], fields["event_time"]):
                duplicates += 1
                continue
            anpr = AnprEvent(
                camera_id=camera.id,
                camera_name=camera.name,
                received_at=fields["event_time"] or _now_iso(),
                **fields,
            )
            event_id = self.db.add_event(anpr)
            imported += 1
            payload = anpr.model_dump()
            payload["id"] = event_id
            payload["has_image"] = False
            payload.pop("image_b64", None)
            await self.hub.broadcast({"type": "anpr_event", "event": payload})
        log.info("History sync for %s: %d found, %d imported, %d duplicates",
                 camera.name, len(records), imported, duplicates)
        return {"found": len(records), "imported": imported,
                "duplicates": duplicates}

    def status_snapshot(self) -> list:
        """Current status of every camera, for newly connected browsers."""
        result = []
        for camera in self.db.list_cameras():
            status, detail = self.status_of(camera.id, camera.enabled)
            result.append({
                "type": "camera_status",
                "camera_id": camera.id,
                "camera_name": camera.name,
                "status": status,
                "detail": detail,
            })
        return result
