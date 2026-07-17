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

from ..database import Database, normalize_plate
from ..models import AnprEvent, Camera
from ..ws import WebSocketHub
from .client import DahuaClient, DahuaError
from .onvif import direction_from_boxes
from .parser import (
    extract_direction,
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
# An ONVIF-tracked vehicle spans several metadata frames (plate in one, image
# and attributes in another). Merge frames per object and emit once the object
# has been gone this long, or as soon as its capture image arrives.
ONVIF_FLUSH_SECONDS = 2.5
# How long a plate's direction (from the HTTP event stream) stays valid, and
# the window for the time-based fallback when the plate hasn't been buffered.
DIRECTION_PLATE_TTL = 60.0
DIRECTION_RECENT_WINDOW = 5.0
# Fields merged across an object's frames (first non-empty value wins).
ONVIF_MERGE_FIELDS = (
    "plate", "plate_type", "country", "plate_color", "vehicle_type",
    "vehicle_brand", "vehicle_color", "vehicle_size", "direction", "lane",
    "speed", "utc", "image_b64",
)


def _merge_onvif(dest: dict, obj: dict) -> None:
    """Copy an object's non-empty fields into the accumulated record."""
    for key in ONVIF_MERGE_FIELDS:
        value = obj.get(key)
        if value in (None, ""):
            continue
        if dest.get(key) in (None, ""):
            dest[key] = value


def _flush_onvif(pending: Dict[str, dict], now: float) -> list:
    """Emit records for vehicles that have left (no frame for a while) but
    never got a snapshot image, and drop old entries to bound memory."""
    out = []
    for oid in list(pending):
        entry = pending[oid]
        if now - entry["last"] <= ONVIF_FLUSH_SECONDS:
            continue
        if not entry["emitted"] and entry["data"].get("plate"):
            entry["emitted"] = True
            if not entry["data"].get("direction"):
                inferred = direction_from_boxes(entry.get("boxes", []))
                if inferred:
                    entry["data"]["direction"] = inferred
            out.append(entry["data"])
        if now - entry["last"] > ONVIF_FLUSH_SECONDS + 15:
            pending.pop(oid, None)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class CameraWorker:
    def __init__(self, camera: Camera, db: Database, hub: WebSocketHub,
                 access=None):
        self.camera = camera
        self.db = db
        self.hub = hub
        self.access = access
        self.status = "connecting"
        self.status_detail = ""
        self._task: Optional[asyncio.Task] = None
        # Most recent stored event still waiting for its jpeg part.
        self._pending_image_event: Optional[int] = None
        self._pending_image_at: float = 0.0
        # A jpeg part that arrived before its event (some firmwares send the
        # picture first), waiting to be attached to the next event.
        self._buffered_image: Optional[bytes] = None
        self._buffered_image_at: float = 0.0
        # Capture direction from the HTTP event stream (ONVIF omits it):
        # normalised plate -> (direction, loop time), plus a time-based fallback.
        self._dir_by_plate: Dict[str, tuple] = {}
        self._last_direction: tuple = ("", 0.0)

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
        if cam.use_onvif:
            # ONVIF metadata (RTSP) carries the plate and its own capture image
            # in the same frame - no cross-stream matching needed. ONVIF omits
            # the capture direction, so also consume the HTTP event stream to
            # learn each plate's direction and attach it to the ONVIF capture.
            http = DahuaClient(cam.host, cam.port, cam.username, cam.password,
                               cam.use_https)
            dir_task = asyncio.create_task(self._run_event_directions(http))
            try:
                await self._run_onvif()
            finally:
                dir_task.cancel()
            return
        client = DahuaClient(cam.host, cam.port, cam.username, cam.password,
                             cam.use_https)
        # ITC cameras deliver pictures on a separate stream; consume it
        # concurrently and feed the pictures into the same image matching.
        snap_task = asyncio.create_task(self._run_snapshots(client))
        try:
            await self._run_events(client)
        finally:
            snap_task.cancel()

    async def _run_onvif(self) -> None:
        """Consume the ONVIF metadata stream as the source of events+images."""
        from .onvif import normalize_onvif_object, parse_onvif_metadata
        from .rtsp import RtspError, RtspMetadataClient

        cam = self.camera
        rtsp = RtspMetadataClient(cam.host, cam.rtsp_port, cam.username,
                                  cam.password, cam.channel)
        retry = RETRY_MIN_SECONDS
        # object_id -> {"data": merged fields, "last": t, "emitted": bool}
        pending: Dict[str, dict] = {}
        while True:
            await self._set_status("connecting")
            try:
                async for xml in rtsp.stream_metadata():
                    if self.status != "connected":
                        retry = RETRY_MIN_SECONDS
                        await self._set_status("connected")
                    if not xml.strip():
                        continue  # connected-signal, no document yet
                    now = asyncio.get_event_loop().time()
                    for obj in parse_onvif_metadata(xml):
                        for rec in self._accumulate_onvif(obj, pending, now):
                            await self._store_onvif(rec, normalize_onvif_object)
                    for rec in _flush_onvif(pending, now):
                        await self._store_onvif(rec, normalize_onvif_object)
            except asyncio.CancelledError:
                raise
            except RtspError as exc:
                log.info("Camera %s ONVIF stream dropped: %s", cam.name, exc)
                await self._set_status("error", str(exc))
            except Exception as exc:  # defensive: never let a worker die
                log.exception("Camera %s ONVIF worker error", cam.name)
                await self._set_status("error", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(retry)
            retry = min(retry * 2, RETRY_MAX_SECONDS)

    async def _run_event_directions(self, client: DahuaClient) -> None:
        """Consume the HTTP event stream to learn each plate's capture
        direction (ONVIF does not carry it). Best-effort: failures never affect
        the camera's shown status."""
        cam = self.camera
        retry = RETRY_MIN_SECONDS
        while True:
            try:
                async for part in client.stream_events(cam.event_codes):
                    if part.kind != "event" or not part.event:
                        continue
                    data = part.event.get("data") or {}
                    direction = extract_direction(data)
                    if not direction:
                        continue
                    now = asyncio.get_event_loop().time()
                    self._last_direction = (direction, now)
                    car = data.get("TrafficCar") if isinstance(
                        data.get("TrafficCar"), dict) else {}
                    obj = data.get("Object") if isinstance(
                        data.get("Object"), dict) else {}
                    plate = car.get("PlateNumber") or obj.get("Text") or ""
                    norm = normalize_plate(plate)
                    if norm:
                        self._dir_by_plate[norm] = (direction, now)
                        self._prune_directions(now)
                retry = RETRY_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except DahuaError as exc:
                log.debug("Event-direction stream for %s: %s", cam.name, exc)
            except Exception:
                log.debug("Event-direction error for %s", cam.name, exc_info=True)
            await asyncio.sleep(retry)
            retry = min(retry * 2, RETRY_MAX_SECONDS)

    def _prune_directions(self, now: float) -> None:
        stale = [p for p, (_, t) in self._dir_by_plate.items()
                 if now - t > DIRECTION_PLATE_TTL]
        for plate in stale:
            self._dir_by_plate.pop(plate, None)

    def _resolve_direction(self, current: str, plate: str) -> str:
        # A fixed per-camera direction always wins.
        if self.camera.direction_mode:
            return self.camera.direction_mode
        if current:
            return current
        now = asyncio.get_event_loop().time()
        hit = self._dir_by_plate.get(normalize_plate(plate))
        if hit and now - hit[1] <= DIRECTION_PLATE_TTL:
            return hit[0]
        # Fallback: the most recent direction seen, if it is fresh.
        if self._last_direction[0] and \
                now - self._last_direction[1] <= DIRECTION_RECENT_WINDOW:
            return self._last_direction[0]
        return current

    def _accumulate_onvif(self, obj: dict, pending: Dict[str, dict],
                          now: float) -> list:
        """Merge an object's frame; emit the record once its image arrives."""
        oid = obj.get("object_id", "")
        if not oid and not obj.get("plate") and not obj.get("image_b64"):
            return []  # empty tracking frame
        entry = pending.get(oid)
        if entry is None:
            entry = {"data": {}, "last": now, "emitted": False, "boxes": []}
            pending[oid] = entry
        entry["last"] = now
        # Track the vehicle's bounding boxes to infer direction from movement.
        if obj.get("bbox"):
            entry["boxes"].append(obj["bbox"])
        _merge_onvif(entry["data"], obj)
        # The capture is complete once the snapshot image is present.
        if entry["data"].get("image_b64") and not entry["emitted"]:
            entry["emitted"] = True
            self._finalise_direction(entry)
            return [entry["data"]]
        return []

    @staticmethod
    def _finalise_direction(entry: dict) -> None:
        # Only infer when the camera did not supply a direction itself.
        if not entry["data"].get("direction"):
            inferred = direction_from_boxes(entry.get("boxes", []))
            if inferred:
                entry["data"]["direction"] = inferred

    async def _store_onvif(self, data: dict, normalize) -> None:
        fields = normalize(data)
        # Direction: fixed override, else the value from metadata/movement, else
        # the capture direction learned for this plate from the event stream.
        fields["direction"] = self._resolve_direction(
            fields.get("direction", ""), data.get("plate", ""))
        anpr = AnprEvent(
            camera_id=self.camera.id,
            camera_name=self.camera.name,
            # Use the local receive time (the camera UTC timestamp can be in a
            # different zone); keep the camera time in event_time.
            received_at=_now_iso(),
            image_b64=data.get("image_b64"),
            **fields,
        )
        event_id = self.db.add_event(anpr)
        payload = anpr.model_dump()
        payload["id"] = event_id
        payload["has_image"] = anpr.image_b64 is not None
        payload.pop("image_b64", None)
        await self.hub.broadcast({"type": "anpr_event", "event": payload})
        await self._apply_access(anpr)

    async def _apply_access(self, anpr: AnprEvent) -> None:
        if self.access is not None:
            try:
                await self.access.on_event(anpr, self.camera)
            except Exception:
                log.exception("Access control failed for %s", self.camera.name)

    async def _run_events(self, client: DahuaClient) -> None:
        cam = self.camera
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

    async def _run_snapshots(self, client: DahuaClient) -> None:
        """Consume the ITC picture stream, feeding images into matching.

        Best-effort: failures here never change the camera's shown status
        (that is driven by the event stream); we just retry quietly.
        """
        cam = self.camera
        retry = RETRY_MIN_SECONDS
        while True:
            try:
                async for image in client.stream_snapshots(cam.event_codes,
                                                           cam.channel):
                    await self._handle_image(image)
                retry = RETRY_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except DahuaError as exc:
                log.debug("Snapshot stream unavailable for %s: %s", cam.name, exc)
            except Exception:
                log.debug("Snapshot stream error for %s", cam.name, exc_info=True)
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
        if self.camera.direction_mode:
            fields["direction"] = self.camera.direction_mode
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
        now = asyncio.get_event_loop().time()
        # Picture sources, in order of fidelity:
        #   1. embedded base64 in the event metadata
        #   2. a jpeg part that arrived just before this event
        #   3. (later) a jpeg part that arrives just after this event
        #   4. (last resort) a live snapshot fallback
        image_b64 = extract_image_b64(data)
        if not image_b64 and self._buffered_image is not None \
                and now - self._buffered_image_at <= IMAGE_MATCH_WINDOW:
            image_b64 = base64.b64encode(self._buffered_image).decode("ascii")
            self._buffered_image = None
        if image_b64:
            anpr.image_b64 = image_b64

        event_id = self.db.add_event(anpr)
        has_image = image_b64 is not None
        if not has_image:
            # No inline picture yet: wait for a separate jpeg multipart part.
            self._pending_image_event = event_id
            self._pending_image_at = now

        payload = anpr.model_dump()
        payload["id"] = event_id
        payload["has_image"] = has_image
        payload.pop("image_b64", None)
        await self.hub.broadcast({"type": "anpr_event", "event": payload})
        await self._apply_access(anpr)

        # Only pull a live snapshot when the event carried no picture at all.
        if not has_image and self.camera.snapshot_on_event:
            asyncio.create_task(self._snapshot_fallback(event_id, client))

    async def _handle_image(self, image: bytes) -> None:
        """A jpeg multipart part: attach it to its event.

        If an event is waiting for its picture, attach immediately. Otherwise
        the picture arrived before its event, so buffer it briefly for the
        next event to pick up.
        """
        loop_now = asyncio.get_event_loop().time()
        event_id = self._pending_image_event
        if event_id is not None and loop_now - self._pending_image_at <= IMAGE_MATCH_WINDOW:
            self._pending_image_event = None
            self._store_image(event_id, image)
            await self.hub.broadcast({"type": "event_image", "event_id": event_id})
            return
        # No event waiting - hold the picture for the next event.
        self._buffered_image = image
        self._buffered_image_at = loop_now

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
    def __init__(self, db: Database, hub: WebSocketHub, access=None):
        self.db = db
        self.hub = hub
        self.access = access
        self._workers: Dict[int, CameraWorker] = {}

    async def start_all(self) -> None:
        for camera in self.db.list_cameras():
            if camera.enabled:
                self.start_camera(camera)

    def start_camera(self, camera: Camera) -> None:
        worker = CameraWorker(camera, self.db, self.hub, self.access)
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
