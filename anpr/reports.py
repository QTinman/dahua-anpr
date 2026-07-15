"""Scheduled daily CSV reports, replacing the WinForms report generator."""

import asyncio
import csv
import logging
import os
from datetime import date, datetime
from typing import Optional

from .database import Database
from .models import ReportSettings

log = logging.getLogger("anpr.reports")

CSV_COLUMNS = [
    "id", "camera_name", "plate", "plate_color", "country", "vehicle_type",
    "vehicle_color", "vehicle_brand", "speed", "direction", "lane",
    "event_time", "received_at",
]


def write_report(db: Database, directory: str, day: date,
                 plate: str = "", camera_id: Optional[int] = None) -> str:
    """Write a CSV report of a single day's events, returns the file path."""
    day_str = day.isoformat()
    rows = db.iter_events_for_export(
        plate=plate, camera_id=camera_id, date_from=day_str, date_to=day_str
    )
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"anpr-report-{day_str}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Report written: %s (%d rows)", path, len(rows))
    return path


class ReportScheduler:
    """Checks once a minute whether the configured report time has passed."""

    SETTINGS_KEY = "report_settings"

    def __init__(self, db: Database):
        self.db = db
        self._task: Optional[asyncio.Task] = None

    def get_settings(self) -> ReportSettings:
        raw = self.db.get_setting(self.SETTINGS_KEY)
        return ReportSettings(**raw) if raw else ReportSettings()

    def set_settings(self, settings: ReportSettings) -> None:
        self.db.set_setting(self.SETTINGS_KEY, settings.model_dump())

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="report-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                self._maybe_generate()
            except Exception:
                log.exception("Report generation failed")
            await asyncio.sleep(30)

    def _maybe_generate(self) -> None:
        settings = self.get_settings()
        if not settings.enabled:
            return
        now = datetime.now()
        today = now.date().isoformat()
        if now.strftime("%H:%M") < settings.time:
            return
        if self.db.get_setting("last_report_date") == today:
            return
        write_report(self.db, settings.directory, now.date())
        self.db.set_setting("last_report_date", today)
