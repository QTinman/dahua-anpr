"""Demo event generator - lets you exercise the UI without a camera.

Enable with the environment variable ``ANPR_DEMO=1``. Generates a plausible
ANPR event every few seconds under a virtual "Demo Camera" (camera_id 0).
"""

import asyncio
import base64
import random
import struct
import zlib
from datetime import datetime, timezone
from typing import Optional

from .database import Database
from .models import AnprEvent
from .ws import WebSocketHub

PLATES = ["ABC123", "XYZ789", "KLM456", "JQR318", "TUV902", "GHF267",
          "BNP544", "WSD671", "PLK098", "RTG335"]
PLATE_COLORS = ["White", "Yellow", "Blue"]
VEHICLE_COLORS = ["White", "Black", "Silver", "Red", "Blue", "Green"]
VEHICLE_TYPES = ["Light", "Motorcycle", "Truck", "Bus", "SUV"]
BRANDS = ["Volvo", "Toyota", "VW", "Ford", "Tesla", "Scania", ""]
DIRECTIONS = ["Approaching", "Leaving"]


def _tiny_png(width: int = 320, height: int = 120,
              rgb: tuple = (40, 44, 52)) -> bytes:
    """Generate a small solid-colour PNG (placeholder plate image)."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class DemoSimulator:
    def __init__(self, db: Database, hub: WebSocketHub):
        self.db = db
        self.hub = hub
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="demo-simulator")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        await self.hub.broadcast({
            "type": "camera_status", "camera_id": 0,
            "camera_name": "Demo Camera", "status": "connected", "detail": "",
        })
        while True:
            await asyncio.sleep(random.uniform(2.0, 6.0))
            event = self._make_event()
            event_id = self.db.add_event(event)
            payload = event.model_dump()
            payload["id"] = event_id
            payload["has_image"] = payload.pop("image_b64", None) is not None
            await self.hub.broadcast({"type": "anpr_event", "event": payload})

    def _make_event(self) -> AnprEvent:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        color = random.choice([(40, 44, 52), (72, 32, 32), (32, 56, 40),
                               (30, 40, 70)])
        return AnprEvent(
            camera_id=0,
            camera_name="Demo Camera",
            event_code="TrafficJunction",
            plate=random.choice(PLATES),
            plate_color=random.choice(PLATE_COLORS),
            country="SE",
            vehicle_type=random.choice(VEHICLE_TYPES),
            vehicle_color=random.choice(VEHICLE_COLORS),
            vehicle_brand=random.choice(BRANDS),
            speed=round(random.uniform(20, 95), 1),
            direction=random.choice(DIRECTIONS),
            lane=random.randint(1, 2),
            event_time=now,
            received_at=now,
            image_b64=base64.b64encode(_tiny_png(rgb=color)).decode("ascii"),
        )
