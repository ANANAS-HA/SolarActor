"""Weather, on-site sensor and wind tools. Numbers only; reads only the telemetry store.

- reference_weather / sensor_weather: weather frames ready for twin.plant.expected_power().
  The sensor frame decomposes sensor GHI into DNI/DHI with pvlib erbs. erbs puts less light
  in diffuse than NASA POWER (~17% vs ~32% at midday here), so expected-from-sensor runs a few
  percent high on trackers even with a clean sensor (up to ~10% in unclipped summer shoulder
  hours). sensor_check therefore compares against the reference directly, not through erbs.
- sensor_flagged: from the commands table (flag_sensor_use_reference), never from twin state.
- sensor_check: the dusty-sensor test. The reference is not ground truth (per-run bias sd ~2%,
  slow error sd ~2.5%, max 8%), so a single sensor/reference ratio is not enough. It combines
  (1) sensor vs reference on clear steps, (2) clear-sky shape: the sky is clear per the
  reference and the sensor follows the clear-sky curve at a steady scale, as a dust layer
  would, (3) a step down in the ratio inside the window, (4) peers: the inverters' DC vs
  expected from the reference says whether the panels see the sensor's loss too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pvlib

from raqeeb.config import load_plant
from raqeeb.contracts import ActionType, InverterStatus, PlantConfig
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.twin import plant as physics
from raqeeb.twin.weather import location

DAYLIGHT_CLEARSKY_WM2 = 200.0  # steps used for irradiance comparisons
CLEAR_KT_MIN = 0.80  # reference GHI / clear-sky GHI at or above this ...
CLEAR_KT_ROLL_SD = 0.03  # ... and steady (rolling sd over CLEAR_ROLL_STEPS) = clear sky
CLEAR_ROLL_STEPS = 7  # 35 min, centred
MIN_CLEAR_STEPS = 6  # 30 min of clear sky needed for a verdict
SMOOTH_STEPS = 5  # rolling median of sensor/reference before looking for the low period

SENSOR_LOW_RATIO = 0.90  # sensor/reference at or below this: beyond the reference's 8% max error
SENSOR_CLEAR_KT_MAX = 0.90  # a clean sensor under a clear sky reads >= ~1.0x pvlib Ineichen here
SENSOR_STEP_DROP = 0.08  # ratio fell this much vs the first clear hour (reference bias cancels)
SENSOR_SHAPE_CV_MAX = 0.05  # sensor/clear-sky steady: uniform scaling, not clouds or shading
PEERS_AGREE_TOL = 0.05  # |peers - sensor| (both vs reference) below this: panels see the same loss
PEERS_DISAGREE_TOL = 0.10  # above this: the panels do not see the sensor's loss
UNCLIPPED_FRACTION = 0.95  # peers compared only where expected AC is below this share of rating


def get_plant(plant: PlantConfig | None = None) -> PlantConfig:
    return plant or load_plant()


def solar_geometry(index: pd.DatetimeIndex, plant: PlantConfig | None = None) -> pd.DataFrame:
    sp = location(get_plant(plant)).get_solarposition(index)
    return sp[["apparent_zenith", "zenith", "azimuth"]]


def reference_weather(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None) -> pd.DataFrame:
    """weather_reference rows + solar geometry, in the column names of twin.weather.weather_5min()."""
    ref = store.weather_reference(start, end)
    return ref.join(solar_geometry(ref.index, plant))


def sensor_weather(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                   plant: PlantConfig | None = None) -> pd.DataFrame:
    """On-site sensor as a weather frame: GHI split into DNI/DHI with erbs; ambient and wind
    from the station. Only steps the station reported (a station dropout stays missing)."""
    ws = store.weather_station(start, end)
    geo = solar_geometry(ws.index, plant)
    parts = pvlib.irradiance.erbs(ws["ghi_sensor_wm2"], geo["zenith"], ws.index)
    clearsky = store.weather_reference(start, end)["clearsky_ghi_wm2"].reindex(ws.index)
    frame = pd.DataFrame(
        {
            "ghi_wm2": ws["ghi_sensor_wm2"],
            "dni_wm2": parts["dni"].fillna(0.0),
            "dhi_wm2": parts["dhi"].fillna(0.0),
            "ambient_c": ws["ambient_c"],
            "wind_ms": ws["wind_ms"],
            "clearsky_ghi_wm2": clearsky,
        },
        index=ws.index,
    )
    return frame.join(geo)


def sensor_flagged(store: TelemetryStore, index: pd.DatetimeIndex, plant: PlantConfig | None = None) -> pd.Series:
    """True at each step after the last flag_sensor_use_reference command set the flag on.

    The flag applies from the command's timestamp onward.
    """
    station = get_plant(plant).weather_station_id
    cmds = store.commands(device_id=station, action=ActionType.FLAG_SENSOR_USE_REFERENCE.value)
    flags = pd.Series([bool(p.get("flagged", True)) for p in cmds["params"]], index=cmds.index, dtype=bool)
    flags = flags[~flags.index.duplicated(keep="last")]
    if flags.empty:
        return pd.Series(False, index=index)
    return flags.reindex(flags.index.union(index)).ffill().fillna(False).astype(bool).reindex(index)


def wind_series(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None) -> pd.Series:
    """Wind from the station; the reference where the station has no row."""
    wind = store.weather_reference(start, end)["wind_ms"].copy()
    station = store.weather_station(start, end)["wind_ms"]
    wind.loc[station.index.intersection(wind.index)] = station
    return wind


def wind_summary(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                 plant: PlantConfig | None = None) -> dict[str, Any]:
    """Wind vs the tracker stow limit and the high-wind alarm level."""
    plant = get_plant(plant)
    wind = wind_series(store, start, end)
    if wind.empty:
        return {"steps": 0}
    above_stow = wind[wind > plant.tracker.stow_wind_ms]
    return {
        "steps": len(wind),
        "max_wind_ms": round(float(wind.max()), 2),
        "max_at": wind.idxmax().to_pydatetime(),
        "stow_wind_ms": plant.tracker.stow_wind_ms,
        "steps_above_stow": len(above_stow),
        "first_above_stow": above_stow.index[0].to_pydatetime() if len(above_stow) else None,
        "last_above_stow": above_stow.index[-1].to_pydatetime() if len(above_stow) else None,
        "steps_above_high_wind_alarm": int((wind > plant.high_wind_alarm_ms).sum()),
    }


def clear_steps(ref: pd.DataFrame) -> pd.Series:
    """Daylight steps where the reference follows the clear-sky curve (high, steady kt)."""
    daylight = ref["clearsky_ghi_wm2"] >= DAYLIGHT_CLEARSKY_WM2
    kt = (ref["ghi_wm2"] / ref["clearsky_ghi_wm2"]).where(daylight)
    steady = kt.rolling(CLEAR_ROLL_STEPS, center=True, min_periods=3).std() <= CLEAR_KT_ROLL_SD
    return daylight & (kt >= CLEAR_KT_MIN) & steady


def _peers_vs_reference(store: TelemetryStore, ref_weather: pd.DataFrame, steps: pd.Index,
                        plant: PlantConfig) -> pd.Series:
    """Per step: median over RUNNING, unclipped inverters of measured DC / expected DC (reference)."""
    if len(steps) == 0:
        return pd.Series(dtype=float)
    expected = physics.expected_power(ref_weather.loc[steps], plant)
    exp_dc = expected.xs("dc_kw", axis=1, level="quantity")
    exp_ac = expected.xs("ac_kw", axis=1, level="quantity")
    start, end = steps[0], steps[-1] + pd.Timedelta(minutes=5)
    dc = store.inverter_wide("dc_kw", start, end).reindex(index=steps, columns=exp_dc.columns)
    status = store.inverter_wide("status", start, end).reindex(index=steps, columns=exp_dc.columns)
    usable = (status == InverterStatus.RUNNING.value) & (exp_ac < UNCLIPPED_FRACTION * plant.inverters.ac_kw)
    return (dc / exp_dc).where(usable).median(axis=1).dropna()


def sensor_check(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                 plant: PlantConfig | None = None) -> dict[str, Any]:
    """Is the on-site irradiance sensor reading low (dusty)? See the module docstring.

    `suspect` needs: sensor/reference <= SENSOR_LOW_RATIO on clear steps, a steady clear-sky
    shape, and at least one more vote (sensor below clear-sky level, a step drop in the window,
    or peers disagreeing with the sensor). `panels_share_loss` says the inverters lose about
    as much as the sensor does (the trap: performance ratio vs the sensor looks normal).
    """
    plant = get_plant(plant)
    ref_weather = reference_weather(store, start, end, plant)
    ghi_sensor = store.weather_station(start, end)["ghi_sensor_wm2"]
    clear = clear_steps(ref_weather).reindex(ghi_sensor.index, fill_value=False)
    steps = ghi_sensor.index[clear.to_numpy()]
    result: dict[str, Any] = {
        "clear_steps": len(steps),
        "flagged": bool(sensor_flagged(store, ghi_sensor.index[-1:], plant).any()) if len(ghi_sensor) else False,
        "thresholds": {
            "sensor_low_ratio": SENSOR_LOW_RATIO, "sensor_clear_kt_max": SENSOR_CLEAR_KT_MAX,
            "sensor_step_drop": SENSOR_STEP_DROP, "shape_cv_max": SENSOR_SHAPE_CV_MAX,
            "peers_agree_tol": PEERS_AGREE_TOL, "peers_disagree_tol": PEERS_DISAGREE_TOL,
        },
    }
    if len(steps) < MIN_CLEAR_STEPS:
        return result | {"verdict": "inconclusive", "suspect": False,
                         "reasons": [f"only {len(steps)} clear steps (need {MIN_CLEAR_STEPS})"]}

    ratio_all = ghi_sensor.loc[steps] / ref_weather.loc[steps, "ghi_wm2"]
    # Current low period: the last run of low (smoothed) ratio that reaches the end of the window
    smooth = ratio_all.rolling(SMOOTH_STEPS, center=True, min_periods=1).median()
    low = (smooth <= SENSOR_LOW_RATIO).to_numpy()
    onset = None
    if low[-1]:
        first = len(low) - int(np.argmin(low[::-1])) if not low.all() else 0
        if len(low) - first >= MIN_CLEAR_STEPS:
            onset = steps[first]
    period = steps[steps >= onset] if onset is not None else steps
    before = steps[steps < onset] if onset is not None else steps[:0]

    ref = ref_weather.loc[period]
    ratio = ratio_all.loc[period]
    sensor_kt = ghi_sensor.loc[period] / ref["clearsky_ghi_wm2"]
    vs_ref = float(ratio.median())
    shape_cv = float(ratio.std() / ratio.mean()) if len(period) > 1 else 0.0
    # Step drop vs the clear steps before the onset (the reference's slow bias mostly cancels)
    drop = float(ratio_all.loc[before].median() - vs_ref) if len(before) >= MIN_CLEAR_STEPS else None

    peers = _peers_vs_reference(store, ref_weather, period, plant)
    peers_vs_ref = float(peers.median()) if len(peers) else None
    gap = None if peers_vs_ref is None else peers_vs_ref - vs_ref

    votes = {
        "below_reference": vs_ref <= SENSOR_LOW_RATIO,
        "below_clear_sky": float(sensor_kt.median()) <= SENSOR_CLEAR_KT_MAX,
        "step_drop": drop is not None and drop >= SENSOR_STEP_DROP,
        "peers_disagree": gap is not None and gap >= PEERS_DISAGREE_TOL,
    }
    shape_ok = shape_cv <= SENSOR_SHAPE_CV_MAX
    support = votes["below_clear_sky"] or votes["step_drop"] or votes["peers_disagree"]
    suspect = votes["below_reference"] and shape_ok and support
    panels_share = gap is not None and abs(gap) < PEERS_AGREE_TOL

    reasons = [f"sensor/reference median {vs_ref:.3f} on {len(period)} clear steps"
               + (f" since {onset:%H:%M} UTC" if onset is not None else "")]
    reasons.append(f"sensor/clear-sky median {sensor_kt.median():.3f}, ratio cv {shape_cv:.3f}")
    if drop is not None:
        reasons.append(f"ratio fell {drop:+.3f} vs the clear steps before the onset")
    if peers_vs_ref is not None:
        reasons.append(f"peers DC/expected(reference) median {peers_vs_ref:.3f}")
    if suspect:
        verdict = "sensor_low_panels_too" if panels_share else "sensor_low"
    elif votes["below_reference"]:
        verdict = "inconclusive"
        reasons.append("sensor deficit is not steady (clouds or shading?)" if not shape_ok
                       else "sensor below reference, but no clear-sky, step or peer evidence")
    else:
        verdict = "ok"
    return result | {
        "verdict": verdict,
        "suspect": bool(suspect),
        "panels_share_loss": bool(suspect and panels_share),
        "sensor_vs_reference": round(vs_ref, 4),
        "sensor_kt_clear": round(float(sensor_kt.median()), 4),
        "reference_kt_clear": round(float((ref["ghi_wm2"] / ref["clearsky_ghi_wm2"]).median()), 4),
        "shape_cv": round(shape_cv, 4),
        "ratio_drop": None if drop is None else round(drop, 4),
        "peers_vs_reference": None if peers_vs_ref is None else round(peers_vs_ref, 4),
        "onset": None if onset is None else onset.to_pydatetime(),
        "votes": votes,
        "reasons": reasons,
    }


def irradiance_ratios(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None) -> pd.DataFrame:
    """Per step: sensor/reference, sensor/clear-sky, reference/clear-sky, clear flag (for charts)."""
    ref = reference_weather(store, start, end, plant)
    ghi = store.weather_station(start, end)["ghi_sensor_wm2"].reindex(ref.index)
    daylight = ref["clearsky_ghi_wm2"] >= DAYLIGHT_CLEARSKY_WM2
    return pd.DataFrame(
        {
            "sensor_vs_reference": (ghi / ref["ghi_wm2"]).where(daylight),
            "sensor_kt": (ghi / ref["clearsky_ghi_wm2"]).where(daylight),
            "reference_kt": (ref["ghi_wm2"] / ref["clearsky_ghi_wm2"]).where(daylight),
            "clear": clear_steps(ref),
        },
        index=ref.index,
    ).replace([np.inf, -np.inf], np.nan)
