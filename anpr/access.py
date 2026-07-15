"""Whitelist-driven gate control and email alerting.

When enabled, each captured plate is checked against the whitelist:
  * a whitelisted plate pulses the capturing camera's alarm output (gate open);
  * a plate that is not whitelisted triggers an email alert.

Everything here is opt-in and configured from the web UI. Repeated sightings of
the same plate within a debounce window are ignored so a car sitting in front
of the camera does not fire the gate or email repeatedly.
"""

import asyncio
import base64
import logging
import smtplib
from email.message import EmailMessage
from typing import Dict

from .database import Database, normalize_plate
from .dahua.client import DahuaClient, DahuaError
from .models import AccessSettings, Camera, AnprEvent

log = logging.getLogger("anpr.access")

SETTINGS_KEY = "access_settings"


class AccessController:
    def __init__(self, db: Database):
        self.db = db
        self._last_action: Dict[str, float] = {}  # normalised plate -> monotonic

    # ------------------------------------------------------------ settings

    def get_settings(self) -> AccessSettings:
        raw = self.db.get_setting(SETTINGS_KEY)
        return AccessSettings(**raw) if raw else AccessSettings()

    def save_settings(self, settings: AccessSettings) -> None:
        self.db.set_setting(SETTINGS_KEY, settings.model_dump())

    # --------------------------------------------------------------- event

    async def on_event(self, event: AnprEvent, camera: Camera) -> None:
        """Apply whitelist policy to a freshly captured event."""
        settings = self.get_settings()
        if not settings.enabled:
            return
        norm = normalize_plate(event.plate)
        if not norm:
            return  # no readable plate - nothing to decide on

        now = asyncio.get_event_loop().time()
        last = self._last_action.get(norm)
        if last is not None and now - last < settings.debounce_seconds:
            return
        self._last_action[norm] = now
        self._prune(now, settings.debounce_seconds)

        listed = self.db.whitelist_contains(event.plate)
        if listed:
            if settings.gate_enabled:
                asyncio.create_task(self._open_gate(camera, settings, event))
        else:
            if settings.email_enabled:
                asyncio.create_task(self._send_alert(settings, event, camera))

    def _prune(self, now: float, window: int) -> None:
        stale = [p for p, t in self._last_action.items() if now - t > window * 4]
        for plate in stale:
            self._last_action.pop(plate, None)

    # ---------------------------------------------------------------- gate

    async def _open_gate(self, camera: Camera, settings: AccessSettings,
                         event: AnprEvent) -> None:
        client = DahuaClient(camera.host, camera.port, camera.username,
                             camera.password, camera.use_https)
        try:
            await client.trigger(settings.gate_open_path)
            log.info("Gate opened for %s on %s", event.plate, camera.name)
            if settings.gate_close_path and settings.gate_pulse_seconds > 0:
                await asyncio.sleep(settings.gate_pulse_seconds)
                await client.trigger(settings.gate_close_path)
        except DahuaError as exc:
            log.warning("Gate open failed for %s: %s", camera.name, exc)

    async def test_gate(self, camera: Camera, settings: AccessSettings) -> None:
        client = DahuaClient(camera.host, camera.port, camera.username,
                             camera.password, camera.use_https)
        await client.trigger(settings.gate_open_path)
        if settings.gate_close_path and settings.gate_pulse_seconds > 0:
            await asyncio.sleep(settings.gate_pulse_seconds)
            await client.trigger(settings.gate_close_path)

    # --------------------------------------------------------------- email

    async def _send_alert(self, settings: AccessSettings, event: AnprEvent,
                          camera: Camera) -> None:
        try:
            await asyncio.to_thread(self._send_email_sync, settings, event, camera)
            log.info("Alert email sent for unlisted plate %s", event.plate)
        except Exception as exc:
            log.warning("Alert email failed: %s", exc)

    async def send_test_email(self, settings: AccessSettings) -> None:
        sample = AnprEvent(camera_id=0, camera_name="Test", plate="TEST123",
                           received_at="(test)")
        await asyncio.to_thread(self._send_email_sync, settings, sample, None)

    def _send_email_sync(self, settings: AccessSettings, event: AnprEvent,
                         camera) -> None:
        recipients = [a.strip() for a in settings.email_to.split(",") if a.strip()]
        if not settings.smtp_host or not recipients:
            raise ValueError("SMTP host and at least one recipient are required")

        msg = EmailMessage()
        msg["Subject"] = f"ANPR alert: unlisted plate {event.plate or 'Unknown'}"
        msg["From"] = settings.email_from or settings.smtp_user
        msg["To"] = ", ".join(recipients)
        cam_name = camera.name if camera else event.camera_name
        msg.set_content(
            "A plate that is not on the whitelist was captured.\n\n"
            f"Plate:     {event.plate or 'Unknown'}\n"
            f"Camera:    {cam_name}\n"
            f"Time:      {event.received_at}\n"
            f"Country:   {event.country}\n"
            f"Vehicle:   {event.vehicle_type} {event.vehicle_color}\n"
            f"Direction: {event.direction}\n"
        )
        if settings.email_attach_image and event.image_b64:
            try:
                data = base64.b64decode(event.image_b64)
                subtype = "png" if data[:8].startswith(b"\x89PNG") else "jpeg"
                msg.add_attachment(data, maintype="image", subtype=subtype,
                                   filename=f"{event.plate or 'capture'}.{subtype}")
            except Exception:
                pass

        if settings.smtp_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            server.starttls()
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        try:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        finally:
            server.quit()
