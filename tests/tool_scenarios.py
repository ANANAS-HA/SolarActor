"""Fixture scenarios for the tool tests: run the Twin, change twin.state directly, store the telemetry.

Lane B's injector is not used here. Day: 15 Jan 2025 (clear, cool, so midday soiling loss is
not hidden by clipping), 07:00-18:00 Riyadh = 04:00-15:00 UTC. Times below are UTC.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from raqeeb.contracts import PlantState
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.twin.controls import flag_sensor
from raqeeb.twin.simulator import Twin

DAY = datetime(2025, 1, 15, tzinfo=timezone.utc)
START_H, END_H = 4, 15
SEED = 7


def at(twin: Twin, hhmm: str) -> datetime:
    """UTC clock time on the twin's start date."""
    hh, mm = (int(p) for p in hhmm.split(":"))
    day = twin.start.replace(hour=0, minute=0)
    return day + timedelta(hours=hh, minutes=mm)


def _set(**changes) -> Callable[[PlantState], None]:
    """State change from dotted paths, e.g. _set(**{"station.sensor_soiling": 0.2})."""

    def change(state: PlantState) -> None:
        for path, value in changes.items():
            *parents, name = path.split(".")
            obj = state
            for p in parents:
                obj = obj.inverters[p] if p.startswith("INV-") else getattr(obj, p)
            setattr(obj, name, value)

    return change


def _soil_all(loss: float) -> Callable[[PlantState], None]:
    def change(state: PlantState) -> None:
        for inv in state.inverters.values():
            inv.soiling_loss = loss

    return change


def healthy(twin: Twin) -> None:
    pass


def sensor_only(twin: Twin) -> None:
    twin.schedule(at(twin, "07:00"), "sensor dust", _set(**{"station.sensor_soiling": 0.20}))


def soiling_only(twin: Twin) -> None:
    twin.schedule(at(twin, "07:00"), "panel dust", _soil_all(0.20))


def trap(twin: Twin) -> None:
    """Dust storm -> panels AND sensor dusty, plus a derate and a comms dropout."""
    twin.schedule(at(twin, "06:00"), "storm", _set(**{"atmosphere.wind_add_ms": 16.0, "atmosphere.irradiance_factor": 0.7}))
    twin.schedule(at(twin, "07:00"), "storm over", _set(**{"atmosphere.wind_add_ms": 0.0, "atmosphere.irradiance_factor": 1.0,
                                                     "station.sensor_soiling": 0.20}))
    twin.schedule(at(twin, "07:00"), "panel dust", _soil_all(0.20))
    twin.schedule(at(twin, "08:30"), "INV-07 fan", _set(**{"INV-07.derate_cap_kw": 1400.0, "INV-07.temp_offset_c": 40.0}))
    twin.schedule(at(twin, "09:15"), "INV-12 comms down", _set(**{"INV-12.comms_up": False}))
    twin.schedule(at(twin, "11:00"), "INV-12 comms back", _set(**{"INV-12.comms_up": True}))


def curtailment(twin: Twin) -> None:
    twin.schedule(at(twin, "07:30"), "grid limit", _set(grid_limit_kw=24000.0))
    twin.schedule(at(twin, "09:30"), "grid limit off", _set(grid_limit_kw=None))


def tracker_stuck(twin: Twin) -> None:
    twin.schedule(at(twin, "05:00"), "INV-03 tracker", _set(**{"INV-03.tracker_override_deg": 0.0}))


def station_dropout(twin: Twin) -> None:
    twin.schedule(at(twin, "07:00"), "WS-01 comms down", _set(**{"station.comms_up": False}))
    twin.schedule(at(twin, "07:30"), "WS-01 comms back", _set(**{"station.comms_up": True}))


def dead_and_silent(twin: Twin) -> None:
    """INV-05 stops producing AND loses comms: the meter shows the loss."""
    twin.schedule(at(twin, "08:00"), "INV-05 down", _set(**{"INV-05.comms_up": False, "INV-05.stopped": True}))


SCENARIOS: dict[str, Callable[[Twin], None]] = {
    "station_dropout": station_dropout,
    "dead_and_silent": dead_and_silent,
    "healthy": healthy,
    "sensor_only": sensor_only,
    "soiling_only": soiling_only,
    "trap": trap,
    "curtailment": curtailment,
    "tracker_stuck": tracker_stuck,
    "sensor_flagged": sensor_only,  # build() flags the sensor with the real control at FLAG_AT
}

FLAG_AT = "09:00"


def build(name: str, seed: int = SEED, day: datetime = DAY) -> tuple[TelemetryStore, Twin]:
    twin = Twin(day + timedelta(hours=START_H), day + timedelta(hours=END_H), seed=seed)
    SCENARIOS[name](twin)
    store = TelemetryStore()
    if name == "sensor_flagged":
        store.write_batch(twin.advance((at(twin, FLAG_AT) - twin.start).total_seconds() / 60))
        flag_sensor(twin, twin.plant.weather_station_id)
    store.write_batch(twin.run_to_end())
    return store, twin
