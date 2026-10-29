"""Small helpers shared by the tools: plant default, result window, display time."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from raqeeb.config import load_plant
from raqeeb.contracts import DISPLAY_TZ, STEP_MINUTES, PlantConfig
from raqeeb.telemetry.store import TelemetryStore

STEP = timedelta(minutes=STEP_MINUTES)
_DISPLAY = ZoneInfo(DISPLAY_TZ)


def get_plant(plant: PlantConfig | None = None) -> PlantConfig:
    return plant or load_plant()


def window(store: TelemetryStore, start: datetime | None, end: datetime | None,
           table: str = "weather_reference") -> tuple[datetime | None, datetime | None]:
    """The [start, end) a tool looked at: open bounds become the stored data's first / last step."""
    if start is not None and end is not None:
        return start, end
    span = store.span(table)
    if span is None:
        return start, end
    return start or span[0], end or span[1] + STEP


def local_time(ts: datetime) -> str:
    """Time for evidence text, in the display time zone (Asia/Riyadh)."""
    return f"{ts.astimezone(_DISPLAY):%H:%M} Riyadh"
