"""Measured vs expected: performance, energy, data gaps, curtailment. Numbers only; reads only the store.

Ratios use daylight steps (expected above DAYLIGHT_FRACTION of rating) outside curtailment.
"Unclipped" ratios also drop steps where expected output is at the inverter AC limit: there,
clipping hides part of a loss (20% soiling is only ~-16% energy on a clear July day).

A data gap is missing rows, never zeros. Each gap is checked against the meter: if the meter
minus the reported inverters still carries about one peer's output per missing device, the
device kept producing and only its data is lost.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from raqeeb.contracts import STEP_MINUTES, PlantConfig
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.physics import Basis, expected_inverter, expected_plant_ac
from raqeeb.tools.weather_tools import UNCLIPPED_FRACTION, get_plant, sensor_flagged

STEP = timedelta(minutes=STEP_MINUTES)
DAYLIGHT_FRACTION = 0.05  # expected output above this share of rating = a production step
PLANT_LOW_RATIO = 0.90  # plant vs reference at or below this: beyond reference error (max 8%)
ONSET_SMOOTH_STEPS = 5  # rolling median before looking for a loss onset
ONSET_MIN_STEPS = 6  # a loss must last 30 min of usable steps
PRODUCING_FRACTION = 0.8  # meter residual >= this share of peer output: missing device still produces
CURTAIL_AT_LIMIT_TOL = 0.02  # meter within 2% of (or under) the grid limit


def energy_kwh(power_kw: pd.Series) -> float:
    """Energy of a 5-minute power series (missing steps count as nothing, not zero power)."""
    return float(power_kw.dropna().sum() * STEP_MINUTES / 60.0)


def runs(mask: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Contiguous True runs on a regular 5-minute index: (start, end exclusive, steps)."""
    out = []
    values = mask.to_numpy(dtype=bool)
    index = mask.index
    k = 0
    while k < len(values):
        if values[k]:
            j = k
            while j + 1 < len(values) and values[j + 1] and index[j + 1] - index[j] == STEP:
                j += 1
            out.append((index[k], index[j] + STEP, j - k + 1))
            k = j + 1
        else:
            k += 1
    return out


def loss_onset(ratio: pd.Series, threshold: float, min_steps: int = ONSET_MIN_STEPS) -> pd.Timestamp | None:
    """First step from which the smoothed ratio stays <= threshold for min_steps usable steps.

    `ratio` may have gaps (e.g. clipped midday steps removed): steps are counted, not minutes.
    """
    ratio = ratio.dropna()
    smooth = ratio.rolling(ONSET_SMOOTH_STEPS, center=True, min_periods=1).median()
    low = (smooth <= threshold).to_numpy()
    for k in range(len(low) - min_steps + 1):
        if low[k : k + min_steps].all():
            return ratio.index[k]
    return None


def _curtailed(store: TelemetryStore, index: pd.DatetimeIndex) -> pd.Series:
    if len(index) == 0:
        return pd.Series(False, index=index)
    limits = store.grid_setpoint(index[0], index[-1] + STEP)
    return pd.Series(index.isin(limits.index), index=index)


def performance_series(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                       plant: PlantConfig | None = None, basis: Basis = "reference") -> pd.DataFrame:
    """Per meter step: meter kW, expected kW, ratio, and the usable / clipped / curtailed flags."""
    plant = get_plant(plant)
    meter = store.meter(start, end)["plant_ac_kw"]
    exp = expected_plant_ac(store, start, end, plant, basis).reindex(meter.index)
    daylight = exp > DAYLIGHT_FRACTION * plant.rated_ac_kw
    curtailed = _curtailed(store, meter.index)
    clipped = exp >= UNCLIPPED_FRACTION * plant.rated_ac_kw
    usable = daylight & ~curtailed
    return pd.DataFrame(
        {
            "meter_kw": meter,
            "expected_kw": exp,
            "ratio": (meter / exp).where(usable),
            "usable": usable,
            "clipped": clipped,
            "curtailed": curtailed,
        },
        index=meter.index,
    )


def _ratio_summary(perf: pd.DataFrame) -> dict[str, Any]:
    usable = perf[perf["usable"]]
    unclipped = usable[~usable["clipped"]]

    def ratio(df):
        return round(float(df["meter_kw"].sum() / df["expected_kw"].sum()), 4) if len(df) else None

    return {
        "ratio": ratio(usable),
        "ratio_unclipped": ratio(unclipped),
        "expected_kwh": round(energy_kwh(usable["expected_kw"]), 1),
        "shortfall_kwh": round(energy_kwh(usable["expected_kw"] - usable["meter_kw"]), 1),
        "usable_steps": len(usable),
        "unclipped_steps": len(unclipped),
    }


def plant_performance(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None) -> dict[str, Any]:
    """Meter vs expected from the trusted reference and from the on-site sensor.

    `vs_sensor` is the plant's usual health check (performance ratio on the sensor). When the
    sensor is dusty it can look normal while `vs_reference` shows the real loss.
    """
    plant = get_plant(plant)
    ref = performance_series(store, start, end, plant, "reference")
    sensor = performance_series(store, start, end, plant, "sensor")
    vs_ref = _ratio_summary(ref)
    unclipped = ref["ratio"].where(~ref["clipped"])
    onset = loss_onset(unclipped, PLANT_LOW_RATIO)
    low = vs_ref["ratio_unclipped"] is not None and vs_ref["ratio_unclipped"] <= PLANT_LOW_RATIO
    return {
        "meter_kwh": round(energy_kwh(ref.loc[ref["usable"], "meter_kw"]), 1),
        "vs_reference": vs_ref,
        "vs_sensor": _ratio_summary(sensor),
        "sensor_flagged": bool(sensor_flagged(store, ref.index[-1:], plant).any()) if len(ref) else False,
        "curtailed_steps": int(ref["curtailed"].sum()),
        "underperforming": bool(low or onset is not None),
        "onset": None if onset is None else onset.to_pydatetime(),
        "threshold": PLANT_LOW_RATIO,
    }


