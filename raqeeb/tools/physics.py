"""Expected (fault-free) output from stored weather. Numbers only; reads only the store.

Expected output is raqeeb.twin.plant.expected_power() on a weather frame built from the store:
- basis "reference": the trusted NASA reference (weather_reference table).
- basis "sensor": the on-site sensor, GHI decomposed with erbs (what a plain PR alarm uses).
- basis "auto": the sensor, except where it is flagged (commands table) or has no row; there
  the reference. This is the expected line the plant's own health check would draw.

Nominal ratings are used for every inverter (with_variation=False): the +/-2% unit variation
is not known to an operator, so peers differ from expected by up to that much.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from raqeeb.contracts import PlantConfig
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.weather_tools import get_plant, reference_weather, sensor_flagged, sensor_weather, wind_series
from raqeeb.twin import plant as twin_physics

Basis = Literal["reference", "sensor", "auto"]

TRACKER_DEVIATION_DEG = 10.0  # measured angle this far from the expected tracking angle ...
TRACKER_MIN_STEPS = 6  # ... for at least this many daylight steps (wind below stow) = not tracking
TRACKER_FLAT_SD_DEG = 1.0  # measured angle sd below this while expected moves = held still


def expected_basis(store: TelemetryStore, index: pd.DatetimeIndex, plant: PlantConfig | None = None) -> pd.Series:
    """Which weather 'auto' uses at each step: 'sensor' or 'reference'."""
    station = store.weather_station(index.min(), index.max() + pd.Timedelta(minutes=5)).index
    use_sensor = index.isin(station) & ~sensor_flagged(store, index, plant).to_numpy()
    return pd.Series(np.where(use_sensor, "sensor", "reference"), index=index, name="basis")


def expected(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
             plant: PlantConfig | None = None, basis: Basis = "reference") -> pd.DataFrame:
    """Expected AC and DC per inverter: columns (inverter_id, quantity), as expected_power()."""
    plant = get_plant(plant)
    ref = reference_weather(store, start, end, plant)
    if basis == "reference":
        return twin_physics.expected_power(ref, plant)
    sensor = twin_physics.expected_power(sensor_weather(store, start, end, plant), plant)
    if basis == "sensor":
        return sensor
    if basis != "auto":
        raise ValueError(f"unknown basis {basis!r}")
    out = twin_physics.expected_power(ref, plant)
    use_sensor = (expected_basis(store, out.index, plant) == "sensor").to_numpy()
    out.loc[use_sensor] = sensor.reindex(out.index[use_sensor]).to_numpy()
    return out


def expected_plant_ac(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None, basis: Basis = "reference") -> pd.Series:
    power = expected(store, start, end, plant, basis)
    return power.xs("ac_kw", axis=1, level="quantity").sum(axis=1).rename("plant_ac_kw")


def expected_inverter(store: TelemetryStore, quantity: str = "ac_kw", start: datetime | None = None,
                      end: datetime | None = None, plant: PlantConfig | None = None,
                      basis: Basis = "reference") -> pd.DataFrame:
    """One column per inverter of expected `quantity` (ac_kw or dc_kw)."""
    return expected(store, start, end, plant, basis).xs(quantity, axis=1, level="quantity")


def expected_tracker_angle(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                           plant: PlantConfig | None = None) -> pd.Series:
    """Normal tracking angle, or the stow angle where stored wind is above the stow limit."""
    plant = get_plant(plant)
    angle = twin_physics.tracking_angle(reference_weather(store, start, end, plant), plant)
    wind = wind_series(store, start, end).reindex(angle.index)
    return angle.where(wind <= plant.tracker.stow_wind_ms, plant.tracker.stow_angle_deg).rename("angle_deg")


def tracker_check(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                  plant: PlantConfig | None = None) -> dict[str, dict[str, Any]]:
    """Per inverter: does the measured tracker angle follow the expected angle?

    Only daylight steps with wind at or below the stow limit count (stow in high wind is normal).
    """
    plant = get_plant(plant)
    exp = expected_tracker_angle(store, start, end, plant)
    daylight = store.weather_reference(start, end)["clearsky_ghi_wm2"].reindex(exp.index) > 0
    calm = wind_series(store, start, end).reindex(exp.index) <= plant.tracker.stow_wind_ms
    usable = daylight & calm
    measured = store.inverter_wide("tracker_angle_deg", start, end).reindex(exp.index)
    out: dict[str, dict[str, Any]] = {}
    for inv_id in measured.columns:
        m = measured[inv_id][usable].dropna()
        e = exp.reindex(m.index)
        if len(m) < TRACKER_MIN_STEPS:
            out[inv_id] = {"steps": len(m), "stuck": None}
            continue
        dev = (m - e).abs()
        off = dev > TRACKER_DEVIATION_DEG
        held = m[off]
        flat = bool(len(held) > 1 and held.std() < TRACKER_FLAT_SD_DEG and e[off].std() > 5 * TRACKER_FLAT_SD_DEG)
        out[inv_id] = {
            "steps": len(m),
            "median_deviation_deg": round(float(dev.median()), 2),
            "steps_off": int(off.sum()),
            "first_off": off[off].index[0].to_pydatetime() if off.any() else None,
            "held_at_deg": round(float(held.median()), 1) if flat else None,
            "stuck": bool(off.sum() >= TRACKER_MIN_STEPS),
        }
    return out