def inverter_performance(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                         plant: PlantConfig | None = None, basis: Basis = "reference") -> pd.DataFrame:
    """Per inverter, over the steps it reported: measured and expected kWh and their ratio."""
    plant = get_plant(plant)
    measured = store.inverter_wide("ac_kw", start, end)
    exp = expected_inverter(store, "ac_kw", start, end, plant, basis)
    measured = measured.reindex(index=exp.index, columns=exp.columns)
    usable = exp > DAYLIGHT_FRACTION * plant.inverters.ac_kw
    usable.loc[_curtailed(store, exp.index).to_numpy()] = False
    unclipped = usable & (exp < UNCLIPPED_FRACTION * plant.inverters.ac_kw)
    rows = {}
    for inv_id in exp.columns:
        present = measured[inv_id].notna()
        u, uc = usable[inv_id] & present, unclipped[inv_id] & present
        m, e = measured[inv_id], exp[inv_id]
        rows[inv_id] = {
            "measured_kwh": round(energy_kwh(m[u]), 1),
            "expected_kwh": round(energy_kwh(e[u]), 1),
            "ratio": round(float(m[u].sum() / e[u].sum()), 4) if u.any() else None,
            "ratio_unclipped": round(float(m[uc].sum() / e[uc].sum()), 4) if uc.any() else None,
            "reported_steps": int(present.sum()),
            "missing_daylight_steps": int((usable[inv_id] & ~present).sum()),
        }
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("inverter_id")


def _clock(store: TelemetryStore, start, end) -> pd.DatetimeIndex:
    """Steps the logger ran: every step with a meter or reference row."""
    return store.meter(start, end).index.union(store.weather_reference(start, end).index)


def data_gaps(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
              plant: PlantConfig | None = None) -> list[dict[str, Any]]:
    """Runs of missing rows per device (inverters, weather station, meter), checked against the meter."""
    plant = get_plant(plant)
    clock = _clock(store, start, end)
    if len(clock) == 0:
        return []
    ac = store.inverter_wide("ac_kw", start, end).reindex(index=clock, columns=plant.inverters.ids)
    meter = store.meter(start, end)["plant_ac_kw"].reindex(clock)
    n_missing = ac.isna().sum(axis=1)
    peer_median = ac.median(axis=1)
    residual = meter - ac.sum(axis=1)
    producing = (residual / (n_missing * peer_median)).where(
        (n_missing > 0) & (peer_median > DAYLIGHT_FRACTION * plant.inverters.ac_kw))

    gaps = []
    last = clock[-1]
    devices = {inv_id: ac[inv_id].isna() for inv_id in ac.columns}
    devices[plant.weather_station_id] = pd.Series(~clock.isin(store.weather_station(start, end).index), index=clock)
    devices[plant.meter_id] = meter.isna()
    for device_id, missing in devices.items():
        for gap_start, gap_end, steps in runs(missing):
            gap = {
                "device_id": device_id,
                "start": gap_start.to_pydatetime(),
                "end": gap_end.to_pydatetime(),
                "steps": steps,
                "ongoing": gap_end > last,
            }
            if device_id in ac.columns:
                window = slice(gap_start, gap_end - STEP)
                frac = producing.loc[window].dropna()
                gap |= {
                    "meter_residual_kw": round(float(residual.loc[window].median()), 1),
                    "peer_median_kw": round(float(peer_median.loc[window].median()), 1),
                    "producing_fraction": round(float(frac.median()), 3) if len(frac) else None,
                    "still_producing": bool(frac.median() >= PRODUCING_FRACTION) if len(frac) else None,
                }
            gaps.append(gap)
    return sorted(gaps, key=lambda g: (g["start"], g["device_id"]))


def curtailment_periods(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                        plant: PlantConfig | None = None) -> list[dict[str, Any]]:
    """Periods with a grid active-power limit, and whether the meter sat at the limit."""
    plant = get_plant(plant)
    limits = store.grid_setpoint(start, end)["limit_kw"]
    if limits.empty:
        return []
    meter = store.meter(start, end)["plant_ac_kw"].reindex(limits.index)
    exp = expected_plant_ac(store, start, end, plant).reindex(limits.index)
    out = []
    changed = limits.ne(limits.shift()).cumsum()
    for _, part in limits.groupby(changed):
        for p_start, p_end, steps in runs(pd.Series(True, index=part.index)):
            limit = float(part.iloc[0])
            m, e = meter.loc[p_start:p_end - STEP], exp.loc[p_start:p_end - STEP]
            binding = e > limit
            at_limit = (m[binding] <= limit * (1 + CURTAIL_AT_LIMIT_TOL)) & (m[binding] >= limit * (1 - CURTAIL_AT_LIMIT_TOL))
            out.append({
                "start": p_start.to_pydatetime(),
                "end": p_end.to_pydatetime(),
                "steps": steps,
                "limit_kw": limit,
                "meter_median_kw": round(float(m.median()), 1),
                "expected_median_kw": round(float(e.median()), 1),
                "binding_steps": int(binding.sum()),
                "meter_at_limit": bool(at_limit.mean() >= 0.8) if binding.any() else None,
            })
    return out
